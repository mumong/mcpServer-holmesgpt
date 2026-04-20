"""
runbook 工具：fetch_runbook
移植自 holmes/mcp/tools/runbook_tools.py
根据 runbook_id（.md 文件名或路径）从配置的搜索路径读取 Runbook 内容。
通过环境变量 RUNBOOK_SEARCH_PATH 配置多个搜索目录（逗号或冒号分隔），默认仅当前目录。
"""
import os
import textwrap
from typing import List, Optional

from mcp.types import Tool


def _get_search_paths() -> List[str]:
    raw = os.environ.get("RUNBOOK_SEARCH_PATH", "").strip()
    if not raw:
        return [os.getcwd()]
    paths = []
    for p in raw.replace(",", ":").split(":"):
        p = p.strip()
        if p and os.path.isdir(p):
            paths.append(os.path.realpath(p))
    return paths if paths else [os.getcwd()]


def _resolve_runbook_path(runbook_id: str, search_paths: List[str]) -> Optional[str]:
    if not runbook_id or not runbook_id.strip():
        return None
    runbook_id = runbook_id.strip()
    for base in search_paths:
        base_real = os.path.realpath(base)
        candidate = os.path.join(base, runbook_id)
        if not os.path.exists(candidate):
            continue
        real_candidate = os.path.realpath(candidate)
        # Path traversal protection
        if real_candidate != base_real and not real_candidate.startswith(base_real + os.sep):
            continue
        if os.path.isfile(real_candidate):
            return real_candidate
    return None


def _run_fetch_runbook(arguments: dict) -> str:
    runbook_id = (arguments.get("runbook_id") or "").strip()
    if not runbook_id:
        return "Error: runbook_id cannot be empty."
    search_paths = _get_search_paths()
    path = _resolve_runbook_path(runbook_id, search_paths)
    if not path:
        return f"Error: Runbook '{runbook_id}' not found in search paths: {search_paths}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading runbook: {e}"
    wrapped = textwrap.dedent(f"""\
        <runbook>
{textwrap.indent(content, " " * 8)}
        </runbook>
        Note: the above are DIRECTIONS not ACTUAL RESULTS. Follow the steps using tools and report back.
        You must call tools yourself to execute the steps.
        """)
    return wrapped


def _runbook_list_for_description() -> str:
    paths = _get_search_paths()
    names: list[str] = []
    for base in paths:
        try:
            for f in os.listdir(base):
                if f.endswith(".md"):
                    names.append(f)
        except OSError:
            pass
    return ", ".join(sorted(set(names))) if names else "(none found)"


TOOLS: List[Tool] = [
    Tool(
        name="fetch_runbook",
        description=(
            "Get runbook content by runbook link. Use this to get troubleshooting steps for incidents. "
            "runbook_id can be a .md filename (e.g. dns_troubleshooting_instructions.md) "
            "or path relative to RUNBOOK_SEARCH_PATH."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "runbook_id": {
                    "type": "string",
                    "description": f"The runbook_id: a .md filename or path. Example files: {_runbook_list_for_description()}",
                }
            },
            "required": ["runbook_id"],
        },
    ),
]


def call_tool(name: str, arguments: dict) -> Optional[str]:
    if name != "fetch_runbook":
        return None
    return _run_fetch_runbook(arguments)
