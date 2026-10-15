"""DeepFlow ClickHouse collector for Pod-scoped flow/trace context.

Two collection modes, in priority order:
1. Direct HTTP when DEEPFLOW_CLICKHOUSE_URL is explicitly configured.
2. kubectl-exec (default): `kubectl exec` into the ClickHouse Pod and run
   clickhouse-client — the proven approach from the data-project scripts.

Node-level flows are weak related context, never strong Pod-level evidence.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import _http
from ..config import get_config
from ..schemas import EvidenceItem, SourceResult, evidence_id
from . import _kube

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


def _safe_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        return "flow_log"
    return value


def _clickhouse_datetime(value: str) -> str:
    normalized = value.strip().replace("Z", "").replace("T", " ")
    if "." in normalized:
        normalized = normalized.split(".", 1)[0]
    if "+" in normalized:
        normalized = normalized.split("+", 1)[0]
    return normalized


def _build_sql(database: str, pod_ip: str, start: str, end: str, max_records: int) -> str:
    # 窗口是 UTC，但 ClickHouse 服务器时区可能是 Asia/Shanghai 等。用 toDateTime(..,'UTC')
    # 显式按 UTC 解析，避免 8 小时偏移导致查询错过真实 flow（历史 bug）。
    # 额外取 DeepFlow eBPF 自动追踪字段：syscall_trace_id/tcp_seq 无需 app 插桩即可拼调用链，
    # response_duration 给真实延迟。UInt64 用 toString 避免 JSON 精度丢失。
    return (
        "SELECT time, IPv4NumToString(ip4_0) AS src_ip, IPv4NumToString(ip4_1) AS dst_ip, "
        "l7_protocol_str, request_type, request_resource, response_code, response_status, response_duration, "
        "trace_id, span_id, toString(syscall_trace_id_request) AS syscall_trace_id_request, "
        "toString(syscall_trace_id_response) AS syscall_trace_id_response "
        f"FROM {database}.l7_flow_log "
        f"WHERE time >= toDateTime('{_clickhouse_datetime(start)}', 'UTC') "
        f"AND time <= toDateTime('{_clickhouse_datetime(end)}', 'UTC') "
        f"AND (ip4_0 = '{pod_ip}' OR ip4_1 = '{pod_ip}') "
        f"ORDER BY (trace_id != '') DESC, (ip4_1 = '{pod_ip}') DESC, "
        f"(l7_protocol_str != 'DNS') DESC, time DESC "
        f"LIMIT {int(max_records)} FORMAT JSON"
    )


def _build_call_chains(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """用 eBPF syscall_trace_id 把同一次请求的多跳拼成调用链（无需 app 插桩）。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        stid = r.get("syscall_trace_id_request") or r.get("syscall_trace_id_response") or "0"
        if stid and stid != "0":
            groups.setdefault(stid, []).append(r)
    chains: list[dict[str, Any]] = []
    for stid, hops in groups.items():
        if len(hops) < 2:
            continue  # 单跳不算链
        ordered = sorted(hops, key=lambda x: str(x.get("time") or ""))
        chains.append(
            {
                "syscall_trace_id": stid,
                "hop_count": len(ordered),
                "hops": [
                    {
                        "src": h.get("src_ip"),
                        "dst": h.get("dst_ip"),
                        "proto": h.get("l7_protocol_str"),
                        "endpoint": str(h.get("request_resource") or "")[:120],
                        "code": h.get("response_code"),
                        "duration_us": h.get("response_duration"),
                    }
                    for h in ordered[:8]
                ],
            }
        )
    chains.sort(key=lambda c: -c["hop_count"])
    return chains[:5]


def _flow_samples(rows: list[dict[str, Any]], max_samples: int = 5) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    ranked = sorted(
        rows,
        key=lambda row: (
            not bool(str(row.get("trace_id") or "").strip()),
            str(row.get("l7_protocol_str") or row.get("l7_protocol") or "").upper() == "DNS",
            str(row.get("l7_protocol_str") or row.get("l7_protocol") or "").upper() != "HTTP",
        ),
    )
    for row in ranked[:max_samples]:
        request_type = str(row.get("request_type") or "").strip()
        resource = str(row.get("request_resource") or "").strip()
        request = " ".join(part for part in (request_type, resource) if part)
        samples.append(
            {
                "timestamp": row.get("time") or row.get("timestamp"),
                "src": row.get("src_ip") or row.get("ip4_0"),
                "dst": row.get("dst_ip") or row.get("ip4_1"),
                "protocol": row.get("l7_protocol_str") or row.get("l7_protocol"),
                "request": request,
                "response_code": row.get("response_code"),
                "duration_us": row.get("response_duration") or row.get("duration_us"),
                "trace_id": row.get("trace_id") or "",
                "span_id": row.get("span_id") or "",
            }
        )
    return samples


