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
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import connectivity, internet, prometheus

_MODULES = [internet, connectivity, prometheus]


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
        result = handler(name, arguments)
        if result is not None:
            return result
    return "未知或未实现的工具: {}".format(name)


server = Server("holmes-tools-mcp-server")


@server.list_tools()
async def list_tools():
    return _all_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    return [TextContent(type="text", text=_call_tool(name, arguments))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
