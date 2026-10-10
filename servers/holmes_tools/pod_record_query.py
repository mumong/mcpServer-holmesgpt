"""Lifecycle fact adapter for optional, bounded record operations."""

import os

from .record_query import CONTRACT_VERSION, QUERY_KEYS, encode, select_records, validate_query


POD_FIELDS = ["namespace", "pod", "uid", "node", "phase", "reason", "created_at",
              "deletion_timestamp", "conditions"]
CONTAINER_FIELDS = POD_FIELDS + [
    "container", "role", "status_observed", "ready", "restart_count_total",
    "restart_count_in_window", "state", "last_state", "last_termination_reason",
    "last_exit_code", "last_finished_at", "latest_observed_termination_at",
    "termination_within_window", "resources",
]
DEFAULT_FIELDS = {
    "pod": ["namespace", "pod", "uid", "node", "phase"],
    "container": ["namespace", "pod", "uid", "container", "role", "ready", "state",
                  "last_termination_reason", "last_exit_code", "last_finished_at",
                  "restart_count_total", "restart_count_in_window", "termination_within_window"],
}
SOURCE_KEYS = ("contract_version", "status", "source", "observed_at", "scope", "window",
               "coverage", "history_complete", "limitations", "next_continue_token",
               "remaining_item_count", "resource_version", "returned_count")


def enabled():
    return os.getenv("AIOPS_RECORD_QUERY_ENABLED", "true").lower() not in {"false", "0", "off"}


DESCRIPTION = (
    " Optional record_type enables field selection/filtering/counts instead of the full snapshot. "
    "pod rows have Pod identity/phase/conditions only; container rows include readiness, "
    "restarts and termination plus parent Pod identity (including init/ephemeral roles). "
    "where predicates apply to the SAME row, match defaults to all. group_by computes exact "
    "counts in the chosen row unit over this page, not over missing pages. Missing values stay unknown. "
    "Example inventory count: record_type=pod, group_by=node. No record parameters preserves "
    "the original full lifecycle response."
)
PROPERTIES = {
    "record_type": {"type": "string", "enum": ["pod", "container"],
                    "description": "Optional computed view: pod for inventory/counts; container for state/restarts/termination."},
    "fields": {"type": "array", "minItems": 1, "maxItems": 40,
               "items": {"type": "string", "enum": CONTAINER_FIELDS},
               "description": "Omit for essentials; identities retained. Pod rows only: " + ", ".join(POD_FIELDS) +
               ". Container rows allow all fields. last_termination_reason/last_exit_code/last_finished_at copy last_state.terminated."},
    "where": {"type": "array", "maxItems": 12, "items": {
        "type": "object", "additionalProperties": False, "required": ["field", "op", "value"],
        "properties": {"field": {"type": "string", "enum": CONTAINER_FIELDS},
                       "op": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte"]},
                       "value": {"type": ["string", "number", "boolean", "null"]}}}},
    "match": {"type": "string", "enum": ["all", "any"], "description": "Combine where conditions; defaults to all."},
    "group_by": {"type": "string", "enum": [f for f in CONTAINER_FIELDS if f not in {
        "conditions", "state", "last_state", "resources"}],
        "description": "Return exact count per scalar value instead of rows. Counts use record_type's unit."},
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
    parsed = validate_query(query, POD_FIELDS if kind == "pod" else CONTAINER_FIELDS, DEFAULT_FIELDS[kind])
    if parsed.get("group_by") in {"conditions", "state", "last_state", "resources"}:
        raise ValueError("group_by requires a scalar field")
    limit = int(os.getenv("AIOPS_RECORD_RESULT_MAX_BYTES", "12000"))
    if not 1024 <= limit <= 65536:
        raise ValueError("AIOPS_RECORD_RESULT_MAX_BYTES must be between 1024 and 65536")
    return kind, parsed, limit


def lifecycle_rows(payload, kind):
    rows = []
    for pod in payload["pods"]:
        parent = {f: pod.get("name" if f == "pod" else f) for f in POD_FIELDS}
        if kind == "pod":
            rows.append(parent)
        else:
            for container in pod.get("containers", []):
                previous = (container.get("last_state") or {}).get("terminated") or {}
                row = parent | {f: container.get(f) for f in CONTAINER_FIELDS if f not in POD_FIELDS}
                row.update(container=container.get("name"), last_termination_reason=previous.get("reason"),
                           last_exit_code=previous.get("exitCode"), last_finished_at=previous.get("finishedAt"))
                rows.append(row)
    return rows


def error_result(message, arguments, source=None):
    return {"contract_version": CONTRACT_VERSION, "status": "error", "coverage": "unknown",
            "error": str(message), "executed_query": arguments,
            "source_metadata": {k: source[k] for k in SOURCE_KEYS if source and k in source}}


def project(payload, prepared, arguments):
    kind, query, limit = prepared
    try:
        if payload.get("status") != "success":
            return error_result(payload.get("error", "source_not_successful"), arguments, payload)
        selected = select_records(lifecycle_rows(payload, kind), query,
                                  ["namespace", "pod", "uid"] + (["container", "role"] if kind == "container" else []))
        packet = {"contract_version": CONTRACT_VERSION, "status": "success",
                  "coverage": payload["coverage"], "record_unit": kind,
                  "executed_query": arguments,
                  "source_metadata": {k: payload[k] for k in SOURCE_KEYS if k in payload}, **selected}
        if len(encode(packet).encode()) > limit:
            error = error_result("result_too_large: select fewer fields, filter or group; no records were truncated",
                                 arguments, payload)
            error.update(matched_rows=selected["matched_rows"], max_result_bytes=limit)
            return error
        return packet
    except (ValueError, TypeError, KeyError) as exc:
        return error_result(exc, arguments, payload)
