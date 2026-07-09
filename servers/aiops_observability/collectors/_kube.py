"""Shared Kubernetes helpers for AIOps collectors.

These mirror the proven approach of the data-project collection scripts:
auto-fetch credentials from Kubernetes secrets and query backends by
`kubectl exec`-ing into the backend Pod (curl to localhost for Elasticsearch,
clickhouse-client for DeepFlow ClickHouse). This avoids fragile direct network
+ TLS + auth wiring and works wherever mcpstander has kubectl + RBAC.
"""

from __future__ import annotations

import base64
import subprocess
from typing import Optional


def run_kubectl(
    args: list[str],
    *,
    input_text: Optional[str] = None,
    timeout: int = 60,
) -> tuple[bool, str, str]:
    """Run a kubectl command; return (ok, stdout, stderr). Never raises."""
    try:
        completed = subprocess.run(
            ["kubectl", *args],
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - collectors must degrade gracefully
        return False, "", str(exc)
    return completed.returncode == 0, _strip_defaulted(completed.stdout or ""), (completed.stderr or "").strip()


def _strip_defaulted(text: str) -> str:
    """Drop the 'Defaulted container ...' notice kubectl exec prepends."""
    lines = [line for line in text.splitlines() if not line.startswith("Defaulted container ")]
    return "\n".join(lines).strip()


def secret_value(namespace: str, secret_name: str, key: str, timeout: int = 30) -> Optional[str]:
    """Read and base64-decode a single key from a Kubernetes secret."""
    ok, out, _ = run_kubectl(
        ["-n", namespace, "get", "secret", secret_name, "-o", f"jsonpath={{.data.{key}}}"],
        timeout=timeout,
    )
    if not ok or not out.strip():
        return None
    try:
        return base64.b64decode(out.strip()).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None


def exec_in_pod(
    namespace: str,
    pod: str,
    argv: list[str],
    *,
    input_text: Optional[str] = None,
    timeout: int = 60,
    stdin: bool = False,
) -> tuple[bool, str, str]:
    """kubectl exec into a Pod and run argv; return (ok, stdout, stderr)."""
    exec_args = ["-n", namespace, "exec"]
    if stdin:
        exec_args.append("-i")
    exec_args.extend([pod, "--", *argv])
    return run_kubectl(exec_args, input_text=input_text, timeout=timeout)


def find_pod(namespace: str, selector: str, timeout: int = 30) -> Optional[str]:
    """Return the first Pod name in namespace matching a label selector."""
    ok, out, _ = run_kubectl(
        ["-n", namespace, "get", "pod", "-l", selector, "-o", "jsonpath={.items[0].metadata.name}"],
        timeout=timeout,
    )
    if ok and out.strip():
        return out.strip()
    return None
