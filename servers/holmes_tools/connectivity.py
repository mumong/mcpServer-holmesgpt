"""
connectivity_check: tcp_check - check TCP connectivity to host:port.
"""
import json
import socket
from typing import Optional

from mcp.types import Tool


def _run_tcp_check(arguments: dict) -> str:
    host = arguments.get("host")
    port = arguments.get("port")
    timeout = float(arguments.get("timeout", 3.0))
    if host is None:
        return json.dumps({"ok": False, "error": "host is required"})
    if port is None:
        return json.dumps({"ok": False, "error": "port is required"})
    port = int(port)
    if not (1 <= port <= 65535):
        return json.dumps({"ok": False, "error": "invalid port (must be 1-65535)"})
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return json.dumps({"ok": True})
    except (OSError, socket.timeout) as e:
        return json.dumps({"ok": False, "error": str(e)})


TOOLS = [
    Tool(
        name="tcp_check",
        description="Check if a TCP socket can be opened to a host and port.",
        inputSchema={
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "The hostname or IP address to connect to"},
                "port": {"type": "integer", "description": "The port to connect to"},
                "timeout": {"type": "number", "description": "Timeout in seconds (default: 3.0)"},
            },
            "required": ["host", "port"],
        },
    ),
]


def call_tool(name: str, arguments: dict) -> Optional[str]:
    if name != "tcp_check":
        return None
    return _run_tcp_check(arguments)
