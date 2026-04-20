"""
prometheus 工具集：查询 Prometheus API。
对应 Holmes 的 prometheus/metrics。配置：PROMETHEUS_URL（默认 http://localhost:9090/）。

重要：Prometheus API 的 POST 接口使用 form-encoded body（data=），不是 JSON body（json=）。
Holmes 原始实现使用 requests.request(method="POST", data=payload)，此处保持一致。
"""
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

from mcp.types import Tool

try:
    from .mcp_logger import get_logger, log_http_request
except ImportError:
    from mcp_logger import get_logger, log_http_request

try:
    import requests
except ImportError:
    requests = None

_logger = get_logger("prometheus")


def _get_prometheus_url() -> str:
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
        r = requests.get(url, params=params or {}, timeout=_get_timeout())
        elapsed = time.monotonic() - t0
        log_http_request("GET", url, params_or_data=params,
                         status_code=r.status_code, response_text=r.text, elapsed=elapsed)
        r.raise_for_status()
        return r.text
    except Exception as e:
        elapsed = time.monotonic() - t0
        log_http_request("GET", url, params_or_data=params,
                         status_code=getattr(e, 'response', {}) and getattr(e.response, 'status_code', None),
                         response_text=str(e), elapsed=elapsed)
        return json.dumps({"error": str(e), "url": url})


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
        r = requests.post(url, data=data or {}, timeout=_get_timeout())
        elapsed = time.monotonic() - t0
        log_http_request("POST", url, params_or_data=data,
                         status_code=r.status_code, response_text=r.text, elapsed=elapsed)
        r.raise_for_status()
        return r.text
    except Exception as e:
        elapsed = time.monotonic() - t0
        log_http_request("POST", url, params_or_data=data,
                         status_code=getattr(e, 'response', {}) and getattr(e.response, 'status_code', None),
                         response_text=str(e), elapsed=elapsed)
        return json.dumps({"error": str(e), "url": url})


def _run_list_prometheus_rules(arguments: dict) -> str:
    return _do_get("api/v1/rules")


def _run_get_metric_names(arguments: dict) -> str:
    return _do_get("api/v1/label/__name__/values")


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


def _run_execute_prometheus_instant_query(arguments: dict) -> str:
    query = arguments.get("query")
    if not query:
        return json.dumps({"error": "query is required"})
    time = arguments.get("time")
    params = {"query": query}
    if time:
        params["time"] = time
    return _do_get("api/v1/query", params=params)


def _run_execute_prometheus_range_query(arguments: dict) -> str:
    query = arguments.get("query")
    start = arguments.get("start")
    end = arguments.get("end")
    step = arguments.get("step", "15s")
    if not query:
        return json.dumps({"error": "query is required"})
    data = {"query": query, "start": start, "end": end, "step": step}
    return _do_post("api/v1/query_range", data=data)


_HANDLERS: Dict[str, Callable[..., str]] = {
    "list_prometheus_rules": _run_list_prometheus_rules,
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
        name="list_prometheus_rules",
        description="List Prometheus recording and alerting rules.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_metric_names",
        description="Get all metric names (label __name__ values).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_label_values",
        description="Get all values for a label.",
        inputSchema=_schema_req({"label_name": {"type": "string", "description": "Label name"}}, ["label_name"]),
    ),
    Tool(
        name="get_all_labels",
        description="Get all label names.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_series",
        description="Get time series matching match[] (and optional start/end).",
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
        description="Get metadata for metrics. Optionally pass metric_name.",
        inputSchema=_schema_req({"metric_name": {"type": "string"}}, []),
    ),
    Tool(
        name="execute_prometheus_instant_query",
        description="Execute instant PromQL query. Optional: time (RFC3339 or unix).",
        inputSchema=_schema_req(
            {"query": {"type": "string"}, "time": {"type": "string"}},
            ["query"],
        ),
    ),
    Tool(
        name="execute_prometheus_range_query",
        description="Execute range PromQL query. Required: query, start, end. Optional: step (e.g. 15s).",
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
