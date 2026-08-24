"""Shared contracts for model-directed observability queries."""

from __future__ import annotations

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


# Tool inputs and backend queries remain bounded for safety, but successful
# result serialization is lossless.  Context shaping belongs to the AIOps
# observation layer, where the full MCP response can first be archived.
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
        "limits": {"response_serialization": "lossless"},
        "error": {
            "code": _bounded_text(code, 64),
            "message": str(message or ""),
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
    """Serialize a complete MCP result without response-budget evidence loss.

    The historical function name is retained for call-site compatibility.
    ``truncated`` continues to describe backend/query collection boundaries;
    ``serialization.response_truncated`` explicitly describes this final MCP
    response stage.
    """
    result = copy.deepcopy(payload)
    result.setdefault("truncated", False)
    limits = result.setdefault("limits", {})
    limits.pop("max_serialized_bytes", None)
    limits["response_serialization"] = "lossless"
    _sync_result_projection(result)
    serialization = result.setdefault("serialization", {})
    serialization.update(
        {
            "policy": "lossless",
            "response_truncated": False,
            "serialized_bytes": 0,
        }
    )
    raw = ""
    # serialized_bytes includes its own field.  Iterate until digit width and
    # final UTF-8 size stabilize (normally two passes).
    for _ in range(4):
        raw = _dump(result)
        size = len(raw.encode("utf-8"))
        if serialization.get("serialized_bytes") == size:
            break
        serialization["serialized_bytes"] = size
    return _dump(result)


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


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
