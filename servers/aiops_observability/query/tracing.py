"""Query Pod-scoped DeepFlow flows and correlated Tempo spans."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

from ..collectors import _kube
from ..collectors.tempo import collect_tempo
from ..config import get_config
from .common import bounded_window, error_result, evidence_ref, resolve_pod_scope

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


MAX_RECORDS = 50
MAX_TRACES = 5
_GENERIC_ERROR_STATUSES = {
    "abnormal",
    "error",
    "failed",
    "failure",
    "non_ok",
}
_RESPONSE_STATUS_ALIASES = {
    "ok": 0,
    "normal": 0,
    "success": 0,
    "generic_error": 1,
    "protocol_error": 1,
    "not_exist": 2,
    "missing": 2,
    "no_response": 2,
    "server_error": 3,
    "server_failure": 3,
    "client_error": 4,
    "client_failure": 4,
    "timeout": 5,
}


def build_deepflow_sql(
    *,
    database: str,
    pod_ip: str,
    start: str,
    end: str,
    direction: str,
    protocols: list[str],
    response_codes: list[int],
    response_statuses: list[str],
    min_duration_us: int | None,
    peer_ip: str | None,
    trace_id: str | None,
    request_resource: str | None,
    max_records: int,
) -> str:
    database = _safe_identifier(database)
    pod_ip = _safe_ip(pod_ip)
    conditions = [
        f"flow.time >= toDateTime('{_clickhouse_datetime(start)}', 'UTC')",
        f"flow.time <= toDateTime('{_clickhouse_datetime(end)}', 'UTC')",
    ]
    if direction == "inbound":
        conditions.append(f"ip4_1 = '{pod_ip}'")
    elif direction == "outbound":
        conditions.append(f"ip4_0 = '{pod_ip}'")
    else:
        conditions.append(f"(ip4_0 = '{pod_ip}' OR ip4_1 = '{pod_ip}')")
    if protocols:
        values = ",".join(f"'{_sql_string(value.upper())}'" for value in protocols)
        conditions.append(f"l7_protocol_str IN ({values})")
    if response_codes:
        values = ",".join(str(int(value)) for value in response_codes)
        conditions.append(f"response_code IN ({values})")
    if response_statuses:
        status_condition = _response_status_condition(response_statuses)
        if status_condition:
            conditions.append(status_condition)
    if min_duration_us is not None:
        conditions.append(f"response_duration >= {int(min_duration_us)}")
    if peer_ip:
        peer_ip = _safe_ip(peer_ip)
        if direction == "inbound":
            conditions.append(f"ip4_0 = '{peer_ip}'")
        elif direction == "outbound":
            conditions.append(f"ip4_1 = '{peer_ip}'")
        else:
            conditions.append(f"(ip4_0 = '{peer_ip}' OR ip4_1 = '{peer_ip}')")
    if trace_id:
        conditions.append(f"trace_id = '{_safe_trace_id(trace_id)}'")
    if request_resource:
        conditions.append(
            "positionCaseInsensitive(request_resource, "
            f"'{_sql_string(request_resource)}') > 0"
        )
    where = " AND ".join(conditions)
    return (
        "SELECT toString(flow.time) AS time, "
        "IPv4NumToString(ip4_0) AS src_ip, "
        "IPv4NumToString(ip4_1) AS dst_ip, "
        "l7_protocol_str, request_type, request_resource, "
        "response_code, response_status, response_duration, "
        "trace_id, span_id, "
        "toString(syscall_trace_id_request) AS syscall_trace_id_request, "
        "toString(syscall_trace_id_response) AS syscall_trace_id_response "
        f"FROM {database}.l7_flow_log AS flow WHERE {where} "
        "ORDER BY (trace_id != '') DESC, response_duration DESC, flow.time DESC "
        f"LIMIT {int(max_records)} FORMAT JSON"
    )


def execute(arguments: dict[str, Any]) -> dict[str, Any]:
    namespace = str(arguments.get("namespace") or "").strip()
    pod = str(arguments.get("pod") or "").strip()
    purpose = str(arguments.get("purpose") or "").strip()
    if not namespace or not pod or not purpose:
        return error_result(
            source_system="deepflow+tempo",
            dimension="tracing",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="invalid_arguments",
            message="namespace, pod and purpose are required",
        )
    entity, entity_error = resolve_pod_scope(
        namespace,
        pod,
        arguments.get("pod_uid"),
    )
    if entity_error:
        return error_result(
            source_system="deepflow+tempo",
            dimension="tracing",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code=entity_error["code"],
            message=entity_error["message"],
        )
    assert entity is not None
    window, window_error = bounded_window(arguments)
    if window_error:
        return error_result(
            source_system="deepflow+tempo",
            dimension="tracing",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code=window_error["code"],
            message=window_error["message"],
            entity=entity,
        )
    assert window is not None

    direction = str(arguments.get("direction") or "any").strip().lower()
    if direction not in {"any", "inbound", "outbound"}:
        return error_result(
            source_system="deepflow+tempo",
            dimension="tracing",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="invalid_direction",
            message="direction must be any, inbound or outbound",
            entity=entity,
        )
    protocols = _bounded_strings(arguments.get("protocols"), 6)
    response_statuses = _bounded_strings(arguments.get("response_statuses"), 6)
    response_codes = [
        int(value)
        for value in (arguments.get("response_codes") or [])[:12]
    ]
    trace_id = str(arguments.get("trace_id") or "").strip() or None
    if trace_id:
        try:
            trace_id = _safe_trace_id(trace_id)
        except ValueError as exc:
            return error_result(
                source_system="deepflow+tempo",
                dimension="tracing",
                namespace=namespace,
                pod=pod,
                purpose=purpose,
                code="invalid_trace_id",
                message=str(exc),
                entity=entity,
            )
    peer_ip = str(arguments.get("peer_ip") or "").strip() or None
    if peer_ip:
        try:
            peer_ip = _safe_ip(peer_ip)
        except ValueError as exc:
            return error_result(
                source_system="deepflow+tempo",
                dimension="tracing",
                namespace=namespace,
                pod=pod,
                purpose=purpose,
                code="invalid_peer_ip",
                message=str(exc),
                entity=entity,
            )
    request_resource = str(arguments.get("request_resource") or "").strip() or None
    if request_resource and len(request_resource) > 200:
        return error_result(
            source_system="deepflow+tempo",
            dimension="tracing",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="request_resource_too_long",
            message="request_resource must be at most 200 characters",
            entity=entity,
        )
    max_records = min(MAX_RECORDS, max(1, int(arguments.get("max_records") or 20)))
    pod_ip = str(entity.get("pod_ip") or "").strip()
    include_tempo = bool(arguments.get("include_tempo", True))
    service = str(arguments.get("service") or "").strip() or None
    if not pod_ip:
        tempo_result = _tempo_not_executed(
            "include_tempo=false"
            if not include_tempo
            else "no_valid_trace_ids"
        )
        tempo_trace_ids = [trace_id] if trace_id and include_tempo else []
        if tempo_trace_ids:
            tempo_result = _query_tempo(
                namespace=namespace,
                pod=pod,
                trace_ids=tempo_trace_ids,
                service=service,
            )
        spans = _dedupe_records(tempo_result.get("spans") or [])
        tempo_coverage = str(tempo_result.get("coverage") or "empty")
        tempo_error = str(tempo_result.get("error") or "").strip() or None
        telemetry = {
            "deepflow": {
                "coverage": "absent",
                "error": "pod_ip is empty",
            },
            "tempo": {
                "coverage": tempo_coverage,
                "executed": bool(tempo_result.get("executed")),
                "query_meta": tempo_result.get("query_meta") or {},
                **(
                    {"reason": tempo_result.get("reason")}
                    if tempo_result.get("reason")
                    else {}
                ),
                **({"error": tempo_error} if tempo_error else {}),
            },
        }
        limitations = [
            (
                "Pod IP is empty; Tempo spans were queried by the explicitly "
                "supplied trace ID, but no Pod-scoped DeepFlow flow association "
                "can be verified"
                if trace_id
                else
                "pod_ip is empty; Pod-scoped DeepFlow flow lookup cannot be executed"
            )
        ]
        if tempo_error:
            limitations.append(tempo_error)
        if tempo_coverage == "error":
            result = error_result(
                source_system="deepflow+tempo",
                dimension="tracing",
                namespace=namespace,
                pod=pod,
                purpose=purpose,
                code="tempo_error",
                message=tempo_error or "Tempo query failed",
                entity=entity,
                query={
                    "sql": None,
                    "time_range": window,
                    "pod_ip": None,
                    "tempo_trace_ids": tempo_trace_ids,
                    "tempo": tempo_result.get("query_meta") or {},
                },
            )
            result.update(
                {
                    "directness": "related_context",
                    "telemetry": telemetry,
                    "limitations": limitations,
                    "flows": [],
                    "spans": [],
                    "correlations": [],
                }
            )
            return result
        facts = _facts(
            namespace,
            pod,
            [],
            spans,
            span_directness="related_context",
            pod_uid=str(entity.get("pod_uid") or ""),
        )
        return {
            "ok": True,
            "status": "query_succeeded",
            "source_system": "deepflow+tempo",
            "dimension": "tracing",
            "entity": entity,
            "purpose": purpose,
            "coverage": (
                "present"
                if spans
                else (tempo_coverage if trace_id else "empty")
            ),
            "directness": "related_context" if trace_id else "direct",
            "query": {
                "sql": None,
                "time_range": window,
                "pod_ip": None,
                "tempo_trace_ids": tempo_trace_ids,
                "tempo": tempo_result.get("query_meta") or {},
            },
            "facts": facts,
            "samples": [{"type": "span", **span} for span in spans],
            "flows": [],
            "spans": spans,
            "correlations": [],
            "evidence_refs": [fact["ref"] for fact in facts],
            "counts": {
                "matched": len(spans),
                "retrieved": len(spans),
                "normalized": len(spans),
                "returned": len(spans),
                "dropped": 0,
                "flow_count": 0,
                "span_count": len(spans),
            },
            "truncation": {"truncated": False, "stages": []},
            "telemetry": telemetry,
            "limitations": limitations,
            "truncated": False,
            "limits": {"max_records": MAX_RECORDS, "max_traces": MAX_TRACES},
        }

    try:
        min_duration_us = (
            int(arguments["min_duration_us"])
            if arguments.get("min_duration_us") is not None
            else None
        )
        sql = build_deepflow_sql(
            database=get_config().deepflow_clickhouse_database,
            pod_ip=pod_ip,
            start=window["start"],
            end=window["end"],
            direction=direction,
            protocols=protocols,
            response_codes=response_codes,
            response_statuses=response_statuses,
            min_duration_us=min_duration_us,
            peer_ip=peer_ip,
            trace_id=trace_id,
            request_resource=request_resource,
            max_records=max_records,
        )
    except (TypeError, ValueError) as exc:
        return error_result(
            source_system="deepflow+tempo",
            dimension="tracing",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="invalid_trace_filter",
            message=str(exc),
            entity=entity,
        )
    payload = _query_deepflow(sql)
    if payload.get("error"):
        return error_result(
            source_system="deepflow+tempo",
            dimension="tracing",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code="deepflow_error",
            message=str(payload.get("error")),
            entity=entity,
            query={"sql": sql, "time_range": window},
        )
    filtered_rows = (
        payload.get("data")
        if isinstance(payload.get("data"), list)
        else []
    )
    rows = filtered_rows
    baseline_sql: str | None = None
    baseline_error: str | None = None
    baseline_attempted = False
    semantic_filters_present = bool(
        protocols
        or response_codes
        or response_statuses
        or min_duration_us is not None
        or peer_ip
        or request_resource
    )
    if (
        not filtered_rows
        and semantic_filters_present
        and not trace_id
    ):
        baseline_attempted = True
        baseline_sql = build_deepflow_sql(
            database=get_config().deepflow_clickhouse_database,
            pod_ip=pod_ip,
            start=window["start"],
            end=window["end"],
            direction="any",
            protocols=[],
            response_codes=[],
            response_statuses=[],
            min_duration_us=None,
            peer_ip=None,
            trace_id=None,
            request_resource=None,
            max_records=min(8, max_records),
        )
        baseline_payload = _query_deepflow(baseline_sql)
        if baseline_payload.get("error"):
            baseline_error = str(baseline_payload.get("error"))
        elif isinstance(baseline_payload.get("data"), list):
            rows = baseline_payload.get("data") or []

    related_baseline = baseline_attempted and bool(rows)
    flows = _dedupe_records(
        [_flow(row) for row in rows[:max_records] if isinstance(row, dict)]
    )
    trace_ids, invalid_trace_id_count = _validated_trace_ids(
        ([trace_id] if trace_id else [])
        + [
            str(flow.get("trace_id") or "")
            for flow in flows
            if flow.get("trace_id")
        ]
    )
    tempo_result = _tempo_not_executed(
        "include_tempo=false"
        if not include_tempo
        else "no_valid_trace_ids"
    )
    if include_tempo and trace_ids:
        tempo_result = _query_tempo(
            namespace=namespace,
            pod=pod,
            trace_ids=trace_ids,
            service=service,
        )
    spans = _dedupe_records(tempo_result.get("spans") or [])
    tempo_error = str(tempo_result.get("error") or "").strip() or None
    telemetry = {
        "deepflow": {
            "coverage": "present" if flows else "empty",
            "directness": (
                "related_context" if related_baseline else "direct"
            ),
        },
        "tempo": {
            "coverage": str(tempo_result.get("coverage") or "empty"),
            "executed": bool(tempo_result.get("executed")),
            "query_meta": tempo_result.get("query_meta") or {},
            **(
                {"reason": tempo_result.get("reason")}
                if tempo_result.get("reason")
                else {}
            ),
            **({"error": tempo_error} if tempo_error else {}),
        },
    }
    correlations = _correlations(flows, spans)
    samples = [
        {"type": "flow", **flow}
        for flow in flows
    ] + [
        {"type": "span", **span}
        for span in spans
    ]
    facts = _facts(
        namespace,
        pod,
        flows,
        spans,
        span_directness=(
            "related_context" if related_baseline else "direct"
        ),
        flow_directness=(
            "related_context" if related_baseline else "direct"
        ),
        pod_uid=str(entity.get("pod_uid") or ""),
    )
    tempo_coverage = str(tempo_result.get("coverage") or "empty")
    overall_coverage = "present" if flows or spans else "empty"
    if related_baseline and (flows or spans):
        overall_coverage = "weak"
    elif tempo_coverage == "weak":
        overall_coverage = "weak"
    elif tempo_coverage == "error":
        overall_coverage = "weak" if flows or spans else "error"
    limitations: list[str] = []
    if related_baseline:
        limitations.append(
            "The model-selected trace filters matched no flows; bounded "
            "Pod-scoped baseline flows and spans are related context and do "
            "not satisfy the original predicate"
        )
    elif baseline_attempted:
        limitations.append(
            "The model-selected trace filters matched no flows and the "
            "bounded Pod-scoped baseline query also returned no flows"
        )
    if baseline_error:
        limitations.append(
            f"Pod-scoped baseline query failed: {baseline_error}"
        )
    if tempo_error:
        limitations.append(tempo_error)
    source_limit = min(8, max_records) if baseline_attempted else max_records
    source_limit_possible = len(rows) >= source_limit
    tracing_truncated = source_limit_possible or len(rows) > len(flows)
    truncation_stages = []
    if source_limit_possible:
        truncation_stages.append(
            {
                "stage": "backend_query",
                "reason": "sql_limit_reached_possible_more_rows",
                "before": None,
                "after": len(rows),
            }
        )
    if len(rows) > len(flows):
        truncation_stages.append(
            {
                "stage": "normalization",
                "reason": "deduplicated",
                "before": len(rows),
                "after": len(flows),
            }
        )
    return {
        "ok": overall_coverage != "error",
        "status": (
            "query_partial"
            if baseline_attempted and overall_coverage != "error"
            else "query_succeeded"
            if overall_coverage != "error"
            else "query_rejected"
        ),
        "source_system": "deepflow+tempo",
        "dimension": "tracing",
        "entity": entity,
        "purpose": purpose,
        "coverage": overall_coverage,
        "directness": (
            "related_context" if related_baseline else "direct"
        ),
        "query": {
            "sql": sql,
            "filtered_match_count": len(filtered_rows),
            **(
                {
                    "baseline_sql": baseline_sql,
                    "baseline_match_count": len(rows),
                }
                if baseline_attempted
                else {}
            ),
            "time_range": window,
            "pod_ip": pod_ip,
            "direction": direction,
            "tempo_trace_ids": trace_ids,
            "invalid_tempo_trace_id_count": invalid_trace_id_count,
            "tempo": tempo_result.get("query_meta") or {},
        },
        "facts": facts,
        "samples": samples,
        "flows": flows,
        "spans": spans,
        "correlations": correlations,
        "telemetry": telemetry,
        "limitations": limitations,
        "evidence_refs": [fact["ref"] for fact in facts],
        "counts": {
            "matched": None,
            "retrieved": len(rows) + len(spans),
            "normalized": len(flows) + len(spans),
            "returned": len(facts),
            "dropped": max(0, len(rows) + len(spans) - len(facts)),
            "flow_count": len(flows),
            "span_count": len(spans),
        },
        "truncation": {
            "truncated": tracing_truncated,
            "stages": truncation_stages,
        },
        "truncated": tracing_truncated,
        "limits": {
            "max_records": MAX_RECORDS,
            "requested_max_records": max_records,
            "max_traces": MAX_TRACES,
        },
    }


def _query_deepflow(sql: str) -> dict[str, Any]:
    cfg = get_config()
    if cfg.deepflow_clickhouse_url:
        if requests is None:
            return {"error": "requests is not installed"}
        try:
            response = requests.post(
                cfg.deepflow_clickhouse_url,
                data=sql.encode("utf-8"),
                timeout=cfg.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {"error": "invalid ClickHouse payload"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
    if not cfg.clickhouse_exec_enabled:
        return {"error": "DeepFlow ClickHouse is not configured"}
    clickhouse_pod = cfg.clickhouse_exec_pod or _kube.find_pod(
        cfg.clickhouse_exec_namespace,
        cfg.clickhouse_exec_selector,
    )
    if not clickhouse_pod:
        return {"error": "DeepFlow ClickHouse Pod is unavailable"}
    ok, out, error = _kube.exec_in_pod(
        cfg.clickhouse_exec_namespace,
        clickhouse_pod,
        ["clickhouse-client", "--query", sql],
        timeout=cfg.timeout_seconds,
    )
    if not ok:
        return {"error": error or out}
    try:
        payload = json.loads(out) if out.strip() else {"data": []}
    except json.JSONDecodeError as exc:
        return {"error": f"invalid ClickHouse JSON: {exc}"}
    return payload if isinstance(payload, dict) else {"error": "invalid ClickHouse payload"}


def _query_tempo(
    *,
    namespace: str,
    pod: str,
    trace_ids: list[str],
    service: str | None,
) -> dict[str, Any]:
    result = collect_tempo(
        namespace=namespace,
        pod=pod,
        trace_ids=trace_ids,
        timeout=get_config().timeout_seconds,
        max_traces=MAX_TRACES,
    )
    spans: list[dict[str, Any]] = []
    unfiltered_span_count = 0
    for evidence in result.evidence:
        for span in evidence.payload.get("spans") or []:
            if not isinstance(span, dict):
                continue
            unfiltered_span_count += 1
            if service and str(span.get("service") or "") != service:
                continue
            spans.append(span)
    coverage = result.coverage
    error = result.error
    filtered_out_count = max(0, unfiltered_span_count - len(spans))
    if coverage == "present" and not spans:
        coverage = "empty"
        if service and filtered_out_count:
            error = (
                f"Tempo returned spans, but none matched service {service}"
            )
    return {
        "coverage": coverage,
        "spans": _dedupe_records(spans)[:8],
        "query_meta": result.query_meta,
        "error": error,
        "executed": True,
        "filtered_out_count": filtered_out_count,
    }


def _flow(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": row.get("time"),
        "src_ip": row.get("src_ip"),
        "dst_ip": row.get("dst_ip"),
        "protocol": row.get("l7_protocol_str"),
        "request_type": row.get("request_type"),
        "request_resource": row.get("request_resource"),
        "response_code": row.get("response_code"),
        "response_status": row.get("response_status"),
        "duration_us": row.get("response_duration"),
        "trace_id": row.get("trace_id") or "",
        "span_id": row.get("span_id") or "",
        "syscall_trace_id_request": row.get("syscall_trace_id_request") or "0",
        "syscall_trace_id_response": row.get("syscall_trace_id_response") or "0",
    }


def _correlations(
    flows: list[dict[str, Any]],
    spans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    span_ids = {
        trace_id
        for item in spans
        if (trace_id := _valid_trace_id(item.get("trace_id")))
    }
    correlated_ids: list[str] = []
    for item in flows:
        trace_id = _valid_trace_id(item.get("trace_id"))
        if (
            trace_id
            and trace_id in span_ids
            and trace_id not in correlated_ids
        ):
            correlated_ids.append(trace_id)
    return [
        {
            "trace_id": trace_id,
            "flow_count": sum(1 for item in flows if item.get("trace_id") == trace_id),
            "span_count": sum(1 for item in spans if item.get("trace_id") == trace_id),
            "basis": "exact_trace_id",
        }
        for trace_id in correlated_ids
    ]


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        key = json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(record)
    return deduplicated


def _tempo_not_executed(reason: str) -> dict[str, Any]:
    return {
        "coverage": "absent",
        "spans": [],
        "query_meta": {},
        "error": None,
        "executed": False,
        "reason": reason,
        "filtered_out_count": 0,
    }


def _facts(
    namespace: str,
    pod: str,
    flows: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    *,
    span_directness: str = "direct",
    flow_directness: str = "direct",
    pod_uid: str = "",
) -> list[dict[str, Any]]:
    flow_facts: list[dict[str, Any]] = []
    for flow in flows:
        ref = evidence_ref("deepflow", namespace, pod, flow)
        flow_facts.append(
            {
                "ref": ref,
                "source_system": "deepflow",
                "dimension": "tracing",
                "entity": f"{namespace}/{pod}",
                "observed_at": flow.get("timestamp"),
                "name": "l7_flow",
                "value": flow,
                "trace_id": flow.get("trace_id"),
                "directness": flow_directness,
            }
        )
    span_facts: list[dict[str, Any]] = []
    for span in spans:
        attributes = (
            span.get("attributes")
            if isinstance(span.get("attributes"), dict)
            else {}
        )
        observed_pod = str(
            attributes.get("k8s.pod.name")
            or attributes.get("k8s.pod.name")
            or ""
        )
        observed_uid = str(
            attributes.get("k8s.pod.uid")
            or attributes.get("k8s.pod.uid")
            or ""
        )
        directness = span_directness
        if span_directness == "direct":
            matches_scope = (
                bool(pod_uid and observed_uid == pod_uid)
                if observed_uid
                else bool(observed_pod and observed_pod == pod)
            )
            directness = "direct" if matches_scope else "related_context"
        ref = evidence_ref("tempo", namespace, pod, span)
        span_facts.append(
            {
                "ref": ref,
                "source_system": "tempo",
                "dimension": "tracing",
                "entity": f"{namespace}/{pod}",
                "pod_uid": observed_uid or None,
                "observed_at": span.get("start"),
                "name": "application_span",
                "value": span,
                "trace_id": span.get("trace_id"),
                "span_id": span.get("span_id"),
                "directness": directness,
            }
        )
    facts: list[dict[str, Any]] = []
    used_flow_indexes: set[int] = set()
    used_span_indexes: set[int] = set()
    for flow_index, flow_fact in enumerate(flow_facts):
        trace_id = _valid_trace_id(flow_fact.get("trace_id"))
        if not trace_id:
            continue
        for span_index, span_fact in enumerate(span_facts):
            if span_index in used_span_indexes:
                continue
            if _valid_trace_id(span_fact.get("trace_id")) != trace_id:
                continue
            facts.extend((flow_fact, span_fact))
            used_flow_indexes.add(flow_index)
            used_span_indexes.add(span_index)
            break

    remaining_flows = [
        fact
        for index, fact in enumerate(flow_facts)
        if index not in used_flow_indexes
    ]
    remaining_spans = [
        fact
        for index, fact in enumerate(span_facts)
        if index not in used_span_indexes
    ]
    for index in range(max(len(remaining_flows), len(remaining_spans))):
        if index < len(remaining_flows):
            facts.append(remaining_flows[index])
        if index < len(remaining_spans):
            facts.append(remaining_spans[index])
    return facts


def _safe_identifier(value: str) -> str:
    text = str(value or "flow_log")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        raise ValueError("invalid ClickHouse database identifier")
    return text


def _safe_ip(value: str) -> str:
    return str(ipaddress.ip_address(str(value or "").strip()))


def _safe_trace_id(value: str) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"(?:[0-9a-f]{16}|[0-9a-f]{32})", text):
        raise ValueError("trace_id must be 16 or 32 hexadecimal characters")
    return text


def _valid_trace_id(value: Any) -> str | None:
    try:
        return _safe_trace_id(str(value or ""))
    except ValueError:
        return None


def _validated_trace_ids(values: list[Any]) -> tuple[list[str], int]:
    trace_ids: list[str] = []
    invalid_count = 0
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        trace_id = _valid_trace_id(text)
        if not trace_id:
            invalid_count += 1
            continue
        if trace_id not in trace_ids:
            trace_ids.append(trace_id)
        if len(trace_ids) >= MAX_TRACES:
            break
    return trace_ids, invalid_count


def _sql_string(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _clickhouse_datetime(value: str) -> str:
    normalized = str(value).strip().replace("Z", "").replace("T", " ")
    if "." in normalized:
        normalized = normalized.split(".", 1)[0]
    if "+" in normalized:
        normalized = normalized.split("+", 1)[0]
    return normalized


def _bounded_strings(value: Any, limit: int) -> list[str]:
    items = [
        str(item).strip()
        for item in value or []
        if str(item).strip()
    ]
    if len(items) > limit:
        raise ValueError(f"filter accepts at most {limit} values")
    return items


def _response_status_condition(values: list[str]) -> str | None:
    numeric_statuses: set[int] = set()
    include_non_ok = False
    for value in values:
        normalized = re.sub(
            r"[\s-]+",
            "_",
            str(value or "").strip().lower(),
        )
        if not normalized:
            continue
        if normalized in _GENERIC_ERROR_STATUSES:
            include_non_ok = True
            continue
        if normalized.isdigit():
            status = int(normalized)
            if status > 255:
                raise ValueError(
                    f"unsupported response_status {value!r}; numeric values must fit UInt8"
                )
            numeric_statuses.add(status)
            continue
        if normalized not in _RESPONSE_STATUS_ALIASES:
            raise ValueError(
                f"unsupported response_status {value!r}; use ok, error, "
                "not_exist, server_error, client_error, timeout, or a UInt8 value"
            )
        numeric_statuses.add(_RESPONSE_STATUS_ALIASES[normalized])

    if include_non_ok:
        if 0 in numeric_statuses:
            return None
        return "response_status != 0"
    if not numeric_statuses:
        return None
    encoded = ",".join(str(status) for status in sorted(numeric_statuses))
    return f"response_status IN ({encoded})"
