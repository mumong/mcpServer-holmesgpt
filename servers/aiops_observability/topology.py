"""Lightweight topology builder for Pod-scoped AIOps cases."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any


def entity_id(kind: str, namespace: str | None, name: str | None) -> str:
    ns = namespace or "cluster"
    return f"{kind.lower()}:{ns}/{name or 'unknown'}"


def pod_entity_id(namespace: str | None, name: str | None, uid: str | None) -> str:
    ns = namespace or "cluster"
    suffix = f":{uid}" if uid else ""
    return f"k8s.pod:{ns}/{name or 'unknown'}{suffix}"


def scope_primary_topology_edge(
    edge: dict[str, Any],
    primary: dict[str, Any],
) -> dict[str, Any] | None:
    namespace = str(primary.get("namespace") or "cluster")
    name = str(primary.get("name") or "")
    if not name:
        return None
    primary_id = pod_entity_id(namespace, name, str(primary.get("uid") or "") or None)
    source = str(edge.get("source") or "")
    target = str(edge.get("target") or "")
    if _is_conflicting_primary_pod_id(source, primary_id, namespace, name):
        return None
    if _is_conflicting_primary_pod_id(target, primary_id, namespace, name):
        return None

    aliases = {
        f"pod:{namespace}/{name}",
        f"k8s.pod:{namespace}/{name}",
        primary_id,
    }
    scoped = dict(edge)
    scoped["source"] = primary_id if source in aliases else source
    scoped["target"] = primary_id if target in aliases else target
    if primary_id not in {scoped["source"], scoped["target"]}:
        return None
    return scoped


def _is_conflicting_primary_pod_id(
    value: str,
    primary_id: str,
    namespace: str,
    name: str,
) -> bool:
    if value == primary_id:
        return False
    return any(
        value.startswith(f"{prefix}:{namespace}/{name}:")
        for prefix in ("pod", "k8s.pod")
    )


def build_topology(
    *,
    primary: dict[str, Any],
    k8s_record: dict[str, Any] | None,
    evidence_refs_by_dimension: dict[str, list[str]],
    deepflow_coverage: str,
    deepflow_peers: list[str] | None = None,
    resolved_callers: list[dict[str, Any]] | None = None,
    coverage_by_dimension: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    namespace = primary.get("namespace")
    pod_name = primary.get("name")
    pod_id = pod_entity_id(namespace, pod_name, primary.get("uid"))
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

    coverage = coverage_by_dimension or {}
    metrics_coverage = str(coverage.get("metrics") or ("present" if evidence_refs_by_dimension.get("metrics") else "absent"))
    if evidence_refs_by_dimension.get("metrics") and metrics_coverage in {"present", "weak"}:
        directness = "direct" if metrics_coverage == "present" else "related_context"
        confidence = "high" if metrics_coverage == "present" else "weak"
        evidence_id = entity_id("evidence", namespace, "prometheus-metrics")
        entities.append({"entity_id": evidence_id, "kind": "Evidence", "namespace": namespace, "name": "prometheus-metrics", "attributes": {"dimension": "metrics"}, "evidence_refs": evidence_refs_by_dimension["metrics"][:5]})
        edges.append(
            {
                "source": evidence_id,
                "target": pod_id,
                "relation": "observes",
                "source_system": "prometheus",
                "directness": directness,
                "confidence": confidence,
                "evidence_refs": evidence_refs_by_dimension["metrics"][:5],
            }
        )

    logs_coverage = str(coverage.get("logs") or ("present" if evidence_refs_by_dimension.get("logs") else "absent"))
    if evidence_refs_by_dimension.get("logs") and logs_coverage in {"present", "weak"}:
        directness = "direct" if logs_coverage == "present" else "related_context"
        confidence = "medium" if logs_coverage == "present" else "weak"
        evidence_id = entity_id("evidence", namespace, "logs")
        entities.append({"entity_id": evidence_id, "kind": "Evidence", "namespace": namespace, "name": "logs", "attributes": {"dimension": "logs"}, "evidence_refs": evidence_refs_by_dimension["logs"][:5]})
        edges.append(
            {
                "source": evidence_id,
                "target": pod_id,
                "relation": "observes",
                "source_system": "logs",
                "directness": directness,
                "confidence": confidence,
                "evidence_refs": evidence_refs_by_dimension["logs"][:5],
            }
        )

    tracing_coverage = str(coverage.get("tracing") or deepflow_coverage)
    if evidence_refs_by_dimension.get("tracing") and tracing_coverage in {"present", "weak"}:
        evidence_id = entity_id("evidence", namespace, "deepflow")
        directness = "direct" if tracing_coverage == "present" else "related_context"
        confidence = "medium" if tracing_coverage == "present" else "weak"
        entities.append({"entity_id": evidence_id, "kind": "Evidence", "namespace": namespace, "name": "deepflow", "attributes": {"dimension": "tracing"}, "evidence_refs": evidence_refs_by_dimension["tracing"][:5]})
        edges.append(
            {
                "source": evidence_id,
                "target": pod_id if tracing_coverage == "present" else entity_id("node", None, node),
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
        caller_ip = str(caller.get("caller_ip") or "")
        bound_pod_ip = str(caller.get("pod_ip") or "")
        caller_uid = str(caller.get("uid") or caller.get("pod_uid") or "")
        if not caller_namespace or not caller_pod:
            continue
        caller_id = pod_entity_id(caller_namespace, caller_pod, caller_uid)
        resolved_caller_ips.add(caller_ip)
        identity_verified = _caller_identity_verified(caller)
        entities.append(
            {
                "entity_id": caller_id,
                "kind": "Pod",
                "namespace": caller_namespace,
                "name": caller_pod,
                "attributes": {
                    "uid": caller_uid or None,
                    "pod_ip": bound_pod_ip or caller_ip,
                    "role": "request_driver",
                },
                "evidence_refs": tracing_refs,
            }
        )
        edges.append(
            {
                "source": caller_id,
                "target": pod_id,
                "relation": "calls",
                "source_system": "deepflow+kubernetes",
                "directness": "direct" if identity_verified else "related_context",
                "confidence": "high" if identity_verified else "weak",
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


def _caller_identity_verified(caller: dict[str, Any]) -> bool:
    uid = str(caller.get("uid") or caller.get("pod_uid") or "")
    caller_ip = str(caller.get("caller_ip") or "")
    pod_ip = str(caller.get("pod_ip") or "")
    proof_start = _parse_time(caller.get("proof_start") or caller.get("proof_timestamp"))
    proof_end = _parse_time(caller.get("proof_end") or caller.get("proof_timestamp"))
    creation = _parse_time(caller.get("creation_timestamp"))
    deletion_value = caller.get("deletion_timestamp")
    deletion = _parse_time(deletion_value)
    if (
        caller.get("identity_verified") is not True
        or not uid
        or not caller_ip
        or caller_ip != pod_ip
        or creation is None
        or proof_start is None
        or proof_end is None
        or proof_start > proof_end
        or (deletion_value and deletion is None)
    ):
        return False
    if proof_start < creation:
        return False
    if deletion is not None and proof_end > deletion:
        return False
    return True


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


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
                "source_entity_id": edge.get("source"),
                "target_entity_id": edge.get("target"),
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
