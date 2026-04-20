#!/usr/bin/env python3
"""
Internet MCP Server

仅暴露 fetch_webpage 工具。配置：可选 INTERNET_TIMEOUT_SECONDS。

运行方式:
    python internet_server.py
    npx -y mcp-proxy --port 8096 --server sse -- python internet_server.py
"""

import asyncio
import time
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import internet
from holmes_tools.arg_utils import sanitize_arguments_for_tools
from holmes_tools.mcp_logger import log_tool_call, log_tool_result

_SERVER = "internet-mcp"
server = Server("internet-mcp-server")


@server.list_tools()
async def list_tools():
    return internet.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    log_tool_call(_SERVER, name, arguments)
    t0 = time.monotonic()

    result = internet.call_tool(name, sanitize_arguments_for_tools(arguments))
    if result is None:
        result = "未知工具: {}".format(name)

    elapsed = time.monotonic() - t0
    log_tool_result(_SERVER, name, result, elapsed)
    return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
