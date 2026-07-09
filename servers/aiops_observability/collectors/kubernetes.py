"""Kubernetes source collector for Pod-scoped AIOps cases."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import yaml

from ..schemas import EvidenceItem, SourceResult, evidence_id


def _run_kubectl(args: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["kubectl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        return False, str(exc)
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, output.strip()


def get_pod(namespace: str, pod: str, timeout: int = 30) -> tuple[dict[str, Any] | None, str | None]:
    args = ["get", "pod", pod, "-o", "json"]
    if namespace:
        args.extend(["-n", namespace])
    ok, out = _run_kubectl(args, timeout=timeout)
    if not ok:
        return None, out
    try:
        return json.loads(out), None
    except json.JSONDecodeError as exc:
        return None, f"invalid pod json: {exc}"


def _get_json(kind: str, name: str, namespace: str, timeout: int = 30) -> dict[str, Any] | None:
    args = ["get", kind, name, "-o", "json"]
    if namespace:
        args.extend(["-n", namespace])
    ok, out = _run_kubectl(args, timeout=timeout)
    if not ok:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def resolve_owner_chain(pod_json: dict[str, Any], namespace: str, timeout: int = 30) -> list[dict[str, Any]]:
    """Walk ownerReferences upward: Pod -> ReplicaSet -> Deployment (best effort, bounded)."""
    chain: list[dict[str, Any]] = []
    obj = pod_json
    seen: set[tuple[str, str]] = set()
    for _ in range(4):
        owners = (obj.get("metadata", {}) or {}).get("ownerReferences") or []
        if not owners:
            break
        owner = owners[0]
        kind = owner.get("kind")
        name = owner.get("name")
        if not kind or not name or (kind, name) in seen:
            break
        seen.add((kind, name))
        chain.append({"kind": kind, "name": name})
        nxt = _get_json(str(kind).lower(), str(name), namespace, timeout=timeout)
        if nxt is None:
            break
        obj = nxt
    return chain


def matching_services(namespace: str, pod_labels: dict[str, Any], timeout: int = 30) -> list[dict[str, Any]]:
    """Find Services in the namespace whose non-empty selector is satisfied by the Pod labels."""
    if not namespace or not pod_labels:
        return []
    ok, out = _run_kubectl(["get", "svc", "-n", namespace, "-o", "json"], timeout=timeout)
    if not ok:
        return []
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return []
    result: list[dict[str, Any]] = []
    for svc in payload.get("items", []) or []:
        selector = ((svc.get("spec", {}) or {}).get("selector")) or {}
        if not selector:
            continue
        if all(str(pod_labels.get(k)) == str(v) for k, v in selector.items()):
            spec = svc.get("spec", {}) or {}
            result.append(
                {
                    "name": (svc.get("metadata", {}) or {}).get("name"),
                    "selector": selector,
                    "cluster_ip": spec.get("clusterIP"),
                    "ports": [{"port": p.get("port"), "targetPort": p.get("targetPort")} for p in (spec.get("ports") or [])],
                }
            )
    return result


def collect_kubernetes(namespace: str, pod: str, timeout: int = 30, tail: int = 120) -> SourceResult:
    pod_json, error = get_pod(namespace, pod, timeout=timeout)
    if error:
        return SourceResult(
            source_system="kubernetes",
            dimension="k8s",
            coverage="error",
            error=error,
            query_meta={"pod": pod, "namespace": namespace},
        )

    assert pod_json is not None
    meta = pod_json.get("metadata", {})
    status = pod_json.get("status", {})
    spec = pod_json.get("spec", {})
    ns = meta.get("namespace") or namespace
    name = meta.get("name") or pod

    describe_ok, describe = _run_kubectl(["describe", "pod", name, "-n", ns], timeout=timeout)
    events_ok, events = _run_kubectl(["events", "--for", f"pod/{name}", "-n", ns], timeout=timeout)
    logs_ok, logs = _run_kubectl(["logs", name, "-n", ns, "--all-containers", f"--tail={tail}"], timeout=timeout)
    prev_ok, prev_logs = _run_kubectl(
        ["logs", name, "-n", ns, "--previous", "--all-containers", f"--tail={tail}"],
        timeout=timeout,
    )

    container_statuses = status.get("containerStatuses") or []
    last_terminated = []
    for item in container_statuses:
        terminated = ((item.get("lastState") or {}).get("terminated") or {})
        if terminated:
            last_terminated.append(
                {
                    "container": item.get("name"),
                    "reason": terminated.get("reason"),
                    "exitCode": terminated.get("exitCode"),
                    "finishedAt": terminated.get("finishedAt"),
                }
            )

    labels = meta.get("labels") or {}
    owner_chain = resolve_owner_chain(pod_json, ns, timeout=timeout)
    services = matching_services(ns, labels, timeout=timeout)
    records = [
        {
            "pod": name,
            "namespace": ns,
            "uid": meta.get("uid"),
            "pod_ip": status.get("podIP"),
            "node": spec.get("nodeName"),
            "phase": status.get("phase"),
            "labels": labels,
            "containers": [c.get("name") for c in spec.get("containers") or [] if c.get("name")],
            "container_statuses": container_statuses,
            "last_terminated": last_terminated,
            "deletion_timestamp": meta.get("deletionTimestamp"),
            "finalizers": meta.get("finalizers") or [],
            "owner_chain": owner_chain,
            "services": services,
        }
    ]

    evidence = [
        EvidenceItem(
            evidence_id=evidence_id("k8s", ns, name, "pod-yaml"),
            source_system="kubernetes",
            dimension="k8s",
            summary=f"Pod {ns}/{name} phase={status.get('phase')} node={spec.get('nodeName')} restarts={sum(int(s.get('restartCount') or 0) for s in container_statuses)}",
            confidence="high",
            directness="direct",
            payload=records[0],
        )
    ]
    if last_terminated:
        reason_text = ", ".join(
            f"{item.get('container')}={item.get('reason')} exit={item.get('exitCode')}"
            for item in last_terminated
        )
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id("k8s", ns, name, "last-terminated"),
                source_system="kubernetes",
                dimension="k8s",
                summary=f"Last terminated state: {reason_text}",
                severity="warning",
                confidence="high",
                directness="direct",
                payload={"last_terminated": last_terminated},
            )
        )
    if events_ok and events:
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id("k8s", ns, name, "events"),
                source_system="kubernetes",
                dimension="k8s",
                summary=events.splitlines()[-1][:500] if events.splitlines() else "Kubernetes events returned",
                severity="warning" if "Warning" in events else "info",
                confidence="high",
                directness="direct",
                payload={"events_preview": events[:4000]},
            )
        )
    if logs_ok and logs:
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id("log", ns, name, "current"),
                source_system="kubernetes",
                dimension="logs",
                summary=logs.splitlines()[-1][:500] if logs.splitlines() else "current logs returned",
                confidence="medium",
                directness="direct",
                payload={"log_preview": logs[-4000:]},
            )
        )
    if prev_ok and prev_logs:
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id("log", ns, name, "previous"),
                source_system="kubernetes",
                dimension="logs",
                summary=prev_logs.splitlines()[-1][:500] if prev_logs.splitlines() else "previous logs returned",
                confidence="high",
                directness="direct",
                payload={"log_preview": prev_logs[-4000:]},
            )
        )

    return SourceResult(
        source_system="kubernetes",
        dimension="k8s",
        coverage="present",
        records=records,
        evidence=evidence,
        query_meta={
            "describe_ok": describe_ok,
            "events_ok": events_ok,
            "logs_ok": logs_ok,
            "previous_logs_ok": prev_ok,
            "pod_yaml": yaml.safe_dump(pod_json, allow_unicode=True, sort_keys=False),
            "describe": describe,
            "events": events,
            "logs": logs,
            "previous_logs": prev_logs,
        },
    )

