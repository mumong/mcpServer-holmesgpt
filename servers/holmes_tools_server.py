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
import json
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
    # 聚合 Server 的统一日志：可以看到是哪个具体工具被调用、参数是什么、结果有多大
    try:
        print(
            "[holmes-tools-mcp] call_tool name=%s args=%s"
            % (name, json.dumps(arguments, ensure_ascii=False)),
            flush=True,
        )
    except Exception:
        print("[holmes-tools-mcp] call_tool name=%s (args json dump failed)" % name, flush=True)

    result = _call_tool(name, arguments)
    try:
        r_str = str(result)
        print("[holmes-tools-mcp] result_len=%d" % len(r_str), flush=True)
    except Exception:
        print("[holmes-tools-mcp] result convert to str failed", flush=True)

    return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
