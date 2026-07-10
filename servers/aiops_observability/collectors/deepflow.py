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
    return (
        "SELECT time, ip4_0, ip4_1, l7_protocol_str, request_type, request_resource, "
        "response_code, response_status, trace_id, span_id "
        f"FROM {database}.l7_flow_log "
        f"WHERE time >= toDateTime('{_clickhouse_datetime(start)}', 'UTC') "
        f"AND time <= toDateTime('{_clickhouse_datetime(end)}', 'UTC') "
        f"AND (ip4_0 = '{pod_ip}' OR ip4_1 = '{pod_ip}') "
        f"ORDER BY time DESC "
        f"LIMIT {int(max_records)} FORMAT JSON"
    )


def _result_from_rows(rows: list[dict[str, Any]], namespace: str, pod: str, pod_ip: str, node: str | None, sql: str, database: str, source_desc: str, max_records: int) -> SourceResult:
    coverage = "present" if rows else "empty"
    evidence = []
    if rows:
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id("deepflow", namespace, pod, "pod-ip-flow"),
                source_system="deepflow",
                dimension="tracing",
                summary=f"DeepFlow 返回 {len(rows)} 条该 Pod IP 的 L7 flow 记录（来源：{source_desc}）",
                confidence="medium",
                directness="direct",
                trace_correlation={"pod_ip": pod_ip, "trace_ids": [r.get("trace_id") for r in rows if r.get("trace_id")][:5]},
                payload={"records": rows[:10], "source": source_desc},
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
    return _result_from_rows(rows, namespace, pod, pod_ip, node, sql, database, f"kubectl exec {ch_pod}", max_records)


def _collect_via_http(clickhouse_url: str, database: str, namespace: str, pod: str, pod_ip: str, node: str | None, start: str, end: str, timeout: int, max_records: int) -> SourceResult:
    if requests is None:
        return SourceResult("deepflow", "tracing", "error", error="requests is not installed")
    sql = _build_sql(database, pod_ip, start, end, max_records)
    try:
        response = requests.post(clickhouse_url, data=sql.encode("utf-8"), timeout=timeout)
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
    return _result_from_rows(rows, namespace, pod, pod_ip, node, sql, database, "direct clickhouse http", max_records)


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