def _run_clickhouse_json(cfg: Any, database: str, sql: str, timeout: int) -> dict[str, Any] | None:
    """Run a ClickHouse query returning JSON, via kubectl-exec or direct URL."""
    if cfg.deepflow_clickhouse_url:
        if requests is None:
            return None
        try:
            resp = _http.SESSION.post(cfg.deepflow_clickhouse_url, data=sql.encode("utf-8"), timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception:  # noqa: BLE001
            return None
    if not cfg.clickhouse_exec_enabled:
        return None
    ch_pod = cfg.clickhouse_exec_pod or _kube.find_pod(cfg.clickhouse_exec_namespace, cfg.clickhouse_exec_selector)
    if not ch_pod:
        return None
    ok, out, _ = _kube.exec_in_pod(cfg.clickhouse_exec_namespace, ch_pod, ["clickhouse-client", "--query", sql], timeout=timeout)
    if not ok or not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def fetch_call_chains(cfg: Any, database: str, pod_ip: str, start: str, end: str, timeout: int, max_chains: int = 5) -> list[dict[str, Any]]:
    """定向抓该 Pod 的 eBPF 多跳调用链（syscall_trace_id 关联，无需 app 插桩）。
    先找出有 >=2 跳的 syscall_trace_id，再取回它们的完整链路。"""
    db = _safe_identifier(database or "flow_log")
    win = (
        f"time >= toDateTime('{_clickhouse_datetime(start)}', 'UTC') "
        f"AND time <= toDateTime('{_clickhouse_datetime(end)}', 'UTC')"
    )
    top_sql = (
        f"SELECT toString(syscall_trace_id_request) AS stid, count() AS hops FROM {db}.l7_flow_log "
        f"WHERE {win} AND (ip4_0='{pod_ip}' OR ip4_1='{pod_ip}') AND syscall_trace_id_request!=0 "
        f"GROUP BY syscall_trace_id_request HAVING hops>=2 ORDER BY hops DESC LIMIT {int(max_chains)} FORMAT JSON"
    )
    payload = _run_clickhouse_json(cfg, db, top_sql, timeout)
    stids = [str(r.get("stid")) for r in (payload.get("data") or []) if r.get("stid")] if payload else []
    if not stids:
        return []
    in_list = ",".join(stids)
    hop_sql = (
        f"SELECT toString(time) AS time, IPv4NumToString(ip4_0) AS src, IPv4NumToString(ip4_1) AS dst, "
        f"l7_protocol_str AS proto, request_resource AS endpoint, response_code AS code, response_duration AS dur, "
        f"toString(syscall_trace_id_request) AS stid FROM {db}.l7_flow_log "
        f"WHERE {win} AND syscall_trace_id_request IN ({in_list}) ORDER BY time LIMIT 200 FORMAT JSON"
    )
    hp = _run_clickhouse_json(cfg, db, hop_sql, timeout)
    by_stid: dict[str, list[dict[str, Any]]] = {}
    for r in (hp.get("data") or []) if hp else []:
        by_stid.setdefault(str(r.get("stid")), []).append(r)
    chains = []
    for stid, hops in by_stid.items():
        chains.append({
            "syscall_trace_id": stid,
            "hop_count": len(hops),
            "hops": [
                {"src": h.get("src"), "dst": h.get("dst"), "proto": h.get("proto"),
                 "endpoint": str(h.get("endpoint") or "")[:120], "code": h.get("code"), "duration_us": h.get("dur")}
                for h in hops[:8]
            ],
        })
    chains.sort(key=lambda c: -c["hop_count"])
    return chains[:max_chains]


def discover_correlation(cfg: Any, database: str, pod_ip: str, start: str, end: str, timeout: int) -> dict[str, list[str]]:
    """Discover distinct trace_ids (for Tempo lookup) and caller IPs (who called
    this pod = request drivers) for the target Pod IP within the window.

    A dedicated DISTINCT query is used so successful traces surface even when the
    most-recent flows are dominated by a failing trace.
    """
    db = _safe_identifier(database or "flow_log")
    ip = pod_ip
    win = (
        f"time >= toDateTime('{_clickhouse_datetime(start)}', 'UTC') "
        f"AND time <= toDateTime('{_clickhouse_datetime(end)}', 'UTC')"
    )
    trace_sql = (
        f"SELECT DISTINCT trace_id FROM {db}.l7_flow_log "
        f"WHERE {win} AND (ip4_0='{ip}' OR ip4_1='{ip}') AND trace_id != '' "
        f"ORDER BY trace_id LIMIT 20 FORMAT JSON"
    )
    caller_sql = (
        f"SELECT DISTINCT IPv4NumToString(ip4_0) AS caller FROM {db}.l7_flow_log "
        f"WHERE {win} AND ip4_1='{ip}' AND ip4_0 != ip4_1 "
        f"LIMIT 10 FORMAT JSON"
    )
    trace_ids: list[str] = []
    caller_ips: list[str] = []
    tpayload = _run_clickhouse_json(cfg, db, trace_sql, timeout)
    if tpayload and isinstance(tpayload.get("data"), list):
        trace_ids = [str(r.get("trace_id")) for r in tpayload["data"] if r.get("trace_id")]
    cpayload = _run_clickhouse_json(cfg, db, caller_sql, timeout)
    if cpayload and isinstance(cpayload.get("data"), list):
        caller_ips = [str(r.get("caller")) for r in cpayload["data"] if r.get("caller")]
    return {"trace_ids": trace_ids, "caller_ips": caller_ips}


def _result_from_rows(rows: list[dict[str, Any]], namespace: str, pod: str, pod_ip: str, node: str | None, sql: str, database: str, source_desc: str, max_records: int, _fetched_chains: list[dict[str, Any]] | None = None) -> SourceResult:
    coverage = "present" if rows else "empty"
    evidence = []
    if rows:
        app_trace_ids = [r.get("trace_id") for r in rows if r.get("trace_id")]
        # 优先用定向查询抓到的多跳链（更可靠），否则从已取回 flow 现拼
        call_chains = _fetched_chains if _fetched_chains else _build_call_chains(rows)
        has_syscall = any((r.get("syscall_trace_id_request") or "0") != "0" for r in rows)
        # 主证据：L7 flow（真实 endpoint/延迟）
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id("deepflow", namespace, pod, "pod-ip-flow"),
                source_system="deepflow",
                dimension="tracing",
                summary=f"DeepFlow 返回 {len(rows)} 条该 Pod 的 L7 flow（真实 endpoint/协议/延迟，来源：{source_desc}）",
                confidence="medium",
                directness="direct",
                trace_correlation={"pod_ip": pod_ip, "trace_ids": app_trace_ids[:5]},
                payload={
                    "records": rows[:10],
                    "samples": _flow_samples(rows),
                    "source": source_desc,
                },
            )
        )
        # eBPF 自动追踪调用链（无需 app 插桩，任意 pod 可得）
        if call_chains:
            top = call_chains[0]
            hop_desc = " → ".join(
                f"{h['src']}→{h['dst']} {h['proto']} {h['endpoint'][:40]}" for h in top["hops"][:3]
            )
            evidence.append(
                EvidenceItem(
                    evidence_id=evidence_id("deepflow", namespace, pod, "auto-call-chain"),
                    source_system="deepflow",
                    dimension="tracing",
                    summary=f"eBPF 自动追踪拼出 {len(call_chains)} 条调用链（无需 app 插桩）；最长链 {top['hop_count']} 跳：{hop_desc}",
                    confidence="high" if has_syscall else "medium",
                    directness="direct",
                    trace_correlation={"auto_traced": True, "call_chains": call_chains, "app_trace_ids": app_trace_ids[:5]},
                    payload={"call_chains": call_chains},
                )
            )
    return SourceResult(
        "deepflow",
        "tracing",
        coverage,
        records=rows[:max_records],
        evidence=evidence,
        query_meta={"sql": sql, "pod_ip": pod_ip, "node": node, "database": database, "source": source_desc},
        truncated=len(rows) > max_records,
    )


