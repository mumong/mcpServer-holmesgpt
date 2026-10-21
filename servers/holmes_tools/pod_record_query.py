"""Lifecycle fact adapter for optional, bounded record operations."""

import os
from datetime import datetime

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
    "stopped_by_pod_deletion",
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
# A Pod being deleted changes what every other field means (a 137 after the deletion is
# the kubelet stopping it, not an OOM), so these ride along on its rows whatever the
# caller selected. Live run f8e56239b1b74313 picked fields without them and misread
# a finalizer-blocked Pod as an application exit.
DELETION_FIELDS = ["deletion_timestamp", "terminating_seconds", "finalizers"]
CONTAINER_DELETION_FIELDS = DELETION_FIELDS + ["stopped_by_pod_deletion"]
# Names callers write for a field that exists under another name; resolved, never guessed.
FIELD_ALIASES = {
    "ns": "namespace",
    "name": "pod",
    "pod_name": "pod",
    "node_name": "node",
    "container_name": "container",
    "restarts": "restart_count_total",
    "exit_code": "last_exit_code",
}
# Fields whose values separate a broken Pod from a healthy one; ``phase`` alone does not.
HEALTH_FIELDS = {
    "ready", "deletion_timestamp", "terminating_seconds", "finalizers", "condition_states", "conditions",
    "state", "current_state", "last_state", "restart_count_total", "restart_count_in_window",
    "termination_within_window", "last_termination_reason", "last_exit_code", "stopped_by_pod_deletion",
}
PHASE_CAVEAT = (
    "phase=Running 也包含 NotReady、反复重启（CrashLoopBackOff）和 Terminating 中的 Pod；"
    "判断异常请结合 ready、restart_count_total、deletion_timestamp、current_state"
)
ABNORMAL_FIRST_NOTE = (
    "Rows are ordered abnormal-first (deleting, not ready, phase other than Running/Succeeded, "
    "waiting/terminated container, termination within window) and then in source order, "
    "so the first page holds the anomalies when they fit."
)
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
    "Missing values stay null (unknown). Record pages list abnormal Pods first (deleting, not ready, "
    "non-Running phase, waiting/terminated, recent termination), then healthy ones; phase alone does not "
    "tell them apart. Rows of a Pod being deleted always carry deletion_timestamp, "
    "terminating_seconds, finalizers and (container rows) stopped_by_pod_deletion = the container ended "
    "after the deletion was requested. Example count: record_type=pod, group_by=node."
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
    arguments, notes = canonical_arguments(arguments)
    query = {k: arguments[k] for k in QUERY_KEYS if k in arguments}
    kind, query, notes = _lenient(kind, query, notes)
    known = POD_FIELDS if kind == "pod" else CONTAINER_FIELDS
    group_by = query.get("group_by")
    if "group_by" in query and (group_by in NON_SCALAR_FIELDS or group_by not in known):
        scalar = ", ".join(f for f in known if f not in NON_SCALAR_FIELDS)
        raise ValueError(f"group_by must name a scalar field; available for {kind} rows: {scalar}")
    parsed = validate_query(query, known, DEFAULT_FIELDS[kind])
    limit = int(os.getenv("AIOPS_RECORD_RESULT_MAX_BYTES", "12000"))
    if not 1024 <= limit <= 65536:
        raise ValueError("AIOPS_RECORD_RESULT_MAX_BYTES must be between 1024 and 65536")
    return kind, parsed, limit, notes, arguments


def canonical_arguments(arguments):
    """Arguments as executed: aliases resolved, empty group_by dropped; notes say what changed.

    Applied before the page fingerprint, so a continuation call written with the same
    alias still matches its snapshot, and ``executed_query`` echoes real field names.
    """
    notes = []
    out = _resolve_aliases(dict(arguments or {}), notes)
    if "group_by" in out and out["group_by"] in ("", None):
        out.pop("group_by")
        notes.append("已忽略空的 group_by，按记录行返回")
    return out, notes


def _lenient(kind, query, notes=None):
    """Repair field names that cannot change the result's meaning; report every repair.

    Container rows carry every Pod-level field, so a Pod query that names container-level
    fields (state, restarts, exit codes) is answered with container rows instead of
    failing. Unknown names in ``fields`` are dropped while others remain. Filters and
    group_by on unknown names still fail: dropping them would silently change the rows.
    """
    notes = list(notes or [])
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
    deciding = {c.get("field") for c in where if isinstance(c, dict)}
    if isinstance(query.get("group_by"), str):
        deciding.add(query["group_by"])
    if "phase" in deciding and not deciding & HEALTH_FIELDS:
        notes.append(PHASE_CAVEAT)
    return kind, query, notes


def _resolve_aliases(query, notes):
    """Map alias names in fields / where / group_by to the real field; note each one."""
    used = []

    def real(name):
        if isinstance(name, str) and name in FIELD_ALIASES:
            used.append(f"{name}→{FIELD_ALIASES[name]}")
            return FIELD_ALIASES[name]
        return name

    out = dict(query)
    if isinstance(out.get("fields"), list):
        out["fields"] = [real(f) for f in out["fields"]]
    if isinstance(out.get("where"), list):
        out["where"] = [
            {**c, "field": real(c.get("field"))} if isinstance(c, dict) else c for c in out["where"]
        ]
    if isinstance(out.get("group_by"), str):
        out["group_by"] = real(out["group_by"])
    if used:
        notes.append("字段别名已换成实际字段名：" + ", ".join(dict.fromkeys(used)))
    return out


