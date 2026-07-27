"""Tempo distributed-trace collector for Pod-scoped AIOps cases.

Mirrors the data-project script: `kubectl exec` into the LGTM/Tempo Pod and
GET /api/traces/{trace_id}. Only meaningful when the target workload is
OpenTelemetry-instrumented and its trace_ids appear in DeepFlow L7 flows or
logs. For non-instrumented pods there are simply no trace_ids -> absent
(a data limitation, not a collection failure).
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
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
    span_samples: list[dict[str, Any]] = []
    successful_lookups = 0
    failures: list[str] = []
    for tid in ids:
        url = f"{cfg.tempo_local_url.rstrip('/')}/api/traces/{tid}"
        ok, out, error = _kube.exec_in_pod(
            cfg.tempo_exec_namespace,
            tempo_pod,
            ["curl", "-s", url],
            timeout=timeout,
        )
        if not ok:
            failures.append(str(error or out or f"Tempo query failed for {tid}"))
            continue
        if not out.strip():
            failures.append(f"Tempo returned an empty response for {tid}")
            continue
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            failures.append(f"Tempo returned invalid JSON for {tid}: {exc}")
            continue
        successful_lookups += 1
        extracted = _extract_spans(payload, fallback_trace_id=tid)
        span_count = len(extracted)
        if span_count > 0:
            found.append({"trace_id": tid, "span_count": span_count})
            span_samples.extend(extracted[: max(0, 3 - len(span_samples))])
    if not found:
        if failures and successful_lookups == 0:
            return SourceResult(
                "tempo",
                "trace",
                "error",
                error="; ".join(failures[:3]),
                query_meta={"queried_trace_ids": ids, "tempo_pod": tempo_pod},
            )
        if failures:
            return SourceResult(
                "tempo",
                "trace",
                "weak",
                error="; ".join(failures[:3]),
                query_meta={
                    "queried_trace_ids": ids,
                    "tempo_pod": tempo_pod,
                    "failed_trace_count": len(failures),
                    "successful_lookup_count": successful_lookups,
                },
            )
        return SourceResult(
            "tempo", "trace", "empty",
            error=f"Tempo 中未找到这些 trace 的 span（查询了 {len(ids)} 个 trace_id）",
            query_meta={
                "queried_trace_ids": ids,
                "tempo_pod": tempo_pod,
                "failed_trace_count": 0,
                "successful_lookup_count": successful_lookups,
            },
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
            payload={
                "traces": found,
                "spans": span_samples[:3],
                "source": f"kubectl exec {tempo_pod}",
            },
        )
    ]
    return SourceResult(
        "tempo", "trace", "weak" if failures else "present",
        records=found,
        evidence=evidence,
        query_meta={
            "queried_trace_ids": ids,
            "tempo_pod": tempo_pod,
            "failed_trace_count": len(failures),
            "successful_lookup_count": successful_lookups,
        },
        error="; ".join(failures[:3]) if failures else None,
    )


def _count_spans(payload: Any) -> int:
    """Count spans across Tempo's OTLP-ish trace JSON (batches/resourceSpans/scopeSpans/spans)."""
    return len(_extract_spans(payload))


def _extract_spans(payload: Any, fallback_trace_id: str = "") -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    extracted: list[dict[str, Any]] = []
    batches = []
    for key in ("batches", "resourceSpans"):
        value = payload.get(key)
        if isinstance(value, list):
            batches.extend(value)
    for batch in batches:
        resource_attributes = _attributes(((batch or {}).get("resource") or {}).get("attributes"))
        service = str(resource_attributes.get("service.name") or "")
        scopes = (batch or {}).get("scopeSpans") or (batch or {}).get("instrumentationLibrarySpans") or []
        for scope in scopes:
            for span in (scope or {}).get("spans") or []:
                extracted.append(_span_sample(span, service, fallback_trace_id))
    if not extracted and isinstance(payload.get("spans"), list):
        extracted.extend(_span_sample(span, "", fallback_trace_id) for span in payload["spans"])
    return extracted


def _span_sample(span: Any, service: str, fallback_trace_id: str) -> dict[str, Any]:
    item = span if isinstance(span, dict) else {}
    attributes = _attributes(item.get("attributes"))
    return {
        "trace_id": _normalize_trace_id(
            item.get("traceId") or item.get("trace_id"),
            fallback_trace_id,
        ),
        "service": service or attributes.get("service.name") or "",
        "name": item.get("name") or "",
        "start": _readable_time(item.get("startTimeUnixNano") or item.get("start_time_unix_nano") or item.get("start")),
        "end": _readable_time(item.get("endTimeUnixNano") or item.get("end_time_unix_nano") or item.get("end")),
        "attributes": attributes,
    }


def _normalize_trace_id(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return text.lower()
    if text:
        try:
            decoded = base64.b64decode(text, validate=True)
        except (ValueError, TypeError):
            decoded = b""
        if len(decoded) == 16:
            return decoded.hex()
    return str(fallback or "").strip().lower()


def _attributes(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): _attribute_value(item) for key, item in value.items()}
    result: dict[str, Any] = {}
    for item in value or []:
        if not isinstance(item, dict) or not item.get("key"):
            continue
        result[str(item["key"])] = _attribute_value(item.get("value"))
    return result


def _attribute_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            raw = value[key]
            if key == "intValue":
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return raw
            return raw
    if "arrayValue" in value:
        values = (value.get("arrayValue") or {}).get("values") or []
        return [_attribute_value(item) for item in values]
    return value


def _readable_time(value: Any) -> str:
    text = str(value or "")
    try:
        nanoseconds = int(text)
    except ValueError:
        return text
    if nanoseconds < 1_000_000_000_000_000:
        return text
    return datetime.fromtimestamp(nanoseconds / 1_000_000_000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
