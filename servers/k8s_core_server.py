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
import json
import sys
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import kubernetes_core
from holmes_tools.arg_utils import sanitize_arguments_for_tools

server = Server("k8s-core-mcp-server")


@server.list_tools()
async def list_tools():
    return kubernetes_core.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    # 日志打到 stderr，避免污染 MCP stdio 的 JSON-RPC stdout
    try:
        print(
            "[k8s-core-mcp] call_tool name=%s args=%s"
            % (name, json.dumps(arguments, ensure_ascii=False)),
            file=sys.stderr,
            flush=True,
        )
    except Exception:
        print("[k8s-core-mcp] call_tool name=%s (args json dump failed)" % name, file=sys.stderr, flush=True)

    result = kubernetes_core.call_tool(name, sanitize_arguments_for_tools(arguments))
    if result is None:
        print("[k8s-core-mcp] result is None, treat as unknown tool", file=sys.stderr, flush=True)
        result = "未知工具: {}".format(name)
    else:
        try:
            r_str = str(result)
            print("[k8s-core-mcp] result_len=%d" % len(r_str), file=sys.stderr, flush=True)
            preview = (r_str[:500] + "..." if len(r_str) > 500 else r_str).replace("\n", " ")
            print("[k8s-core-mcp] result_preview: %s" % preview, file=sys.stderr, flush=True)
        except Exception:
            print("[k8s-core-mcp] result convert to str failed", file=sys.stderr, flush=True)

    return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
