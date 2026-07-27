"""Execute bounded, model-selected PromQL for one Kubernetes Pod."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from ..config import get_config
from .common import (
    MAX_WINDOW_SECONDS,
    bounded_window,
    error_result,
    evidence_ref,
    resolve_pod_scope,
)

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


MAX_PROMQL_LENGTH = 2000
MAX_SERIES = 20
MAX_SAMPLES = 20
MAX_EVALUATION_POINTS = 500
SCRAPER_LABELS = {
    "job",
    "instance",
    "endpoint",
    "service",
    "prometheus",
    "prometheus_replica",
}
PROMQL_RESERVED = {
    "and",
    "or",
    "unless",
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "bool",
    "offset",
    "sum",
    "avg",
    "group",
    "min",
    "max",
    "count",
    "stddev",
    "stdvar",
    "bottomk",
    "topk",
    "quantile",
    "count_values",
    "limitk",
    "limit_ratio",
}


def validate_promql_scope(
    promql: str,
    namespace: str,
    pod: str,
) -> dict[str, str] | None:
    text = str(promql or "").strip()
    if not text:
        return {"error_code": "invalid_arguments", "error": "promql is required"}
    if len(text) > MAX_PROMQL_LENGTH:
        return {
            "error_code": "query_too_long",
            "error": f"PromQL exceeds {MAX_PROMQL_LENGTH} characters",
        }

    masked = _mask_quoted_strings(text)
    time_error = _validate_promql_time_bounds(masked)
    if time_error:
        return time_error
    selector_spans: list[tuple[int, int]] = []
    for match in re.finditer(r"\{([^{}]*)\}", masked):
        selector_spans.append(match.span())
        original_selector = text[match.start() + 1 : match.end() - 1]
        matchers = {
            label: (operator, value)
            for label, operator, value in re.findall(
                r'([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:\\.|[^"\\])*)"',
                original_selector,
            )
        }
        if matchers.get("namespace") != ("=", namespace) or matchers.get("pod") != ("=", pod):
            return {
                "error_code": "missing_pod_scope",
                "error": (
                    "every PromQL vector selector must contain exact matchers "
                    f'namespace="{namespace}" and pod="{pod}"'
                ),
            }

    if not selector_spans:
        return {
            "error_code": "missing_pod_scope",
            "error": "PromQL must contain at least one Pod-scoped vector selector",
        }

    label_list_spans = [
        match.span()
        for match in re.finditer(
            (
                r"\b(?:by|without|on|ignoring|group_left|group_right)"
                r"\s*\([^()]*\)"
            ),
            masked,
        )
    ]
    range_selector_spans = [
        match.span()
        for match in re.finditer(r"\[[^\[\]]*\]", masked)
    ]
    for match in re.finditer(r"\b[A-Za-z_:][A-Za-z0-9_:]*\b", masked):
        token = match.group(0)
        if (
            _inside_spans(match.start(), selector_spans)
            or _inside_spans(match.start(), label_list_spans)
            or _inside_spans(match.start(), range_selector_spans)
        ):
            continue
        before = masked[: match.start()]
        after = masked[match.end() :]
        if before and before[-1].isdigit() and token in {"s", "m", "h", "d", "w", "y"}:
            continue
        if token.lower() in PROMQL_RESERVED:
            continue
        if re.match(r"\s*\(", after):
            continue
        if re.match(r"\s*\{", after):
            continue
        if token in {"Inf", "NaN"}:
            continue
        return {
            "error_code": "missing_pod_scope",
            "error": f"unscoped vector selector is not allowed: {token}",
        }
    return None


def compact_prometheus_result(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    raw_series = data.get("result") if isinstance(data.get("result"), list) else []
    grouped: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    truncated = False

    for item in raw_series:
        if not isinstance(item, dict):
            continue
        labels = item.get("metric") if isinstance(item.get("metric"), dict) else {}
        logical_labels = {
            str(key): str(value)
            for key, value in labels.items()
            if key not in SCRAPER_LABELS
        }
        key = tuple(sorted(logical_labels.items()))
        row: dict[str, Any] = {"metric": logical_labels}
        if isinstance(item.get("value"), list):
            row["value"] = item["value"]
            row["stats"] = _stats([item["value"]])
            row["sample_count"] = 1
        if isinstance(item.get("values"), list):
            original_values = _chronological_values(item["values"])
            values, was_truncated = _bounded_values(original_values)
            row["values"] = values
            row["stats"] = _stats(original_values)
            row["sample_count"] = len(original_values)
            truncated = truncated or was_truncated
        current = grouped.get(key)
        if current is None or _point_count(row) > _point_count(current):
            grouped[key] = row

    series = list(grouped.values())
    series.sort(key=lambda item: tuple(sorted(item.get("metric", {}).items())))
    if len(series) > MAX_SERIES:
        series = series[:MAX_SERIES]
        truncated = True
    return {
        "result_type": data.get("resultType"),
        "raw_series_count": len(raw_series),
        "series_count": len(grouped),
        "series": series,
        "truncated": truncated,
    }


def execute(arguments: dict[str, Any]) -> dict[str, Any]:
    namespace = str(arguments.get("namespace") or "").strip()
    pod = str(arguments.get("pod") or "").strip()
    purpose = str(arguments.get("purpose") or "").strip()
    promql = str(arguments.get("promql") or "").strip()
    query_type = str(arguments.get("query_type") or "").strip().lower()
    base_query = {"promql": promql, "query_type": query_type}

    if not namespace or not pod or not purpose or not promql:
        return error_result(
            source_system="prometheus",
            dimension="metrics",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="invalid_arguments",
            message="namespace, pod, purpose and promql are required",
            query=base_query,
        )
    if query_type not in {"instant", "range"}:
        return error_result(
            source_system="prometheus",
            dimension="metrics",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="invalid_query_type",
            message="query_type must be instant or range",
            query=base_query,
        )
    scope_error = validate_promql_scope(promql, namespace, pod)
    if scope_error:
        return error_result(
            source_system="prometheus",
            dimension="metrics",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code=scope_error["error_code"],
            message=scope_error["error"],
            query=base_query,
        )

    entity, entity_error = resolve_pod_scope(
        namespace,
        pod,
        arguments.get("pod_uid"),
    )
    if entity_error:
        return error_result(
            source_system="prometheus",
            dimension="metrics",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code=entity_error["code"],
            message=entity_error["message"],
            query=base_query,
        )
    assert entity is not None

    params: dict[str, Any] = {"query": promql}
    path = "api/v1/query"
    if query_type == "range":
        window, requested_window, window_error = _range_window(
            arguments,
            not_before=entity.get("creation_timestamp"),
        )
        range_query = {
            **base_query,
            **(
                {"requested_time_range": requested_window}
                if requested_window is not None
                else {}
            ),
            **(
                {
                    "pod_creation_timestamp": entity.get(
                        "creation_timestamp"
                    )
                }
                if entity.get("creation_timestamp")
                else {}
            ),
        }
        if window_error:
            return error_result(
                source_system="prometheus",
                dimension="metrics",
                namespace=namespace,
                pod=pod,
                purpose=purpose,
                code=window_error["code"],
                message=window_error["message"],
                entity=entity,
                query=range_query,
            )
        assert window is not None
        step = str(arguments.get("step") or "30s")
        step_error = _validate_range_step(window, step, promql)
        if step_error:
            return error_result(
                source_system="prometheus",
                dimension="metrics",
                namespace=namespace,
                pod=pod,
                purpose=purpose,
                code=step_error["error_code"],
                message=step_error["error"],
                entity=entity,
                query={**range_query, "time_range": window, "step": step},
            )
        path = "api/v1/query_range"
        params.update(
            {
                "start": window["start"],
                "end": window["end"],
                "step": step,
            }
        )
        base_query.update(
            {
                **range_query,
                "time_range": window,
                "step": step,
            }
        )

    payload, status_code = _request_prometheus(path, params)
    current_entity, lifecycle_error = resolve_pod_scope(
        namespace,
        pod,
        entity.get("pod_uid"),
    )
    if lifecycle_error or _entity_lifecycle_changed(entity, current_entity):
        changed_entity = {**entity, "lifecycle_changed": True}
        return error_result(
            source_system="prometheus",
            dimension="metrics",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="pod_lifecycle_changed",
            message=(
                str((lifecycle_error or {}).get("message") or "")
                or "target Pod identity changed while the query was executing"
            ),
            entity=changed_entity,
            query={**base_query, "http_status": status_code},
        )
    if payload.get("status") != "success":
        return error_result(
            source_system="prometheus",
            dimension="metrics",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="prometheus_error",
            message=str(payload.get("error") or "Prometheus query failed"),
            entity=entity,
            query={**base_query, "http_status": status_code},
        )
    shape_error = _prometheus_result_shape_error(payload, query_type)
    if shape_error:
        return error_result(
            source_system="prometheus",
            dimension="metrics",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="malformed_prometheus_response",
            message=shape_error,
            entity=entity,
            query={**base_query, "http_status": status_code},
        )

    compact = compact_prometheus_result(payload)
    compact["series"] = _annotate_series(
        compact.get("series") or [],
        entity,
        query_type,
    )
    facts = _facts(compact.get("series") or [], namespace, pod)
    refs = [fact["ref"] for fact in facts]
    coverage = "present" if compact["raw_series_count"] else "empty"
    value_units = {
        str(item.get("unit") or "")
        for item in (compact.get("series") or [])
        if item.get("unit")
    }
    trend_evaluable = any(
        item.get("trend_evaluable") is True
        for item in (compact.get("series") or [])
    )
    directness = (
        "direct"
        if any(
            item.get("directness") == "direct"
            for item in (compact.get("series") or [])
        )
        else "related_context"
    )
    return {
        "ok": True,
        "status": "query_succeeded",
        "source_system": "prometheus",
        "dimension": "metrics",
        "entity": entity,
        "purpose": purpose,
        "coverage": coverage,
        "directness": directness,
        "query": {
            **base_query,
            "http_status": status_code,
            "value_unit": (
                next(iter(value_units))
                if len(value_units) == 1
                else "mixed"
                if value_units
                else "unknown"
            ),
            "trend_evaluable": trend_evaluable,
        },
        "facts": facts,
        "samples": compact.get("series") or [],
        "evidence_refs": refs,
        "truncated": compact["truncated"],
        "limits": {
            "max_window_seconds": MAX_WINDOW_SECONDS,
            "max_series": MAX_SERIES,
            "max_samples_per_series": MAX_SAMPLES,
            "max_evaluation_points": MAX_EVALUATION_POINTS,
        },
        "result_type": compact.get("result_type"),
        "raw_series_count": compact.get("raw_series_count"),
        "series_count": compact.get("series_count"),
        "series": compact.get("series"),
    }


def _request_prometheus(
    path: str,
    params: dict[str, Any],
) -> tuple[dict[str, Any], int | None]:
    cfg = get_config()
    if not cfg.prometheus_url:
        return {"status": "error", "error": "PROMETHEUS_URL is not configured"}, None
    if requests is None:
        return {"status": "error", "error": "requests is not installed"}, None
    endpoint = urljoin(
        cfg.prometheus_url if cfg.prometheus_url.endswith("/") else cfg.prometheus_url + "/",
        path,
    )
    try:
        response = requests.get(endpoint, params=params, timeout=cfg.timeout_seconds)
        status_code = response.status_code
        payload = response.json()
        response.raise_for_status()
        return payload if isinstance(payload, dict) else {}, status_code
    except Exception as exc:  # noqa: BLE001
        response = getattr(exc, "response", None)
        return {
            "status": "error",
            "error": str(exc),
        }, getattr(response, "status_code", None)


def _facts(
    series: list[dict[str, Any]],
    namespace: str,
    pod: str,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for item in series[:8]:
        labels = item.get("metric") if isinstance(item.get("metric"), dict) else {}
        point = item.get("value")
        if not isinstance(point, list):
            values = item.get("values")
            point = values[-1] if isinstance(values, list) and values else None
        if not isinstance(point, list) or len(point) < 2:
            continue
        metric_name = str(labels.get("__name__") or "prometheus_sample")
        ref = evidence_ref(
            "metric",
            namespace,
            pod,
            {"metric": metric_name, "labels": labels, "point": point},
        )
        facts.append(
            {
                "ref": ref,
                "source_system": "prometheus",
                "dimension": "metrics",
                "entity": "/".join(
                    part
                    for part in (
                        namespace,
                        pod,
                        str(labels.get("container") or ""),
                    )
                    if part
                ),
                "observed_at": _sample_time(point[0]),
                "name": metric_name,
                "value": point[1],
                "unit": item.get("unit") or "unknown",
                "labels": labels,
                "stats": item.get("stats") or {},
                "sample_count": item.get("sample_count") or 0,
                "trend_evaluable": item.get("trend_evaluable") is True,
                "series_role": item.get("series_role") or "unknown",
                "directness": item.get("directness") or "related_context",
            }
        )
    return facts


def _annotate_series(
    series: list[dict[str, Any]],
    entity: dict[str, Any],
    query_type: str,
) -> list[dict[str, Any]]:
    application_containers = {
        str(name)
        for name in (entity.get("containers") or [])
        if str(name)
    }
    role_order = {
        "application_container": 0,
        "pod_aggregate": 1,
        "infrastructure_container": 2,
        "unknown": 3,
    }
    annotated: list[dict[str, Any]] = []
    for item in series:
        row = dict(item)
        labels = row.get("metric") if isinstance(row.get("metric"), dict) else {}
        container = str(labels.get("container") or "")
        image = str(labels.get("image") or "").lower()
        name = str(labels.get("name") or "").lower()
        if container and container in application_containers:
            role = "application_container"
        elif (
            container.upper() == "POD"
            or "pause:" in image
            or "/pause@" in image
            or "pause" in name
        ):
            role = "infrastructure_container"
        elif not container:
            role = "pod_aggregate"
        else:
            role = "unknown"

        values = row.get("values") if isinstance(row.get("values"), list) else []
        sample_count = int(
            row.get("sample_count")
            or (len(values) if values else 1 if isinstance(row.get("value"), list) else 0)
        )
        row["unit"] = _metric_unit(labels)
        row["sample_count"] = sample_count
        row["trend_evaluable"] = query_type == "range" and sample_count >= 2
        row["series_role"] = role
        row["directness"] = (
            "direct"
            if role == "application_container"
            else "related_context"
        )
        annotated.append(row)
    annotated.sort(
        key=lambda item: (
            role_order.get(str(item.get("series_role") or "unknown"), 9),
            tuple(sorted((item.get("metric") or {}).items())),
        )
    )
    return annotated


def _metric_unit(labels: dict[str, Any]) -> str:
    explicit = str(labels.get("unit") or "").strip().lower()
    if explicit:
        return "bytes" if explicit in {"byte", "bytes"} else explicit
    metric_name = str(labels.get("__name__") or "").strip().lower()
    if not metric_name:
        return "unknown"
    if metric_name.endswith("_bytes"):
        return "bytes"
    if metric_name.endswith(("_seconds", "_seconds_total")):
        return "seconds"
    if metric_name.endswith("_total"):
        return "count"
    return "unitless"


def _bounded_values(values: list[Any]) -> tuple[list[Any], bool]:
    if len(values) <= MAX_SAMPLES:
        return values, False
    numeric_points: list[tuple[int, float]] = []
    for index, point in enumerate(values):
        if not isinstance(point, list) or len(point) < 2:
            continue
        try:
            numeric_points.append((index, float(point[1])))
        except (TypeError, ValueError):
            continue
    indexes = {0, len(values) - 1}
    if numeric_points:
        indexes.add(min(numeric_points, key=lambda item: item[1])[0])
        indexes.add(max(numeric_points, key=lambda item: item[1])[0])
    evenly_spaced = [
        round(index * (len(values) - 1) / (MAX_SAMPLES - 1))
        for index in range(MAX_SAMPLES)
    ]
    for index in evenly_spaced:
        if len(indexes) >= MAX_SAMPLES:
            break
        indexes.add(index)
    return [values[index] for index in sorted(indexes)], True


def _chronological_values(values: list[Any]) -> list[Any]:
    def timestamp(point: Any) -> float:
        if not isinstance(point, list) or not point:
            return float("inf")
        try:
            return float(point[0])
        except (TypeError, ValueError):
            return float("inf")

    return sorted(values, key=timestamp)


def _stats(points: list[Any]) -> dict[str, float]:
    values: list[float] = []
    for point in points:
        if not isinstance(point, list) or len(point) < 2:
            continue
        try:
            values.append(float(point[1]))
        except (TypeError, ValueError):
            continue
    if not values:
        return {}
    return {
        "first": values[0],
        "min": min(values),
        "max": max(values),
        "last": values[-1],
    }


def _point_count(item: dict[str, Any]) -> int:
    if isinstance(item.get("sample_count"), int):
        return item["sample_count"]
    if isinstance(item.get("values"), list):
        return len(item["values"])
    return 1 if isinstance(item.get("value"), list) else 0


def _mask_quoted_strings(value: str) -> str:
    chars = list(value)
    in_quote = False
    escaped = False
    for index, char in enumerate(chars):
        if in_quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quote = False
            else:
                chars[index] = " "
        elif char == '"':
            in_quote = True
    return "".join(chars)


def _inside_spans(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in spans)


def _validate_promql_time_bounds(masked: str) -> dict[str, str] | None:
    max_range_seconds = 0.0
    for match in re.finditer(r"\[([^\[\]]*)\]", masked):
        selector = match.group(1).strip()
        range_text, separator, resolution_text = selector.partition(":")
        range_seconds = _prometheus_duration_seconds(range_text)
        if range_seconds is None:
            return {
                "error_code": "unsafe_time_modifier",
                "error": f"invalid PromQL range or subquery duration: [{selector}]",
            }
        max_range_seconds = max(max_range_seconds, range_seconds)
        if range_seconds > MAX_WINDOW_SECONDS:
            return {
                "error_code": "query_too_expensive",
                "error": "PromQL internal ranges must not exceed two hours",
            }
        if separator:
            resolution_seconds = _prometheus_duration_seconds(resolution_text)
            if not resolution_seconds or resolution_seconds <= 0:
                return {
                    "error_code": "query_too_expensive",
                    "error": "PromQL subqueries require a positive explicit resolution",
                }
            if int(range_seconds // resolution_seconds) + 1 > MAX_EVALUATION_POINTS:
                return {
                    "error_code": "query_too_expensive",
                    "error": (
                        "PromQL subquery exceeds the "
                        f"{MAX_EVALUATION_POINTS}-point evaluation budget"
                    ),
                }

    max_offset_seconds = 0.0
    for match in re.finditer(
        r"\boffset\s+([+-]?(?:\d+(?:\.\d+)?(?:ms|s|m|h|d|w|y))+)",
        masked,
        flags=re.IGNORECASE,
    ):
        offset_seconds = _prometheus_duration_seconds(match.group(1))
        if offset_seconds is None:
            return {
                "error_code": "unsafe_time_modifier",
                "error": "invalid PromQL offset duration",
            }
        max_offset_seconds = max(max_offset_seconds, abs(offset_seconds))
    if max_offset_seconds > MAX_WINDOW_SECONDS:
        return {
            "error_code": "query_too_expensive",
            "error": "PromQL offsets must not exceed two hours",
        }
    if max_range_seconds + max_offset_seconds > MAX_WINDOW_SECONDS:
        return {
            "error_code": "query_too_expensive",
            "error": "combined PromQL range and offset must not exceed two hours",
        }

    for match in re.finditer(r"@", masked):
        modifier = masked[match.end() :]
        if not re.match(r"\s*(?:start|end)\s*\(\s*\)", modifier):
            return {
                "error_code": "unsafe_time_modifier",
                "error": "absolute PromQL @ timestamps are not allowed",
            }
    return None


def _validate_range_step(
    window: dict[str, str],
    step: str,
    promql: str = "",
) -> dict[str, str] | None:
    step_seconds = _prometheus_duration_seconds(step)
    if not step_seconds or step_seconds <= 0:
        return {
            "error_code": "invalid_step",
            "error": "range query step must be a positive Prometheus duration",
        }
    try:
        start = datetime.fromisoformat(window["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(window["end"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return {
            "error_code": "invalid_time_range",
            "error": "range query contains an invalid time window",
        }
    points = int((end - start).total_seconds() // step_seconds) + 1
    if points > MAX_EVALUATION_POINTS:
        return {
            "error_code": "query_too_expensive",
            "error": (
                "range query exceeds the "
                f"{MAX_EVALUATION_POINTS}-point backend evaluation budget"
            ),
        }
    subquery_points = _max_subquery_points(promql)
    combined_points = points * subquery_points
    if combined_points > MAX_EVALUATION_POINTS:
        return {
            "error_code": "query_too_expensive",
            "error": (
                "combined outer range and PromQL subquery evaluation "
                f"requires {combined_points} points, exceeding the "
                f"{MAX_EVALUATION_POINTS}-point budget"
            ),
        }
    return None


def _range_window(
    arguments: dict[str, Any],
    *,
    not_before: str | None,
) -> tuple[
    dict[str, str] | None,
    dict[str, str] | None,
    dict[str, str] | None,
]:
    requested, error = bounded_window(arguments)
    if error or requested is None:
        return None, requested, error
    effective = dict(requested)
    if not not_before:
        return effective, requested, None

    try:
        requested_start = _parse_rfc3339(requested["start"])
        requested_end = _parse_rfc3339(requested["end"])
        lifecycle_start = _parse_rfc3339(not_before)
    except (KeyError, TypeError, ValueError) as exc:
        return None, requested, {
            "code": "invalid_time_range",
            "message": str(exc),
        }
    if requested_start >= lifecycle_start:
        return effective, requested, None
    if arguments.get("start"):
        return None, requested, {
            "code": "range_precedes_pod_lifecycle",
            "message": (
                f"explicit range start {requested['start']} predates current "
                f"Pod creation {not_before}; query the exact current Pod "
                "only within its lifecycle"
            ),
        }
    if lifecycle_start >= requested_end:
        return None, requested, {
            "code": "invalid_time_range",
            "message": (
                "the synthesized query window does not overlap the current "
                "Pod lifecycle"
            ),
        }
    effective["start"] = _format_rfc3339(lifecycle_start)
    return effective, requested, None


def _prometheus_result_shape_error(
    payload: dict[str, Any],
    query_type: str,
) -> str | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return "Prometheus success response must contain a data object"
    expected_result_type = "matrix" if query_type == "range" else "vector"
    result_type = data.get("resultType")
    if result_type != expected_result_type:
        return (
            f"Prometheus {query_type} response resultType must be "
            f"{expected_result_type}, got {result_type!r}"
        )
    if not isinstance(data.get("result"), list):
        return "Prometheus success response data.result must be an array"
    return None


def _max_subquery_points(promql: str) -> int:
    masked = _mask_quoted_strings(str(promql or ""))
    maximum = 1
    for match in re.finditer(r"\[([^\[\]]*)\]", masked):
        selector = match.group(1).strip()
        range_text, separator, resolution_text = selector.partition(":")
        if not separator:
            continue
        range_seconds = _prometheus_duration_seconds(range_text)
        resolution_seconds = _prometheus_duration_seconds(resolution_text)
        if (
            range_seconds is None
            or not resolution_seconds
            or resolution_seconds <= 0
        ):
            continue
        maximum = max(
            maximum,
            int(range_seconds // resolution_seconds) + 1,
        )
    return maximum


def _parse_rfc3339(value: str) -> datetime:
    text = str(value or "").strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _prometheus_duration_seconds(value: str) -> float | None:
    text = str(value or "").strip().lower()
    sign = -1.0 if text.startswith("-") else 1.0
    if text[:1] in {"+", "-"}:
        text = text[1:]
    if not text:
        return None
    units = {
        "ms": 0.001,
        "s": 1.0,
        "m": 60.0,
        "h": 3600.0,
        "d": 86400.0,
        "w": 604800.0,
        "y": 31536000.0,
    }
    position = 0
    total = 0.0
    for match in re.finditer(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)", text):
        if match.start() != position:
            return None
        total += float(match.group(1)) * units[match.group(2)]
        position = match.end()
    if position != len(text):
        return None
    return sign * total


def _entity_lifecycle_changed(
    initial: dict[str, Any],
    current: dict[str, Any] | None,
) -> bool:
    if current is None:
        return True
    for key in ("pod_uid", "creation_timestamp"):
        initial_value = str(initial.get(key) or "")
        current_value = str(current.get(key) or "")
        if initial_value and current_value and initial_value != current_value:
            return True
    return False


def _sample_time(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return str(value)
