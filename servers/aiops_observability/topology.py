"""Lightweight topology builder for Pod-scoped AIOps cases."""

from __future__ import annotations

from collections import Counter
from typing import Any


def entity_id(kind: str, namespace: str | None, name: str | None) -> str:
    ns = namespace or "cluster"
    return f"{kind.lower()}:{ns}/{name or 'unknown'}"


def build_topology(
    *,
    primary: dict[str, Any],
    k8s_record: dict[str, Any] | None,
    evidence_refs_by_dimension: dict[str, list[str]],
    deepflow_coverage: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    namespace = primary.get("namespace")
    pod_name = primary.get("name")
    pod_id = entity_id("pod", namespace, pod_name)
    entities: list[dict[str, Any]] = [
        {
            "entity_id": pod_id,
            "kind": "Pod",
            "namespace": namespace,
            "name": pod_name,
            "attributes": {
                "uid": primary.get("uid"),
                "pod_ip": primary.get("pod_ip"),
                "node": primary.get("node"),
            },
            "evidence_refs": evidence_refs_by_dimension.get("k8s", [])[:5],
        }
    ]
    edges: list[dict[str, Any]] = []

    node = primary.get("node")
    if node:
        node_id = entity_id("node", None, node)
        entities.append({"entity_id": node_id, "kind": "Node", "name": node, "attributes": {}, "evidence_refs": evidence_refs_by_dimension.get("k8s", [])[:5]})
        edges.append(
            {
                "source": pod_id,
                "target": node_id,
                "relation": "scheduled_on",
                "source_system": "kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": evidence_refs_by_dimension.get("k8s", [])[:5],
            }
        )

    pod_ip = primary.get("pod_ip")
    if pod_ip:
        ip_id = entity_id("ip", None, pod_ip)
        entities.append({"entity_id": ip_id, "kind": "IP", "name": pod_ip, "attributes": {}, "evidence_refs": evidence_refs_by_dimension.get("k8s", [])[:5]})
        edges.append(
            {
                "source": pod_id,
                "target": ip_id,
                "relation": "assigned_to",
                "source_system": "kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": evidence_refs_by_dimension.get("k8s", [])[:5],
            }
        )

    for container in (k8s_record or {}).get("containers") or []:
        container_id = entity_id("container", namespace, f"{pod_name}/{container}")
        entities.append({"entity_id": container_id, "kind": "Container", "namespace": namespace, "name": container, "attributes": {}, "evidence_refs": evidence_refs_by_dimension.get("k8s", [])[:5]})
        edges.append(
            {
                "source": pod_id,
                "target": container_id,
                "relation": "owns_container",
                "source_system": "kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": evidence_refs_by_dimension.get("k8s", [])[:5],
            }
        )

    if evidence_refs_by_dimension.get("metrics"):
        evidence_id = entity_id("evidence", namespace, "prometheus-metrics")
        entities.append({"entity_id": evidence_id, "kind": "Evidence", "namespace": namespace, "name": "prometheus-metrics", "attributes": {"dimension": "metrics"}, "evidence_refs": evidence_refs_by_dimension["metrics"][:5]})
        edges.append(
            {
                "source": evidence_id,
                "target": pod_id,
                "relation": "observes",
                "source_system": "prometheus",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": evidence_refs_by_dimension["metrics"][:5],
            }
        )

    if evidence_refs_by_dimension.get("logs"):
        evidence_id = entity_id("evidence", namespace, "logs")
        entities.append({"entity_id": evidence_id, "kind": "Evidence", "namespace": namespace, "name": "logs", "attributes": {"dimension": "logs"}, "evidence_refs": evidence_refs_by_dimension["logs"][:5]})
        edges.append(
            {
                "source": evidence_id,
                "target": pod_id,
                "relation": "observes",
                "source_system": "logs",
                "directness": "direct",
                "confidence": "medium",
                "evidence_refs": evidence_refs_by_dimension["logs"][:5],
            }
        )

    if evidence_refs_by_dimension.get("tracing"):
        evidence_id = entity_id("evidence", namespace, "deepflow")
        directness = "direct" if deepflow_coverage == "present" else "related_context"
        confidence = "medium" if deepflow_coverage == "present" else "weak"
        entities.append({"entity_id": evidence_id, "kind": "Evidence", "namespace": namespace, "name": "deepflow", "attributes": {"dimension": "tracing"}, "evidence_refs": evidence_refs_by_dimension["tracing"][:5]})
        edges.append(
            {
                "source": evidence_id,
                "target": pod_id if deepflow_coverage == "present" else entity_id("node", None, node),
                "relation": "observes",
                "source_system": "deepflow",
                "directness": directness,
                "confidence": confidence,
                "evidence_refs": evidence_refs_by_dimension["tracing"][:5],
            }
        )

    summary = summarize_topology(entities, edges)
    return entities, edges, summary


def summarize_topology(entities: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "entity_count": len(entities),
        "edge_count": len(edges),
        "entity_kinds": dict(Counter(str(item.get("kind", "unknown")) for item in entities)),
        "relations": dict(Counter(str(item.get("relation", "unknown")) for item in edges)),
        "directness": dict(Counter(str(item.get("directness", "unknown")) for item in edges)),
        "confidence": dict(Counter(str(item.get("confidence", "unknown")) for item in edges)),
    }

