"""Pure launch configuration shared by package and local MCP processes.

These functions preserve existing shell strings and environment defaults.
They do not resolve executables, read configuration files, or spawn processes.
Changing command quoting or TLS defaults is a separate behavior/security change.
"""

from collections.abc import Mapping
from typing import Any


def build_server_environment(
    base: Mapping[str, str], port: Any, overrides: Mapping[str, Any]
) -> dict[str, str]:
    """Copy inherited variables, set per-port caches, then apply overrides."""
    environment = dict(base)
    environment["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    environment["npm_config_cache"] = f"/tmp/npm-cache-{port}"
    environment["NPX_HOME"] = f"/tmp/npx-{port}"
    for key, value in overrides.items():
        environment[key] = str(value)
    return environment


def build_proxy_command(port: Any, inner_command: str, *, proxy_available: bool) -> str:
    """Keep the installed-proxy preference and legacy npx fallback exactly."""
    proxy = "mcp-proxy" if proxy_available else "npx -y mcp-proxy"
    return f"{proxy} --port {port} --server sse -- {inner_command}"
