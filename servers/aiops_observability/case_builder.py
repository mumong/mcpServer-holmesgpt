"""Build and summarize AIOps case packages from live source collectors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .collectors.deepflow import collect_deepflow
from .collectors.elasticsearch import collect_elasticsearch
from .collectors.kubernetes import collect_kubernetes
from .collectors.prometheus import collect_prometheus
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
from .topology import build_topology, summarize_topology


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

    sources = [k8s, metrics, logs, deepflow]
    case_dir.mkdir(parents=True, exist_ok=True)
    _write_package(case_dir, case_id, scenario, primary, window.to_dict(), sources)
    return case_summary(case_dir)


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
    entities, topology_edges, topology_summary = build_topology(
        primary=primary,
        k8s_record=k8s_record,
        evidence_refs_by_dimension=refs_by_dimension,
        deepflow_coverage=deepflow.coverage if deepflow else "absent",
    )
    coverage = {source.dimension: source.coverage for source in sources}
    coverage["topology"] = "present" if topology_edges else "empty"

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
    write_jsonl(case_dir / "evidence" / "logs.jsonl", _records_for_dimension(evidence_records, "logs"))
    write_jsonl(case_dir / "evidence" / "deepflow_l4.jsonl", [])
    write_jsonl(case_dir / "evidence" / "deepflow_l7.jsonl", _records_for_dimension(evidence_records, "tracing"))
    write_jsonl(case_dir / "evidence" / "k8s_events.jsonl", _records_for_dimension(evidence_records, "k8s"))
    (case_dir / "evidence").mkdir(parents=True, exist_ok=True)
    k8s = next((source for source in sources if source.dimension == "k8s"), None)
    if k8s:
        meta = k8s.query_meta
        (case_dir / "evidence" / "k8s_describe.txt").write_text(str(meta.get("describe") or ""), encoding="utf-8")
        (case_dir / "evidence" / "k8s_pod.yaml").write_text(str(meta.get("pod_yaml") or ""), encoding="utf-8")


def case_summary(case_dir: Path) -> dict[str, Any]:
    case = read_yaml(case_dir / "case.yaml")
    if not case:
        raise FileNotFoundError(f"case.yaml not found under {case_dir}")
    signals = read_jsonl(case_dir / "signals.jsonl", limit=12)
    timeline = read_jsonl(case_dir / "timeline.jsonl", limit=12)
    entities = read_jsonl(case_dir / "entities.jsonl")
    topology = read_jsonl(case_dir / "topology.jsonl")
    refs_by_dimension = case.get("recommended_refs_by_dimension") or _recommended_refs_by_dimension(_all_evidence_records(case_dir))
    return {
        "ok": True,
        "case_id": case.get("case_id") or case_dir.name,
        "abnormal_type": case.get("abnormal_type"),
        "scenario": case.get("scenario"),
        "primary_entity": case.get("primary_entity", {}),
        "time_window": case.get("time_window", {}),
        "coverage": case.get("coverage", {}),
        "signals_summary": [_select(item, ["signal_id", "dimension", "strength", "observed", "evidence_refs"]) for item in signals],
        "timeline_summary": [_select(item, ["timestamp", "dimension", "summary", "evidence_refs"]) for item in timeline],
        "topology_summary": summarize_topology(entities, topology),
        "evidence_inventory": evidence_inventory(case_dir),
        "evidence_refs": evidence_refs(case_dir),
        "recommended_refs_by_dimension": refs_by_dimension,
        "package_ref": str(case_dir),
        "runtime_status": runtime_status(),
    }


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


def _signals_from_sources(sources: list[SourceResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in sources:
        refs = [item.evidence_id for item in source.evidence]
        rows.append(
            {
                "signal_id": f"{source.dimension}-{source.coverage}",
                "dimension": source.dimension,
                "strength": "strong" if source.coverage == "present" else source.coverage,
                "observed": source.error or f"{source.source_system} coverage={source.coverage} records={len(source.records)}",
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
    status_text = str(record.get("container_statuses") or record.get("last_terminated") or "").lower()
    evidence_text = " ".join(item.summary for item in (k8s.evidence if k8s else [])).lower()
    text = f"{status_text} {evidence_text}"
    if "oomkilled" in text:
        return "oomkilled"
    if "imagepullbackoff" in text or "errimagepull" in text:
        return "imagepullbackoff"
    if "crashloopbackoff" in text:
        return "crashloopbackoff"
    if record.get("deletion_timestamp"):
        return "terminating"
    return "unknown"


def _select(item: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: item.get(key) for key in keys if key in item}
