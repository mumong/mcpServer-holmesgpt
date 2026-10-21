"""Bounded operations on typed fact rows; no I/O, expressions or diagnosis rules."""

import copy
import json
import math

CONTRACT_VERSION = "evidence-records.v1"
QUERY_KEYS = frozenset({"record_type", "fields", "where", "match", "group_by"})


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def validate_query(query, known_fields, default_fields):
    """Validate before collection, including empty-input cases."""
    fields = query.get("fields", default_fields)
    if not isinstance(fields, list) or not 1 <= len(fields) <= 40:
        raise ValueError("fields must contain 1..40 field names")
    if any(not isinstance(f, str) or f not in known_fields for f in fields):
        raise ValueError(f"Unknown fields for this record_type; available: {', '.join(known_fields)}")
    where = query.get("where", [])
    if not isinstance(where, list) or len(where) > 12:
        raise ValueError("where must be a list of at most 12 conditions")
    for clause in where:
        if not isinstance(clause, dict) or set(clause) != {"field", "op", "value"}:
            raise ValueError("Each condition requires exactly field, op, value")
        field, op, value = clause["field"], clause["op"], clause["value"]
        if not isinstance(field, str) or field not in known_fields:
            raise ValueError(f"Unknown filter field; available: {', '.join(known_fields)}")
        if not isinstance(op, str) or op not in {"eq", "ne", "gt", "gte", "lt", "lte"}:
            raise ValueError("Unsupported filter operator")
        if type(value) not in (str, int, float, bool, type(None)):
            raise ValueError("Filter value must be a JSON scalar")
        if isinstance(value, str) and len(value) > 1024:
            raise ValueError("Filter value exceeds 1024 characters")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("Filter numbers must be finite")
        if op not in {"eq", "ne"} and type(value) not in (int, float):
            raise ValueError("Ordered comparisons require numeric values")
    match = query.get("match", "all")
    if match not in ("all", "any"):
        raise ValueError("match must be all or any")
    group_by = query.get("group_by")
    if "group_by" in query and (not isinstance(group_by, str) or group_by not in known_fields):
        raise ValueError("group_by must name an available scalar field")
    return {
        "fields": list(dict.fromkeys(fields)),
        "where": copy.deepcopy(where),
        "match": match,
        **({"group_by": group_by} if group_by is not None else {}),
    }


def _compare(actual, op, expected):
    if actual is None and expected is not None:
        return None
    if op in {"eq", "ne"}:
        equal = (
            type(actual) is type(expected) or type(actual) in (int, float) and type(expected) in (int, float)
        ) and actual == expected
        return equal if op == "eq" else not equal
    if type(actual) not in (int, float) or type(expected) not in (int, float):
        raise ValueError("Ordered comparisons require numeric source fields")
    return {
        "gt": actual > expected,
        "gte": actual >= expected,
        "lt": actual < expected,
        "lte": actual <= expected,
    }[op]


def select_records(rows, query, identity_fields=(), group_facts=None):
    """Filter the whole received page before projecting or counting; null is unknown.

    ``group_facts(rows) -> dict`` (supplied by a record type) adds counted facts to
    each group, computed from that group's own rows; this module stays rule-free.
    """
    matched, unknown = [], 0
    for row in rows:
        decisions = [_compare(row.get(c["field"]), c["op"], c["value"]) for c in query["where"]]
        if not decisions:
            selected = True
        elif query["match"] == "all":
            selected = False if False in decisions else None if None in decisions else True
        else:
            selected = True if True in decisions else None if None in decisions else False
        if selected is None:
            unknown += 1
        elif selected:
            matched.append(row)
    result = {"input_rows": len(rows), "matched_rows": len(matched), "unknown_filter_rows": unknown}
    if "group_by" in query:
        groups, members = {}, {}
        field = query["group_by"]
        for row in matched:
            value = row.get(field)
            if isinstance(value, (list, dict)):
                raise ValueError("group_by requires a scalar source field")
            key = encode(value)
            groups.setdefault(key, {field: value, "count": 0})["count"] += 1
            members.setdefault(key, []).append(row)
        if group_facts is not None:
            for key, group in groups.items():
                group.update(group_facts(members[key]))
        result.update(result_type="groups", groups=list(groups.values()))
    else:
        columns = list(dict.fromkeys([*identity_fields, *query["fields"]]))
        result.update(
            result_type="records",
            columns=columns,
            rows=[{f: copy.deepcopy(row.get(f)) for f in columns} for row in matched],
        )
    return result
