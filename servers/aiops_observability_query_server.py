#!/usr/bin/env python3
"""Model-directed Pod observability MCP server."""

import asyncio
import json
import os
import sys
import time

import anyio.to_thread

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from holmes_tools import aiops_observability_query
from holmes_tools.mcp_logger import get_logger, log_tool_call, log_tool_result
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent

_SERVER = "aiops-observability-query-mcp"
server = Server("aiops-observability-query-mcp-server")
_logger = get_logger(_SERVER)


def _log_result_contract(name: str, result: str) -> None:
    """Log stage counts without copying evidence or query bodies."""
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        _logger.warning(
            "[OBSERVABILITY_CONTRACT] %s | invalid_json | chars=%d",
            name,
            len(result or ""),
        )
        return
    ledger = payload.get("fact_ledger")
    records = ledger.get("records") if isinstance(ledger, dict) else []
    serialization = payload.get("serialization")
    serialization = serialization if isinstance(serialization, dict) else {}
    _logger.info(
        "[OBSERVABILITY_CONTRACT] %s | dimension=%s coverage=%s "
        "facts=%d ledger=%d samples=%d source_truncated=%s "
        "response_truncated=%s bytes=%s",
        name,
        payload.get("dimension"),
        payload.get("coverage"),
        len(payload.get("facts") or []),
        len(records or []),
        len(payload.get("samples") or []),
        bool(payload.get("truncated")),
        serialization.get("response_truncated"),
        serialization.get("serialized_bytes"),
    )


@server.list_tools()
async def list_tools():
    return aiops_observability_query.TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    log_tool_call(_SERVER, name, arguments)
    started = time.monotonic()
    try:
        result = await anyio.to_thread.run_sync(
            aiops_observability_query.call_tool,
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
        _log_result_contract(name, result)
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
        _log_result_contract(name, result)
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
