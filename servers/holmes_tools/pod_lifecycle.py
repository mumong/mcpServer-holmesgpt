"""Read-only Pod lifecycle facts; no diagnosis, alert filtering, or follow-up calls."""

import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from mcp.types import Tool

from . import pod_record_query

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


@contextmanager
def _kubectl_config():
    """Raw kubectl requests need explicit in-cluster config; retain external contexts."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    if (
        os.environ.get("KUBECONFIG")
        or (Path.home() / ".kube/config").is_file()
        or not host
        or not (SERVICE_ACCOUNT / "token").is_file()
    ):
        yield []
        return
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    server = f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
    # tokenFile keeps the mounted, rotating identity; no credentials in argv/output.
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "in-cluster",
        "clusters": [
            {
                "name": "in-cluster",
                "cluster": {"server": server, "certificate-authority": str(SERVICE_ACCOUNT / "ca.crt")},
            }
        ],
        "users": [{"name": "service-account", "user": {"tokenFile": str(SERVICE_ACCOUNT / "token")}}],
        "contexts": [{"name": "in-cluster", "context": {"cluster": "in-cluster", "user": "service-account"}}],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".kubeconfig") as handle:
        json.dump(config, handle)
        handle.flush()
        yield ["--kubeconfig=" + handle.name]


TOOL = Tool(
    name="query_pod_lifecycle",
    description=(
        "Structured Pod lifecycle facts; preferred for Pod health, restart and termination checks. "
        "Returns ALL selected Pods (Running/Ready included): UID, phase, readiness, container current/last "
        "state, exit reason/code, restarts. Facts only, no logs/metrics/diagnosis. Omit namespace for the "
        "whole cluster; pod requires namespace. Recent window defaults to 10 minutes. Running/Ready does not "
        "rule out a recent termination; lastState is not full history; null restart_count_in_window means "
        "unknown, not zero. Paged: repeat the SAME arguments with next_continue_token; one page is not full coverage."
        + (pod_record_query.DESCRIPTION if pod_record_query.enabled() else "")
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "description": "Namespace; omit for all namespaces."},
            "pod": {"type": "string", "description": "Exact Pod name; requires namespace."},
            "label_selector": {
                "type": "string",
                "description": "Optional Kubernetes label selector for lists.",
            },
            "window_minutes": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 10},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
            "continue_token": {
                "type": "string",
                "description": "Opaque token returned by this tool; keep scope unchanged.",
            },
            **(pod_record_query.PROPERTIES if pod_record_query.enabled() else {}),
        },
        "additionalProperties": False,
    },
)


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def _state(value):
    # Deliberately project status fields, never env, annotations, commands or Secret data.
    return {
        kind: {
            key: fields.get(key)
            for key in ("reason", "exitCode", "signal", "startedAt", "finishedAt")
            if key in fields
        }
        for kind, fields in (value or {}).items()
        if kind in {"waiting", "running", "terminated"} and isinstance(fields, dict)
    }


def project_pod(pod, observed_at, window_minutes):
    metadata, spec, status = (pod.get(key) or {} for key in ("metadata", "spec", "status"))
    containers = []
    for role, spec_key, status_key in (
        ("app", "containers", "containerStatuses"),
        ("init", "initContainers", "initContainerStatuses"),
        ("ephemeral", "ephemeralContainers", "ephemeralContainerStatuses"),
    ):
        specs = {c["name"]: c for c in spec.get(spec_key, [])}
        statuses = {c["name"]: c for c in status.get(status_key, [])}
        for name in dict.fromkeys([*specs, *statuses]):
            item = statuses.get(name, {})
            current, previous = _state(item.get("state")), _state(item.get("lastState"))
            finished = [_timestamp(s.get("terminated", {}).get("finishedAt")) for s in (current, previous)]
            latest = max((t for t in finished if t is not None), default=None)
            age = (observed_at - latest).total_seconds() if latest else None
            containers.append(
                {
                    "name": name,
                    "role": role,
                    "status_observed": name in statuses,
                    "ready": item.get("ready"),
                    "restart_count_total": item.get("restartCount"),
                    "state": current,
                    "last_state": previous,
                    "latest_observed_termination_at": latest.isoformat() if latest else None,
                    "termination_within_window": (
                        0 <= age <= window_minutes * 60 if age is not None and age >= 0 else None
                    ),
                    "restart_count_in_window": None,
                    "resources": specs.get(name, {}).get("resources", {}),
                }
            )
    # Deletion facts: a Pod past its grace period that still exists is blocked by
    # something (finalizers, kubelet, volume detach); expose the facts, not a verdict.
    deletion_at = _timestamp(metadata.get("deletionTimestamp"))
    terminating_seconds = (
        max(0, int((observed_at - deletion_at).total_seconds())) if deletion_at else None
    )
    return {
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
        "uid": metadata.get("uid"),
        "created_at": metadata.get("creationTimestamp"),
        "deletion_timestamp": metadata.get("deletionTimestamp"),
        "deletion_grace_period_seconds": metadata.get("deletionGracePeriodSeconds"),
        "terminating_seconds": terminating_seconds,
        "finalizers": list(metadata.get("finalizers") or []),
        "node": spec.get("nodeName"),
        "phase": status.get("phase"),
        "reason": status.get("reason"),
        "conditions": [
            {k: c.get(k) for k in ("type", "status", "reason", "lastTransitionTime")}
            for c in status.get("conditions", [])
        ],
        "containers": containers,
    }


def _integer(arguments, name, default, maximum):
    value = arguments.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if not re.fullmatch(r"[0-9]+", str(value)) or not 1 <= int(value) <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return int(value)


def query(arguments):
    """Use argv and the API's paging contract, never shell templates or health filters."""
    prepared = None
    try:
        arguments = arguments or {}
        prepared = pod_record_query.prepare(arguments)
        if prepared:
            arguments = prepared[4]  # aliases resolved: the fingerprint and echo use real names
        from .record_pages import PREFIX, read

        if str(arguments.get("continue_token") or "").startswith(PREFIX):
            if not prepared:
                raise ValueError("record_cursor_query_changed: repeat original record parameters")
            return json.dumps(read(arguments, prepared[2]), ensure_ascii=False, separators=(",", ":"))
        namespace, pod = (str(arguments.get(k) or "") for k in ("namespace", "pod"))
        for key, value, maximum in (("namespace", namespace, 63), ("pod", pod, 253)):
            if value and (
                len(value) > maximum or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value)
            ):
                raise ValueError(f"invalid {key}")
        selector, token = (str(arguments.get(k) or "") for k in ("label_selector", "continue_token"))
        if pod and (not namespace or selector or token):
            raise ValueError("pod requires namespace and cannot be combined with selector/continue_token")
        window = _integer(arguments, "window_minutes", 10, 1440)
        limit = _integer(arguments, "page_size", 100, 100)
        path = "/api/v1/" + (f"namespaces/{quote(namespace, safe='')}/" if namespace else "") + "pods"
        if pod:
            path += "/" + quote(pod, safe="")
        else:
            params = {"limit": limit}
            if selector:
                params["labelSelector"] = selector
            if token:
                params["continue"] = token
            path += "?" + urlencode(params)
        with _kubectl_config() as config_flags:
            response = subprocess.run(
                ["kubectl", *config_flags, "--request-timeout=25s", "get", "--raw", path],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        if response.returncode:
            if prepared:
                return json.dumps(
                    pod_record_query.error_result((response.stderr or response.stdout).strip(), arguments),
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "status": "error",
                    "coverage": "unknown",
                    "pods": [],
                    "error": (response.stderr or response.stdout).strip(),
                },
                ensure_ascii=False,
            )
        payload = json.loads(response.stdout)
        expected = "Pod" if pod else "PodList"
        if not isinstance(payload, dict) or payload.get("kind") != expected:
            raise ValueError(f"expected Kubernetes {expected} response")
        items = [payload] if pod else payload.get("items")
        if not isinstance(items, list) or any(not isinstance(p, dict) for p in items):
            raise ValueError("invalid PodList items")
        now = datetime.now(timezone.utc)
        metadata = payload.get("metadata") or {}
        next_token = (metadata.get("continue") or "") if not pod else ""
        result = {
            "contract_version": "pod-lifecycle.v1",
            "status": "success",
            "source": "kubernetes",
            "observed_at": now.isoformat(),
            "scope": {"namespace": namespace or None, "pod": pod or None, "label_selector": selector or None},
            "window": {
                "minutes": window,
                "start": (now - timedelta(minutes=window)).isoformat(),
                "end": now.isoformat(),
            },
            "coverage": "partial" if next_token else "present" if items else "empty",
            "returned_count": len(items),
            "next_continue_token": next_token or None,
            "remaining_item_count": metadata.get("remainingItemCount") if not pod else 0,
            "resource_version": metadata.get("resourceVersion"),
            "history_complete": False,
            "limitations": [
                "Current snapshot and last termination only; not complete window history.",
                "Restart totals are lifetime counters, not restarts during this window; missing timestamps are unknown.",
                "Coverage describes this page; use next_continue_token with unchanged scope for remaining Pods.",
            ],
            "pods": [project_pod(p, now, window) for p in items],
        }
        if prepared:
            result = pod_record_query.project(result, prepared, arguments)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except (ValueError, TypeError, KeyError, AttributeError, OSError, subprocess.TimeoutExpired) as exc:
        if isinstance(arguments, dict) and pod_record_query.QUERY_KEYS.intersection(arguments):
            return json.dumps(pod_record_query.error_result(exc, arguments), ensure_ascii=False)
        return json.dumps(
            {"status": "error", "coverage": "unknown", "pods": [], "error": str(exc)}, ensure_ascii=False
        )
