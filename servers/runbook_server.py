#!/usr/bin/env python3
"""
Runbook MCP Server

仅暴露 fetch_runbook 工具（Runbook 知识库获取）。
配置：环境变量 RUNBOOK_SEARCH_PATH（逗号或冒号分隔的搜索目录）。

运行方式:
    python runbook_server.py
    npx -y mcp-proxy --port 8099 --server sse -- python runbook_server.py
"""

import asyncio
import time
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import runbook
from holmes_tools.arg_utils import sanitize_arguments_for_tools
from holmes_tools.mcp_logger import log_tool_call, log_tool_result

_SERVER = "runbook-mcp"
server = Server("runbook-mcp-server")


@server.list_tools()
async def list_tools():
    return runbook.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    log_tool_call(_SERVER, name, arguments)
    t0 = time.monotonic()

    result = runbook.call_tool(name, sanitize_arguments_for_tools(arguments))
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
