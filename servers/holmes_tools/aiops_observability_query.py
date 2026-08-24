"""Generic, model-directed Pod observability MCP tools."""

from __future__ import annotations

from typing import Any, Callable

from mcp.types import Tool

from servers.aiops_observability.query import logs, metrics, topology, tracing
from servers.aiops_observability.query.common import (
    MAX_INPUT_BYTES,
    dump_bounded_result,
    error_result,
    input_exceeds_budget,
    query_descriptor,
)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


_ENTITY = {
    "namespace": {
        "type": "string",
        "minLength": 1,
        "maxLength": 63,
        "description": "Exact Kubernetes namespace of the target Pod.",
    },
    "pod": {
        "type": "string",
        "minLength": 1,
        "maxLength": 253,
        "description": "Exact Kubernetes Pod name.",
    },
    "pod_uid": {
        "type": "string",
        "maxLength": 128,
        "description": "Optional expected Pod UID used to reject lifecycle/name reuse.",
    },
    "purpose": {
        "type": "string",
        "minLength": 1,
        "maxLength": 300,
        "description": (
            "The diagnostic question this query should verify or rule out. "
            "Use a new purpose when performing a follow-up query."
        ),
    },
}
_WINDOW = {
    "start": {
        "type": "string",
        "maxLength": 64,
        "description": "RFC3339 range start.",
    },
    "end": {
        "type": "string",
        "maxLength": 64,
        "description": "RFC3339 range end.",
    },
    "window_minutes": {
        "type": "integer",
        "minimum": 1,
        "maximum": 120,
        "default": 30,
        "description": "Fallback bounded window ending now; maximum 120 minutes.",
    },
}


TOOLS = [
    Tool(
        name="execute_pod_promql",
        description=(
            "Execute one PromQL expression selected by the model for an exact Kubernetes Pod. "
            "The model chooses metrics from the current diagnostic question; the tool never "
            "routes by fault type. Every vector selector must contain exact namespace and pod "
            "matchers. Returns every series retained by the declared query safety limits, with "
            "real values, labels, statistics, stage counts and explicit truncation metadata. "
            "Iterate before concluding: if an instant query returns only a single point "
            "(trend_evaluable=false) but your purpose needs growth/trend evidence, call again "
            "with query_type=range and a window covering the anomaly (e.g. lookback_minutes "
            "30-60, step 30s-1m). If the result is empty, first re-check metric name and label "
            "matchers, then state a new purpose and retry once with corrected parameters."
        ),
        inputSchema=_schema(
            {
                **_ENTITY,
                "promql": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": (
                        "Model-selected PromQL. Every vector selector must use exact "
                        'namespace="<namespace>" and pod="<pod>" matchers.'
                    ),
                },
                "query_type": {
                    "type": "string",
                    "enum": ["instant", "range"],
                },
                **_WINDOW,
                "step": {
                    "type": "string",
                    "maxLength": 32,
                    "default": "30s",
                    "description": "Range query step such as 30s.",
                },
            },
            ["namespace", "pod", "purpose", "promql", "query_type"],
        ),
    ),
    Tool(
        name="query_pod_logs",
        description=(
            "Query real Elasticsearch/Filebeat logs for one Kubernetes Pod using structured "
            "filters selected by the model. The tool builds safe DSL, prefers Pod UID identity, "
            "and never chooses keywords by fault type. Returns every record within max_records, "
            "including complete log text, raw references, stage counts and truncation metadata. "
            "Iterate before concluding: compare the returned lines with your purpose — if they "
            "are empty, truncated or not decisive (e.g. missing the crash window), call again "
            "with an adjusted time window, container, keywords, levels or trace_id instead of "
            "declaring logging absent. A genuinely empty result after a reasonable retry is "
            "itself valid evidence; report it honestly."
        ),
        inputSchema=_schema(
            {
                **_ENTITY,
                **_WINDOW,
                "container": {"type": "string", "maxLength": 253},
                "keywords": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string", "maxLength": 120},
                },
                "keyword_mode": {
                    "type": "string",
                    "enum": ["any", "all", "phrase"],
                    "default": "any",
                },
                "levels": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {"type": "string", "maxLength": 32},
                },
                "trace_id": {"type": "string", "maxLength": 32},
                "fields": {
                    "type": "array",
                    "maxItems": 12,
                    "items": {"type": "string", "maxLength": 128},
                },
                "max_records": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                },
                "allow_name_fallback": {
                    "type": "boolean",
                    "default": True,
                },
            },
            ["namespace", "pod", "purpose"],
        ),
    ),
    Tool(
        name="query_pod_tracing",
        description=(
            "Query real Pod-scoped DeepFlow L7 flows and optional Tempo application spans using "
            "structured filters selected by the model. DeepFlow queries always preserve Pod IP "
            "scope. A valid explicit trace ID may query Tempo without a Pod IP, marked as related "
            "context because flow association cannot be verified. The tool keeps network flows "
            "separate from spans, returns all records within the selected limits, and never routes "
            "by fault type. Response serialization does not discard flows or spans. "
            "Iterate before concluding: if flows are too broad, noisy or empty for your purpose, "
            "refine with direction, protocols, response_codes, min_duration_us, request_resource "
            "or a narrower window and query again. When a decisive trace_id appears in flows or "
            "logs, follow it with a targeted call to correlate application spans."
        ),
        inputSchema=_schema(
            {
                **_ENTITY,
                **_WINDOW,
                "direction": {
                    "type": "string",
                    "enum": ["any", "inbound", "outbound"],
                    "default": "any",
                },
                "protocols": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {"type": "string", "maxLength": 32},
                },
                "response_codes": {
                    "type": "array",
                    "maxItems": 12,
                    "items": {"type": "integer"},
                },
                "response_statuses": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {"type": "string", "maxLength": 32},
                },
                "min_duration_us": {"type": "integer", "minimum": 0},
                "peer_ip": {"type": "string", "maxLength": 64},
                "trace_id": {"type": "string", "maxLength": 32},
                "request_resource": {"type": "string", "maxLength": 200},
                "service": {"type": "string", "maxLength": 200},
                "include_tempo": {
                    "type": "boolean",
                    "default": True,
                },
                "max_records": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                },
            },
            ["namespace", "pod", "purpose"],
        ),
    ),
    Tool(
        name="query_pod_topology",
        description=(
            "Query source-backed Kubernetes relationships for one exact Pod. "
            "The model receives source-bounded owner, Service selector, node scheduling, "
            "and container membership facts derived from live Kubernetes objects. "
            "The tool never infers relationships from names or routes by fault type. "
            "One call per Pod is usually enough; use the returned owner/service/node "
            "facts to decide which entity to investigate next with the other tools."
        ),
        inputSchema=_schema(
            {
                **_ENTITY,
                **_WINDOW,
            },
            ["namespace", "pod", "purpose"],
        ),
    ),
]


