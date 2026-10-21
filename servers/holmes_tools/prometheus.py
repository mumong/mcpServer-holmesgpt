"""
prometheus 工具集：查询 Prometheus API。
对应 Holmes 的 prometheus/metrics。配置：PROMETHEUS_URL（默认 http://localhost:9090/）。

重要：Prometheus API 的 POST 接口使用 form-encoded body（data=），不是 JSON body（json=）。
Holmes 原始实现使用 requests.request(method="POST", data=payload)，此处保持一致。
"""

import json
import os
import time
from http.cookiejar import DefaultCookiePolicy
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

import cluster_scope
from mcp.types import Tool

try:
    from .mcp_logger import get_logger, log_http_request
except ImportError:
    from mcp_logger import get_logger, log_http_request

try:
    import requests
except ImportError:
    requests = None


def _new_session():
    """Pooled connections without carrying cookies between calls (same
    semantics as bare requests.get/post). Mirrors aiops_observability._http,
    which this stdio server cannot import (project root is not on sys.path)."""
    session = requests.Session()
    session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return session


_SESSION = _new_session() if requests is not None else None

_logger = get_logger("prometheus")

QUERY_ONLY_BOUNDARY = (
    " AIOps boundary: use only in /query for general node/cluster Prometheus "
    "queries; do not use in /ask Pod diagnosis, where execute_pod_promql is required."
)


def _query_only_description(capability: str) -> str:
    return capability + QUERY_ONLY_BOUNDARY


