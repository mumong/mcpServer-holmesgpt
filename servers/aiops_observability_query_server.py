#!/usr/bin/env python3
"""Model-directed Pod observability MCP server."""

import asyncio
import os
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

from holmes_tools import aiops_observability_query
from holmes_tools.mcp_logger import log_tool_call, log_tool_result


_SERVER = "aiops-observability-query-mcp"
server = Server("aiops-observability-query-mcp-server")


@server.list_tools()
async def list_tools():
    return aiops_observability_query.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    log_tool_call(_SERVER, name, arguments)
    started = time.monotonic()
    try:
        result = aiops_observability_query.call_tool(
            name,
            arguments or {},
        )
        elapsed = time.monotonic() - started
        if result is None:
            result = aiops_observability_query.tool_error_result(
                name,
                arguments or {},
                code="unknown_tool",
                message=f"unknown tool: {name}",
            )
            log_tool_result(
                _SERVER,
                name,
                result,
                elapsed,
                error=ValueError("unknown tool"),
            )
        else:
            log_tool_result(_SERVER, name, result, elapsed)
        return [TextContent(type="text", text=result)]
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        log_tool_result(_SERVER, name, None, elapsed, error=exc)
        result = aiops_observability_query.tool_error_result(
            name,
            arguments or {},
            code="internal_error",
            message=str(exc),
        )
        return [TextContent(type="text", text=result)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
