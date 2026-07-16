"""Canonical, diagnosis-free Fact Ledger projection for AIOps cases."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .topology import pod_entity_id, scope_primary_topology_edge


CONTRACT_VERSION = "aiops.fact-ledger.v1"
DEFAULT_MAX_RECORDS = 48
DEFAULT_MAX_CHARS = 12000

_DIMENSION_NAMES = {
    "k8s": "kubernetes",
    "metrics": "metrics",
    "logs": "logging",
    "caller_logs": "logging",
    "tracing": "tracing",
    "trace": "tracing",
    "topology": "topology",
}
_COVERAGE_SOURCES = {
    "k8s": "kubernetes",
    "metrics": "prometheus",
    "logs": "logging",
    "caller_logs": "logging",
    "tracing": "deepflow",
    "trace": "tempo",
    "topology": "topology",
}
_FORBIDDEN_KEYS = {
    "diagnosis",
    "diagnostic_role",
    "expected_remediation",
    "ground_truth",
    "label",
    "remediation",
    "role",
    "root_cause",
    "root_cause_label",
}
_FORBIDDEN_NORMALIZED_KEYS = {
    "".join(character for character in key.lower() if character.isalnum())
    for key in _FORBIDDEN_KEYS
}
_FORBIDDEN_NORMALIZED_FAMILIES = {
    "diagnosis",
    "diagnostic",
    "evaluator",
    "expectedremediation",
    "groundtruth",
    "prefailurebehavior",
    "remediation",
    "requestdriver",
    "rootcause",
}
_DIRECTNESS = {"direct", "derived", "related_context"}
_CONFIDENCE = {"high", "medium", "low", "weak"}
_STRENGTH_PRIORITY = {
    "critical": 0,
    "strong": 1,
    "supporting": 2,
    "context": 3,
}
_DIRECTNESS_PRIORITY = {
    "direct": 0,
    "derived": 1,
    "related_context": 2,
}
_CONFIDENCE_PRIORITY = {
    "high": 0,
    "medium": 1,
    "low": 2,
    "weak": 3,
}
_RESERVED_DIMENSION_ORDER = (
    "kubernetes",
    "metrics",
    "logging",
    "tracing",
    "topology",
)
_INTERVAL_FACT_TYPES = {"measurement", "range", "span"}


def build_fact_ledger(
    *,
    case: dict[str, Any],
    evidence_records: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    topology: list[dict[str, Any]],
    max_records: int = DEFAULT_MAX_RECORDS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """Project source evidence into stable, bounded facts without diagnosis."""
    case_id = str(case.get("case_id") or "")
    primary = case.get("primary_entity") if isinstance(case.get("primary_entity"), dict) else {}
    primary_id = _primary_entity_id(primary, case_id)
    scope_entity_ids = [primary_id] if primary_id else []
    records: list[dict[str, Any]] = []
    time_window = case.get("time_window") if isinstance(case.get("time_window"), dict) else {}

    projected_evidence: list[
        tuple[str, str, str, list[dict[str, Any]]]
    ] = []
    ref_states: dict[str, set[str]] = {}
    ref_conflict_fingerprints: dict[str, set[str]] = {}
    for evidence in evidence_records:
        authority = _evidence_authority(evidence, primary)
        dimension = str(evidence.get("dimension") or "")
        evidence_ref = str(evidence.get("evidence_id") or "")
        projected: list[dict[str, Any]] = []
        if authority is not None:
            projected = _facts_from_evidence(
                evidence,
                primary_id,
                primary,
                force_related_context=authority == "temporal_context",
            )
            projected = [
                fact
                for fact in projected
                if _fact_within_window(
                    fact,
                    time_window,
                    require_temporal=authority == "temporal_context",
                )
            ]
        if evidence_ref:
            ref_states.setdefault(evidence_ref, set()).add(
                authority if authority is not None and projected else "rejected"
            )
            ref_conflict_fingerprints.setdefault(evidence_ref, set()).add(
                _evidence_conflict_fingerprint(evidence, authority, projected)
            )
        if projected and evidence_ref:
            projected_evidence.append(
                (evidence_ref, dimension, authority or "rejected", projected)
            )

    accepted_ref_authority = {
        evidence_ref: next(iter(states))
        for evidence_ref, states in ref_states.items()
        if len(states) == 1
        and len(ref_conflict_fingerprints.get(evidence_ref, set())) == 1
        and next(iter(states)) in {"current", "temporal_context"}
    }
    evidence_refs_by_dimension: dict[str, dict[str, list[str]]] = {}
    evidence_facts: list[dict[str, Any]] = []
    for evidence_ref, dimension, authority, projected in projected_evidence:
        if evidence_ref and accepted_ref_authority.get(evidence_ref) != authority:
            continue
        evidence_facts.extend(projected)
        if evidence_ref:
            evidence_refs_by_dimension.setdefault(dimension, {}).setdefault(
                authority,
                [],
            ).append(evidence_ref)

    for dimension, state_value in sorted((case.get("coverage") or {}).items()):
        state = str(state_value or "absent")
        refs_by_authority = evidence_refs_by_dimension.get(str(dimension), {})
        current_refs = sorted(set(refs_by_authority.get("current", [])))
        temporal_refs = sorted(
            set(refs_by_authority.get("temporal_context", []))
        )
        refs: list[str] = []
        directness = "direct"
        confidence = _coverage_confidence(state)
        if state in {"present", "weak"}:
            if current_refs:
                refs = current_refs
            elif temporal_refs:
                refs = temporal_refs
                directness = "related_context"
                confidence = "weak"
        if not str(primary.get("uid") or ""):
            directness = "related_context"
            confidence = "weak"
        records.append(
            _fact(
                entity_id=primary_id,
                entity_kind=str(primary.get("kind") or ("Pod" if primary else "Case")),
                namespace=_optional_text(primary.get("namespace")),
                entity_name=_optional_text(primary.get("name") or case_id),
                dimension=_dimension(str(dimension)),
                fact_type="coverage",
                attribute=f"{dimension}.coverage",
                value=state,
                source_system=_COVERAGE_SOURCES.get(str(dimension), str(dimension) or "unknown"),
                directness=directness,
                confidence=confidence,
                strength=(
                    "strong"
                    if state == "present" and str(primary.get("uid") or "")
                    else "context"
                ),
                evidence_refs=refs,
            )
        )

    records.extend(evidence_facts)

    entity_lookup = {
        str(entity.get("entity_id")): entity
        for entity in entities
        if isinstance(entity, dict) and entity.get("entity_id")
    }
    for edge in topology:
        if isinstance(edge, dict):
            fact = _fact_from_topology(
                edge,
                entity_lookup,
                primary_id,
                primary,
                case.get("coverage") or {},
                accepted_ref_authority,
            )
            if fact is not None:
                records.append(fact)

    merged = _merge_semantic_observations(records)
    deduplicated = _deduplicate(merged)
    ordered = sorted(deduplicated, key=_record_priority)
    return _bounded_ledger(
        case=case,
        case_id=case_id,
        scope_entity_ids=scope_entity_ids,
        ordered=ordered,
        max_records=max_records,
        max_chars=max_chars,
    )


def _facts_from_evidence(
    evidence: dict[str, Any],
    primary_id: str,
    primary: dict[str, Any],
    *,
    force_related_context: bool = False,
) -> list[dict[str, Any]]:
    dimension_name = str(evidence.get("dimension") or "unknown")
    payload = evidence.get("payload") if isinstance(evidence.get("payload"), dict) else {}
    coverage = str(payload.get("coverage") or "")
    if coverage in {"absent", "empty", "error"}:
        return []

    evidence_ref = str(evidence.get("evidence_id") or "")
    refs = [evidence_ref] if evidence_ref else []
    directness, confidence = _evidence_semantics(evidence, dimension_name)
    if force_related_context:
        directness, confidence = "related_context", "weak"
    source_system = str(evidence.get("source_system") or "unknown")
    namespace = _optional_text(primary.get("namespace"))
    entity_name = _optional_text(primary.get("name"))
    entity_kind = str(primary.get("kind") or "Pod")
    common = {
        "entity_id": primary_id,
        "entity_kind": entity_kind,
        "namespace": namespace,
        "entity_name": entity_name,
        "dimension": _dimension(dimension_name),
        "source_system": source_system,
        "directness": directness,
        "confidence": confidence,
        "evidence_refs": refs,
    }
    facts: list[dict[str, Any]] = []

    if dimension_name == "k8s":
        for termination in payload.get("last_terminated") or []:
            if not isinstance(termination, dict):
                continue
            facts.append(
                _fact(
                    **common,
                    fact_type="state",
                    attribute="container.last_termination",
                    value={
                        "container": termination.get("container"),
                        "reason": termination.get("reason"),
                        "exit_code": termination.get("exitCode"),
                        "finished_at": termination.get("finishedAt"),
                    },
                    timestamp=_optional_text(termination.get("finishedAt")),
                    strength="critical",
                )
            )
        state = {
            key: payload.get(key)
            for key in (
                "phase",
                "node",
                "pod_ip",
                "deletion_timestamp",
                "finalizers",
                "container_statuses",
            )
            if payload.get(key) not in (None, [], {})
        }
        if state:
            facts.append(
                _fact(
                    **common,
                    fact_type="state",
                    attribute="pod.state",
                    value=state,
                    strength="strong",
                )
            )
        if payload.get("events_preview"):
            facts.append(
                _fact(
                    **common,
                    fact_type="event",
                    attribute="kubernetes.event",
                    value=str(payload.get("events_preview")),
                    timestamp=_optional_text(evidence.get("timestamp")),
                    strength="strong" if confidence == "high" else "supporting",
                )
            )
    elif dimension_name == "metrics":
        for highlight in (payload.get("highlights") or [])[:8]:
            if not isinstance(highlight, dict):
                continue
            metric = str(highlight.get("metric") or "measurement")
            facts.append(
                _fact(
                    **common,
                    fact_type="measurement",
                    attribute=f"metric.{metric}",
                    value=highlight,
                    start=_optional_text(highlight.get("start_time")),
                    end=_optional_text(highlight.get("end_time")),
                    strength="strong" if directness == "direct" else "context",
                )
            )
    elif dimension_name in {"logs", "caller_logs"}:
        samples = payload.get("samples") or []
        if not samples and payload.get("log_preview"):
            samples = [
                {"message": line}
                for line in str(payload.get("log_preview") or "").splitlines()
                if line.strip()
            ][-8:]
        for sample in samples[:8]:
            if not isinstance(sample, dict):
                continue
            message = str(sample.get("message") or "").strip()
            if not message:
                continue
            facts.append(
                _fact(
                    **common,
                    fact_type="log",
                    attribute="log.sample",
                    value={
                        "message": message,
                        "container": sample.get("container"),
                    },
                    timestamp=_optional_text(sample.get("timestamp") or evidence.get("timestamp")),
                    strength="strong" if directness == "direct" else "context",
                )
            )
    elif dimension_name == "tracing":
        for sample in (payload.get("samples") or [])[:8]:
            if not isinstance(sample, dict):
                continue
            facts.append(
                _fact(
                    **common,
                    fact_type="flow",
                    attribute="network.flow",
                    value=sample,
                    timestamp=_optional_text(sample.get("timestamp")),
                    strength="supporting" if directness == "direct" else "context",
                )
            )
        for chain in (payload.get("call_chains") or [])[:3]:
            if isinstance(chain, dict):
                facts.append(
                    _fact(
                        **common,
                        fact_type="relationship",
                        attribute="network.call_chain",
                        value=chain,
                        strength="supporting" if directness == "direct" else "context",
                    )
                )
    elif dimension_name == "trace":
        for span in (payload.get("spans") or [])[:6]:
            if not isinstance(span, dict):
                continue
            facts.append(
                _fact(
                    **common,
                    fact_type="span",
                    attribute="trace.span",
                    value=span,
                    start=_optional_text(span.get("start")),
                    end=_optional_text(span.get("end")),
                    strength="supporting" if directness == "direct" else "context",
                )
            )

    if not facts:
        summary = str(evidence.get("summary") or "").strip()
        if summary:
            facts.append(
                _fact(
                    **common,
                    fact_type="state" if dimension_name == "k8s" else "event",
                    attribute=f"{_dimension(dimension_name)}.observation",
                    value=summary,
                    timestamp=_optional_text(evidence.get("timestamp")),
                    strength="supporting" if directness == "direct" else "context",
                )
            )
    return facts


def _fact_from_topology(
    edge: dict[str, Any],
    entity_lookup: dict[str, dict[str, Any]],
    primary_id: str,
    primary: dict[str, Any],
    coverage: dict[str, Any],
    accepted_ref_authority: dict[str, str],
) -> dict[str, Any] | None:
    edge_refs = sorted(
        {
            str(ref)
            for ref in (edge.get("evidence_refs") or [])
            if str(ref or "").strip()
        }
    )
    current_refs = [
        ref
        for ref in edge_refs
        if accepted_ref_authority.get(ref) == "current"
    ]
    temporal_refs = [
        ref
        for ref in edge_refs
        if accepted_ref_authority.get(ref) == "temporal_context"
    ]
    if current_refs:
        accepted_refs = current_refs
        force_related_context = False
    elif temporal_refs:
        accepted_refs = temporal_refs
        force_related_context = True
    else:
        return None

    scoped_edge = scope_primary_topology_edge(edge, primary)
    if scoped_edge is None:
        return None
    source = str(scoped_edge.get("source") or "")
    target = str(scoped_edge.get("target") or "")
    relation = str(scoped_edge.get("relation") or "related_to")
    directness = _normalize_directness(edge.get("directness"))
    confidence = _normalize_confidence(edge.get("confidence"))
    if relation == "observes":
        observed_dimension = _observed_dimension(str(edge.get("source_system") or ""))
        observed_coverage = str(coverage.get(observed_dimension) or "")
        if observed_coverage in {"absent", "empty", "error"}:
            return None
        if observed_coverage == "weak":
            directness = "related_context"
            confidence = "weak"
    if force_related_context:
        directness = "related_context"
        confidence = "weak"
    if directness == "related_context" and confidence == "high":
        confidence = "medium"
    attached_id = primary_id
    source_entity = entity_lookup.get(str(edge.get("source") or ""), {})
    target_entity = entity_lookup.get(str(edge.get("target") or ""), {})
    return _fact(
        entity_id=attached_id,
        entity_kind="TopologyEdge",
        namespace=_optional_text(primary.get("namespace")),
        entity_name=relation,
        dimension="topology",
        fact_type="relationship",
        attribute=f"topology.{relation}",
        value={
            "source": source,
            "source_kind": source_entity.get("kind"),
            "relation": relation,
            "target": target,
            "target_kind": target_entity.get("kind"),
        },
        source_system=str(edge.get("source_system") or "topology"),
        directness=directness,
        confidence=confidence,
        strength="strong" if directness == "direct" and confidence == "high" else "context",
        evidence_refs=accepted_refs,
    )


def _fact(
    *,
    entity_id: str,
    entity_kind: str,
    dimension: str,
    fact_type: str,
    attribute: str,
    value: Any,
    source_system: str,
    directness: str,
    confidence: str,
    evidence_refs: list[Any],
    namespace: str | None = None,
    entity_name: str | None = None,
    strength: str | None = None,
    unit: str | None = None,
    timestamp: str | None = None,
    start: str | None = None,
    end: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_directness = _normalize_directness(directness)
    normalized_confidence = _normalize_confidence(confidence)
    if normalized_directness == "related_context" and normalized_confidence == "high":
        normalized_confidence = "medium"
    record: dict[str, Any] = {
        "entity_id": entity_id,
        "entity_kind": entity_kind,
        "dimension": dimension,
        "fact_type": fact_type,
        "attribute": attribute,
        "value": _sanitize(value),
        "source_system": source_system,
        "directness": normalized_directness,
        "confidence": normalized_confidence,
        "evidence_refs": sorted(
            {
                str(ref)
                for ref in evidence_refs
                if str(ref or "").strip()
            }
        ),
    }
    optional = {
        "namespace": namespace,
        "entity_name": entity_name,
        "strength": strength,
        "unit": unit,
        "timestamp": timestamp,
        "start": start,
        "end": end,
        "metadata": _sanitize(metadata) if metadata else None,
    }
    record.update({key: item for key, item in optional.items() if item not in (None, {}, [])})
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {"fact_id": f"fact-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}", **record}


def _sanitize(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item, depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not _is_forbidden_key(key)
        }
    if isinstance(value, list):
        return [_sanitize(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, depth + 1) for item in value]
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _canonicalize_raw_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonicalize_raw_payload(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_canonicalize_raw_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_canonicalize_raw_payload(item) for item in value]
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _evidence_conflict_fingerprint(
    evidence: dict[str, Any],
    authority: str | None,
    projected: list[dict[str, Any]],
) -> str:
    payload = evidence.get("payload") if isinstance(evidence.get("payload"), dict) else {}
    projected_content = []
    for fact in projected:
        projected_content.append(
            {
                key: value
                for key, value in fact.items()
                if key not in {"evidence_refs", "fact_id"}
            }
        )
    projected_content.sort(
        key=lambda fact: json.dumps(
            fact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )
    identity = {
        "authority": authority or "rejected",
        "source_system": str(evidence.get("source_system") or "unknown"),
        "dimension": str(evidence.get("dimension") or ""),
        "pod_uids": sorted(_pod_uids(payload)),
        "timestamp": _optional_text(evidence.get("timestamp")),
        "raw_payload": _canonicalize_raw_payload(payload),
        "projected": projected_content,
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _belongs_to_primary(evidence: dict[str, Any], primary: dict[str, Any]) -> bool:
    return _evidence_authority(evidence, primary) == "current"


def _evidence_authority(
    evidence: dict[str, Any],
    primary: dict[str, Any],
) -> str | None:
    primary_uid = str(primary.get("uid") or "")
    if not primary_uid:
        return "temporal_context"
    payload = evidence.get("payload") if isinstance(evidence.get("payload"), dict) else {}
    observed_uids = _pod_uids(payload)
    if str(evidence.get("dimension") or "") == "k8s" and payload.get("uid"):
        observed_uids.add(str(payload["uid"]))
    if observed_uids:
        return "current" if observed_uids == {primary_uid} else None
    return "temporal_context"


def _pod_uids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace(".", "_")
            if normalized in {"pod_uid", "kubernetes_pod_uid"} and item:
                found.add(str(item))
            elif normalized == "pod" and isinstance(item, dict) and item.get("uid"):
                found.add(str(item["uid"]))
                found.update(_pod_uids(item))
            elif normalized == "uid" and any(
                pod_key in value
                for pod_key in ("pod", "pod_ip", "phase", "container_statuses")
            ) and item:
                found.add(str(item))
            else:
                found.update(_pod_uids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_pod_uids(item))
    return found


def _primary_entity_id(primary: dict[str, Any], case_id: str) -> str:
    name = str(primary.get("name") or "")
    if name:
        return pod_entity_id(
            str(primary.get("namespace") or "cluster"),
            name,
            str(primary.get("uid") or "") or None,
        )
    return f"case:{case_id or 'unknown'}"


def _evidence_semantics(evidence: dict[str, Any], dimension: str) -> tuple[str, str]:
    directness = _normalize_directness(evidence.get("directness"))
    confidence = _normalize_confidence(evidence.get("confidence"))
    if dimension == "caller_logs":
        return "related_context", "weak"
    if directness == "related_context" and confidence == "high":
        confidence = "medium"
    return directness, confidence


def _normalize_directness(value: Any) -> str:
    normalized = str(value or "derived")
    return normalized if normalized in _DIRECTNESS else "derived"


def _normalize_confidence(value: Any) -> str:
    normalized = str(value or "medium")
    return normalized if normalized in _CONFIDENCE else "medium"


def _dimension(value: str) -> str:
    return _DIMENSION_NAMES.get(value, value or "unknown")


def _observed_dimension(source_system: str) -> str:
    normalized = source_system.lower()
    if "prometheus" in normalized:
        return "metrics"
    if "elastic" in normalized or normalized == "logs":
        return "logs"
    if "deepflow" in normalized:
        return "tracing"
    if "tempo" in normalized:
        return "trace"
    if "kubernetes" in normalized:
        return "k8s"
    return ""


def _coverage_confidence(state: str) -> str:
    if state in {"present", "empty"}:
        return "high"
    if state == "weak":
        return "medium"
    return "low"


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _deduplicate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("fact_type") != "coverage" and not record.get("evidence_refs"):
            continue
        by_id.setdefault(str(record["fact_id"]), record)
    return list(by_id.values())


def _merge_semantic_observations(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for record in records:
        if record.get("fact_type") == "coverage":
            passthrough.append(record)
            continue
        identity = {
            key: record.get(key)
            for key in (
                "entity_id",
                "entity_kind",
                "namespace",
                "entity_name",
                "dimension",
                "fact_type",
                "attribute",
                "value",
                "unit",
                "timestamp",
                "start",
                "end",
                "metadata",
            )
            if record.get(key) is not None
        }
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        grouped.setdefault(canonical, []).append(record)

    merged = list(passthrough)
    for group in grouped.values():
        strongest = min(
            group,
            key=lambda record: (
                _DIRECTNESS_PRIORITY.get(
                    str(record.get("directness") or ""),
                    9,
                ),
                _CONFIDENCE_PRIORITY.get(
                    str(record.get("confidence") or ""),
                    9,
                ),
                _STRENGTH_PRIORITY.get(
                    str(record.get("strength") or "context"),
                    9,
                ),
                str(record.get("source_system") or ""),
                str(record.get("fact_id") or ""),
            ),
        )
        evidence_refs = sorted(
            {
                str(ref)
                for record in group
                for ref in (record.get("evidence_refs") or [])
                if str(ref or "").strip()
            }
        )
        source_systems = sorted(
            {
                str(record.get("source_system") or "unknown")
                for record in group
            }
        )
        merged.append(
            _fact(
                entity_id=str(strongest.get("entity_id") or ""),
                entity_kind=str(strongest.get("entity_kind") or "Entity"),
                namespace=_optional_text(strongest.get("namespace")),
                entity_name=_optional_text(strongest.get("entity_name")),
                dimension=str(strongest.get("dimension") or "unknown"),
                fact_type=str(strongest.get("fact_type") or "event"),
                attribute=str(strongest.get("attribute") or "observation"),
                value=strongest.get("value"),
                unit=_optional_text(strongest.get("unit")),
                timestamp=_optional_text(strongest.get("timestamp")),
                start=_optional_text(strongest.get("start")),
                end=_optional_text(strongest.get("end")),
                metadata=(
                    strongest.get("metadata")
                    if isinstance(strongest.get("metadata"), dict)
                    else None
                ),
                source_system="+".join(source_systems),
                directness=str(strongest.get("directness") or "derived"),
                confidence=str(strongest.get("confidence") or "medium"),
                strength=_optional_text(strongest.get("strength")),
                evidence_refs=evidence_refs,
            )
        )
    return merged


def _record_priority(record: dict[str, Any]) -> tuple[Any, ...]:
    coverage_rank = 0 if record.get("fact_type") == "coverage" else 1
    return (
        coverage_rank,
        _STRENGTH_PRIORITY.get(str(record.get("strength") or "context"), 9),
        _DIRECTNESS_PRIORITY.get(str(record.get("directness") or ""), 9),
        _CONFIDENCE_PRIORITY.get(str(record.get("confidence") or ""), 9),
        str(record.get("dimension") or ""),
        str(record.get("attribute") or ""),
        str(record.get("entity_id") or ""),
        str(record.get("fact_id") or ""),
    )


def _bounded_ledger(
    *,
    case: dict[str, Any],
    case_id: str,
    scope_entity_ids: list[str],
    ordered: list[dict[str, Any]],
    max_records: int,
    max_chars: int,
) -> dict[str, Any]:
    coverage_records = [
        record
        for record in ordered
        if record.get("fact_type") == "coverage"
    ]
    non_coverage = [
        record
        for record in ordered
        if record.get("fact_type") != "coverage"
    ]
    limit = max(len(coverage_records), max(1, int(max_records)))
    selected = list(coverage_records)
    reservations = _dimension_reservations(
        case=case,
        case_id=case_id,
        scope_entity_ids=scope_entity_ids,
        coverage_records=coverage_records,
        non_coverage=non_coverage,
        limit=limit,
        max_chars=max_chars,
    )
    selected.extend(reservations)
    selected_ids = {
        str(record.get("fact_id") or "")
        for record in selected
    }

    for record in non_coverage:
        fact_id = str(record.get("fact_id") or "")
        if fact_id in selected_ids or len(selected) >= limit:
            continue
        trial = sorted(selected + [record], key=_record_priority)
        if _records_fit(
            case_id,
            scope_entity_ids,
            trial,
            max_chars,
        ):
            selected = trial
            selected_ids.add(fact_id)

    selected = sorted(selected, key=_record_priority)
    truncated = len(selected) < len(ordered)
    return _ledger(case_id, scope_entity_ids, selected, truncated)


def _dimension_reservations(
    *,
    case: dict[str, Any],
    case_id: str,
    scope_entity_ids: list[str],
    coverage_records: list[dict[str, Any]],
    non_coverage: list[dict[str, Any]],
    limit: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    present_dimensions = {
        _dimension(str(dimension))
        for dimension, state in (case.get("coverage") or {}).items()
        if str(state or "") == "present"
    }
    dimensions = [
        dimension
        for dimension in _RESERVED_DIMENSION_ORDER
        if dimension in present_dimensions
        and any(
            record.get("dimension") == dimension
            for record in non_coverage
        )
    ]
    if not dimensions or len(coverage_records) + len(dimensions) > limit:
        return []

    candidates = {
        dimension: [
            record
            for record in non_coverage
            if record.get("dimension") == dimension
        ]
        for dimension in dimensions
    }
    selected = {
        dimension: min(
            records,
            key=lambda record: (
                _record_size(record),
                _record_priority(record),
            ),
        )
        for dimension, records in candidates.items()
    }
    if not _records_fit(
        case_id,
        scope_entity_ids,
        coverage_records + list(selected.values()),
        max_chars,
    ):
        return _partial_dimension_reservations(
            case_id=case_id,
            scope_entity_ids=scope_entity_ids,
            coverage_records=coverage_records,
            dimensions=dimensions,
            candidates=candidates,
            limit=limit,
            max_chars=max_chars,
        )

    for dimension in dimensions:
        for candidate in candidates[dimension]:
            trial = dict(selected)
            trial[dimension] = candidate
            if _records_fit(
                case_id,
                scope_entity_ids,
                coverage_records + list(trial.values()),
                max_chars,
            ):
                selected = trial
                break
    return [selected[dimension] for dimension in dimensions]


def _partial_dimension_reservations(
    *,
    case_id: str,
    scope_entity_ids: list[str],
    coverage_records: list[dict[str, Any]],
    dimensions: list[str],
    candidates: dict[str, list[dict[str, Any]]],
    limit: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for dimension in dimensions:
        if len(coverage_records) + len(selected) >= limit:
            break
        for candidate in candidates[dimension]:
            trial = coverage_records + selected + [candidate]
            if _records_fit(
                case_id,
                scope_entity_ids,
                trial,
                max_chars,
            ):
                selected.append(candidate)
                break
    return selected


def _record_size(record: dict[str, Any]) -> int:
    return len(
        json.dumps(
            record,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


def _records_fit(
    case_id: str,
    scope_entity_ids: list[str],
    records: list[dict[str, Any]],
    max_chars: int,
) -> bool:
    ordered = sorted(records, key=_record_priority)
    return _wire_size(
        _ledger(
            case_id,
            scope_entity_ids,
            ordered,
            False,
        )
    ) <= max_chars


def _ledger(
    case_id: str,
    scope_entity_ids: list[str],
    records: list[dict[str, Any]],
    truncated: bool,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "case_id": case_id,
        "scope_entity_ids": scope_entity_ids,
        "records": records,
        "record_count": len(records),
        "truncated": truncated,
        "source": "mcp_canonical",
        "legacy_contract": False,
    }


def _is_forbidden_key(key: Any) -> bool:
    normalized = "".join(
        character
        for character in str(key).lower()
        if character.isalnum()
    )
    return (
        normalized in _FORBIDDEN_NORMALIZED_KEYS
        or any(
            normalized.startswith(family)
            for family in _FORBIDDEN_NORMALIZED_FAMILIES
        )
    )


def _fact_within_window(
    fact: dict[str, Any],
    time_window: dict[str, Any],
    *,
    require_temporal: bool,
) -> bool:
    is_interval = (
        str(fact.get("fact_type") or "") in _INTERVAL_FACT_TYPES
        or "start" in fact
        or "end" in fact
    )
    window_start = _parse_time(time_window.get("start"))
    window_end = _parse_time(time_window.get("end"))
    if window_start is None or window_end is None or time_window.get("empty") is True:
        return (
            not require_temporal
            and not is_interval
            and time_window.get("empty") is not True
        )

    timestamp = _parse_time(fact.get("timestamp"))
    fact_start = _parse_time(fact.get("start"))
    fact_end = _parse_time(fact.get("end"))
    if is_interval:
        if fact_start is None or fact_end is None or fact_start > fact_end:
            return False
        return fact_start >= window_start and fact_end <= window_end
    if timestamp is None:
        return not require_temporal
    return window_start <= timestamp <= window_end


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _wire_size(ledger: dict[str, Any]) -> int:
    return len(
        json.dumps(
            {"fact_ledger": ledger},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
