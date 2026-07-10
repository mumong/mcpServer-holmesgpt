"""Tempo distributed-trace collector for Pod-scoped AIOps cases.

Mirrors the data-project script: `kubectl exec` into the LGTM/Tempo Pod and
GET /api/traces/{trace_id}. Only meaningful when the target workload is
OpenTelemetry-instrumented and its trace_ids appear in DeepFlow L7 flows or
logs. For non-instrumented pods there are simply no trace_ids -> absent
(a data limitation, not a collection failure).
"""

from __future__ import annotations

import json
from typing import Any

from ..config import get_config
from ..schemas import EvidenceItem, SourceResult, evidence_id
from . import _kube


def collect_tempo(*, namespace: str, pod: str, trace_ids: list[str], timeout: int, max_traces: int = 5) -> SourceResult:
    cfg = get_config()
    if not cfg.tempo_exec_enabled:
        return SourceResult("tempo", "trace", "absent", error="Tempo kubectl-exec 模式已禁用")
    ids = [t for t in dict.fromkeys(trace_ids) if t][:max_traces]
    if not ids:
        return SourceResult(
            "tempo", "trace", "empty",
            error="无可查询的 trace_id（未从 DeepFlow flow 或日志中发现 trace_id；可能该 Pod 未插桩，或采集窗口内无 trace 且日志未打 trace_id）",
        )
    tempo_pod = cfg.tempo_exec_pod or _kube.find_pod(cfg.tempo_exec_namespace, cfg.tempo_exec_selector)
    if not tempo_pod:
        return SourceResult(
            "tempo", "trace", "absent",
            error=f"未找到 Tempo/LGTM Pod（ns={cfg.tempo_exec_namespace} selector={cfg.tempo_exec_selector}）",
        )
    found: list[dict[str, Any]] = []
    for tid in ids:
        url = f"{cfg.tempo_local_url.rstrip('/')}/api/traces/{tid}"
        ok, out, _ = _kube.exec_in_pod(cfg.tempo_exec_namespace, tempo_pod, ["curl", "-s", url], timeout=timeout)
        if not ok or not out.strip():
            continue
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            continue
        span_count = _count_spans(payload)
        if span_count > 0:
            found.append({"trace_id": tid, "span_count": span_count})
    if not found:
        return SourceResult(
            "tempo", "trace", "empty",
            error=f"Tempo 中未找到这些 trace 的 span（查询了 {len(ids)} 个 trace_id）",
            query_meta={"queried_trace_ids": ids, "tempo_pod": tempo_pod},
        )
    trace_list = ", ".join(t["trace_id"] for t in found)
    evidence = [
        EvidenceItem(
            evidence_id=evidence_id("tempo", namespace, pod, "spans"),
            source_system="tempo",
            dimension="trace",
            summary=f"Tempo 返回 {len(found)} 条 trace 的分布式 span：{trace_list}（来源：kubectl exec {tempo_pod}）",
            confidence="high",
            directness="direct",
            trace_correlation={"trace_ids": [t["trace_id"] for t in found]},
            payload={"traces": found, "source": f"kubectl exec {tempo_pod}"},
        )
    ]
    return SourceResult(
        "tempo", "trace", "present",
        records=found,
        evidence=evidence,
        query_meta={"queried_trace_ids": ids, "tempo_pod": tempo_pod},
    )


def _count_spans(payload: Any) -> int:
    """Count spans across Tempo's OTLP-ish trace JSON (batches/resourceSpans/scopeSpans/spans)."""
    total = 0
    if isinstance(payload, dict):
        for key in ("batches", "resourceSpans"):
            for batch in payload.get(key) or []:
                for scope in (batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []):
                    total += len(scope.get("spans") or [])
        if not total and isinstance(payload.get("spans"), list):
            total += len(payload["spans"])
    return total
