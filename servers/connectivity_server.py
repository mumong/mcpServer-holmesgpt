#!/usr/bin/env python3
"""
Connectivity Check MCP Server

仅暴露 tcp_check 工具。

运行方式:
    python connectivity_server.py
    npx -y mcp-proxy --port 8097 --server sse -- python connectivity_server.py
"""

import asyncio

from holmes_tools import connectivity
from holmes_tools.mcp_logger import log_tool_call, log_tool_result
from holmes_tools.server_adapter import call_text_tool_in_thread
from mcp.server import Server
from mcp.server.stdio import stdio_server

_SERVER = "connectivity-mcp"
server = Server("connectivity-mcp-server")


@server.list_tools()
async def list_tools():
    return connectivity.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    return await call_text_tool_in_thread(
        _SERVER,
        name,
        arguments,
        connectivity.call_tool,
        log_call=log_tool_call,
        log_result=log_tool_result,
        sanitize=True,
        catch_errors=False,
    )


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
