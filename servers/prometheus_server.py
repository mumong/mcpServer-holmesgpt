#!/usr/bin/env python3
"""
Prometheus MCP Server

仅暴露 Prometheus 查询工具（8 个）。配置：环境变量 PROMETHEUS_URL。

运行方式:
    python prometheus_server.py
    npx -y mcp-proxy --port 8095 --server sse -- python prometheus_server.py
"""

import asyncio
import json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import prometheus

server = Server("prometheus-mcp-server")


@server.list_tools()
async def list_tools():
    return prometheus.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    # 记录每次工具调用的入参，便于排查「没有结果」的问题
    try:
        print(
            "[prometheus-mcp] call_tool name=%s args=%s"
            % (name, json.dumps(arguments, ensure_ascii=False)),
            flush=True,
        )
    except Exception:
        # 打印失败不影响正常调用
        print("[prometheus-mcp] call_tool name=%s (args json dump failed)" % name, flush=True)

    result = prometheus.call_tool(name, arguments)
    if result is None:
        print("[prometheus-mcp] result is None, treat as unknown tool", flush=True)
        result = "未知工具: {}".format(name)
    else:
        # 避免日志过长，只打长度
        try:
            r_str = str(result)
            print("[prometheus-mcp] result_len=%d" % len(r_str), flush=True)
        except Exception:
            print("[prometheus-mcp] result convert to str failed", flush=True)

    return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
