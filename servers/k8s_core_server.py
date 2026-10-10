#!/usr/bin/env python3
"""
K8s Core MCP Server（只读）

暴露 Holmes kubernetes/core 风格工具：kubectl_describe, kubectl_get_by_name,
kubectl_get_by_kind_in_namespace, kubectl_get_by_kind_in_cluster, kubectl_find_resource,
kubectl_get_yaml, kubectl_events, kubernetes_jq_query, kubernetes_tabular_query, kubernetes_count,
kubectl_top_pods, kubectl_top_nodes, get_prometheus_target, kubectl_lineage_children,
kubectl_lineage_parents。
依赖：kubectl、jq（部分工具）。运行在 8093 端口（由 start.py/config 指定）。

运行方式:
    python k8s_core_server.py
    npx -y mcp-proxy --port 8093 --server sse -- python k8s_core_server.py
"""

import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server

from holmes_tools import kubernetes_core
from holmes_tools.server_adapter import call_text_tool
from holmes_tools.mcp_logger import log_tool_call, log_tool_result

_SERVER = "k8s-core-mcp"
server = Server("k8s-core-mcp-server")


@server.list_tools()
async def list_tools():
    return kubernetes_core.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    return call_text_tool(
        _SERVER, name, arguments, kubernetes_core.call_tool,
        log_call=log_tool_call, log_result=log_tool_result,
        # Lifecycle is an argv/data tool with typed list predicates, not a shell template.
        sanitize=name != "query_pod_lifecycle", catch_errors=False,
    )


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
