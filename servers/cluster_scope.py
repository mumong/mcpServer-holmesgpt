"""Which cluster the current MCP tool call targets (specs/008-multi-cluster).

Every tool entrypoint wraps its handler in ``activate(arguments)``; low-level
call sites (Prometheus requests, kubectl subprocesses) read the active scope
instead of growing a ``cluster`` parameter of their own. Without a multi-cluster
config file nothing changes: one ``local`` cluster with the legacy backends,
no extra schema field, no extra result field (spec FR-003).

Always import this module as ``cluster_scope`` (the ``servers`` directory is on
``sys.path`` for every server and test) so there is a single ContextVar.
"""

from __future__ import annotations

import contextvars
import copy
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from aiops_edge.clusters import Cluster, EdgeConfig, load_config

ARG = "cluster"
_current: contextvars.ContextVar[Optional[Cluster]] = contextvars.ContextVar("aiops_cluster", default=None)
_lock = threading.Lock()
_config: Optional[EdgeConfig] = None

# Backend names reported in results for the legacy (environment-configured) stack.
_LEGACY_BACKENDS = {"metrics": "prometheus", "logs": "elasticsearch", "traces": "deepflow+tempo"}


def config() -> EdgeConfig:
    global _config
    with _lock:
        if _config is None:
            _config = load_config()
        return _config


def reset() -> None:
    """Forget the loaded config (tests)."""
    global _config
    with _lock:
        _config = None


def multi() -> bool:
    return config().multi


@dataclass
class Scope:
    cluster: Optional[Cluster]
    error: Optional[Dict[str, str]] = None


@contextmanager
def activate(arguments: Optional[Dict[str, Any]]) -> Iterator[Scope]:
    """Consume ``arguments['cluster']`` and make it the active cluster for this call."""
    name = ""
    if isinstance(arguments, dict) and ARG in arguments:
        name = str(arguments.pop(ARG) or "").strip()
    cfg = config()
    cluster = cfg.resolve(name or None)
    scope = Scope(cluster)
    if cluster is None:
        available = ", ".join(cfg.names())
        if name:
            scope.error = {
                "code": "cluster_unknown",
                "message": f"no cluster named {name!r}; available: {available}",
            }
        else:
            scope.error = {
                "code": "cluster_required",
                "message": f"multi-cluster deployment: pass cluster (one of: {available})",
            }
    token = _current.set(cluster)
    try:
        yield scope
    finally:
        _current.reset(token)


def current() -> Optional[Cluster]:
    return _current.get()


def _profile() -> Dict[str, Any]:
    cluster = current()
    if cluster is None:
        return {}
    return config().profiles.get(cluster.observability) or {}


def backend(dimension: str) -> str:
    """Backend type for metrics / logs / traces of the active cluster."""
    source = _profile().get(dimension) or {}
    return str(source.get("type") or _LEGACY_BACKENDS.get(dimension, "unknown"))


def source(dimension: str) -> Dict[str, Any]:
    return dict(_profile().get(dimension) or {})


def prometheus_url() -> str:
    """Metrics endpoint of the active cluster; legacy keeps ``PROMETHEUS_URL``."""
    if backend("metrics") == "victoriametrics":
        return str(source("metrics").get("url") or "")
    return os.environ.get("PROMETHEUS_URL", "").strip()


def prometheus_params() -> Dict[str, str]:
    """Query parameters VictoriaMetrics uses to force every selector to the cluster."""
    cluster = current()
    if cluster is None or backend("metrics") != "victoriametrics":
        return {}
    label = source("metrics").get("cluster_label") or "cluster"
    return {"extra_label": f"{label}={cluster.name}"}


def subprocess_env() -> Optional[Dict[str, str]]:
    """Environment for kubectl subprocesses: None (inherit) unless the cluster is remote."""
    cluster = current()
    if cluster is None or not cluster.remote:
        return None
    return {**os.environ, "AIOPS_CLUSTER": cluster.name}


def is_remote() -> bool:
    cluster = current()
    return bool(cluster and cluster.remote)


def annotate(result: Dict[str, Any], dimension: Optional[str] = None) -> Dict[str, Any]:
    """Add ``cluster`` / ``backend`` to a structured result in multi-cluster mode."""
    if not multi() or not isinstance(result, dict):
        return result
    cluster = current()
    out = dict(result)
    if cluster is not None:
        out["cluster"] = cluster.name
    if dimension:
        out["backend"] = backend(dimension) if dimension in _LEGACY_BACKENDS else dimension
    return out


def error_text(scope: Scope) -> str:
    assert scope.error is not None
    return f"aiops: {scope.error['code']}: {scope.error['message']}"


_PARAM = {
    "type": "string",
    "maxLength": 128,
    "description": (
        "Target cluster (canonical name or alias, e.g. edge-88). Required in a multi-cluster "
        "deployment; the AIOps service fills it from the conversation scope."
    ),
}


def with_cluster_param(tools: List[Any]) -> List[Any]:
    """Tool schemas with an optional ``cluster`` property; unchanged in single-cluster mode."""
    if not multi():
        return tools
    out = []
    for tool in tools:
        schema = copy.deepcopy(tool.inputSchema or {"type": "object"})
        schema.setdefault("properties", {})[ARG] = dict(_PARAM)
        out.append(tool.model_copy(update={"inputSchema": schema}))
    return out