def _collect_via_exec(cfg: Any, database: str, namespace: str, pod: str, pod_ip: str, node: str | None, start: str, end: str, timeout: int, max_records: int) -> SourceResult:
    ch_pod = cfg.clickhouse_exec_pod or _kube.find_pod(cfg.clickhouse_exec_namespace, cfg.clickhouse_exec_selector)
    if not ch_pod:
        return SourceResult(
            "deepflow", "tracing", "absent",
            error=f"未找到 ClickHouse Pod（ns={cfg.clickhouse_exec_namespace} selector={cfg.clickhouse_exec_selector}）",
        )
    sql = _build_sql(database, pod_ip, start, end, max_records)
    ok, out, err = _kube.exec_in_pod(
        cfg.clickhouse_exec_namespace, ch_pod, ["clickhouse-client", "--query", sql], timeout=timeout
    )
    if not ok:
        return SourceResult(
            "deepflow", "tracing", "error",
            error=f"kubectl exec 查询 ClickHouse 失败（pod={ch_pod}）: {err or out}",
            query_meta={"sql": sql, "pod_ip": pod_ip, "node": node, "database": database, "source": "kubectl_exec"},
        )
    try:
        payload = json.loads(out) if out.strip() else {"data": []}
    except json.JSONDecodeError as exc:
        return SourceResult(
            "deepflow", "tracing", "error",
            error=f"ClickHouse 返回非法 JSON: {exc}",
            query_meta={"sql": sql, "source": "kubectl_exec", "raw_preview": out[:300]},
        )
    rows = payload.get("data") if isinstance(payload, dict) else []
    rows = rows if isinstance(rows, list) else []
    chains = fetch_call_chains(cfg, database, pod_ip, start, end, timeout) if rows else []
    return _result_from_rows(rows, namespace, pod, pod_ip, node, sql, database, f"kubectl exec {ch_pod}", max_records, chains)


