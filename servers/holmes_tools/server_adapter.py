"""Shared text-tool adapter; wire-contract policy stays at each entrypoint.

This adapter intentionally does not handle the observability-query server's
structured errors or the aggregate server's per-module dispatch policy.
"""

import functools
import time
from collections.abc import Callable
from typing import Any

import anyio.to_thread
from mcp.types import TextContent

from .arg_utils import sanitize_arguments_for_tools


def call_text_tool(
    server_name: str,
    name: str,
    arguments: dict[str, Any] | None,
    handler: Callable,
    *,
    log_call: Callable,
    log_result: Callable,
    sanitize: bool = True,
    catch_errors: bool = False,
) -> list[TextContent]:
    """Preserve scalar/raw arguments and propagating/text-error policies.

    Only ``None`` means unknown tool; empty output is a valid response. Logging
    callbacks are supplied by the entrypoint so its instrumentation remains local.
    """
    log_call(server_name, name, arguments)
    started = time.monotonic()
    try:
        params = sanitize_arguments_for_tools(arguments) if sanitize else arguments
        result = handler(name, params)
        if catch_errors:
            elapsed = time.monotonic() - started
        if result is None:
            result = f"未知工具: {name}"
            if catch_errors:
                log_result(server_name, name, result, elapsed, error=ValueError("unknown tool"))
        elif catch_errors:
            log_result(server_name, name, result, elapsed)
        if not catch_errors:
            elapsed = time.monotonic() - started
            log_result(server_name, name, result, elapsed)
        return [TextContent(type="text", text=result)]
    except Exception as exc:
        if not catch_errors:
            raise
        elapsed = time.monotonic() - started
        log_result(server_name, name, None, elapsed, error=exc)
        return [TextContent(type="text", text=f"工具调用异常: {exc}")]


async def call_text_tool_in_thread(*args: Any, **kwargs: Any) -> list[TextContent]:
    """Run ``call_text_tool`` in a worker thread so blocking handlers
    (subprocess/HTTP) do not stall the MCP event loop."""
    return await anyio.to_thread.run_sync(functools.partial(call_text_tool, *args, **kwargs))
