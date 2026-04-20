#!/usr/bin/env python3
"""
Holmes 风格工具聚合 MCP Server

聚合 internet（fetch_webpage）、connectivity（tcp_check）、prometheus（8 个工具），
单端口对外暴露。不依赖 holmes 包。
配置：PROMETHEUS_URL 环境变量（prometheus 查询用）。

运行方式:
    python holmes_tools_server.py
    npx -y mcp-proxy --port 8095 --server sse -- python holmes_tools_server.py
"""

import asyncio
import time
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import connectivity, internet, prometheus, core_investigation, runbook
from holmes_tools.arg_utils import sanitize_arguments_for_tools
from holmes_tools.mcp_logger import log_tool_call, log_tool_result

_MODULES = [internet, connectivity, prometheus, core_investigation, runbook]


def _all_tools():
    tools = []
    for mod in _MODULES:
        tools.extend(getattr(mod, "TOOLS", []))
    return tools


def _call_tool(name: str, arguments: dict) -> str:
    for mod in _MODULES:
        handler = getattr(mod, "call_tool", None)
        if not handler:
            continue
        # 模块可声明 SKIP_SANITIZE = True 以接收原始参数（如含 list/dict 的工具）
        skip = getattr(mod, "SKIP_SANITIZE", False)
        args = arguments if skip else sanitize_arguments_for_tools(arguments)
        result = handler(name, args)
        if result is not None:
            return result
    return "未知或未实现的工具: {}".format(name)


_SERVER = "holmes-tools-mcp"

server = Server("holmes-tools-mcp-server")


@server.list_tools()
async def list_tools():
    return _all_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    log_tool_call(_SERVER, name, arguments)
    t0 = time.monotonic()

    result = _call_tool(name, arguments)

    elapsed = time.monotonic() - t0
    log_tool_result(_SERVER, name, result, elapsed)
    return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
