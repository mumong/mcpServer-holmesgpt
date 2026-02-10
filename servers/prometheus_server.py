#!/usr/bin/env python3
"""
Prometheus MCP Server

仅暴露 Prometheus 查询工具（8 个）。配置：环境变量 PROMETHEUS_URL。

运行方式:
    python prometheus_server.py
    npx -y mcp-proxy --port 8095 --server sse -- python prometheus_server.py
"""

import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import prometheus

server = Server("prometheus-mcp-server")


@server.list_tools()
async def list_tools():
    return prometheus.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    result = prometheus.call_tool(name, arguments)
    if result is None:
        result = "未知工具: {}".format(name)
    return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
