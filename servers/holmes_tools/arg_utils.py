"""
MCP 入口层参数清洗：保证传入内置工具（Jinja2/命令）的 arguments 仅含标量。
与 Holmes 的 _command_runner 一致：工具层只做 None→""；非标量（如误传的 Jinja2 对象）在入口统一转为 ""，
避免渲染进 shell 导致语法错误。参考：holmes/mcp/tools 的 list_tools/call_tool 由 JSON-RPC 入参驱动。
"""
import os
from typing import Any, Dict

DEFAULT_MAX_TIMEOUT_SECONDS = 600


def sanitize_arguments_for_tools(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    返回仅含标量值的副本，供 Jinja2 模板或命令使用。
    - None -> ""
    - str, int, float, bool -> 原样
    - 其他类型（dict, list, 对象等）-> ""
    """
    if not arguments:
        return {}
    out = {}
    for k, v in arguments.items():
        if v is None:
            out[k] = ""
        elif isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = ""
    return out


def clamp_timeout(seconds):
    """Cap a caller-supplied timeout at MCP_TOOL_MAX_TIMEOUT_SECONDS (default 600)."""
    try:
        limit = float(os.environ.get("MCP_TOOL_MAX_TIMEOUT_SECONDS", DEFAULT_MAX_TIMEOUT_SECONDS))
    except ValueError:
        limit = DEFAULT_MAX_TIMEOUT_SECONDS
    if seconds > limit:
        return type(seconds)(limit)
    return seconds
