#!/usr/bin/env python3
"""
Connectivity Check MCP Server

仅暴露 tcp_check 工具。

运行方式:
    python connectivity_server.py
    npx -y mcp-proxy --port 8097 --server sse -- python connectivity_server.py
"""

import asyncio
import json
import sys
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import connectivity
from holmes_tools.arg_utils import sanitize_arguments_for_tools

server = Server("connectivity-mcp-server")


@server.list_tools()
async def list_tools():
    return connectivity.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    # 记录每次工具调用，方便排查网络连通性检查「没有结果」的问题
    try:
        print(
            "[connectivity-mcp] call_tool name=%s args=%s"
            % (name, json.dumps(arguments, ensure_ascii=False)),
            file=sys.stderr,
            flush=True,
        )
    except Exception:
        print("[connectivity-mcp] call_tool name=%s (args json dump failed)" % name, file=sys.stderr, flush=True)

    result = connectivity.call_tool(name, sanitize_arguments_for_tools(arguments))
    if result is None:
        print("[connectivity-mcp] result is None, treat as unknown tool", file=sys.stderr, flush=True)
        result = "未知工具: {}".format(name)
    else:
        try:
            r_str = str(result)
            print("[connectivity-mcp] result_len=%d" % len(r_str), file=sys.stderr, flush=True)
        except Exception:
            print("[connectivity-mcp] result convert to str failed", file=sys.stderr, flush=True)

    return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
