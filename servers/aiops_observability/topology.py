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
    record = k8s_record or {}
    entities: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    _add_pod_structure(entities, edges, primary, pod_id, record, evidence_refs_by_dimension)
    _add_evidence_observers(entities, edges, namespace, pod_id, primary.get("node"), evidence_refs_by_dimension, deepflow_coverage)
    k8s_refs = evidence_refs_by_dimension.get("k8s", [])[:5]
    _add_owner_chain(entities, edges, namespace, pod_id, record, k8s_refs)
    _add_services(entities, edges, namespace, pod_id, record, k8s_refs)
    tracing_refs = evidence_refs_by_dimension.get("tracing", [])[:5]
    resolved_caller_ips = _add_resolved_callers(entities, edges, pod_id, resolved_callers, tracing_refs)
    _add_deepflow_peers(entities, edges, pod_id, primary.get("pod_ip"), deepflow_peers, resolved_caller_ips, tracing_refs)

    summary = summarize_topology(entities, edges)
    return entities, edges, summary


def _edge(
    source: str,
    target: str,
    relation: str,
    source_system: str,
    directness: str,
    confidence: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "source_system": source_system,
        "directness": directness,
        "confidence": confidence,
        "evidence_refs": evidence_refs,
    }


def _add_pod_structure(
    entities: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    primary: dict[str, Any],
    pod_id: str,
    record: dict[str, Any],
    evidence_refs_by_dimension: dict[str, list[str]],
) -> None:
    """Pod itself plus its Node, IP and containers (all direct Kubernetes facts)."""
    namespace = primary.get("namespace")
    pod_name = primary.get("name")

    def k8s_refs() -> list[str]:
        return evidence_refs_by_dimension.get("k8s", [])[:5]

    entities.append(
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
            "evidence_refs": k8s_refs(),
        }
    )

    node = primary.get("node")
    if node:
        node_id = entity_id("node", None, node)
        entities.append({"entity_id": node_id, "kind": "Node", "name": node, "attributes": {}, "evidence_refs": k8s_refs()})
        edges.append(_edge(pod_id, node_id, "scheduled_on", "kubernetes", "direct", "high", k8s_refs()))

    pod_ip = primary.get("pod_ip")
    if pod_ip:
        ip_id = entity_id("ip", None, pod_ip)
        entities.append({"entity_id": ip_id, "kind": "IP", "name": pod_ip, "attributes": {}, "evidence_refs": k8s_refs()})
        edges.append(_edge(pod_id, ip_id, "assigned_to", "kubernetes", "direct", "high", k8s_refs()))

    for container in record.get("containers") or []:
        container_id = entity_id("container", namespace, f"{pod_name}/{container}")
        entities.append({"entity_id": container_id, "kind": "Container", "namespace": namespace, "name": container, "attributes": {}, "evidence_refs": k8s_refs()})
        edges.append(_edge(pod_id, container_id, "owns_container", "kubernetes", "direct", "high", k8s_refs()))


def _add_evidence_observers(
    entities: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    namespace: str | None,
    pod_id: str,
    node: str | None,
    evidence_refs_by_dimension: dict[str, list[str]],
    deepflow_coverage: str,
) -> None:
    """Evidence entities (metrics, logs, tracing) observing the Pod."""
    if evidence_refs_by_dimension.get("metrics"):
        evidence_id = entity_id("evidence", namespace, "prometheus-metrics")
        entities.append({"entity_id": evidence_id, "kind": "Evidence", "namespace": namespace, "name": "prometheus-metrics", "attributes": {"dimension": "metrics"}, "evidence_refs": evidence_refs_by_dimension["metrics"][:5]})
        edges.append(_edge(evidence_id, pod_id, "observes", "prometheus", "direct", "high", evidence_refs_by_dimension["metrics"][:5]))

    if evidence_refs_by_dimension.get("logs"):
        evidence_id = entity_id("evidence", namespace, "logs")
        entities.append({"entity_id": evidence_id, "kind": "Evidence", "namespace": namespace, "name": "logs", "attributes": {"dimension": "logs"}, "evidence_refs": evidence_refs_by_dimension["logs"][:5]})
        edges.append(_edge(evidence_id, pod_id, "observes", "logs", "direct", "medium", evidence_refs_by_dimension["logs"][:5]))

    if evidence_refs_by_dimension.get("tracing"):
        evidence_id = entity_id("evidence", namespace, "deepflow")
        present = deepflow_coverage == "present"
        entities.append({"entity_id": evidence_id, "kind": "Evidence", "namespace": namespace, "name": "deepflow", "attributes": {"dimension": "tracing"}, "evidence_refs": evidence_refs_by_dimension["tracing"][:5]})
        edges.append(
            _edge(
                evidence_id,
                pod_id if present else entity_id("node", None, node),
                "observes",
                "deepflow",
                "direct" if present else "related_context",
                "medium" if present else "weak",
                evidence_refs_by_dimension["tracing"][:5],
            )
        )


def _add_owner_chain(
    entities: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    namespace: str | None,
    pod_id: str,
    record: dict[str, Any],
    k8s_refs: list[str],
) -> None:
    """Workload ownership chain: Pod -> ReplicaSet -> Deployment (real structure)."""
    owner_source = pod_id
    for owner in record.get("owner_chain") or []:
        okind = str(owner.get("kind") or "Owner")
        oname = owner.get("name")
        if not oname:
            continue
        oid = entity_id(okind, namespace, oname)
        entities.append({"entity_id": oid, "kind": okind, "namespace": namespace, "name": oname, "attributes": {}, "evidence_refs": k8s_refs})
        edges.append(_edge(owner_source, oid, "owned_by", "kubernetes", "direct", "high", k8s_refs))
        owner_source = oid


def _add_services(
    entities: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    namespace: str | None,
    pod_id: str,
    record: dict[str, Any],
    k8s_refs: list[str],
) -> None:
    """Service topology: Service selects Pod (label-selector match)."""
    for svc in record.get("services") or []:
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
        edges.append(_edge(sid, pod_id, "selects", "kubernetes", "direct", "high", k8s_refs))


def _add_resolved_callers(
    entities: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    pod_id: str,
    resolved_callers: list[dict[str, Any]] | None,
    tracing_refs: list[str],
) -> set[str]:
    """A caller IP confirmed by DeepFlow and resolved through Kubernetes is a
    direct Pod-to-Pod call edge, not merely an anonymous network peer."""
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
        edges.append(_edge(caller_id, pod_id, "calls", "deepflow+kubernetes", "direct", "high", tracing_refs))
    return resolved_caller_ips


def _add_deepflow_peers(
    entities: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    pod_id: str,
    pod_ip: str | None,
    deepflow_peers: list[str] | None,
    resolved_caller_ips: set[str],
    tracing_refs: list[str],
) -> None:
    """Unresolved network peers remain weak related context."""
    seen_peers: set[str] = set()
    for peer_ip in (deepflow_peers or []):
        if not peer_ip or peer_ip == pod_ip or peer_ip in resolved_caller_ips or peer_ip in seen_peers:
            continue
        seen_peers.add(peer_ip)
        if len(seen_peers) > 10:
            break
        peer_id = entity_id("ip", None, peer_ip)
        entities.append({"entity_id": peer_id, "kind": "IP", "name": peer_ip, "attributes": {"role": "deepflow_peer"}, "evidence_refs": tracing_refs})
        edges.append(_edge(peer_id, pod_id, "communicates_with", "deepflow", "related_context", "weak", tracing_refs))


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