def _is_abnormal(row):
    """True when the row shows a problem by Kubernetes facts, not by phase alone."""
    phase = row.get("phase")
    if row.get("deletion_timestamp"):
        return True
    if phase == "Succeeded":
        return False  # finished Jobs: not ready by design
    if row.get("ready") is False and row.get("role") != "init":
        return True
    if phase not in (None, "Running"):
        return True
    state = row.get("state") or {}
    if isinstance(state, dict):
        if "waiting" in state:
            return True
        terminated = state.get("terminated")
        if isinstance(terminated, dict):
            # exit 0 is a finished init container or a completed task, however recent
            return (terminated.get("exitCode") or 0) != 0
    return row.get("termination_within_window") is True


def abnormal_first(rows):
    """Stable: abnormal rows keep their source order and come before healthy ones."""
    return [r for r in rows if _is_abnormal(r)] + [r for r in rows if not _is_abnormal(r)]


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _stopped_by_deletion(container, deleted_at):
    """True when the container's latest termination came after the Pod deletion request."""
    finished = _parse_time(container.get("latest_observed_termination_at"))
    if deleted_at is None or finished is None:
        return None
    return finished >= deleted_at


def lifecycle_rows(payload, kind):
    rows = []
    for pod in payload["pods"]:
        deleted_at = _parse_time(pod.get("deletion_timestamp"))
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
                    stopped_by_pod_deletion=_stopped_by_deletion(container, deleted_at),
                )
                rows.append(row)
    return rows


MAX_GROUP_EXAMPLES = 5


def group_health(kind):
    """Counted health facts per group: phase=Running includes NotReady and crash-looping
    Pods, so a count alone hides them (run 97f67069325c471c reported 26 Running, none
    abnormal, while two were in CrashLoopBackOff). Facts only; no diagnosis.
    """

    def facts(rows):
        out = {"not_ready": sum(1 for r in rows if r.get("ready") is False)}
        unknown = sum(1 for r in rows if r.get("ready") is None)
        if unknown:
            out["ready_unknown"] = unknown
        terminating = sum(1 for r in rows if r.get("deletion_timestamp"))
        if terminating:
            out["terminating"] = terminating
        examples = [f"{r.get('namespace')}/{r.get('pod')}" for r in rows if r.get("ready") is False]
        if examples:
            out["not_ready_examples"] = list(dict.fromkeys(examples))[:MAX_GROUP_EXAMPLES]
        if kind == "container":
            restarted = sum(1 for r in rows if (r.get("restart_count_in_window") or 0) > 0)
            if restarted:
                out["restarted_in_window"] = restarted
            for key, values in (
                ("waiting_reasons", [(r.get("current_state") or {}).get("reason") for r in rows
                                     if (r.get("current_state") or {}).get("state") == "waiting"]),
                ("last_termination_reasons", [r.get("last_termination_reason") for r in rows]),
            ):
                counts = {}
                for value in values:
                    if value:
                        counts[value] = counts.get(value, 0) + 1
                if counts:
                    out[key] = counts
        return out

    return facts


def _keep_deletion_facts(selected, rows, kind):
    """When a selected row's Pod is being deleted, add the deletion facts as columns.

    Every row gets the added columns (null/[] for Pods that are not being deleted), so
    rows and columns stay one shape: consumers reject a page whose rows differ.
    """
    if selected.get("result_type") != "records":
        return
    sticky = CONTAINER_DELETION_FIELDS if kind == "container" else DELETION_FIELDS
    identity = ("namespace", "pod", "container") if kind == "container" else ("namespace", "pod")
    by_identity = {tuple(row.get(f) for f in identity): row for row in rows}
    sources = [by_identity.get(tuple(row.get(f) for f in identity)) or {} for row in selected["rows"]]
    if not any(source.get("deletion_timestamp") for source in sources):
        return
    for row, source in zip(selected["rows"], sources, strict=True):
        row.update({f: source.get(f) for f in sticky if f not in row})
    selected["columns"] = list(dict.fromkeys([*selected["columns"], *sticky]))


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
    kind, query, limit, notes, arguments = prepared
    try:
        if payload.get("status") != "success":
            return error_result(payload.get("error", "source_not_successful"), arguments, payload)
        rows = abnormal_first(lifecycle_rows(payload, kind))
        selected = select_records(
            rows,
            query,
            ["namespace", "pod", "uid"] + (["container", "role"] if kind == "container" else []),
            group_facts=group_health(kind),
        )
        _keep_deletion_facts(selected, rows, kind)
        source = {k: payload[k] for k in SOURCE_KEYS if k in payload}
        if selected.get("result_type") == "records":
            source["limitations"] = [*(source.get("limitations") or []), ABNORMAL_FIRST_NOTE]
        packet = {
            "contract_version": CONTRACT_VERSION,
            "status": "success",
            "coverage": payload["coverage"],
            "record_unit": kind,
            "executed_query": arguments,
            "source_metadata": source,
            **({"row_order": "abnormal_first"} if selected.get("result_type") == "records" else {}),
            **({"query_adjustments": notes} if notes else {}),
            **selected,
        }
        from .record_pages import paginate

        return paginate(packet, arguments, limit)
    except (ValueError, TypeError, KeyError) as exc:
        return error_result(exc, arguments, payload)