# ---------------------------------------------------------------------------
# Remote shell rules (bash tool) and the list_clusters tool
# ---------------------------------------------------------------------------

_TEXT_TOOLS = frozenset(
    {"jq", "grep", "egrep", "awk", "sed", "head", "tail", "sort", "uniq", "wc", "cut", "tr", "column"}
)


def remote_shell_error(command: str) -> Optional[str]:
    """On a remote cluster only ``kubectl ... | text-filter ...`` makes sense.

    Any other command would run inside the center's MCP Pod, not on the edge
    cluster, so it is refused with that explanation. Quotes are honoured, so a
    jq program such as ``'.items|length'`` is one argument.
    """
    import shlex

    text = (command or "").strip()
    if "$(" in text or "`" in text or "\n" in text:
        return "remote cluster: command substitution and multi-line commands are not allowed"
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError as exc:
        return f"remote cluster: cannot parse command ({exc})"
    segments: List[List[str]] = [[]]
    for token in tokens:
        if token == "|":
            segments.append([])
        elif token and all(c in "();<>|&" for c in token):
            return f"remote cluster: shell operator {token!r} is not allowed; use a single kubectl pipeline"
        else:
            segments[-1].append(token)
    if not segments[0] or segments[0][0] != "kubectl":
        return (
            "remote cluster: the command must start with kubectl; other programs would run in the "
            "center's MCP Pod, not on the target cluster"
        )
    for segment in segments[1:]:
        program = segment[0] if segment else ""
        if program not in _TEXT_TOOLS:
            return f"remote cluster: {program or 'empty'} is not an allowed text filter ({', '.join(sorted(_TEXT_TOOLS))})"
    return None


_PROBE_TTL_SECONDS = 30
_probe_cache: Dict[str, tuple] = {}


def _probe(cluster: Cluster) -> tuple:
    """(online, reason) — xnet virtual Node Ready and the edge runner answering."""
    if not cluster.remote:
        return True, ""
    import json as _json
    import subprocess
    import time

    cached = _probe_cache.get(cluster.name)
    if cached and time.monotonic() - cached[0] < _PROBE_TTL_SECONDS:
        return cached[1], cached[2]
    online, reason = True, ""
    try:
        out = subprocess.run(
            ["kubectl", "get", "node", cluster.name, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        node = _json.loads(out.stdout) if out.returncode == 0 else {}
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in (node.get("status") or {}).get("conditions", [])
        )
        if not ready:
            online, reason = False, "xnet_node_not_ready"
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        online, reason = False, f"xnet_node_unknown: {exc}"
    if online:
        try:
            from aiops_edge.tunnel import EdgeError, TunnelClient

            TunnelClient.from_access(config().xnet).request(
                cluster, "/healthz", None, method="GET", timeout=5
            )
        except EdgeError as exc:
            online, reason = False, exc.code
    _probe_cache[cluster.name] = (time.monotonic(), online, reason)
    return online, reason


def _capabilities(cluster: Cluster) -> Dict[str, Any]:
    profile = config().profiles.get(cluster.observability) or {}

    def kind(dimension: str) -> str:
        return str((profile.get(dimension) or {}).get("type") or _LEGACY_BACKENDS[dimension])

    logs = kind("logs")
    return {
        "metrics": kind("metrics"),
        "logs": "k8s_events"
        if logs == "victorialogs_events"
        else ("container_logs" if logs != "none" else False),
        "traces": False if kind("traces") == "none" else kind("traces"),
        "kubernetes": True,
        "exec": True,
        "helm": not cluster.remote,
    }


def list_clusters_text() -> str:
    import json as _json

    cfg = config()
    clusters = []
    for cluster in cfg.clusters.values():
        online, reason = _probe(cluster)
        capabilities = _capabilities(cluster)
        # Operator-facing facts only: how a cluster is reached (tunnel, runner, backend
        # profile) is deployment detail the agent must not present to the user.
        entry = {
            "name": cluster.name,
            "aliases": list(cluster.aliases),
            "role": "edge" if cluster.remote else "center",
            "online": online,
            "data": {
                "metrics": capabilities["metrics"] not in ("none", False),
                "logs": capabilities["logs"],
                "traces": bool(capabilities["traces"]),
            },
        }
        if reason:
            entry["reason"] = reason
        clusters.append(entry)
    return _json.dumps({"center": cfg.center, "multi": cfg.multi, "clusters": clusters}, ensure_ascii=False)


LIST_CLUSTERS = "list_clusters"


def list_clusters_tool():
    from mcp.types import Tool

    return Tool(
        name=LIST_CLUSTERS,
        description=(
            "List the clusters this AIOps deployment manages: canonical name, aliases, role (center or "
            "edge), online status, and which data exist (metrics, logs as container logs or only "
            "Kubernetes events, traces). When answering, describe clusters in operations terms; "
            "do not present connection mechanics."
        ),
        inputSchema={"type": "object", "properties": {}},
    )