_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "execute_pod_promql": metrics.execute,
    "query_pod_logs": logs.execute,
    "query_pod_tracing": tracing.execute,
    "query_pod_topology": topology.execute,
}

_TOOL_RESULT_SCOPE = {
    "execute_pod_promql": ("prometheus", "metrics"),
    "query_pod_logs": ("elasticsearch", "logging"),
    "query_pod_tracing": ("deepflow+tempo", "tracing"),
    "query_pod_topology": ("kubernetes", "topology"),
}


def call_tool(name: str, arguments: dict[str, Any]) -> str | None:
    handler = _HANDLERS.get(name)
    if not handler:
        return None
    safe_arguments = arguments or {}
    if input_exceeds_budget(safe_arguments):
        source_system, dimension = _TOOL_RESULT_SCOPE[name]
        return dump_bounded_result(
            error_result(
                source_system=source_system,
                dimension=dimension,
                namespace=str(safe_arguments.get("namespace") or ""),
                pod=str(safe_arguments.get("pod") or ""),
                purpose=str(safe_arguments.get("purpose") or ""),
                code="input_budget_exceeded",
                message=(
                    "serialized tool input exceeds the "
                    f"{MAX_INPUT_BYTES}-byte execution budget"
                ),
                query=query_descriptor(safe_arguments, executed=False),
            )
        )
    return dump_bounded_result(handler(safe_arguments))


def tool_error_result(
    name: str,
    arguments: dict[str, Any],
    *,
    code: str,
    message: str,
) -> str:
    source_system, dimension = _TOOL_RESULT_SCOPE.get(
        name,
        ("aiops-observability-query", "unknown"),
    )
    return dump_bounded_result(
        error_result(
            source_system=source_system,
            dimension=dimension,
            namespace=str(arguments.get("namespace") or ""),
            pod=str(arguments.get("pod") or ""),
            purpose=str(arguments.get("purpose") or ""),
            code=code,
            message=message,
        )
    )
