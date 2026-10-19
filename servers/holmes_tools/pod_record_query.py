"""Lifecycle fact adapter for optional, bounded record operations."""

import os

from .record_query import CONTRACT_VERSION, QUERY_KEYS, select_records, validate_query

POD_FIELDS = [
    "namespace",
    "pod",
    "uid",
    "node",
    "phase",
    "reason",
    "created_at",
    "deletion_timestamp",
    "deletion_grace_period_seconds",
    "terminating_seconds",
    "finalizers",
    "conditions",
    "ready",
    "condition_states",
]
CONTAINER_FIELDS = POD_FIELDS + [
    "container",
    "role",
    "status_observed",
    "restart_count_total",
    "restart_count_in_window",
    "state",
    "last_state",
    "last_termination_reason",
    "last_exit_code",
    "last_finished_at",
    "latest_observed_termination_at",
    "termination_within_window",
    "resources",
    "current_state",
]
CONTAINER_ONLY_FIELDS = [f for f in CONTAINER_FIELDS if f not in POD_FIELDS]
NON_SCALAR_FIELDS = {"conditions", "condition_states", "state", "current_state", "last_state", "resources", "finalizers"}
DEFAULT_FIELDS = {
    "pod": ["namespace", "pod", "uid", "node", "phase", "ready", "deletion_timestamp", "terminating_seconds", "finalizers", "condition_states"],
    "container": [
        "namespace",
        "pod",
        "uid",
        "container",
        "role",
        "ready",
        "current_state",
        "node",
        "phase",
        "deletion_timestamp",
        "terminating_seconds",
        "finalizers",
        "last_termination_reason",
        "last_exit_code",
        "restart_count_total",
        "restart_count_in_window",
        "termination_within_window",
    ],
}
SOURCE_KEYS = (
    "contract_version",
    "status",
    "source",
    "observed_at",
    "scope",
    "window",
    "coverage",
    "history_complete",
    "limitations",
    "next_continue_token",
    "remaining_item_count",
    "resource_version",
    "returned_count",
)


def enabled():
    return os.getenv("AIOPS_RECORD_QUERY_ENABLED", "true").lower() not in {"false", "0", "off"}


DESCRIPTION = (
    " Optional record_type (pod|container) returns chosen fields instead of the full snapshot: pod rows = "
    "Pod identity/phase/conditions; container rows add readiness, restarts, termination (incl. init/ephemeral). "
    "where filters the SAME row (match all|any); group_by returns exact counts over this page only. "
    "Missing values stay null (unknown). Example count: record_type=pod, group_by=node."
)
PROPERTIES = {
    "record_type": {
        "type": "string",
        "enum": ["pod", "container"],
        "description": "Optional computed view: pod for inventory/counts; container for state/restarts/termination.",
    },
    "fields": {
        "type": "array",
        "minItems": 1,
        "maxItems": 40,
        "items": {"type": "string", "enum": CONTAINER_FIELDS},
        "description": "Omit for essentials; identities retained. Pod rows accept namespace..condition_states "
        "(Pod-level names); container rows accept all. last_* copy last_state.terminated.",
    },
    "where": {
        "type": "array",
        "maxItems": 12,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["field", "op", "value"],
            "properties": {
                "field": {"type": "string", "description": "A name from fields."},
                "op": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte"]},
                "value": {"type": ["string", "number", "boolean", "null"]},
            },
        },
    },
    "match": {
        "type": "string",
        "enum": ["all", "any"],
        "description": "Combine where conditions; defaults to all.",
    },
    "group_by": {
        "type": "string",
        "description": "A scalar name from fields; returns exact counts per value instead of rows.",
    },
}


def prepare(arguments):
    if not QUERY_KEYS.intersection(arguments):
        return None
    if not enabled():
        raise ValueError("Record queries are disabled; omit record parameters to use the original tool")
    kind = arguments.get("record_type", "pod")
    if kind not in ("pod", "container"):
        raise ValueError("record_type must be pod or container")
    query = {k: arguments[k] for k in QUERY_KEYS if k in arguments}
    kind, query, notes = _lenient(kind, query)
    parsed = validate_query(query, POD_FIELDS if kind == "pod" else CONTAINER_FIELDS, DEFAULT_FIELDS[kind])
    if parsed.get("group_by") in NON_SCALAR_FIELDS:
        fields = POD_FIELDS if kind == "pod" else CONTAINER_FIELDS
        scalar = ", ".join(f for f in fields if f not in NON_SCALAR_FIELDS)
        raise ValueError(f"group_by requires a scalar field; available for {kind} rows: {scalar}")
    limit = int(os.getenv("AIOPS_RECORD_RESULT_MAX_BYTES", "12000"))
    if not 1024 <= limit <= 65536:
        raise ValueError("AIOPS_RECORD_RESULT_MAX_BYTES must be between 1024 and 65536")
    return kind, parsed, limit, notes


