"""Build deterministic canonical facts from a bounded query projection."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


CONTRACT_VERSION = "aiops.fact-ledger.v1"
CANONICAL_SOURCE = "mcp_canonical"

_DIMENSIONS = {
    "kubernetes",
    "metrics",
    "logging",
    "tracing",
    "topology",
    "coverage",
}
_DIRECTNESS = {"direct", "derived", "related_context"}
_CONFIDENCE = {"high", "medium", "low", "weak"}
_STRENGTH = {"critical", "strong", "supporting", "context"}
_FORBIDDEN_KEY_PREFIXES = (
    "diagnosis",
    "rootcause",
    "remediation",
    "groundtruth",
    "evaluator",
)
_TOPOLOGY_SOURCE_SYSTEMS = {"kubernetes", "deepflow"}
_MAX_CANONICAL_TOPOLOGY_ENTITY_ID_CHARS = 192
_CANONICAL_TOPOLOGY_SCOPE_ALIAS = "k8s.pod:scope/current"
_METADATA_KEYS = (
    "labels",
    "stats",
    "sample_count",
    "trend_evaluable",
    "series_role",
    "level",
    "trace_id",
    "span_id",
    "raw_ref",
    "fields",
    "value_original_length",
    "value_sha256",
    "value_truncated",
)
_OPTIONAL_RECORD_FIELDS = {
    "namespace",
    "entity_name",
    "strength",
    "unit",
    "timestamp",
    "start",
    "end",
    "metadata",
}


def build_fact_ledger(result: dict[str, Any]) -> dict[str, Any] | None:
    """Project final retained query facts into ``aiops.fact-ledger.v1``."""
    if not isinstance(result, dict):
        return None
    status = str(result.get("status") or "")
    if status not in {"query_succeeded", "query_partial"}:
        return None
    if str(result.get("coverage") or "") in {"empty", "absent", "error"}:
        return None
    entity = result.get("entity")
    if isinstance(entity, dict) and entity.get("lifecycle_changed"):
        return None

    scope = _pod_scope(result)
    if scope is None:
        return None
    scope_id = scope["entity_id"]

    records: list[dict[str, Any]] = []
    facts = result.get("facts")
    fact_list = facts if isinstance(facts, list) else []
    topology_scope_anchor_ref = _topology_scope_anchor_ref(
        result,
        scope,
        fact_list,
    )
    for fact in fact_list:
        record = _fact_record(
            fact,
            result,
            scope,
            topology_scope_anchor_ref=topology_scope_anchor_ref,
        )
        if record is not None:
            records.append(record)

    if status == "query_partial":
        if not records:
            return None
        records.append(_coverage_record(result, scope, records))

    records.sort(key=_record_sort_key)
    refs = sorted(
        {
            ref
            for record in records
            if record.get("dimension") != "coverage"
            for ref in record.get("evidence_refs") or []
        }
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "case_id": canonical_query_case_id(
            scope_entity_ids=[scope_id],
            dimension=str(result.get("dimension") or ""),
            source_system=str(result.get("source_system") or ""),
            evidence_refs=refs,
        ),
        "scope_entity_ids": [scope_id],
        "records": records,
        "record_count": len(records),
        "truncated": bool(result.get("truncated")),
        "source": CANONICAL_SOURCE,
        "legacy_contract": False,
    }


def canonical_fact_id(record_without_id: dict[str, Any]) -> str:
    """Return the canonical SHA-256 prefix for the complete normalized record."""
    record = dict(record_without_id) if isinstance(record_without_id, dict) else {}
    record.pop("fact_id", None)
    encoded = _canonical_json(_normalize_record(record)).encode("utf-8")
    return "fact-" + hashlib.sha256(encoded).hexdigest()[:12]


def canonical_query_case_id(
    result: dict[str, Any] | None = None,
    *,
    scope_entity_ids: list[str] | None = None,
    dimension: str | None = None,
    source_system: str | None = None,
    evidence_refs: list[str] | None = None,
) -> str:
    """Return a stable query case ID from scope, dimension, source and refs."""
    if isinstance(result, dict):
        scope = _pod_scope(result)
        scope_entity_ids = [scope["entity_id"]] if scope else []
        dimension = str(result.get("dimension") or "")
        source_system = str(result.get("source_system") or "")
        facts = result.get("facts")
        evidence_refs = [
            str(fact.get("ref") or "").strip()
            for fact in facts if isinstance(facts, list)
            if isinstance(fact, dict)
            and str(fact.get("ref") or "").strip()
            and str(fact.get("source_system") or "").strip()
        ]
    value = {
        "scope_entity_ids": sorted(set(scope_entity_ids or [])),
        "dimension": str(dimension or "").strip(),
        "source_system": str(source_system or "").strip(),
        "evidence_refs": sorted(set(evidence_refs or [])),
    }
    return "query-" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()[:12]


def _fact_record(
    fact: Any,
    result: dict[str, Any],
    scope: dict[str, str],
    *,
    topology_scope_anchor_ref: str = "",
) -> dict[str, Any] | None:
    if not isinstance(fact, dict):
        return None
    source_system = str(fact.get("source_system") or "").strip()
    evidence_ref = str(fact.get("ref") or "").strip()
    if not source_system or not evidence_ref:
        return None
    fact_pod_uid = str(fact.get("pod_uid") or "").strip()
    if fact_pod_uid and fact_pod_uid != scope["pod_uid"]:
        return None

    dimension = str(fact.get("dimension") or result.get("dimension") or "")
    if dimension not in _DIMENSIONS or dimension == "coverage":
        return None
    if dimension == "topology" and source_system not in _TOPOLOGY_SOURCE_SYSTEMS:
        return None
    attribute = str(fact.get("attribute") or fact.get("name") or "").strip()
    if not attribute:
        attribute = "kubernetes.relationship" if dimension == "topology" else "observation"

    directness = str(fact.get("directness") or result.get("directness") or "")
    if directness not in _DIRECTNESS:
        directness = "related_context"
    confidence = str(fact.get("confidence") or "")
    if confidence not in _CONFIDENCE:
        confidence = {
            "direct": "high",
            "derived": "medium",
            "related_context": "low",
        }[directness]
    if directness == "related_context" and confidence == "high":
        confidence = "low"
    strength_value = fact.get("strength")
    strength = str(strength_value) if strength_value in _STRENGTH else None

    entity_id = scope["entity_id"]
    entity_kind = "Pod"
    value = _sanitize(fact.get("value"))
    if dimension == "topology":
        relationship = _topology_relationship(
            fact,
            result,
            scope,
            canonicalize_scope=(evidence_ref == topology_scope_anchor_ref),
        )
        if relationship is None:
            return None
        value, entity_id, entity_kind = relationship

    record: dict[str, Any] = {
        "entity_id": entity_id,
        "entity_kind": entity_kind,
        "dimension": dimension,
        "fact_type": _fact_type(dimension, source_system, attribute),
        "attribute": attribute,
        "value": value,
        "source_system": source_system,
        "directness": directness,
        "confidence": confidence,
        "strength": strength,
        "evidence_refs": [evidence_ref],
    }
    if dimension != "topology":
        record["namespace"] = scope["namespace"]
        record["entity_name"] = scope["pod"]
    for target, source in (
        ("unit", "unit"),
        ("timestamp", "timestamp" if fact.get("timestamp") is not None else "observed_at"),
        ("start", "start"),
        ("end", "end"),
    ):
        if fact.get(source) is not None:
            record[target] = _sanitize(fact.get(source))

    metadata = {
        key: _sanitize(fact.get(key))
        for key in _METADATA_KEYS
        if fact.get(key) not in (None, {}, [])
    }
    metadata = _sanitize(metadata)
    if metadata:
        record["metadata"] = metadata

    normalized = _normalize_record(record)
    return {"fact_id": canonical_fact_id(normalized), **normalized}


def _coverage_record(
    result: dict[str, Any],
    scope: dict[str, str],
    retained_records: list[dict[str, Any]],
) -> dict[str, Any]:
    refs = sorted(
        {
            ref
            for record in retained_records
            for ref in record.get("evidence_refs") or []
        }
    )
    source_system = str(result.get("source_system") or "").strip() or "coverage"
    record = {
        "entity_id": scope["entity_id"],
        "entity_kind": "Pod",
        "namespace": scope["namespace"],
        "entity_name": scope["pod"],
        "dimension": "coverage",
        "fact_type": "coverage",
        "attribute": "query.coverage",
        "value": str(result.get("coverage") or "partial"),
        "source_system": source_system,
        "directness": "derived",
        "confidence": "weak",
        "strength": "context",
        "evidence_refs": refs,
        "metadata": {"status": "query_partial"},
    }
    normalized = _normalize_record(record)
    return {"fact_id": canonical_fact_id(normalized), **normalized}


def _topology_relationship(
    fact: dict[str, Any],
    result: dict[str, Any],
    scope: dict[str, str],
    *,
    canonicalize_scope: bool,
) -> tuple[dict[str, str], str, str] | None:
    value = fact.get("value")
    if not isinstance(value, dict):
        return None
    relation = str(value.get("relation") or fact.get("relation") or "").strip()
    if not relation:
        return None

    entities = {
        str(entity.get("entity_id")): entity
        for entity in result.get("entities") or []
        if isinstance(entity, dict) and entity.get("entity_id")
    }
    source = _topology_endpoint(
        value.get("source"),
        entities,
        scope,
        canonicalize_scope=canonicalize_scope,
    )
    target = _topology_endpoint(
        value.get("target"),
        entities,
        scope,
        canonicalize_scope=canonicalize_scope,
    )
    if source is None or target is None:
        return None
    source_id, source_kind = source
    target_id, target_kind = target
    if canonicalize_scope and scope["entity_id"] == source_id:
        entity_id, entity_kind = target_id, target_kind
    elif canonicalize_scope and scope["entity_id"] == target_id:
        entity_id, entity_kind = source_id, source_kind
    elif scope["entity_id"] == source_id:
        entity_id, entity_kind = source_id, source_kind
    elif scope["entity_id"] == target_id:
        entity_id, entity_kind = target_id, target_kind
    else:
        entity_id, entity_kind = source_id, source_kind
    return (
        {"relation": relation, "source": source_id, "target": target_id},
        entity_id,
        entity_kind,
    )


def _topology_endpoint(
    endpoint: Any,
    entities: dict[str, dict[str, Any]],
    scope: dict[str, str],
    *,
    canonicalize_scope: bool,
) -> tuple[str, str] | None:
    alias = str(endpoint).strip() if isinstance(endpoint, str) else ""
    entity = entities.get(alias) if alias else endpoint
    if not isinstance(entity, dict):
        return None
    source_system = str(entity.get("source_system") or "").strip()
    if source_system and source_system not in _TOPOLOGY_SOURCE_SYSTEMS:
        return None
    kind = str(entity.get("kind") or "").strip()
    name = str(entity.get("name") or "").strip()
    if not kind or not name:
        return None
    namespace = str(entity.get("namespace") or "").strip()
    uid = str(entity.get("uid") or "").strip()
    if _is_exact_scope_pod(kind, namespace, name, uid, scope):
        if len(scope["entity_id"]) <= _MAX_CANONICAL_TOPOLOGY_ENTITY_ID_CHARS:
            return scope["entity_id"], kind
        return _CANONICAL_TOPOLOGY_SCOPE_ALIAS, kind
    location = namespace or "cluster"
    entity_id = f"k8s.{kind.lower()}:{location}/{name}"
    if uid:
        entity_id += f":{uid}"
    if alias and len(entity_id) > _MAX_CANONICAL_TOPOLOGY_ENTITY_ID_CHARS:
        entity_id = f"k8s.{kind.lower()}:alias/{alias}"
    return entity_id, kind


def _is_exact_scope_pod(
    kind: str,
    namespace: str,
    name: str,
    uid: str,
    scope: dict[str, str],
) -> bool:
    return (
        kind.lower() == "pod"
        and namespace == scope["namespace"]
        and name == scope["pod"]
        and uid == scope["pod_uid"]
    )


def _topology_scope_anchor_ref(
    result: dict[str, Any],
    scope: dict[str, str],
    facts: list[Any],
) -> str:
    entity = result.get("entity")
    entity = entity if isinstance(entity, dict) else {}
    primary_alias = str(entity.get("entity_id") or "").strip()
    if not primary_alias:
        return ""

    candidates: list[tuple[int, str, str]] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        if _fact_record(
            fact,
            result,
            scope,
            topology_scope_anchor_ref="",
        ) is None:
            continue
        value = fact.get("value")
        if not isinstance(value, dict):
            continue
        evidence_ref = str(fact.get("ref") or "").strip()
        if not evidence_ref:
            continue
        relation = str(value.get("relation") or fact.get("relation") or "")
        if value.get("target") == primary_alias:
            candidates.append((0, relation, evidence_ref))
        elif value.get("source") == primary_alias:
            candidates.append((1, relation, evidence_ref))
    return min(candidates)[2] if candidates else ""


def _pod_scope(result: dict[str, Any]) -> dict[str, str] | None:
    entity = result.get("entity")
    entity = entity if isinstance(entity, dict) else {}
    namespace = str(entity.get("namespace") or "").strip()
    pod = str(entity.get("pod") or entity.get("name") or "").strip()
    pod_uid = str(entity.get("pod_uid") or entity.get("uid") or "").strip()

    alias = str(entity.get("entity_id") or "").strip()
    if alias and (not namespace or not pod or not pod_uid):
        for candidate in result.get("entities") or []:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("entity_id") or "") != alias:
                continue
            if str(candidate.get("kind") or "").lower() != "pod":
                continue
            namespace = str(candidate.get("namespace") or "").strip()
            pod = str(candidate.get("name") or "").strip()
            pod_uid = str(candidate.get("uid") or "").strip()
            break
    if not namespace or not pod or not pod_uid:
        return None
    return {
        "namespace": namespace,
        "pod": pod,
        "pod_uid": pod_uid,
        "entity_id": f"k8s.pod:{namespace}/{pod}:{pod_uid}",
    }


def _fact_type(dimension: str, source_system: str, attribute: str) -> str:
    if dimension == "metrics":
        return "measurement"
    if dimension == "logging":
        return "log"
    if dimension == "tracing":
        return "span" if source_system == "tempo" or "span" in attribute else "flow"
    if dimension == "topology":
        return "relationship"
    if dimension == "kubernetes":
        return "state"
    return "event"


def _record_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        {"direct": 0, "derived": 1, "related_context": 2}.get(
            str(record.get("directness")), 3
        ),
        {"high": 0, "medium": 1, "low": 2, "weak": 3}.get(
            str(record.get("confidence")), 4
        ),
        {"critical": 0, "strong": 1, "supporting": 2, "context": 3}.get(
            record.get("strength"), 4
        ),
        str(record.get("dimension") or ""),
        str(record.get("attribute") or ""),
        str(record.get("timestamp") or record.get("start") or ""),
        str(record.get("fact_id") or ""),
    )


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = _sanitize(record)
    if not isinstance(normalized, dict):
        return {}
    normalized = {
        key: value
        for key, value in normalized.items()
        if key not in _OPTIONAL_RECORD_FIELDS
        or value not in (None, {}, [])
    }
    normalized["source_system"] = str(normalized.get("source_system") or "").strip()
    refs = normalized.get("evidence_refs")
    normalized["evidence_refs"] = sorted(
        {
            str(ref).strip()
            for ref in refs if isinstance(refs, list)
            if str(ref).strip()
        }
    )
    return normalized


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not _forbidden_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


def _forbidden_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return normalized.startswith(_FORBIDDEN_KEY_PREFIXES)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
