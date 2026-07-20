"""Build and summarize AIOps case packages from live source collectors."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .collectors import _kube
from .collectors.deepflow import collect_deepflow, discover_correlation
from .collectors.elasticsearch import collect_elasticsearch
from .collectors.kubernetes import collect_kubernetes
from .collectors.prometheus import collect_prometheus
from .collectors.tempo import collect_tempo
from .config import get_config, runtime_status
from .evidence_store import (
    EVIDENCE_FILES,
    line_count,
    read_jsonl,
    read_yaml,
    safe_case_dir,
    safe_relative_path,
    write_jsonl,
    write_yaml,
)
from .schemas import EvidenceItem, SourceResult, parse_time_window
from .topology import build_topology, summarize_topology, topology_details


def collect_case(arguments: dict[str, Any]) -> dict[str, Any]:
    pod = str(arguments.get("pod") or "").strip()
    if not pod:
        return {"ok": False, "error": "pod is required", "runtime_status": runtime_status()}
    namespace = str(arguments.get("namespace") or "").strip()
    scenario = str(arguments.get("scenario") or "auto").strip() or "auto"
    window = parse_time_window(
        start=arguments.get("start"),
        end=arguments.get("end"),
        window_minutes=int(arguments.get("window_minutes") or 30),
    )
    cfg = get_config()
    case_id = str(arguments.get("case_id") or f"{scenario}-{namespace or 'cluster'}-{pod}").strip()
    case_dir = safe_case_dir(cfg.cases_dir, case_id)

    k8s = collect_kubernetes(namespace, pod, timeout=cfg.timeout_seconds)
    if k8s.coverage == "error" and not k8s.records:
        return {"ok": False, "error": k8s.error or "kubernetes collection failed", "runtime_status": runtime_status()}

    k8s_record = k8s.records[0] if k8s.records else {}
    namespace = namespace or str(k8s_record.get("namespace") or "")
    primary = {
        "kind": "Pod",
        "namespace": namespace,
        "name": pod,
        "uid": k8s_record.get("uid"),
        "node": k8s_record.get("node"),
        "pod_ip": k8s_record.get("pod_ip"),
    }

    metrics = collect_prometheus(
        prometheus_url=cfg.prometheus_url,
        namespace=namespace,
        pod=pod,
        pod_uid=primary.get("uid"),
        timeout=cfg.timeout_seconds,
        start=window.start,
        end=window.end,
    )
    logs = collect_elasticsearch(
        elasticsearch_url=cfg.elasticsearch_url,
        index=cfg.elasticsearch_index,
        namespace=namespace,
        pod=pod,
        pod_uid=primary.get("uid"),
        start=window.start,
        end=window.end,
        timeout=cfg.timeout_seconds,
        max_records=cfg.max_records,
    )
    deepflow = collect_deepflow(
        clickhouse_url=cfg.deepflow_clickhouse_url,
        clickhouse_database=cfg.deepflow_clickhouse_database,
        namespace=namespace,
        pod=pod,
        pod_ip=primary.get("pod_ip"),
        node=primary.get("node"),
        start=window.start,
        end=window.end,
        timeout=cfg.timeout_seconds,
        max_records=cfg.max_records,
    )

    # 用专门的 DISTINCT 查询发现全量 trace_id（含成功 trace）+ caller IP（谁调用了本 Pod）
    correlation = discover_correlation(
        get_config(), cfg.deepflow_clickhouse_database, primary.get("pod_ip") or "",
        window.start, window.end, cfg.timeout_seconds,
    ) if primary.get("pod_ip") else {"trace_ids": [], "caller_ips": []}
    flow_trace_ids = [
        str(row.get("trace_id"))
        for row in (deepflow.records or [])
        if row.get("trace_id")
    ]
    discovered_trace_ids = _merge_trace_ids(
        flow_trace_ids,
        list(correlation.get("trace_ids") or []),
        _trace_ids_from_evidence([k8s, logs]),
    )

    tempo = collect_tempo(
        namespace=namespace, pod=pod, trace_ids=discovered_trace_ids, timeout=cfg.timeout_seconds, max_traces=12
    )

    # request_driver：把 caller IP 解析成 pod，采它的日志（谁驱动了这次异常）
    caller_logs = _collect_caller_logs(cfg, correlation.get("caller_ips") or [], window)

    sources = [k8s, metrics, logs, deepflow, tempo]
    if caller_logs is not None:
        sources.append(caller_logs)
    case_dir.mkdir(parents=True, exist_ok=True)
    _write_package(case_dir, case_id, scenario, primary, window.to_dict(), sources)
    return case_summary(case_dir)


_TRACE_ID_RE = re.compile(r'(?:trace[_-]?id["\s:=]+)([0-9a-f]{16,32})', re.IGNORECASE)
_ALLOCATED_MIB_RE = re.compile(r'allocated_mib["\s:=]+(\d+)', re.IGNORECASE)


def _merge_trace_ids(*groups: list[str], limit: int = 20) -> list[str]:
    normalized_groups = [
        [
            trace_id
            for trace_id in dict.fromkeys(
                str(value or "").strip()
                for value in group
            )
            if trace_id
        ]
        for group in groups
    ]
    merged: list[str] = []
    for index, group in enumerate(normalized_groups):
        remaining_groups = sum(
            1
            for later in normalized_groups[index + 1:]
            if any(value not in merged for value in later)
        )
        current_budget = max(0, limit - len(merged) - remaining_groups)
        for value in group:
            if len(merged) >= limit or current_budget <= 0:
                break
            if value not in merged:
                merged.append(value)
                current_budget -= 1
    if len(merged) < limit:
        for group in normalized_groups:
            for value in group:
                if value not in merged:
                    merged.append(value)
                    if len(merged) >= limit:
                        return merged
    return merged


def _trace_ids_from_evidence(sources: list[SourceResult]) -> list[str]:
    """从日志/证据文本里提取 app 级 trace_id（插桩应用在日志打的），供 Tempo 查询。
    插桩 Pod 即使采集窗口内无新 DeepFlow flow，其历史日志里也有 trace_id。"""
    found: list[str] = []
    for src in sources:
        for ev in src.evidence:
            blobs = [str(ev.summary or "")]
            payload = ev.payload or {}
            for key in ("log_preview", "records", "message"):
                v = payload.get(key)
                if v:
                    blobs.append(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v)
            for blob in blobs:
                for tid in _TRACE_ID_RE.findall(blob):
                    t = tid.lower()
                    if t not in found:
                        found.append(t)
    return found[:20]


def _resolve_ip_to_pod(ip: str, timeout: int = 20) -> tuple[str, str] | None:
    """Resolve a Pod IP to (namespace, pod_name) via kubectl (cluster-wide)."""
    ok, out, _ = _kube.run_kubectl(
        ["get", "pod", "-A", "--field-selector", f"status.podIP={ip}",
         "-o", "jsonpath={.items[0].metadata.namespace}/{.items[0].metadata.name}"],
        timeout=timeout,
    )
    if ok and out.strip() and "/" in out:
        ns, name = out.strip().split("/", 1)
        return ns, name
    return None


def _collect_caller_logs(cfg: Any, caller_ips: list[str], window: Any) -> SourceResult | None:
    """request_driver: resolve the caller IP to its Pod and collect that Pod's
    logs — the request side that drove the target Pod's failure. Best-effort;
    returns None when no caller Pod can be resolved."""
    for ip in caller_ips[:5]:
        resolved = _resolve_ip_to_pod(ip, timeout=cfg.timeout_seconds)
        if not resolved:
            continue
        ns, name = resolved
        result = collect_elasticsearch(
            elasticsearch_url=cfg.elasticsearch_url,
            index=cfg.elasticsearch_index,
            namespace=ns, pod=name, pod_uid=None,
            start=window.start, end=window.end,
            timeout=cfg.timeout_seconds, max_records=cfg.max_records,
        )
        if result.coverage == "present" and result.records:
            # Retag as request_driver caller evidence.
            result.dimension = "caller_logs"
            for ev in result.evidence:
                ev.dimension = "caller_logs"
                ev.evidence_id = f"caller-log-{ns}-{name}"
                ev.summary = f"Caller Pod {ns}/{name}（{ip}）日志：{ev.summary}"
            result.query_meta["caller_pod"] = f"{ns}/{name}"
            result.query_meta["caller_ip"] = ip
            return result
    return None


def _write_package(
    case_dir: Path,
    case_id: str,
    scenario: str,
    primary: dict[str, Any],
    time_window: dict[str, Any],
    sources: list[SourceResult],
) -> None:
    evidence_records = _evidence_records(sources)
    refs_by_dimension = _recommended_refs_by_dimension(evidence_records)
    k8s_record = next((source.records[0] for source in sources if source.dimension == "k8s" and source.records), {})
    deepflow = next((source for source in sources if source.source_system == "deepflow"), None)
    pod_ip = primary.get("pod_ip")
    deepflow_peers = _deepflow_peer_ips(
        deepflow.records if deepflow else [],
        pod_ip=str(pod_ip or ""),
    )
    resolved_callers: list[dict[str, Any]] = []
    for source in sources:
        if source.dimension != "caller_logs":
            continue
        caller_pod = str(source.query_meta.get("caller_pod") or "")
        caller_ip = str(source.query_meta.get("caller_ip") or "")
        if "/" not in caller_pod:
            continue
        caller_namespace, caller_name = caller_pod.split("/", 1)
        resolved_callers.append(
            {
                "namespace": caller_namespace,
                "pod": caller_name,
                "pod_ip": caller_ip,
            }
        )
    entities, topology_edges, topology_summary = build_topology(
        primary=primary,
        k8s_record=k8s_record,
        evidence_refs_by_dimension=refs_by_dimension,
        deepflow_coverage=deepflow.coverage if deepflow else "absent",
        deepflow_peers=deepflow_peers,
        resolved_callers=resolved_callers,
    )
    coverage = {source.dimension: source.coverage for source in sources}
    coverage["topology"] = "present" if topology_edges else "empty"
    # logs coverage 应反映"任意来源的真实日志"：K8s collector 采到的容器日志也标了
    # dimension="logs"，避免 ES 空时误报 logs=empty（其实容器日志已采到）。
    if coverage.get("logs") in (None, "empty", "absent"):
        k8s_has_logs = any(
            ev.dimension == "logs" and (ev.payload or {}).get("log_preview")
            for src in sources if src.dimension == "k8s"
            for ev in src.evidence
        )
        if k8s_has_logs:
            coverage["logs"] = "present"

    case = {
        "case_id": case_id,
        "scenario": scenario,
        "abnormal_type": _infer_abnormal_type(sources),
        "primary_entity": primary,
        "time_window": time_window,
        "coverage": coverage,
        "recommended_refs_by_dimension": refs_by_dimension,
    }
    write_yaml(case_dir / "case.yaml", case)
    write_jsonl(case_dir / "signals.jsonl", _signals_from_sources(sources))
    write_jsonl(case_dir / "timeline.jsonl", _timeline_from_sources(sources))
    write_jsonl(case_dir / "entities.jsonl", entities)
    write_jsonl(case_dir / "topology.jsonl", topology_edges)
    write_jsonl(case_dir / "evidence" / "metrics.jsonl", _records_for_dimension(evidence_records, "metrics"))
    write_jsonl(
        case_dir / "evidence" / "logs.jsonl",
        _records_for_dimension(evidence_records, "logs")
        + _records_for_dimension(evidence_records, "caller_logs"),
    )
    write_jsonl(case_dir / "evidence" / "deepflow_l4.jsonl", [])
    write_jsonl(case_dir / "evidence" / "deepflow_l7.jsonl", _records_for_dimension(evidence_records, "tracing"))
    write_jsonl(case_dir / "evidence" / "tempo_traces.jsonl", _records_for_dimension(evidence_records, "trace"))
    write_jsonl(case_dir / "evidence" / "k8s_events.jsonl", _records_for_dimension(evidence_records, "k8s"))
    (case_dir / "evidence").mkdir(parents=True, exist_ok=True)
    k8s = next((source for source in sources if source.dimension == "k8s"), None)
    if k8s:
        meta = k8s.query_meta
        (case_dir / "evidence" / "k8s_describe.txt").write_text(str(meta.get("describe") or ""), encoding="utf-8")
        (case_dir / "evidence" / "k8s_pod.yaml").write_text(str(meta.get("pod_yaml") or ""), encoding="utf-8")


def _deepflow_peer_ips(rows: list[dict[str, Any]], pod_ip: str) -> list[str]:
    peers: list[str] = []
    for row in rows:
        for key in ("src_ip", "dst_ip", "ip4_0", "ip4_1"):
            ip = str(row.get(key) or "").strip()
            if ip and ip != pod_ip and ip not in peers:
                peers.append(ip)
    return peers


def case_summary(case_dir: Path) -> dict[str, Any]:
    case = read_yaml(case_dir / "case.yaml")
    if not case:
        raise FileNotFoundError(f"case.yaml not found under {case_dir}")
    signals = read_jsonl(case_dir / "signals.jsonl", limit=12)
    timeline = read_jsonl(case_dir / "timeline.jsonl", limit=12)
    entities = read_jsonl(case_dir / "entities.jsonl")
    topology = read_jsonl(case_dir / "topology.jsonl")
    refs_by_dimension = case.get("recommended_refs_by_dimension") or _recommended_refs_by_dimension(_all_evidence_records(case_dir))
    dimension_details = _build_dimension_details(case_dir, case.get("coverage", {}), entities, topology)
    return {
        "ok": True,
        "case_id": case.get("case_id") or case_dir.name,
        "abnormal_type": case.get("abnormal_type"),
        "scenario": case.get("scenario"),
        "primary_entity": case.get("primary_entity", {}),
        "time_window": case.get("time_window", {}),
        "coverage": case.get("coverage", {}),
        "signals_summary": [_select(item, ["signal_id", "dimension", "role", "strength", "observed", "interpretation", "source_system", "evidence_refs"]) for item in signals],
        "timeline_summary": [_select(item, ["timestamp", "dimension", "summary", "evidence_refs"]) for item in timeline],
        "topology_summary": summarize_topology(entities, topology),
        "dimension_details": dimension_details,
        "evidence_inventory": evidence_inventory(case_dir),
        "evidence_refs": evidence_refs(case_dir),
        "recommended_refs_by_dimension": refs_by_dimension,
        "package_ref": str(case_dir),
        "runtime_status": runtime_status(),
    }


def _build_dimension_details(
    case_dir: Path,
    coverage: dict[str, Any],
    entities: list[dict[str, Any]],
    topology: list[dict[str, Any]],
) -> dict[str, Any]:
    records = _all_evidence_records(case_dir)
    metrics: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    flows: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    call_chains: list[dict[str, Any]] = []

    for record in records:
        evidence_ref = str(record.get("evidence_id") or "")
        dimension = str(record.get("dimension") or "")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if dimension == "metrics":
            for item in payload.get("highlights") or []:
                if not isinstance(item, dict):
                    continue
                metrics.append(
                    {
                        "metric": item.get("metric") or "container_memory_working_set_bytes",
                        "pod": item.get("pod"),
                        "container": item.get("container"),
                        "start": item.get("start"),
                        "max": item.get("max"),
                        "last": item.get("last"),
                        "limit": item.get("limit"),
                        "max_limit_ratio": item.get("max_limit_ratio"),
                        "samples": (item.get("samples") or [])[:5],
                        "evidence_ref": evidence_ref,
                    }
                )
        elif dimension in {"logs", "caller_logs"}:
            samples = payload.get("samples") or []
            if not samples and payload.get("log_preview"):
                samples = [
                    {
                        "timestamp": record.get("timestamp"),
                        "message": line,
                        "container": _container_from_summary(record.get("summary")),
                    }
                    for line in str(payload["log_preview"]).splitlines()
                    if line.strip()
                ][-8:]
            for item in samples:
                if not isinstance(item, dict) or not str(item.get("message") or "").strip():
                    continue
                logs.append(
                    {
                        "timestamp": item.get("timestamp"),
                        "message": str(item.get("message") or "")[:600],
                        "container": item.get("container"),
                        "role": "request_driver" if dimension == "caller_logs" else "target",
                        "source_system": record.get("source_system"),
                        "evidence_ref": evidence_ref,
                    }
                )
        elif dimension == "tracing":
            for item in payload.get("samples") or []:
                if not isinstance(item, dict):
                    continue
                flows.append(
                    {
                        "timestamp": item.get("timestamp"),
                        "src": item.get("src"),
                        "dst": item.get("dst"),
                        "protocol": item.get("protocol"),
                        "request": str(item.get("request") or "")[:240],
                        "response_code": item.get("response_code"),
                        "duration_us": item.get("duration_us"),
                        "trace_id": item.get("trace_id"),
                        "span_id": item.get("span_id"),
                        "evidence_ref": evidence_ref,
                    }
                )
            for chain in payload.get("call_chains") or []:
                if isinstance(chain, dict):
                    call_chains.append(
                        {
                            "syscall_trace_id": chain.get("syscall_trace_id"),
                            "hop_count": chain.get("hop_count"),
                            "hops": (chain.get("hops") or [])[:5],
                            "evidence_ref": evidence_ref,
                        }
                    )
        elif dimension == "trace":
            for item in payload.get("spans") or []:
                if not isinstance(item, dict):
                    continue
                spans.append(
                    {
                        "trace_id": item.get("trace_id"),
                        "service": item.get("service"),
                        "name": item.get("name"),
                        "start": item.get("start"),
                        "end": item.get("end"),
                        "attributes": _compact_attributes(item.get("attributes")),
                        "evidence_ref": evidence_ref,
                    }
                )

    ranked_logs = _rank_log_samples(logs)
    ranked_logs, flows, spans = _align_correlated_samples(ranked_logs, flows, spans)
    topology_payload = topology_details(entities, topology)
    topology_payload["coverage"] = str(coverage.get("topology") or ("present" if topology else "empty"))
    details = {
        "metrics": {
            "coverage": str(coverage.get("metrics") or "absent"),
            "highlights": metrics[:5],
        },
        "logs": {
            "coverage": str(coverage.get("logs") or "absent"),
            "samples": ranked_logs[:8],
        },
        "tracing": {
            "coverage": _combined_coverage(coverage.get("tracing"), coverage.get("trace")),
            "deepflow_coverage": str(coverage.get("tracing") or "absent"),
            "tempo_coverage": str(coverage.get("trace") or "absent"),
            "flows": flows[:5],
            "spans": spans[:3],
            "call_chains": call_chains[:3],
        },
        "topology": topology_payload,
    }
    return _fit_dimension_details(details)


def _combined_coverage(*values: Any) -> str:
    states = [str(value) for value in values if value]
    for state in ("present", "weak", "error", "empty", "absent"):
        if state in states:
            return state
    return "absent"


def _container_from_summary(summary: Any) -> str | None:
    match = re.search(r"container\s+([A-Za-z0-9._-]+)", str(summary or ""), re.IGNORECASE)
    return match.group(1) if match else None


def _compact_attributes(attributes: Any, max_items: int = 8) -> dict[str, Any]:
    if not isinstance(attributes, dict):
        return {}
    compact: dict[str, Any] = {}
    for key, value in list(attributes.items())[:max_items]:
        if isinstance(value, str):
            compact[str(key)] = value[:160]
        elif isinstance(value, (int, float, bool)) or value is None:
            compact[str(key)] = value
        else:
            compact[str(key)] = str(value)[:160]
    return compact


def _rank_log_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def priority(item: dict[str, Any]) -> tuple[int, int, int, int, int]:
        message = str(item.get("message") or "")
        allocated = [int(value) for value in _ALLOCATED_MIB_RE.findall(message)]
        peak = max(allocated) if allocated else -1
        evidence_ref = str(item.get("evidence_ref") or "")
        role_rank = 0 if item.get("role") == "target" else 1
        unusable_rank = 1 if re.search(
            r"unable to retrieve|no logs|logs? unavailable|not found|error from server",
            message,
            re.IGNORECASE,
        ) else 0
        previous_rank = 0 if "previous" in evidence_ref else 1
        failure_rank = 0 if re.search(
            r"request_failed|config_missing|fatal|error_code|http_status.?[:=]?\s*500|"
            r"exception|traceback|oomkilled|exit.?137",
            message,
            re.IGNORECASE,
        ) else 1
        return role_rank, unusable_rank, -peak, failure_rank, previous_rank

    return sorted(samples, key=priority)


def _align_correlated_samples(
    logs: list[dict[str, Any]],
    flows: list[dict[str, Any]],
    spans: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    def log_trace_id(item: dict[str, Any]) -> str:
        try:
            payload = json.loads(str(item.get("message") or ""))
        except json.JSONDecodeError:
            return ""
        return str(payload.get("trace_id") or "") if isinstance(payload, dict) else ""

    log_ids = {trace_id for item in logs if (trace_id := log_trace_id(item))}
    flow_ids = {str(item.get("trace_id") or "") for item in flows if item.get("trace_id")}
    span_ids = {str(item.get("trace_id") or "") for item in spans if item.get("trace_id")}
    shared_ids = log_ids & flow_ids & span_ids
    if not shared_ids:
        return logs, flows, spans

    preferred = next(
        (
            str(item.get("trace_id"))
            for item in flows
            if str(item.get("trace_id") or "") in shared_ids
        ),
        sorted(shared_ids)[0],
    )

    def prioritize(items: list[dict[str, Any]], trace_id_getter) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: 0 if trace_id_getter(item) == preferred else 1)

    return (
        prioritize(logs, log_trace_id),
        prioritize(flows, lambda item: str(item.get("trace_id") or "")),
        prioritize(spans, lambda item: str(item.get("trace_id") or "")),
    )


def _fit_dimension_details(details: dict[str, Any], max_chars: int = 6000) -> dict[str, Any]:
    bounded = json.loads(json.dumps(details, ensure_ascii=False, default=str))

    def rendered_size() -> int:
        return len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")))

    if rendered_size() <= max_chars:
        return bounded

    collections = [
        bounded.get("logs", {}).get("samples", []),
        bounded.get("tracing", {}).get("flows", []),
        bounded.get("tracing", {}).get("spans", []),
        bounded.get("tracing", {}).get("call_chains", []),
        bounded.get("topology", {}).get("edges", []),
        bounded.get("topology", {}).get("entities", []),
        bounded.get("metrics", {}).get("highlights", []),
    ]
    minimums = [1, 1, 1, 0, 2, 2, 1]
    while rendered_size() > max_chars:
        changed = False
        for items, minimum in zip(collections, minimums):
            if len(items) > minimum:
                items.pop()
                changed = True
                if rendered_size() <= max_chars:
                    return bounded
        if not changed:
            break

    for sample in bounded.get("logs", {}).get("samples", []):
        sample["message"] = str(sample.get("message") or "")[:240]
    for span in bounded.get("tracing", {}).get("spans", []):
        span["attributes"] = _compact_attributes(span.get("attributes"), max_items=4)
    for highlight in bounded.get("metrics", {}).get("highlights", []):
        highlight["samples"] = (highlight.get("samples") or [])[:3]

    if rendered_size() <= max_chars:
        return bounded

    for section in bounded.values():
        _truncate_strings(section, 100)
    return bounded


def _truncate_strings(value: Any, limit: int) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and len(item) > limit:
                value[key] = item[:limit]
            else:
                _truncate_strings(item, limit)
    elif isinstance(value, list):
        for item in value:
            _truncate_strings(item, limit)


def get_case(cases_dir: Path, case_id: str) -> dict[str, Any]:
    return case_summary(safe_case_dir(cases_dir, case_id))


def get_evidence(cases_dir: Path, case_id: str, evidence_ref: str, max_bytes: int = 12000) -> dict[str, Any]:
    case_dir = safe_case_dir(cases_dir, case_id)
    if not evidence_ref:
        return {"ok": False, "error": "evidence_ref is required"}
    for relative in EVIDENCE_FILES:
        path = case_dir / relative
        if path.suffix != ".jsonl":
            continue
        for item in read_jsonl(path):
            if item.get("evidence_id") == evidence_ref:
                return {"ok": True, "case_id": case_dir.name, "ref": relative, "record": item}
    path = safe_relative_path(case_dir, evidence_ref)
    if evidence_ref not in set(EVIDENCE_FILES + ["case.yaml", "signals.jsonl", "timeline.jsonl", "entities.jsonl", "topology.jsonl"]):
        return {"ok": False, "error": f"evidence_ref is not agent-visible: {evidence_ref}"}
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": f"evidence_ref not found: {evidence_ref}"}
    content = path.read_text(encoding="utf-8", errors="replace")
    return {
        "ok": True,
        "case_id": case_dir.name,
        "ref": evidence_ref,
        "truncated": len(content) > max_bytes,
        "content": content[:max_bytes],
    }


def search_cases(cases_dir: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    namespace = str(arguments.get("namespace") or "")
    pod = str(arguments.get("pod") or "")
    abnormal_type = str(arguments.get("abnormal_type") or "")
    cases = []
    for case_dir in sorted(path for path in cases_dir.glob("*") if path.is_dir()):
        case = read_yaml(case_dir / "case.yaml")
        primary = case.get("primary_entity", {}) if isinstance(case, dict) else {}
        if namespace and primary.get("namespace") != namespace:
            continue
        if pod and primary.get("name") != pod:
            continue
        if abnormal_type and case.get("abnormal_type") != abnormal_type:
            continue
        cases.append(
            {
                "case_id": case.get("case_id") or case_dir.name,
                "abnormal_type": case.get("abnormal_type"),
                "namespace": primary.get("namespace"),
                "pod": primary.get("name"),
                "package_ref": str(case_dir),
            }
        )
    return {"ok": True, "cases": cases[:50], "total": len(cases)}


def evidence_inventory(case_dir: Path) -> list[dict[str, Any]]:
    return [
        {
            "ref": relative,
            "exists": (case_dir / relative).exists(),
            "bytes": (case_dir / relative).stat().st_size if (case_dir / relative).exists() else 0,
            "records": line_count(case_dir / relative),
        }
        for relative in EVIDENCE_FILES
    ]


def evidence_refs(case_dir: Path, limit: int = 40) -> list[str]:
    refs: list[str] = []
    for item in _all_evidence_records(case_dir):
        ref = item.get("evidence_id")
        if ref and ref not in refs:
            refs.append(str(ref))
        if len(refs) >= limit:
            break
    return refs


def _all_evidence_records(case_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for relative in EVIDENCE_FILES:
        path = case_dir / relative
        if path.suffix == ".jsonl":
            rows.extend(read_jsonl(path))
    return rows


def _evidence_records(sources: list[SourceResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in sources:
        for item in source.evidence:
            rows.append(item.to_record())
        if not source.evidence and source.coverage in {"absent", "empty", "weak", "error"}:
            rows.append(
                EvidenceItem(
                    evidence_id=f"{source.source_system}-{source.dimension}-{source.coverage}",
                    source_system=source.source_system,
                    dimension=source.dimension,
                    summary=source.error or f"{source.source_system} coverage is {source.coverage}",
                    confidence="weak" if source.coverage == "weak" else "low",
                    directness="related_context" if source.coverage == "weak" else "direct",
                    payload={"coverage": source.coverage, "query_meta": source.query_meta},
                ).to_record()
            )
    return rows


def _records_for_dimension(records: list[dict[str, Any]], dimension: str) -> list[dict[str, Any]]:
    return [item for item in records if item.get("dimension") == dimension]


def _recommended_refs_by_dimension(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    for item in records:
        dimension = str(item.get("dimension") or "unknown")
        ref = item.get("evidence_id")
        if not ref:
            continue
        refs.setdefault(dimension, [])
        if ref not in refs[dimension]:
            refs[dimension].append(str(ref))
    return refs


# 每个维度的语义角色与解读（借鉴 data 脚本，让信号人可读、直接指向问题）
_DIMENSION_ROLE: dict[str, dict[str, str]] = {
    "k8s": {
        "role": "root_cause_state",
        "interpretation": "Kubernetes 终态与事件是根因状态的直接证据（如 OOMKilled/exit137、FailedScheduling）。",
    },
    "metrics": {
        "role": "resource_trend",
        "interpretation": "Prometheus 指标反映资源使用趋势，用于判断是否逼近/超过 limit。",
    },
    "logs": {
        "role": "pre_failure_behavior",
        "interpretation": "应用日志揭示失败前的进程内行为，解释异常是如何发生的。",
    },
    "caller_logs": {
        "role": "request_driver",
        "interpretation": "调用方 Pod 的日志揭示是哪些请求驱动了目标 Pod 的异常（请求侧证据）。",
    },
    "trace": {
        "role": "span_evidence",
        "interpretation": "Tempo 分布式 trace 的 span 证明请求路径真实存在，可端到端追踪。",
    },
    "tracing": {
        "role": "network_flow",
        "interpretation": "DeepFlow L7 流量：带 trace_id 时是 trace 关联的调用链强证据；仅 IP 级时是弱网络背景。",
    },
}

_STRENGTH_BY_COVERAGE = {
    "present": "strong",
    "weak": "weak",
    "empty": "empty",
    "absent": "absent",
    "error": "error",
}


def _best_observed(source: SourceResult) -> str:
    """挑一条人可读的真实原文作为 observed；无数据时给诚实缺失原因。"""
    if source.evidence:
        # 优先带 severity=warning 的证据（更可能指向问题），否则第一条
        warn = next((e for e in source.evidence if getattr(e, "severity", "") == "warning"), None)
        chosen = warn or source.evidence[0]
        return str(chosen.summary)
    if source.error:
        return f"未采集到真实数据：{source.error}"
    return f"{source.source_system} coverage={source.coverage}（无记录）"


def _signals_from_sources(sources: list[SourceResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in sources:
        refs = [item.evidence_id for item in source.evidence]
        role_info = _DIMENSION_ROLE.get(source.dimension, {"role": source.dimension, "interpretation": ""})
        # tracing 带 trace_id 时升级为 trace 关联角色
        role = role_info["role"]
        if source.dimension == "tracing" and source.coverage == "present":
            has_trace = any(
                (e.trace_correlation or {}).get("trace_ids") for e in source.evidence if e.trace_correlation
            )
            if has_trace:
                role = "trace_correlated_traffic"
        rows.append(
            {
                "signal_id": f"sig-{source.dimension}-{source.coverage}",
                "dimension": source.dimension,
                "role": role,
                "observed": _best_observed(source),
                "interpretation": role_info["interpretation"],
                "strength": _STRENGTH_BY_COVERAGE.get(source.coverage, source.coverage),
                "source_system": source.source_system,
                "evidence_refs": refs[:8],
            }
        )
    return rows


def _timeline_from_sources(sources: list[SourceResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in sources:
        for item in source.evidence[:5]:
            rows.append(
                {
                    "timestamp": item.timestamp,
                    "dimension": item.dimension,
                    "summary": item.summary,
                    "evidence_refs": [item.evidence_id],
                }
            )
    return rows


def _infer_abnormal_type(sources: list[SourceResult]) -> str:
    k8s = next((source for source in sources if source.dimension == "k8s"), None)
    record = k8s.records[0] if k8s and k8s.records else {}
    status_text = json.dumps(
        {
            "container_statuses": record.get("container_statuses"),
            "last_terminated": record.get("last_terminated"),
        },
        ensure_ascii=False,
        default=str,
    ).lower()
    evidence_text = " ".join(
        f"{item.summary} {json.dumps(item.payload or {}, ensure_ascii=False, default=str)}"
        for item in (k8s.evidence if k8s else [])
    ).lower()
    text = f"{status_text} {evidence_text}"
    if "oomkilled" in text:
        return "oomkilled"
    if "imagepullbackoff" in text or "errimagepull" in text:
        return "imagepullbackoff"
    if (
        "crashloopbackoff" in text
        or "back-off restarting failed container" in text
        or "backoff restarting failed container" in text
    ):
        return "crashloopbackoff"
    if record.get("deletion_timestamp"):
        return "terminating"
    return "unknown"


def _select(item: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: item.get(key) for key in keys if key in item}
