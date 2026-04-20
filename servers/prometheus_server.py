#!/usr/bin/env python3
"""
Prometheus MCP Server

仅暴露 Prometheus 查询工具（8 个）。配置：环境变量 PROMETHEUS_URL。

运行方式:
    python prometheus_server.py
    npx -y mcp-proxy --port 8095 --server sse -- python prometheus_server.py
"""

import asyncio
import time
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import prometheus
from holmes_tools.arg_utils import sanitize_arguments_for_tools
from holmes_tools.mcp_logger import log_tool_call, log_tool_result, get_logger

_SERVER = "prometheus-mcp"
logger = get_logger(_SERVER)
server = Server("prometheus-mcp-server")


@server.list_tools()
async def list_tools():
    return prometheus.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    log_tool_call(_SERVER, name, arguments)
    t0 = time.monotonic()
    try:
        result = prometheus.call_tool(name, sanitize_arguments_for_tools(arguments))
        elapsed = time.monotonic() - t0
        if result is None:
            result = "未知工具: {}".format(name)
            log_tool_result(_SERVER, name, result, elapsed, error=ValueError("unknown tool"))
        else:
            log_tool_result(_SERVER, name, result, elapsed)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        elapsed = time.monotonic() - t0
        log_tool_result(_SERVER, name, None, elapsed, error=e)
        return [TextContent(type="text", text=f"工具调用异常: {e}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
