#!/usr/bin/env python3
"""
AIOps Case MCP Server

Exposes coarse-grained tools for collecting and reading observability case
packages. The server runs over stdio and is exposed as SSE by mcp-proxy.
"""

import asyncio
import os
import sys

# Ensure the project root (parent of servers/) is importable so the shared
# `servers.aiops_observability.*` absolute imports resolve regardless of the
# process CWD (mcp-proxy launches this from servers/).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from mcp.server import Server
from mcp.server.stdio import stdio_server

from holmes_tools import aiops_case
from holmes_tools.server_adapter import call_text_tool_in_thread
from holmes_tools.mcp_logger import log_tool_call, log_tool_result


_SERVER = "aiops-case-mcp"
server = Server("aiops-case-mcp-server")


@server.list_tools()
async def list_tools():
    return aiops_case.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    return await call_text_tool_in_thread(
        _SERVER, name, arguments, aiops_case.call_tool,
        log_call=log_tool_call, log_result=log_tool_result,
        sanitize=True, catch_errors=True,
    )


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
