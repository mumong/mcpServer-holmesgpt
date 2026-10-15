"""Fine-grained Pod-scoped AIOps observability MCP tools."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

from mcp.types import Tool

from servers.aiops_observability.case_builder import collect_case
from servers.aiops_observability.collectors.deepflow import collect_deepflow
from servers.aiops_observability.collectors.elasticsearch import collect_elasticsearch
from servers.aiops_observability.collectors.kubernetes import collect_kubernetes, get_pod
from servers.aiops_observability.collectors.prometheus import collect_prometheus
from servers.aiops_observability.config import get_config
from servers.aiops_observability.schemas import parse_time_window


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def _window(arguments: dict[str, Any]):
    return parse_time_window(
        start=arguments.get("start"),
        end=arguments.get("end"),
        window_minutes=int(arguments.get("window_minutes") or 30),
    )


def _resolve_entity(arguments: dict[str, Any]) -> str:
    namespace = str(arguments.get("namespace") or "")
    pod = str(arguments.get("pod") or "")
    pod_json, error = get_pod(namespace, pod, timeout=get_config().timeout_seconds)
    if error:
        return _json({"ok": False, "error": error})
    meta = pod_json.get("metadata", {})
    spec = pod_json.get("spec", {})
    status = pod_json.get("status", {})
    return _json(
        {
            "ok": True,
            "entity": {
                "kind": "Pod",
                "namespace": meta.get("namespace") or namespace,
                "name": meta.get("name") or pod,
                "uid": meta.get("uid"),
                "pod_ip": status.get("podIP"),
                "node": spec.get("nodeName"),
                "containers": [item.get("name") for item in spec.get("containers") or [] if item.get("name")],
                "labels": meta.get("labels") or {},
                "owners": meta.get("ownerReferences") or [],
            },
        }
    )


def _query_k8s(arguments: dict[str, Any]) -> str:
    cfg = get_config()
    return _json(collect_kubernetes(str(arguments.get("namespace") or ""), str(arguments.get("pod") or ""), timeout=cfg.timeout_seconds).to_payload())


def _query_metrics(arguments: dict[str, Any]) -> str:
    cfg = get_config()
    namespace = str(arguments.get("namespace") or "")
    pod = str(arguments.get("pod") or "")
    pod_json, _ = get_pod(namespace, pod, timeout=cfg.timeout_seconds)
    uid = ((pod_json or {}).get("metadata") or {}).get("uid")
    return _json(
        collect_prometheus(
            prometheus_url=cfg.prometheus_url,
            namespace=namespace,
            pod=pod,
            pod_uid=uid,
            timeout=cfg.timeout_seconds,
        ).to_payload()
    )


def _query_logs(arguments: dict[str, Any]) -> str:
    cfg = get_config()
    namespace = str(arguments.get("namespace") or "")
    pod = str(arguments.get("pod") or "")
    window = _window(arguments)
    pod_json, _ = get_pod(namespace, pod, timeout=cfg.timeout_seconds)
    uid = ((pod_json or {}).get("metadata") or {}).get("uid")
    return _json(
        collect_elasticsearch(
            elasticsearch_url=cfg.elasticsearch_url,
            index=cfg.elasticsearch_index,
            namespace=namespace,
            pod=pod,
            pod_uid=uid,
            start=window.start,
            end=window.end,
            timeout=cfg.timeout_seconds,
            max_records=int(arguments.get("max_records") or cfg.max_records),
        ).to_payload()
    )


def _query_deepflow(arguments: dict[str, Any]) -> str:
    cfg = get_config()
    namespace = str(arguments.get("namespace") or "")
    pod = str(arguments.get("pod") or "")
    window = _window(arguments)
    pod_json, _ = get_pod(namespace, pod, timeout=cfg.timeout_seconds)
    status = ((pod_json or {}).get("status") or {})
    spec = ((pod_json or {}).get("spec") or {})
    return _json(
        collect_deepflow(
            clickhouse_url=cfg.deepflow_clickhouse_url,
            clickhouse_database=cfg.deepflow_clickhouse_database,
            namespace=namespace,
            pod=pod,
            pod_ip=status.get("podIP"),
            node=spec.get("nodeName"),
            start=window.start,
            end=window.end,
            timeout=cfg.timeout_seconds,
            max_records=int(arguments.get("max_records") or cfg.max_records),
        ).to_payload()
    )


def _build_topology(arguments: dict[str, Any]) -> str:
    summary = collect_case({**arguments, "case_id": arguments.get("case_id") or f"fine-topology-{arguments.get('namespace')}-{arguments.get('pod')}"})
    if not summary.get("ok"):
        return _json(summary)
    return _json(
        {
            "ok": True,
            "source_system": "topology",
            "dimension": "topology",
            "coverage": (summary.get("coverage") or {}).get("topology", "present"),
            "topology_summary": summary.get("topology_summary"),
            "recommended_refs_by_dimension": summary.get("recommended_refs_by_dimension"),
            "case_id": summary.get("case_id"),
            "package_ref": summary.get("package_ref"),
        }
    )


_COMMON_PROPS = {
    "namespace": {"type": "string"},
    "pod": {"type": "string"},
    "start": {"type": "string"},
    "end": {"type": "string"},
    "window_minutes": {"type": "integer"},
    "max_records": {"type": "integer"},
}

TOOLS: List[Tool] = [
    Tool(
        name="resolve_aiops_entity",
        description="Resolve Pod UID, IP, node, containers, labels, and owner metadata for AIOps analysis.",
        inputSchema=_schema({"namespace": {"type": "string"}, "pod": {"type": "string"}}, ["pod"]),
    ),
    Tool(
        name="query_aiops_k8s_snapshot",
        description="Collect bounded Kubernetes Pod status/events/log snapshot for one Pod.",
        inputSchema=_schema(_COMMON_PROPS, ["pod"]),
    ),
    Tool(
        name="query_aiops_metrics",
        description="Collect bounded Prometheus metrics for one Pod. The tool generates PromQL internally.",
        inputSchema=_schema(_COMMON_PROPS, ["pod"]),
    ),
    Tool(
        name="query_aiops_logs",
        description="Collect bounded Elasticsearch/Filebeat logs for one Pod. The tool generates ES query internally.",
        inputSchema=_schema(_COMMON_PROPS, ["pod"]),
    ),
    Tool(
        name="query_aiops_deepflow_flows",
        description="Collect bounded DeepFlow flow/trace context for one Pod. The tool generates ClickHouse SQL internally.",
        inputSchema=_schema(_COMMON_PROPS, ["pod"]),
    ),
    Tool(
        name="build_aiops_topology",
        description="Build lightweight topology for one Pod using the shared AIOps collectors.",
        inputSchema=_schema({**_COMMON_PROPS, "case_id": {"type": "string"}}, ["pod"]),
    ),
]


_HANDLERS: Dict[str, Callable[[dict[str, Any]], str]] = {
    "resolve_aiops_entity": _resolve_entity,
    "query_aiops_k8s_snapshot": _query_k8s,
    "query_aiops_metrics": _query_metrics,
    "query_aiops_logs": _query_logs,
    "query_aiops_deepflow_flows": _query_deepflow,
    "build_aiops_topology": _build_topology,
}


def call_tool(name: str, arguments: dict[str, Any]) -> str | None:
    handler = _HANDLERS.get(name)
    if not handler:
        return None
    return handler(arguments or {})
