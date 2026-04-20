"""
core_investigation 工具：TodoWrite
移植自 holmes/mcp/tools/core_investigation_tools.py
与 Holmes 内置 core_investigation 行为一致：接收任务列表，格式化后返回供 LLM 使用。
"""
import json
from typing import Any, Dict, List, Optional

from mcp.types import Tool

STATUS_ORDER = {"pending": 0, "in_progress": 1, "completed": 2, "failed": 3}
STATUS_ICONS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[✓]", "failed": "[✗]"}

# 此模块接收含 list/dict 的参数，跳过 sanitize_arguments_for_tools
SKIP_SANITIZE = True


def _format_tasks(tasks: List[Dict[str, Any]]) -> str:
    if not tasks:
        return ""
    lines = ["# CURRENT INVESTIGATION TASKS", ""]
    pending = sum(1 for t in tasks if t.get("status") == "pending")
    progress = sum(1 for t in tasks if t.get("status") == "in_progress")
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    lines.append(f"**Task Status**: {completed} completed, {progress} in progress, {pending} pending")
    lines.append("")
    sorted_tasks = sorted(tasks, key=lambda t: (STATUS_ORDER.get(t.get("status", ""), 4),))
    for t in sorted_tasks:
        icon = STATUS_ICONS.get(t.get("status", "pending"), "[?]")
        tid = t.get("id", "")
        content = t.get("content", "")
        lines.append(f"{icon} [{tid}] {content}")
    lines.append("")
    lines.append("**Instructions**: Use TodoWrite tool to update task status as you work.")
    return "\n".join(lines)


def _run_todo_write(arguments: dict) -> str:
    raw = arguments.get("todos")
    if raw is None:
        return json.dumps({"error": "missing parameter: todos"})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"todos is not valid JSON: {e}"})
    if not isinstance(raw, list):
        return json.dumps({"error": "todos must be a list"})
    tasks: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            tasks.append({
                "id": item.get("id", ""),
                "content": item.get("content", ""),
                "status": item.get("status", "pending"),
            })
        else:
            tasks.append({"id": "", "content": str(item), "status": "pending"})
    formatted = _format_tasks(tasks)
    return (
        f"Investigation plan updated with {len(tasks)} tasks.\n\n"
        f"{formatted if formatted else 'No tasks in the investigation plan.'}"
    )


TOOLS: List[Tool] = [
    Tool(
        name="TodoWrite",
        description=(
            "Save investigation tasks to break down complex problems into manageable sub-tasks. "
            "ALWAYS provide the COMPLETE list of all tasks, not just the ones being updated."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": (
                        "COMPLETE list of ALL tasks. Each task: id (string), "
                        "content (string), status (pending/in_progress/completed/failed)"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "failed"],
                            },
                        },
                        "required": ["id", "content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
    ),
]


def call_tool(name: str, arguments: dict) -> Optional[str]:
    if name != "TodoWrite":
        return None
    return _run_todo_write(arguments)
