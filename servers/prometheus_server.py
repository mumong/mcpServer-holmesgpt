#!/usr/bin/env python3
"""
Prometheus MCP Server

仅暴露 Prometheus 查询工具（7 个，不暴露全量规则查询）。配置：环境变量 PROMETHEUS_URL。

运行方式:
    python prometheus_server.py
    npx -y mcp-proxy --port 8095 --server sse -- python prometheus_server.py
"""

import asyncio

import cluster_scope
from holmes_tools import prometheus
from holmes_tools.mcp_logger import get_logger, log_tool_call, log_tool_result
from holmes_tools.server_adapter import call_text_tool_in_thread
from mcp.server import Server
from mcp.server.stdio import stdio_server

_SERVER = "prometheus-mcp"
logger = get_logger(_SERVER)
server = Server("prometheus-mcp-server")


@server.list_tools()
async def list_tools():
    return cluster_scope.with_cluster_param(prometheus.TOOLS)


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    return await call_text_tool_in_thread(
        _SERVER,
        name,
        arguments,
        prometheus.call_tool,
        log_call=log_tool_call,
        log_result=log_tool_result,
        sanitize=True,
        catch_errors=True,
    )


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
