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
    deepflow_peers: list[str] | None = None,
    resolved_callers: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    namespace = primary.get("namespace")
    pod_name = primary.get("name")
    pod_id = entity_id("pod", namespace, pod_name)
    k8s_refs = evidence_refs_by_dimension.get("k8s", [])[:5]
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

    # Workload ownership chain: Pod -> ReplicaSet -> Deployment (real structure).
    owner_source = pod_id
    for owner in (k8s_record or {}).get("owner_chain") or []:
        okind = str(owner.get("kind") or "Owner")
        oname = owner.get("name")
        if not oname:
            continue
        oid = entity_id(okind, namespace, oname)
        entities.append({"entity_id": oid, "kind": okind, "namespace": namespace, "name": oname, "attributes": {}, "evidence_refs": k8s_refs})
        edges.append(
            {
                "source": owner_source,
                "target": oid,
                "relation": "owned_by",
                "source_system": "kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": k8s_refs,
            }
        )
        owner_source = oid

    # Service topology: Service selects Pod (label-selector match).
    for svc in (k8s_record or {}).get("services") or []:
        sname = svc.get("name")
        if not sname:
            continue
        sid = entity_id("service", namespace, sname)
        entities.append(
            {
                "entity_id": sid,
                "kind": "Service",
                "namespace": namespace,
                "name": sname,
                "attributes": {"cluster_ip": svc.get("cluster_ip"), "ports": svc.get("ports")},
                "evidence_refs": k8s_refs,
            }
        )
        edges.append(
            {
                "source": sid,
                "target": pod_id,
                "relation": "selects",
                "source_system": "kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": k8s_refs,
            }
        )

    # A caller IP confirmed by DeepFlow and resolved through Kubernetes is a
    # direct Pod-to-Pod call edge, not merely an anonymous network peer.
    tracing_refs = evidence_refs_by_dimension.get("tracing", [])[:5]
    resolved_caller_ips: set[str] = set()
    for caller in resolved_callers or []:
        caller_namespace = str(caller.get("namespace") or "")
        caller_pod = str(caller.get("pod") or "")
        caller_ip = str(caller.get("pod_ip") or "")
        if not caller_namespace or not caller_pod:
            continue
        caller_id = entity_id("pod", caller_namespace, caller_pod)
        resolved_caller_ips.add(caller_ip)
        entities.append(
            {
                "entity_id": caller_id,
                "kind": "Pod",
                "namespace": caller_namespace,
                "name": caller_pod,
                "attributes": {"pod_ip": caller_ip, "role": "request_driver"},
                "evidence_refs": tracing_refs,
            }
        )
        edges.append(
            {
                "source": caller_id,
                "target": pod_id,
                "relation": "calls",
                "source_system": "deepflow+kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": tracing_refs,
            }
        )

    # Unresolved network peers remain weak related context.
    seen_peers: set[str] = set()
    for peer_ip in (deepflow_peers or []):
        if not peer_ip or peer_ip == pod_ip or peer_ip in resolved_caller_ips or peer_ip in seen_peers:
            continue
        seen_peers.add(peer_ip)
        if len(seen_peers) > 10:
            break
        peer_id = entity_id("ip", None, peer_ip)
        entities.append({"entity_id": peer_id, "kind": "IP", "name": peer_ip, "attributes": {"role": "deepflow_peer"}, "evidence_refs": tracing_refs})
        edges.append(
            {
                "source": peer_id,
                "target": pod_id,
                "relation": "communicates_with",
                "source_system": "deepflow",
                "directness": "related_context",
                "confidence": "weak",
                "evidence_refs": tracing_refs,
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


def topology_details(
    entities: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    max_entities: int = 12,
    max_edges: int = 12,
) -> dict[str, Any]:
    entity_lookup = {
        str(item.get("entity_id")): item
        for item in entities
        if item.get("entity_id")
    }
    entity_samples = [
        {
            "entity_id": item.get("entity_id"),
            "kind": item.get("kind"),
            "namespace": item.get("namespace"),
            "name": item.get("name"),
            "attributes": item.get("attributes") or {},
            "evidence_refs": (item.get("evidence_refs") or [])[:5],
        }
        for item in entities[:max_entities]
    ]
    relation_priority = {
        "calls": 0,
        "selects": 1,
        "owned_by": 2,
        "observes": 3,
        "scheduled_on": 4,
        "assigned_to": 5,
        "owns_container": 6,
        "communicates_with": 7,
    }
    ranked_edges = [
        edge
        for _, edge in sorted(
            enumerate(edges),
            key=lambda item: (
                relation_priority.get(str(item[1].get("relation") or ""), 99),
                item[0],
            ),
        )
    ]
    edge_samples: list[dict[str, Any]] = []
    for edge in ranked_edges[:max_edges]:
        source = entity_lookup.get(str(edge.get("source")), {})
        target = entity_lookup.get(str(edge.get("target")), {})
        source_kind = str(source.get("kind") or "Entity")
        target_kind = str(target.get("kind") or "Entity")
        relation = str(edge.get("relation") or "related_to")
        edge_samples.append(
            {
                "relationship": f"{source_kind} --{relation}--> {target_kind}",
                "source": source.get("name") or edge.get("source"),
                "target": target.get("name") or edge.get("target"),
                "source_system": edge.get("source_system"),
                "directness": edge.get("directness"),
                "confidence": edge.get("confidence"),
                "evidence_refs": (edge.get("evidence_refs") or [])[:5],
            }
        )
    return {
        "entities": entity_samples,
        "edges": edge_samples,
        "truncated": len(entities) > max_entities or len(edges) > max_edges,
    }