def _collect_via_http(clickhouse_url: str, database: str, namespace: str, pod: str, pod_ip: str, node: str | None, start: str, end: str, timeout: int, max_records: int) -> SourceResult:
    if requests is None:
        return SourceResult("deepflow", "tracing", "error", error="requests is not installed")
    sql = _build_sql(database, pod_ip, start, end, max_records)
    try:
        response = _http.SESSION.post(clickhouse_url, data=sql.encode("utf-8"), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return SourceResult(
            "deepflow", "tracing", "error",
            error=str(exc),
            query_meta={"sql": sql, "pod_ip": pod_ip, "node": node, "database": database, "source": "direct_url"},
        )
    rows = payload.get("data") if isinstance(payload, dict) else []
    rows = rows if isinstance(rows, list) else []
    chains = fetch_call_chains(get_config(), database, pod_ip, start, end, timeout) if rows else []
    return _result_from_rows(rows, namespace, pod, pod_ip, node, sql, database, "direct clickhouse http", max_records, chains)


def collect_deepflow(
    *,
    clickhouse_url: str,
    clickhouse_database: str,
    namespace: str,
    pod: str,
    pod_ip: str | None,
    node: str | None,
    start: str,
    end: str,
    timeout: int,
    max_records: int,
) -> SourceResult:
    cfg = get_config()
    database = _safe_identifier(clickhouse_database or "flow_log")
    if not pod_ip:
        return SourceResult("deepflow", "tracing", "empty", error="pod_ip 为空，无法关联 DeepFlow flow")
    # 1. Explicit direct URL wins.
    if clickhouse_url:
        return _collect_via_http(clickhouse_url, database, namespace, pod, pod_ip, node, start, end, timeout, max_records)
    # 2. kubectl-exec mode (default).
    if cfg.clickhouse_exec_enabled:
        return _collect_via_exec(cfg, database, namespace, pod, pod_ip, node, start, end, timeout, max_records)
    # 3. Honestly absent.
    return SourceResult(
        "deepflow", "tracing", "absent",
        error="DEEPFLOW_CLICKHOUSE_URL 未配置且 kubectl-exec 模式已禁用",
    )
