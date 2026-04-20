#!/usr/bin/env python3
"""
Core Investigation MCP Server

仅暴露 TodoWrite 工具（任务分解与状态追踪）。

运行方式:
    python core_investigation_server.py
    npx -y mcp-proxy --port 8098 --server sse -- python core_investigation_server.py
"""

import asyncio
import time
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import core_investigation
from holmes_tools.mcp_logger import log_tool_call, log_tool_result

_SERVER = "core-investigation-mcp"
server = Server("core-investigation-mcp-server")


@server.list_tools()
async def list_tools():
    return core_investigation.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    log_tool_call(_SERVER, name, arguments)
    t0 = time.monotonic()

    # 注意：TodoWrite 接收 list/dict 参数，不做 sanitize
    result = core_investigation.call_tool(name, arguments)
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
