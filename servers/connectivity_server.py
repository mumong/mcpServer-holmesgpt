#!/usr/bin/env python3
"""
Connectivity Check MCP Server

仅暴露 tcp_check 工具。

运行方式:
    python connectivity_server.py
    npx -y mcp-proxy --port 8097 --server sse -- python connectivity_server.py
"""

import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import connectivity

server = Server("connectivity-mcp-server")


@server.list_tools()
async def list_tools():
    return connectivity.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    result = connectivity.call_tool(name, arguments)
    if result is None:
        result = "未知工具: {}".format(name)
    return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
