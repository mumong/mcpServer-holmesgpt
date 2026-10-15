#!/usr/bin/env python3
"""
Core Investigation MCP Server

仅暴露 TodoWrite 工具（任务分解与状态追踪）。

运行方式:
    python core_investigation_server.py
    npx -y mcp-proxy --port 8098 --server sse -- python core_investigation_server.py
"""

import asyncio

from holmes_tools import core_investigation
from holmes_tools.mcp_logger import log_tool_call, log_tool_result
from holmes_tools.server_adapter import call_text_tool_in_thread
from mcp.server import Server
from mcp.server.stdio import stdio_server

_SERVER = "core-investigation-mcp"
server = Server("core-investigation-mcp-server")


@server.list_tools()
async def list_tools():
    return core_investigation.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    return await call_text_tool_in_thread(
        _SERVER,
        name,
        arguments,
        core_investigation.call_tool,
        log_call=log_tool_call,
        log_result=log_tool_result,
        sanitize=False,
        catch_errors=False,
    )


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
