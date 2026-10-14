"""Query bounded, source-backed Kubernetes relationships for one Pod."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .. import schemas
from ..collectors.kubernetes import (
    get_pod,
    matching_services_with_status,
    resolve_owner_chain_with_status,
)
from ..config import get_config
from .common import (
    MAX_WINDOW_SECONDS,
    bounded_window,
    error_result,
    evidence_ref,
    resolve_pod_scope,
)


MAX_OWNER_RELATIONSHIPS = 2
MAX_SERVICE_RELATIONSHIPS = 1
MAX_CONTAINER_RELATIONSHIPS = 1
MAX_RELATIONSHIPS = (
    1
    + MAX_OWNER_RELATIONSHIPS
    + MAX_SERVICE_RELATIONSHIPS
    + MAX_CONTAINER_RELATIONSHIPS
)
RELATIONSHIP_TYPES = (
    "owned_by",
    "selects",
    "scheduled_on",
    "owns_container",
)


def execute(arguments: dict[str, Any]) -> dict[str, Any]:
    namespace = str(arguments.get("namespace") or "").strip()
    pod = str(arguments.get("pod") or "").strip()
    purpose = str(arguments.get("purpose") or "").strip()
    base_query = {
        "relationship_types": list(RELATIONSHIP_TYPES),
        "scope": {"kind": "Pod", "namespace": namespace, "pod": pod},
    }

    def reject(code: str, message: str, entity: dict[str, Any] | None = None) -> dict[str, Any]:
        return error_result(
            source_system="kubernetes",
            dimension="topology",
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            code=code,
            message=message,
            entity=entity,
            query=base_query,
        )

    if not namespace or not pod or not purpose:
        return reject("invalid_arguments", "namespace, pod and purpose are required")

    entity, entity_error = resolve_pod_scope(
        namespace,
        pod,
        arguments.get("pod_uid"),
    )
    if entity_error:
        return reject(entity_error["code"], entity_error["message"])
    assert entity is not None

    window, window_error = bounded_window(
        arguments,
        not_before=entity.get("creation_timestamp"),
    )
    if window_error:
        return reject(window_error["code"], window_error["message"], entity)
    assert window is not None
    base_query["time_range"] = window

    timeout = get_config().timeout_seconds
    pod_json, pod_error = get_pod(namespace, pod, timeout=timeout)
    if pod_error or not pod_json:
        return reject(
            "kubernetes_error",
            str(pod_error or "Kubernetes Pod query returned no object"),
            entity,
        )
    if _pod_uid(pod_json) not in {"", str(entity.get("pod_uid") or "")}:
        return _lifecycle_error(
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            entity=entity,
            query=base_query,
            message="target Pod identity changed while topology was collected",
        )

    spec = pod_json.get("spec") or {}
    labels = (pod_json.get("metadata") or {}).get("labels") or {}
    owner_chain, owner_status = resolve_owner_chain_with_status(
        pod_json,
        namespace,
        timeout=timeout,
    )
    services, service_status = matching_services_with_status(
        namespace,
        labels,
        timeout=timeout,
    )
    base_query["source_status"] = {
        "owners": _source_status(owner_status),
        "services": _source_status(service_status),
    }
    limitations = _source_limitations(
        owners=owner_status,
        services=service_status,
    )
    observed_at = schemas.isoformat(schemas.utc_now()) or ""

    pod_entity = _entity(
        "Pod",
        namespace,
        pod,
        uid=str(entity.get("pod_uid") or "") or None,
    )
    context = {"namespace": namespace, "pod": pod, "observed_at": observed_at}
    container_names = [
        str(item.get("name"))
        for item in spec.get("containers") or []
        if item.get("name")
    ]
    retained = {
        "owners": owner_chain[:MAX_OWNER_RELATIONSHIPS],
        "services": services[:MAX_SERVICE_RELATIONSHIPS],
        "containers": container_names[:MAX_CONTAINER_RELATIONSHIPS],
    }
    facts = [
        *_owner_facts(context, pod_entity, retained["owners"]),
        *_service_facts(context, pod_entity, retained["services"], labels),
        *_node_facts(context, pod_entity, str(spec.get("nodeName") or "")),
        *_container_facts(context, pod_entity, retained["containers"]),
    ]

    current_entity, lifecycle_error = resolve_pod_scope(
        namespace,
        pod,
        entity.get("pod_uid"),
    )
    if lifecycle_error or _entity_lifecycle_changed(entity, current_entity):
        return _lifecycle_error(
            namespace=namespace,
            pod=pod,
            purpose=purpose,
            entity=entity,
            query=base_query,
            message=(
                str((lifecycle_error or {}).get("message") or "")
                or "target Pod identity changed while topology was collected"
            ),
        )

    matched_relationships = (
        len(owner_chain)
        + len(services)
        + (1 if spec.get("nodeName") else 0)
        + len(container_names)
    )
    truncation_stages = _truncation_stages(
        (
            ("max_owner_relationships", len(owner_chain), len(retained["owners"])),
            ("max_service_relationships", len(services), len(retained["services"])),
            ("max_container_relationships", len(container_names), len(retained["containers"])),
        )
    )
    return _query_result(
        entity=entity,
        purpose=purpose,
        query=base_query,
        facts=facts,
        limitations=limitations,
        matched_relationships=matched_relationships,
        truncation_stages=truncation_stages,
    )


def _owner_facts(
    context: dict[str, Any],
    pod_entity: dict[str, Any],
    owners: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Walk the retained ownerReferences chain: Pod -> ReplicaSet -> ..."""
    facts: list[dict[str, Any]] = []
    owner_source = pod_entity
    for owner in owners:
        target = _entity(
            str(owner.get("kind") or "Owner"),
            context["namespace"],
            str(owner.get("name") or ""),
            uid=str(owner.get("uid") or "") or None,
        )
        if not target["name"]:
            continue
        facts.append(
            _relationship_fact(
                **context,
                relation="owned_by",
                source=owner_source,
                target=target,
                source_field="metadata.ownerReferences",
            )
        )
        owner_source = target
    return facts