def _lenient(kind, query):
    """Repair field names that cannot change the result's meaning; report every repair.

    Container rows carry every Pod-level field, so a Pod query that names container-level
    fields (state, restarts, exit codes) is answered with container rows instead of
    failing. Unknown names in ``fields`` are dropped while others remain. Filters and
    group_by on unknown names still fail: dropping them would silently change the rows.
    """
    notes = []
    where = query.get("where") if isinstance(query.get("where"), list) else []
    referenced = {f for f in query.get("fields") or [] if isinstance(f, str)}
    referenced |= {c.get("field") for c in where if isinstance(c, dict)}
    if isinstance(query.get("group_by"), str):
        referenced.add(query["group_by"])
    promoted = sorted(referenced & set(CONTAINER_ONLY_FIELDS))
    if kind == "pod" and promoted:
        kind = "container"
        notes.append(
            f"请求包含容器级字段 {', '.join(promoted)}，已按 record_type=container 返回"
            "（每行一个容器，Pod 级字段同样保留）"
        )
    fields = query.get("fields")
    if isinstance(fields, list) and fields:
        known = POD_FIELDS if kind == "pod" else CONTAINER_FIELDS
        kept = [f for f in fields if isinstance(f, str) and f in known]
        dropped = [f for f in fields if not (isinstance(f, str) and f in known)]
        if kept and dropped:
            query = {**query, "fields": kept}
            notes.append(f"已忽略不存在的字段 {', '.join(map(str, dropped))}；可用字段：{', '.join(known)}")
    return kind, query, notes


def lifecycle_rows(payload, kind):
    rows = []
    for pod in payload["pods"]:
        parent = {f: pod.get("name" if f == "pod" else f) for f in POD_FIELDS}
        conditions = pod.get("conditions") or []
        ready = next((c.get("status") for c in conditions if c.get("type") == "Ready"), None)
        parent["ready"] = {"True": True, "False": False}.get(ready)
        parent["condition_states"] = {
            c["type"]: {k: c[k] for k in ("status", "reason") if c.get(k) is not None}
            for c in conditions
            if c.get("type")
        }
        if kind == "pod":
            rows.append(parent)
        else:
            for container in pod.get("containers", []):
                previous = (container.get("last_state") or {}).get("terminated") or {}
                row = parent | {f: container.get(f) for f in CONTAINER_FIELDS if f not in POD_FIELDS}
                row["ready"] = container.get("ready")
                row["current_state"] = {
                    kind: {k: v for k, v in fields.items() if k in ("reason", "exitCode", "signal")}
                    for kind, fields in (container.get("state") or {}).items()
                }
                row.update(
                    container=container.get("name"),
                    last_termination_reason=previous.get("reason"),
                    last_exit_code=previous.get("exitCode"),
                    last_finished_at=previous.get("finishedAt"),
                )
                rows.append(row)
    return rows


def error_result(message, arguments, source=None):
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "error",
        "coverage": "unknown",
        "error": str(message),
        "executed_query": arguments,
        "source_metadata": {k: source[k] for k in SOURCE_KEYS if source and k in source},
    }


def project(payload, prepared, arguments):
    kind, query, limit, notes = prepared
    try:
        if payload.get("status") != "success":
            return error_result(payload.get("error", "source_not_successful"), arguments, payload)
        selected = select_records(
            lifecycle_rows(payload, kind),
            query,
            ["namespace", "pod", "uid"] + (["container", "role"] if kind == "container" else []),
        )
        packet = {
            "contract_version": CONTRACT_VERSION,
            "status": "success",
            "coverage": payload["coverage"],
            "record_unit": kind,
            "executed_query": arguments,
            "source_metadata": {k: payload[k] for k in SOURCE_KEYS if k in payload},
            **({"query_adjustments": notes} if notes else {}),
            **selected,
        }
        from .record_pages import paginate

        return paginate(packet, arguments, limit)
    except (ValueError, TypeError, KeyError) as exc:
        return error_result(exc, arguments, payload)