def _prometheus_http_error_payload(
    *,
    response_text: str,
    url: str,
    params_or_data: Optional[Dict],
    error: Exception,
) -> str:
    """Preserve Prometheus API error details for the LLM/tool caller."""
    try:
        payload = json.loads(response_text or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    payload.setdefault("status", "error")
    payload["http_error"] = str(error)
    payload["url"] = url
    status_code = getattr(getattr(error, "response", None), "status_code", None)
    if status_code:
        payload["http_status"] = status_code

    if isinstance(params_or_data, dict):
        query = params_or_data.get("query")
        if query:
            payload["query"] = query

    if response_text and "raw_response" not in payload:
        payload["raw_response"] = response_text[:2000]

    return json.dumps(payload, ensure_ascii=False)


def _get_prometheus_url() -> str:
    if cluster_scope.backend("metrics") == "victoriametrics":
        u = cluster_scope.prometheus_url()
    else:
        u = os.environ.get("PROMETHEUS_URL", "http://localhost:9090/").strip()
    return u if u.endswith("/") else u + "/"


def _get_timeout() -> int:
    return int(os.environ.get("PROMETHEUS_TIMEOUT", "30"))


def _do_get(path: str, params: Optional[Dict] = None) -> str:
    if requests is None:
        return json.dumps({"error": "install 'requests' to use Prometheus tools."})
    url = urljoin(_get_prometheus_url(), path)
    t0 = time.monotonic()
    try:
        params = {**(params or {}), **cluster_scope.prometheus_params()}
        r = _SESSION.get(url, params=params, timeout=_get_timeout())
        elapsed = time.monotonic() - t0
        log_http_request(
            "GET",
            url,
            params_or_data=params,
            status_code=r.status_code,
            response_text=r.text,
            elapsed=elapsed,
        )
        r.raise_for_status()
        return r.text
    except Exception as e:
        elapsed = time.monotonic() - t0
        response_text = getattr(getattr(e, "response", None), "text", "") or ""
        log_http_request(
            "GET",
            url,
            params_or_data=params,
            status_code=getattr(e, "response", {}) and getattr(e.response, "status_code", None),
            response_text=response_text or str(e),
            elapsed=elapsed,
        )
        return _prometheus_http_error_payload(
            response_text=response_text,
            url=url,
            params_or_data=params,
            error=e,
        )


def _do_post(path: str, data: Optional[Dict] = None) -> str:
    """
    POST 请求 Prometheus API。
    注意：Prometheus 的 POST 接口（如 /api/v1/query, /api/v1/query_range）
    接受 form-encoded body（data=），不是 JSON body（json=）。
    Holmes 原始实现：requests.request(method="POST", data=payload)
    """
    if requests is None:
        return json.dumps({"error": "install 'requests' to use Prometheus tools."})
    url = urljoin(_get_prometheus_url(), path)
    t0 = time.monotonic()
    try:
        # ⚠️ 关键修复：使用 data= 而非 json=，Prometheus API 要求 form-encoded POST
        data = {**(data or {}), **cluster_scope.prometheus_params()}
        r = _SESSION.post(url, data=data, timeout=_get_timeout())
        elapsed = time.monotonic() - t0
        log_http_request(
            "POST", url, params_or_data=data, status_code=r.status_code, response_text=r.text, elapsed=elapsed
        )
        r.raise_for_status()
        return r.text
    except Exception as e:
        elapsed = time.monotonic() - t0
        response_text = getattr(getattr(e, "response", None), "text", "") or ""
        log_http_request(
            "POST",
            url,
            params_or_data=data,
            status_code=getattr(e, "response", {}) and getattr(e.response, "status_code", None),
            response_text=response_text or str(e),
            elapsed=elapsed,
        )
        return _prometheus_http_error_payload(
            response_text=response_text,
            url=url,
            params_or_data=data,
            error=e,
        )


METRIC_NAMES_DEFAULT_LIMIT = 200
METRIC_NAMES_MAX_LIMIT = 2000


def _run_get_metric_names(arguments: dict) -> str:
    """Metric names, optionally narrowed by a case-insensitive substring.

    1,400 names at once were summarised to a 76-name sample downstream and the model
    re-issued the identical call to see more; ``contains`` lets it ask for the family it
    needs (e.g. "node_memory") and ``limit`` keeps any answer bounded.
    """
    raw = _do_get("api/v1/label/__name__/values")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    names = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(names, list):
        return raw
    arguments = arguments or {}
    needle = str(arguments.get("contains") or "").strip().lower()
    if needle:
        names = [n for n in names if needle in str(n).lower()]
    try:
        limit = int(arguments.get("limit") or METRIC_NAMES_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = METRIC_NAMES_DEFAULT_LIMIT
    limit = max(1, min(METRIC_NAMES_MAX_LIMIT, limit))
    shown = names[:limit]
    return json.dumps(
        {
            "status": payload.get("status", "success"),
            "total": len(names),
            "returned": len(shown),
            "truncated": len(names) > len(shown),
            "contains": needle or None,
            "data": shown,
        },
        ensure_ascii=False,
    )


def _run_get_label_values(arguments: dict) -> str:
    label = arguments.get("label_name")
    if not label:
        return json.dumps({"error": "label_name is required"})
    return _do_get(f"api/v1/label/{label}/values")


def _run_get_all_labels(arguments: dict) -> str:
    return _do_get("api/v1/labels")


def _run_get_series(arguments: dict) -> str:
    match = arguments.get("match") or arguments.get("match[]")
    start = arguments.get("start")
    end = arguments.get("end")
    if not match:
        return json.dumps({"error": "match or match[] is required"})
    params = {"match[]": match}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return _do_get("api/v1/series", params=params)


def _run_get_metric_metadata(arguments: dict) -> str:
    metric = arguments.get("metric_name")
    if not metric:
        return _do_get("api/v1/metadata")
    return _do_get("api/v1/metadata", params={"metric": metric})


def _with_query_echo(response_text: str, query: str) -> str:
    """Add the asked expression to a successful payload.

    A bare result vector does not say what it measures; error payloads already
    carry ``query`` and success payloads now do too. Anything that is not a
    success JSON object is returned unchanged.
    """
    try:
        payload = json.loads(response_text)
    except Exception:
        return response_text
    if not isinstance(payload, dict) or payload.get("status") != "success" or "query" in payload:
        return response_text
    payload["query"] = query
    return json.dumps(payload, ensure_ascii=False)


def _run_execute_prometheus_instant_query(arguments: dict) -> str:
    query = arguments.get("query")
    if not query:
        return json.dumps({"error": "query is required"})
    time = arguments.get("time")
    params = {"query": query}
    if time:
        params["time"] = time
    return _with_query_echo(_do_get("api/v1/query", params=params), query)


def _run_execute_prometheus_range_query(arguments: dict) -> str:
    query = arguments.get("query")
    start = arguments.get("start")
    end = arguments.get("end")
    step = arguments.get("step", "15s")
    if not query:
        return json.dumps({"error": "query is required"})
    data = {"query": query, "start": start, "end": end, "step": step}
    return _with_query_echo(_do_post("api/v1/query_range", data=data), query)


_HANDLERS: Dict[str, Callable[..., str]] = {
    "get_metric_names": _run_get_metric_names,
    "get_label_values": _run_get_label_values,
    "get_all_labels": _run_get_all_labels,
    "get_series": _run_get_series,
    "get_metric_metadata": _run_get_metric_metadata,
    "execute_prometheus_instant_query": _run_execute_prometheus_instant_query,
    "execute_prometheus_range_query": _run_execute_prometheus_range_query,
}


def _schema_req(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


TOOLS: List[Tool] = [
    Tool(
        name="get_metric_names",
        description=_query_only_description(
            "List metric names (label __name__ values). Pass contains to narrow to a family "
            '(e.g. contains="node_memory"); results are bounded by limit (default 200) and '
            "report total/returned/truncated. Repeating the same call returns the same list."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "contains": {
                    "type": "string",
                    "maxLength": 120,
                    "description": "Case-insensitive substring filter on the metric name.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2000,
                    "default": 200,
                    "description": "Maximum names to return.",
                },
            },
        },
    ),
    Tool(
        name="get_label_values",
        description=_query_only_description("Get all values for a label."),
        inputSchema=_schema_req(
            {"label_name": {"type": "string", "description": "Label name"}}, ["label_name"]
        ),
    ),
    Tool(
        name="get_all_labels",
        description=_query_only_description("Get all label names."),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_series",
        description=_query_only_description("Get time series matching match[] (and optional start/end)."),
        inputSchema=_schema_req(
            {
                "match": {"type": "string", "description": "Series selector"},
                "start": {"type": "string", "description": "Start timestamp (RFC3339 or unix)"},
                "end": {"type": "string", "description": "End timestamp"},
            },
            ["match"],
        ),
    ),
    Tool(
        name="get_metric_metadata",
        description=_query_only_description("Get metadata for metrics. Optionally pass metric_name."),
        inputSchema=_schema_req({"metric_name": {"type": "string"}}, []),
    ),
    Tool(
        name="execute_prometheus_instant_query",
        description=_query_only_description(
            "Execute instant PromQL query. Optional: time (RFC3339 or unix)."
        ),
        inputSchema=_schema_req(
            {"query": {"type": "string"}, "time": {"type": "string"}},
            ["query"],
        ),
    ),
    Tool(
        name="execute_prometheus_range_query",
        description=_query_only_description(
            "Execute range PromQL query. Required: query, start, end. Optional: step (e.g. 15s)."
        ),
        inputSchema=_schema_req(
            {
                "query": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "step": {"type": "string"},
            },
            ["query", "start", "end"],
        ),
    ),
]


def call_tool(name: str, arguments: dict) -> Optional[str]:
    h = _HANDLERS.get(name)
    if not h:
        return None
    return h(arguments)