def _service_facts(
    context: dict[str, Any],
    pod_entity: dict[str, Any],
    services: list[dict[str, Any]],
    labels: dict[str, Any],
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for service in services:
        service_name = str(service.get("name") or "")
        selector = service.get("selector") or {}
        if not service_name or not selector:
            continue
        selector = _compact_mapping(selector, max_items=4)
        matched_labels = {
            str(key): labels.get(key)
            for key in selector
            if key in labels
        }
        facts.append(
            _relationship_fact(
                **context,
                relation="selects",
                source=_entity(
                    "Service",
                    context["namespace"],
                    service_name,
                    uid=str(service.get("uid") or "") or None,
                    attributes={
                        "cluster_ip": service.get("cluster_ip"),
                    },
                ),
                target=pod_entity,
                source_field="service.spec.selector + pod.metadata.labels",
                selector=selector,
                matched_pod_labels=matched_labels,
                cluster_ip=service.get("cluster_ip"),
                ports=_compact_ports(service.get("ports")),
            )
        )
    return facts


def _node_facts(
    context: dict[str, Any],
    pod_entity: dict[str, Any],
    node_name: str,
) -> list[dict[str, Any]]:
    if not node_name:
        return []
    return [
        _relationship_fact(
            **context,
            relation="scheduled_on",
            source=pod_entity,
            target=_entity("Node", None, node_name),
            source_field="pod.spec.nodeName",
        )
    ]


def _container_facts(
    context: dict[str, Any],
    pod_entity: dict[str, Any],
    container_names: list[str],
) -> list[dict[str, Any]]:
    return [
        _relationship_fact(
            **context,
            relation="owns_container",
            source=pod_entity,
            target=_entity(
                "Container",
                context["namespace"],
                container_name,
                attributes={"pod": context["pod"]},
            ),
            source_field="pod.spec.containers[].name",
        )
        for container_name in container_names
    ]


def _truncation_stages(limits) -> list[dict[str, Any]]:
    """One tool_query stage per relationship cap that dropped rows."""
    return [
        {
            "stage": "tool_query",
            "reason": reason,
            "before": before,
            "after": after,
        }
        for reason, before, after in limits
        if before > after
    ]


def _query_result(
    *,
    entity: dict[str, Any],
    purpose: str,
    query: dict[str, Any],
    facts: list[dict[str, Any]],
    limitations: list[Any],
    matched_relationships: int,
    truncation_stages: list[dict[str, Any]],
) -> dict[str, Any]:
    truncated = bool(truncation_stages)
    coverage = (
        "partial"
        if limitations
        else "present"
        if facts
        else "empty"
    )
    entities, edges, topology_summary = _topology_graph(
        facts,
        coverage=coverage,
    )
    refs = [fact["ref"] for fact in facts]
    result = {
        "ok": not limitations,
        "status": "query_partial" if limitations else "query_succeeded",
        "source_system": "kubernetes",
        "dimension": "topology",
        "entity": _compact_primary_entity(entity),
        "purpose": purpose,
        "coverage": coverage,
        "directness": "related_context" if limitations else "direct",
        "query": query,
        "facts": facts,
        "samples": [],
        "entities": entities,
        "edges": edges,
        "topology_summary": topology_summary,
        "evidence_refs": refs,
        "counts": {
            "matched": matched_relationships,
            "retrieved": matched_relationships,
            "normalized": matched_relationships,
            "returned": len(facts),
            "dropped": max(0, matched_relationships - len(facts)),
        },
        "truncation": {
            "truncated": truncated,
            "stages": truncation_stages,
        },
        "truncated": truncated,
        "limitations": limitations,
        "limits": {
            "max_window_seconds": MAX_WINDOW_SECONDS,
            "max_relationships": MAX_RELATIONSHIPS,
            "max_owner_relationships": MAX_OWNER_RELATIONSHIPS,
            "max_service_relationships": MAX_SERVICE_RELATIONSHIPS,
            "max_container_relationships": MAX_CONTAINER_RELATIONSHIPS,
        },
    }
    if limitations:
        result["error"] = {
            "code": "topology_sources_incomplete",
            "message": "one or more Kubernetes topology lookups were incomplete",
        }
    return result


def _relationship_fact(
    *,
    namespace: str,
    pod: str,
    relation: str,
    source: dict[str, Any],
    target: dict[str, Any],
    source_field: str,
    observed_at: str,
    **details: Any,
) -> dict[str, Any]:
    value = {
        "relation": relation,
        "relationship": (
            f"{source.get('kind') or 'Entity'} "
            f"--{relation}--> "
            f"{target.get('kind') or 'Entity'}"
        ),
        "source": source,
        "target": target,
        "source_field": source_field,
        **details,
    }
    return {
        "ref": evidence_ref("topology", namespace, pod, value),
        "source_system": "kubernetes",
        "dimension": "topology",
        "entity": f"{namespace}/{pod}",
        "name": "kubernetes.relationship",
        "value": value,
        "relation": relation,
        "observed_at": observed_at,
        "directness": "direct",
        "confidence": "high",
    }


def _entity(
    kind: str,
    namespace: str | None,
    name: str,
    *,
    uid: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entity = {"kind": kind, "name": name}
    if namespace:
        entity["namespace"] = namespace
    if uid:
        entity["uid"] = uid
    if attributes:
        entity["attributes"] = attributes
    return entity


def _topology_graph(
    facts: list[dict[str, Any]],
    *,
    coverage: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    entities_by_id: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for fact in facts:
        value = fact.get("value") if isinstance(fact.get("value"), dict) else {}
        source = value.get("source") if isinstance(value.get("source"), dict) else {}
        target = value.get("target") if isinstance(value.get("target"), dict) else {}
        source_id = _entity_id(source)
        target_id = _entity_id(target)
        for entity_id, entity in ((source_id, source), (target_id, target)):
            if not entity_id or entity_id in entities_by_id:
                continue
            compact = {
                "entity_id": entity_id,
                "kind": entity.get("kind"),
                "name": entity.get("name"),
                "source_system": "kubernetes",
            }
            for key in ("namespace", "uid", "attributes"):
                if entity.get(key) not in (None, {}, []):
                    compact[key] = entity.get(key)
            entities_by_id[entity_id] = compact
        edges.append(
            {
                "relation": value.get("relation") or fact.get("relation"),
                "source": source_id,
                "target": target_id,
                "source_system": "kubernetes",
                "evidence_ref": fact.get("ref"),
            }
        )

    entities = list(entities_by_id.values())
    relations = Counter(str(edge.get("relation") or "unknown") for edge in edges)
    summary = {
        "entity_count": len(entities),
        "edge_count": len(edges),
        "relations": dict(relations),
        "coverage": coverage,
        "source_system": "kubernetes",
    }
    return entities, edges, summary


def _compact_primary_entity(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        key: entity.get(key)
        for key in (
            "kind",
            "namespace",
            "pod",
            "pod_uid",
            "creation_timestamp",
            "lifecycle_changed",
            "pod_ip",
            "node",
        )
        if entity.get(key) is not None
    }


def _entity_id(entity: dict[str, Any]) -> str:
    kind = str(entity.get("kind") or "entity").lower()
    namespace = str(entity.get("namespace") or "cluster")
    name = str(entity.get("name") or "")
    if kind == "container":
        pod = str((entity.get("attributes") or {}).get("pod") or "")
        if pod:
            name = f"{pod}/{name}"
    return f"{kind}:{namespace}/{name}" if name else ""


def _source_status(status: dict[str, Any]) -> str:
    value = str(status.get("status") or "error")
    if value in {"complete", "empty", "partial", "error"}:
        return value
    return "error"


def _source_limitations(
    **statuses: dict[str, Any],
) -> list[dict[str, str]]:
    limitations: list[dict[str, str]] = []
    for source, status in statuses.items():
        value = _source_status(status)
        if value not in {"partial", "error"}:
            continue
        limitations.append(
            {
                "source": source,
                "status": value,
                "message": str(
                    status.get("error")
                    or f"{source} topology lookup was {value}"
                )[:500],
            }
        )
    return limitations


def _compact_mapping(
    value: Any,
    *,
    max_items: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))[
            :max_items
        ]
    }


def _compact_ports(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {
            key: item.get(key)
            for key in ("port", "targetPort")
            if item.get(key) is not None
        }
        for item in value[:2]
        if isinstance(item, dict)
    ]


def _pod_uid(pod_json: dict[str, Any]) -> str:
    return str((pod_json.get("metadata") or {}).get("uid") or "")


def _entity_lifecycle_changed(
    initial: dict[str, Any],
    current: dict[str, Any] | None,
) -> bool:
    if current is None:
        return True
    for key in ("pod_uid", "creation_timestamp"):
        initial_value = str(initial.get(key) or "")
        current_value = str(current.get(key) or "")
        if initial_value and current_value and initial_value != current_value:
            return True
    return False


def _lifecycle_error(
    *,
    namespace: str,
    pod: str,
    purpose: str,
    entity: dict[str, Any],
    query: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    return error_result(
        source_system="kubernetes",
        dimension="topology",
        namespace=namespace,
        pod=pod,
        purpose=purpose,
        code="pod_lifecycle_changed",
        message=message,
        entity={**entity, "lifecycle_changed": True},
        query=query,
    )
