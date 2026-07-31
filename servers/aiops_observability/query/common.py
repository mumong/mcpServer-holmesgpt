"""Shared contracts for model-directed observability queries."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import schemas
from ..collectors.kubernetes import get_pod
from ..config import get_config
from ..schemas import parse_time_window


MAX_RESULT_BYTES = 6144
MAX_INPUT_BYTES = 4096
MAX_QUERY_METADATA_BYTES = 4096
MAX_WINDOW_SECONDS = 2 * 60 * 60
_TOPOLOGY_SOURCE_SYSTEMS = {"kubernetes", "deepflow"}


@dataclass(frozen=True)
class NormalizedTimeWindow:
    requested: dict[str, str]
    effective: dict[str, str]
    adjustment: dict[str, str] | None = None


def error_result(
    *,
    source_system: str,
    dimension: str,
    namespace: str,
    pod: str,
    purpose: str,
    code: str,
    message: str,
    entity: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "query_rejected",
        "source_system": source_system,
        "dimension": dimension,
        "entity": entity
        or {
            "kind": "Pod",
            "namespace": namespace,
            "pod": pod,
        },
        "purpose": purpose,
        "coverage": "error",
        "directness": "direct",
        "query": query or {},
        "facts": [],
        "samples": [],
        "evidence_refs": [],
        "truncated": False,
        "limits": {"max_serialized_bytes": MAX_RESULT_BYTES},
        "error": {
            "code": _bounded_text(code, 64),
            "message": _bounded_text(message, 1000),
        },
    }


def resolve_pod_scope(
    namespace: str,
    pod: str,
    pod_uid: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    namespace = str(namespace or "").strip()
    pod = str(pod or "").strip()
    if not namespace or not pod:
        return None, {
            "code": "invalid_entity",
            "message": "namespace and pod are required",
        }

    pod_json, error = get_pod(
        namespace,
        pod,
        timeout=get_config().timeout_seconds,
    )
    if error or not pod_json:
        return None, {
            "code": "pod_not_found",
            "message": str(error or f"Pod {namespace}/{pod} was not found"),
        }

    metadata = pod_json.get("metadata") or {}
    spec = pod_json.get("spec") or {}
    status = pod_json.get("status") or {}
    observed_uid = str(metadata.get("uid") or "").strip()
    requested_uid = str(pod_uid or "").strip()
    if requested_uid and observed_uid and requested_uid != observed_uid:
        return None, {
            "code": "pod_uid_conflict",
            "message": (
                f"current Pod UID {observed_uid} does not match supplied UID "
                f"{requested_uid}"
            ),
        }

    containers = [
        str(item.get("name"))
        for item in spec.get("containers") or []
        if item.get("name")
    ]
    return {
        "kind": "Pod",
        "namespace": str(metadata.get("namespace") or namespace),
        "pod": str(metadata.get("name") or pod),
        "pod_uid": observed_uid or requested_uid or None,
        "creation_timestamp": metadata.get("creationTimestamp"),
        "lifecycle_changed": False,
        "pod_ip": status.get("podIP"),
        "node": spec.get("nodeName"),
        "containers": containers,
    }, None


def bounded_window(
    arguments: dict[str, Any],
    *,
    not_before: str | None = None,
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    normalized, error = normalize_time_window(arguments)
    if error or normalized is None:
        return None, error

    try:
        start = _parse_datetime(normalized.effective["start"])
        end = _parse_datetime(normalized.effective["end"])
        if not_before:
            lifecycle_start = _parse_datetime(not_before)
            if start < lifecycle_start:
                start = lifecycle_start
    except (KeyError, TypeError, ValueError) as exc:
        return None, {"code": "invalid_time_range", "message": str(exc)}

    duration = (end - start).total_seconds()
    if duration <= 0:
        return None, {
            "code": "invalid_time_range",
            "message": "end must be later than start",
        }
    if duration > MAX_WINDOW_SECONDS:
        return None, {
            "code": "range_too_large",
            "message": "query window must not exceed two hours",
        }
    return {
        "start": _format_datetime(start),
        "end": _format_datetime(end),
    }, None


def normalize_time_window(
    arguments: dict[str, Any],
) -> tuple[NormalizedTimeWindow | None, dict[str, str] | None]:
    start_text = str(arguments.get("start") or "").strip()
    end_text = str(arguments.get("end") or "").strip()
    requested = {
        key: value
        for key, value in (("start", start_text), ("end", end_text))
        if value
    }
    try:
        raw_window_minutes = arguments.get("window_minutes")
        window_minutes = max(
            1,
            int(30 if raw_window_minutes is None else raw_window_minutes),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return None, {"code": "invalid_time_range", "message": str(exc)}

    lookback_seconds = (
        _parse_lookback_seconds(start_text)
        if start_text and not end_text
        else None
    )
    if lookback_seconds is not None:
        if lookback_seconds <= 0:
            return None, {
                "code": "invalid_time_range",
                "message": "lookback must be greater than zero",
            }
        if lookback_seconds > MAX_WINDOW_SECONDS:
            return None, {
                "code": "range_too_large",
                "message": "query window must not exceed two hours",
            }
        end = schemas.utc_now().astimezone(timezone.utc)
        start = end - timedelta(seconds=lookback_seconds)
        if start >= end:
            return None, {
                "code": "invalid_time_range",
                "message": "lookback is too small to form a time window",
            }
        effective = {
            "start": _format_datetime(start),
            "end": _format_datetime(end),
        }
        return NormalizedTimeWindow(
            requested=requested,
            effective=effective,
            adjustment={
                "reason": "relative_lookback",
                "action": "converted_duration_to_bounded_window",
            },
        ), None

    try:
        for value in (start_text, end_text):
            if value:
                _parse_aware_datetime(value)
        window = parse_time_window(
            start=start_text or None,
            end=end_text or None,
            window_minutes=window_minutes,
        )
        start = _parse_datetime(window.start)
        end = _parse_datetime(window.end)
    except (TypeError, ValueError, OverflowError) as exc:
        return None, {"code": "invalid_time_range", "message": str(exc)}

    effective = {
        "start": _format_datetime(start),
        "end": _format_datetime(end),
    }
    duration = (end - start).total_seconds()
    if abs(duration) > MAX_WINDOW_SECONDS:
        return None, {
            "code": "range_too_large",
            "message": "query window must not exceed two hours",
        }

    if duration <= 0:
        fallback, fallback_error = _current_bounded_window(window_minutes)
        if fallback_error or fallback is None:
            return None, fallback_error
        return NormalizedTimeWindow(
            requested=requested,
            effective=fallback,
            adjustment={
                "reason": "reversed_time_range",
                "action": "replaced_with_current_bounded_window",
            },
        ), None

    now = schemas.utc_now().astimezone(timezone.utc)
    if start >= now:
        fallback, fallback_error = _current_bounded_window(window_minutes)
        if fallback_error or fallback is None:
            return None, fallback_error
        return NormalizedTimeWindow(
            requested=requested,
            effective=fallback,
            adjustment={
                "reason": "future_time_range",
                "action": "replaced_with_current_bounded_window",
            },
        ), None

    return NormalizedTimeWindow(
        requested=requested,
        effective=effective,
    ), None


def _current_bounded_window(
    window_minutes: int,
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    try:
        timedelta(minutes=window_minutes)
        minutes = min(120, max(1, window_minutes))
        end = schemas.utc_now().astimezone(timezone.utc)
        start = end - timedelta(minutes=minutes)
    except (TypeError, ValueError, OverflowError) as exc:
        return None, {"code": "invalid_time_range", "message": str(exc)}
    return {
        "start": _format_datetime(start),
        "end": _format_datetime(end),
    }, None


def _parse_aware_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            "explicit timestamps must include Z or a UTC offset"
        )
    return parsed.astimezone(timezone.utc)


def _parse_lookback_seconds(value: str) -> float | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w)", value.lower())
    if not match:
        return None
    units = {
        "ms": 0.001,
        "s": 1.0,
        "m": 60.0,
        "h": 3600.0,
        "d": 86400.0,
        "w": 604800.0,
    }
    return float(match.group(1)) * units[match.group(2)]


def evidence_ref(
    dimension: str,
    namespace: str,
    pod: str,
    payload: Any,
) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    clean_dimension = str(dimension or "evidence").replace("/", "-")
    return f"{clean_dimension}-{namespace}-{pod}-{digest}".lower()


def serialized_bytes(value: Any) -> int:
    return len(_dump(value).encode("utf-8"))


def query_descriptor(
    query: Any,
    *,
    executed: bool,
) -> dict[str, Any]:
    raw = _dump(query)
    return {
        "preserved": False,
        "executed": executed,
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
        "serialized_bytes": len(raw.encode("utf-8")),
    }


def input_exceeds_budget(arguments: dict[str, Any]) -> bool:
    return serialized_bytes(arguments) > MAX_INPUT_BYTES


def query_metadata_exceeds_budget(query: Any) -> bool:
    return serialized_bytes(query) > MAX_QUERY_METADATA_BYTES


def dump_bounded_result(payload: dict[str, Any]) -> str:
    result = copy.deepcopy(payload)
    result.setdefault("truncated", False)
    limits = result.setdefault("limits", {})
    limits["max_serialized_bytes"] = MAX_RESULT_BYTES
    query = copy.deepcopy(result.get("query"))
    _sync_result_projection(result)

    raw = _dump(result)
    while len(raw.encode("utf-8")) > MAX_RESULT_BYTES:
        candidate = copy.deepcopy(result)
        if not _shrink_result_once(candidate):
            break
        candidate["truncated"] = True
        _sync_result_projection(candidate)
        candidate["query"] = copy.deepcopy(query)
        candidate_raw = _dump(candidate)
        if len(candidate_raw.encode("utf-8")) >= len(raw.encode("utf-8")):
            break
        result = candidate
        raw = candidate_raw

    if len(raw.encode("utf-8")) > MAX_RESULT_BYTES:
        result = {
            "ok": False,
            "status": "query_rejected",
            "source_system": _bounded_text(result.get("source_system"), 64),
            "dimension": _bounded_text(result.get("dimension"), 32),
            "entity": _bounded_entity(result.get("entity")),
            "purpose": _bounded_text(result.get("purpose"), 300),
            "coverage": "error",
            "directness": (
                "related_context"
                if result.get("directness") == "related_context"
                else "direct"
            ),
            "query": query_descriptor(
                query,
                executed=result.get("status") == "query_succeeded",
            ),
            "facts": [],
            "samples": [],
            "evidence_refs": [],
            "truncated": True,
            "limits": {"max_serialized_bytes": MAX_RESULT_BYTES},
            "error": {
                "code": "result_budget_exceeded",
                "message": "required query metadata exceeds the 6KB result budget",
            },
        }
        raw = _dump(result)
    return raw


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _drop_list_tail(
    payload: dict[str, Any],
    key: str,
    *,
    minimum: int,
) -> bool:
    value = payload.get(key)
    if isinstance(value, list) and len(value) > minimum:
        value.pop()
        return True
    return False


def _shrink_result_once(payload: dict[str, Any]) -> bool:
    is_topology = payload.get("dimension") == "topology"
    if is_topology and _alias_topology_projection(payload):
        return True
    if is_topology and _compact_topology_fact(payload):
        return True
    if is_topology and _compact_topology_entity(payload):
        return True
    if is_topology and _drop_duplicate_topology_fact(payload):
        return True
    if is_topology and _drop_low_priority_topology_fact(payload):
        return True
    if _drop_list_tail(payload, "correlations", minimum=1):
        return True
    if _drop_list_tail(payload, "flows", minimum=0):
        return True
    if _drop_list_tail(payload, "spans", minimum=0):
        return True
    if _drop_list_tail(payload, "series", minimum=0):
        return True
    if _drop_nested_sample_value(payload):
        return True
    if _drop_list_tail(payload, "samples", minimum=1):
        return True
    if not is_topology and _drop_unprotected_fact(payload, minimum=1):
        return True
    if _shrink_long_evidence_strings(payload):
        return True
    return _drop_list_tail(payload, "correlations", minimum=0)


def _alias_topology_projection(payload: dict[str, Any]) -> bool:
    facts = payload.get("facts")
    if not isinstance(facts, list):
        return False
    removed_invalid_fact = _filter_topology_facts_by_provenance(payload)
    facts = payload["facts"]
    has_expanded_endpoint = any(
        isinstance(value.get(endpoint), dict)
        for fact in facts
        if isinstance(fact, dict)
        for value in [fact.get("value")]
        if isinstance(value, dict)
        for endpoint in ("source", "target")
    )
    if not has_expanded_endpoint:
        return removed_invalid_fact

    source_system = str(payload.get("source_system") or "kubernetes")
    entity_aliases: dict[str, bytes] = {}
    evidence_aliases: dict[str, bytes] = {}
    entities_by_id: dict[str, dict[str, Any]] = {}
    primary_entity_id = ""
    primary = payload.get("entity") if isinstance(payload.get("entity"), dict) else {}
    primary_uid = str(primary.get("pod_uid") or primary.get("uid") or "")

    for fact in facts:
        if not isinstance(fact, dict):
            continue
        value = fact.get("value")
        if not isinstance(value, dict):
            continue
        for endpoint in ("source", "target"):
            entity = value.get(endpoint)
            if not isinstance(entity, dict):
                continue
            entity_id = _stable_topology_alias(
                "e:",
                entity,
                entity_aliases,
            )
            compact = {
                "entity_id": entity_id,
                "kind": entity.get("kind"),
                "name": entity.get("name"),
                "source_system": source_system,
            }
            for key in ("namespace", "uid", "attributes"):
                if entity.get(key) not in (None, {}, []):
                    compact[key] = copy.deepcopy(entity.get(key))
            entities_by_id.setdefault(entity_id, compact)
            if (
                str(entity.get("kind") or "") == str(primary.get("kind") or "Pod")
                and str(entity.get("namespace") or "")
                == str(primary.get("namespace") or "")
                and str(entity.get("name") or "")
                == str(primary.get("pod") or primary.get("name") or "")
                and primary_uid
                and str(entity.get("uid") or "") == primary_uid
            ):
                primary_entity_id = entity_id
            value[endpoint] = entity_id

        ref = fact.get("ref")
        if isinstance(ref, str) and ref and not ref.startswith("r:"):
            fact["ref"] = _stable_topology_alias(
                "r:",
                ref,
                evidence_aliases,
            )

    payload["entities"] = list(entities_by_id.values())
    if primary_entity_id:
        compact_primary = {
            "kind": primary.get("kind") or "Pod",
            "entity_id": primary_entity_id,
        }
        for key in ("creation_timestamp", "lifecycle_changed", "pod_ip"):
            if primary.get(key) is not None:
                compact_primary[key] = primary.get(key)
        payload["entity"] = compact_primary
    return True


def _filter_topology_facts_by_provenance(payload: dict[str, Any]) -> bool:
    facts = payload.get("facts")
    if not isinstance(facts, list):
        return False
    retained = [
        fact for fact in facts if _topology_fact_has_allowed_provenance(fact)
    ]
    if len(retained) == len(facts):
        return False
    payload["facts"] = retained
    return True


def _topology_fact_has_allowed_provenance(fact: Any) -> bool:
    if not isinstance(fact, dict):
        return False
    fact_source = str(fact.get("source_system") or "").strip()
    if fact_source not in _TOPOLOGY_SOURCE_SYSTEMS:
        return False
    value = fact.get("value")
    if not isinstance(value, dict):
        return True
    for key in ("source", "target"):
        endpoint = value.get(key)
        if not isinstance(endpoint, dict):
            continue
        source_system = str(endpoint.get("source_system") or "").strip()
        if (
            source_system
            and source_system not in _TOPOLOGY_SOURCE_SYSTEMS
        ):
            return False
    return True


def _stable_topology_alias(
    prefix: str,
    value: Any,
    used: dict[str, bytes],
) -> str:
    encoded = (
        value.encode("utf-8")
        if isinstance(value, str)
        else json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )
    digest = hashlib.sha256(encoded).digest()
    for byte_count in (6, 9, 12, 16, 20, 24, 28, 32):
        token = base64.urlsafe_b64encode(
            digest[:byte_count]
        ).decode("ascii").rstrip("=")
        alias = f"{prefix}{token}"
        previous = used.get(alias)
        if previous is None or previous == encoded:
            used[alias] = encoded
            return alias
    raise ValueError("unable to allocate a unique topology alias")


def _compact_topology_fact(payload: dict[str, Any]) -> bool:
    facts = payload.get("facts")
    if not isinstance(facts, list):
        return False
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        value = fact.get("value")
        if not isinstance(value, dict):
            continue
        for entity_key in ("source", "target"):
            entity = value.get(entity_key)
            if not isinstance(entity, dict):
                continue
            for key in ("attributes", "uid"):
                if key in entity:
                    entity.pop(key)
                    return True
        for key in (
            "ports",
            "matched_pod_labels",
            "cluster_ip",
            "selector",
            "source_field",
            "relationship",
            "relation",
        ):
            if key in value:
                value.pop(key)
                return True
        for key in ("entity", "name", "observed_at", "confidence"):
            if key in fact:
                fact.pop(key)
                return True
    return False


def _compact_topology_entity(payload: dict[str, Any]) -> bool:
    entities = payload.get("entities")
    if not isinstance(entities, list):
        return False
    primary = payload.get("entity")
    primary = primary if isinstance(primary, dict) else {}
    primary_id = str(primary.get("entity_id") or "")
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_id = str(entity.get("entity_id") or "")
        for key in ("attributes", "source_system"):
            if key in entity:
                entity.pop(key)
                return True
        if entity_id != primary_id:
            for key in ("uid", "namespace"):
                if key in entity:
                    entity.pop(key)
                    return True
    return False


def _drop_duplicate_topology_fact(payload: dict[str, Any]) -> bool:
    facts = payload.get("facts")
    if not isinstance(facts, list):
        return False
    counts = Counter(
        str(fact.get("relation") or "")
        for fact in facts
        if isinstance(fact, dict)
    )
    for index in range(len(facts) - 1, -1, -1):
        fact = facts[index]
        if not isinstance(fact, dict):
            continue
        relation = str(fact.get("relation") or "")
        if relation and counts[relation] > 1:
            facts.pop(index)
            return True
    return False


def _drop_low_priority_topology_fact(payload: dict[str, Any]) -> bool:
    facts = payload.get("facts")
    if not isinstance(facts, list) or len(facts) <= 1:
        return False
    priorities = {
        "owns_container": 3,
        "scheduled_on": 2,
        "owned_by": 1,
        "selects": 0,
    }
    candidates = sorted(
        range(len(facts)),
        key=lambda index: (
            priorities.get(
                str(facts[index].get("relation") or "")
                if isinstance(facts[index], dict)
                else "",
                4,
            ),
            index,
        ),
        reverse=True,
    )
    for index in candidates:
        retained = facts[:index] + facts[index + 1 :]
        if any(_is_direct_primary_topology_fact(payload, fact) for fact in retained):
            facts.pop(index)
            return True
    return False


def _is_direct_primary_topology_fact(
    payload: dict[str, Any],
    fact: Any,
) -> bool:
    if not isinstance(fact, dict) or fact.get("directness") != "direct":
        return False
    value = fact.get("value")
    if not isinstance(value, dict):
        return False
    endpoints = (value.get("source"), value.get("target"))
    primary = payload.get("entity")
    primary = primary if isinstance(primary, dict) else {}
    primary_alias = str(primary.get("entity_id") or "")
    if primary_alias and primary_alias in endpoints:
        return True
    namespace = str(primary.get("namespace") or "")
    pod = str(primary.get("pod") or primary.get("name") or "")
    pod_uid = str(primary.get("pod_uid") or primary.get("uid") or "")
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        if str(endpoint.get("kind") or "").lower() != "pod":
            continue
        if str(endpoint.get("namespace") or "") != namespace:
            continue
        if str(endpoint.get("name") or "") != pod:
            continue
        endpoint_uid = str(endpoint.get("uid") or "")
        if (
            (pod_uid and endpoint_uid == pod_uid)
            or (not pod_uid and not endpoint_uid)
        ):
            return True
    return False


def _drop_nested_sample_value(payload: dict[str, Any]) -> bool:
    for key in ("samples", "series"):
        series = payload.get(key)
        if not isinstance(series, list):
            continue
        for item in reversed(series):
            if not isinstance(item, dict):
                continue
            values = item.get("values")
            if isinstance(values, list) and len(values) > 3:
                values.pop(-2)
                return True
    return False


def _drop_unprotected_fact(
    payload: dict[str, Any],
    *,
    minimum: int,
) -> bool:
    facts = payload.get("facts")
    if not isinstance(facts, list) or len(facts) <= minimum:
        return False
    protected = _correlated_fact_indexes(payload)
    for index in range(len(facts) - 1, -1, -1):
        if index in protected:
            continue
        facts.pop(index)
        return True
    return False


def _shrink_long_evidence_strings(payload: dict[str, Any]) -> bool:
    candidates: list[tuple[dict[str, Any], str, str]] = []
    protected_keys = {
        "ref",
        "trace_id",
        "span_id",
        "syscall_trace_id_request",
        "syscall_trace_id_response",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    key not in protected_keys
                    and isinstance(item, str)
                    and len(item) > 240
                ):
                    candidates.append((value, key, item))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for key in ("facts", "samples", "flows", "spans", "correlations", "series"):
        visit(payload.get(key))
    if not candidates:
        return False
    parent, key, value = max(candidates, key=lambda item: len(item[2]))
    if key == "message":
        parent.setdefault("message_original_length", len(value))
        parent.setdefault(
            "message_sha256",
            hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
        )
        parent["message_truncated"] = True
    elif key == "value" and parent.get("name") == "log.message":
        parent.setdefault("value_original_length", len(value))
        parent.setdefault(
            "value_sha256",
            hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
        )
        parent["value_truncated"] = True
    parent[key] = _strictly_shorter_text(value, 120)
    return True


def _strictly_shorter_text(value: str, max_prefix: int) -> str:
    suffix = "...[truncated]"
    prefix_size = min(
        max_prefix,
        max(0, len(value) // 2),
        max(0, len(value) - len(suffix) - 1),
    )
    return value[:prefix_size] + suffix


def _sync_result_projection(payload: dict[str, Any]) -> None:
    if payload.get("dimension") == "topology":
        _sync_topology_projection(payload)
    _sync_evidence_refs(payload)
    from .fact_ledger import build_fact_ledger

    ledger = build_fact_ledger(payload)
    if ledger is None:
        payload.pop("fact_ledger", None)
    else:
        payload["fact_ledger"] = ledger
    _sync_correlations(payload, ledger)


def _sync_topology_projection(payload: dict[str, Any]) -> None:
    _filter_topology_facts_by_provenance(payload)
    facts = payload.get("facts")
    if not isinstance(facts, list):
        payload["entities"] = []
        payload["edges"] = []
        payload["topology_summary"] = {
            "entity_count": 0,
            "edge_count": 0,
            "relations": {},
            "coverage": payload.get("coverage"),
            "source_system": payload.get("source_system") or "kubernetes",
        }
        return

    source_system = str(payload.get("source_system") or "kubernetes")
    existing_entities = payload.get("entities")
    existing_entity_list = (
        existing_entities if isinstance(existing_entities, list) else []
    )
    entities_by_id: dict[str, dict[str, Any]] = {
        str(entity.get("entity_id")): entity
        for entity in existing_entity_list
        if isinstance(entity, dict) and entity.get("entity_id")
    }
    retained_entity_ids: set[str] = set()
    edges: list[dict[str, Any]] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_source_system = str(
            fact.get("source_system") or source_system
        )
        value = fact.get("value")
        if not isinstance(value, dict):
            continue
        source = value.get("source")
        target = value.get("target")
        if isinstance(source, dict) and isinstance(target, dict):
            source_id = _topology_entity_id(source)
            target_id = _topology_entity_id(target)
            endpoint_entities = ((source_id, source), (target_id, target))
        elif isinstance(source, str) and isinstance(target, str):
            source_id = source
            target_id = target
            endpoint_entities = ()
        else:
            continue
        for entity_id, entity in endpoint_entities:
            if not entity_id or entity_id in entities_by_id:
                continue
            compact = {
                "entity_id": entity_id,
                "kind": entity.get("kind"),
                "name": entity.get("name"),
                "source_system": source_system,
            }
            for key in ("namespace", "uid", "attributes"):
                if entity.get(key) not in (None, {}, []):
                    compact[key] = entity.get(key)
            entities_by_id[entity_id] = compact
        if (
            source_id
            and target_id
            and source_id in entities_by_id
            and target_id in entities_by_id
            and fact.get("ref")
        ):
            retained_entity_ids.update((source_id, target_id))
            edges.append(
                {
                    "relation": value.get("relation") or fact.get("relation"),
                    "source": source_id,
                    "target": target_id,
                    "source_system": fact_source_system,
                    "evidence_ref": fact.get("ref"),
                }
            )

    entities = [
        entity
        for entity_id, entity in entities_by_id.items()
        if entity_id in retained_entity_ids
    ]
    relations = Counter(str(edge.get("relation") or "unknown") for edge in edges)
    payload["entities"] = entities
    payload["edges"] = edges
    payload["topology_summary"] = {
        "entity_count": len(entities),
        "edge_count": len(edges),
        "relations": dict(relations),
        "coverage": payload.get("coverage"),
        "source_system": source_system,
    }


def _topology_entity_id(entity: dict[str, Any]) -> str:
    kind = str(entity.get("kind") or "entity").lower()
    namespace = str(entity.get("namespace") or "cluster")
    name = str(entity.get("name") or "")
    if kind == "container":
        pod = str((entity.get("attributes") or {}).get("pod") or "")
        if pod:
            name = f"{pod}/{name}"
    return f"{kind}:{namespace}/{name}" if name else ""


def _sync_evidence_refs(payload: dict[str, Any]) -> None:
    facts = payload.get("facts")
    if not isinstance(facts, list):
        payload["evidence_refs"] = []
        return
    payload["evidence_refs"] = [
        str(fact["ref"])
        for fact in facts
        if isinstance(fact, dict) and fact.get("ref")
    ]


def _sync_correlations(
    payload: dict[str, Any],
    ledger: dict[str, Any] | None,
) -> None:
    correlations = payload.get("correlations")
    if not isinstance(correlations, list):
        return
    records = ledger.get("records") if isinstance(ledger, dict) else None
    if not isinstance(records, list):
        payload["correlations"] = []
        return
    sources_by_trace: dict[str, set[str]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        metadata = record.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        trace_id = str(metadata.get("trace_id") or "")
        source = str(record.get("source_system") or "")
        if trace_id and source:
            sources_by_trace.setdefault(trace_id, set()).add(source)
    payload["correlations"] = [
        item
        for item in correlations
        if isinstance(item, dict)
        and {"deepflow", "tempo"}.issubset(
            sources_by_trace.get(str(item.get("trace_id") or ""), set())
        )
    ]


def _correlated_fact_indexes(payload: dict[str, Any]) -> set[int]:
    facts = payload.get("facts")
    correlations = payload.get("correlations")
    if not isinstance(facts, list) or not isinstance(correlations, list):
        return set()
    protected: set[int] = set()
    for correlation in correlations:
        if not isinstance(correlation, dict):
            continue
        trace_id = str(correlation.get("trace_id") or "")
        for source in ("deepflow", "tempo"):
            for index, fact in enumerate(facts):
                if (
                    isinstance(fact, dict)
                    and str(fact.get("trace_id") or "") == trace_id
                    and str(fact.get("source_system") or "") == source
                ):
                    protected.add(index)
                    break
    return protected


def _bounded_entity(value: Any) -> dict[str, Any]:
    entity = value if isinstance(value, dict) else {}
    bounded = {
        "kind": _bounded_text(entity.get("kind") or "Pod", 32),
        "namespace": _bounded_text(entity.get("namespace"), 63),
        "pod": _bounded_text(entity.get("pod"), 253),
    }
    for key, limit in (
        ("pod_uid", 128),
        ("pod_ip", 64),
        ("node", 253),
    ):
        if entity.get(key) is not None:
            bounded[key] = _bounded_text(entity.get(key), limit)
    return bounded


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)] + "...[truncated]"


def _parse_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
