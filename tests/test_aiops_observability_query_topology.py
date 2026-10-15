import json
import time
import unittest
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import patch

from servers.aiops_observability.query import common, topology
from servers.holmes_tools import aiops_observability_query


def _oversized_topology_payload():
    namespace = "namespace"
    pod = "pod-" + ("p" * 40)
    pod_uid = "u" * 36
    observed_at = "2026-07-23T12:00:00Z"
    pod_entity = topology._entity("Pod", namespace, pod, uid=pod_uid)
    selector = {f"key{index}-" + ("k" * 20): "v" * 60 for index in range(4)}
    facts = [
        topology._relationship_fact(
            namespace=namespace,
            pod=pod,
            relation="owned_by",
            source=pod_entity,
            target=topology._entity(
                "ReplicaSet",
                namespace,
                "owner-" + ("r" * 40),
                uid="a" * 36,
            ),
            source_field="metadata.ownerReferences",
            observed_at=observed_at,
        ),
        topology._relationship_fact(
            namespace=namespace,
            pod=pod,
            relation="selects",
            source=topology._entity(
                "Service",
                namespace,
                "service-" + ("s" * 40),
                uid="b" * 36,
                attributes={"cluster_ip": "10.96.0.20"},
            ),
            target=pod_entity,
            source_field="service.spec.selector + pod.metadata.labels",
            observed_at=observed_at,
            selector=selector,
            matched_pod_labels=selector,
            cluster_ip="10.96.0.20",
            ports=[
                {"port": 8080, "targetPort": "http"},
            ],
        ),
        topology._relationship_fact(
            namespace=namespace,
            pod=pod,
            relation="scheduled_on",
            source=pod_entity,
            target=topology._entity("Node", None, "node-" + ("d" * 40)),
            source_field="pod.spec.nodeName",
            observed_at=observed_at,
        ),
        topology._relationship_fact(
            namespace=namespace,
            pod=pod,
            relation="owns_container",
            source=pod_entity,
            target=topology._entity(
                "Container",
                namespace,
                "container-" + ("c" * 40),
                attributes={"pod": pod},
            ),
            source_field="pod.spec.containers[].name",
            observed_at=observed_at,
        ),
    ]
    entities, edges, summary = topology._topology_graph(
        facts,
        coverage="present",
    )
    return {
        "ok": True,
        "status": "query_succeeded",
        "source_system": "kubernetes",
        "dimension": "topology",
        "entity": {
            "kind": "Pod",
            "namespace": namespace,
            "pod": pod,
            "pod_uid": pod_uid,
        },
        "purpose": "verify topology budget",
        "coverage": "present",
        "directness": "direct",
        "query": {
            "relationship_types": list(topology.RELATIONSHIP_TYPES),
            "scope": {
                "kind": "Pod",
                "namespace": namespace,
                "pod": pod,
            },
        },
        "facts": facts,
        "samples": [],
        "entities": entities,
        "edges": edges,
        "topology_summary": summary,
        "evidence_refs": [fact["ref"] for fact in facts],
        "truncated": False,
        "limitations": [],
        "limits": {},
    }


def _max_identifier_topology_payload():
    namespace = "n" * 63
    pod = "p" * 253
    pod_uid = "12345678-1234-1234-1234-123456789012"
    observed_at = "2026-07-23T12:00:00Z"
    pod_entity = topology._entity("Pod", namespace, pod, uid=pod_uid)
    replica_set = topology._entity(
        "ReplicaSet",
        namespace,
        "r" * 253,
        uid="22345678-1234-1234-1234-123456789012",
    )
    deployment = topology._entity(
        "Deployment",
        namespace,
        "d" * 253,
        uid="32345678-1234-1234-1234-123456789012",
    )
    selector = {"k" * 63: "v" * 63}
    facts = [
        topology._relationship_fact(
            namespace=namespace,
            pod=pod,
            relation="owned_by",
            source=pod_entity,
            target=replica_set,
            source_field="metadata.ownerReferences",
            observed_at=observed_at,
        ),
        topology._relationship_fact(
            namespace=namespace,
            pod=pod,
            relation="owned_by",
            source=replica_set,
            target=deployment,
            source_field="metadata.ownerReferences",
            observed_at=observed_at,
        ),
        topology._relationship_fact(
            namespace=namespace,
            pod=pod,
            relation="selects",
            source=topology._entity(
                "Service",
                namespace,
                "s" * 63,
                uid="42345678-1234-1234-1234-123456789012",
                attributes={"cluster_ip": "10.96.255.255"},
            ),
            target=pod_entity,
            source_field="service.spec.selector + pod.metadata.labels",
            observed_at=observed_at,
            selector=selector,
            matched_pod_labels=selector,
            cluster_ip="10.96.255.255",
            ports=[{"port": 65535, "targetPort": "t" * 15}],
        ),
        topology._relationship_fact(
            namespace=namespace,
            pod=pod,
            relation="scheduled_on",
            source=pod_entity,
            target=topology._entity("Node", None, "x" * 253),
            source_field="pod.spec.nodeName",
            observed_at=observed_at,
        ),
        topology._relationship_fact(
            namespace=namespace,
            pod=pod,
            relation="owns_container",
            source=pod_entity,
            target=topology._entity(
                "Container",
                namespace,
                "c" * 63,
                attributes={"pod": pod},
            ),
            source_field="pod.spec.containers[].name",
            observed_at=observed_at,
        ),
    ]
    entities, edges, summary = topology._topology_graph(
        facts,
        coverage="present",
    )
    return {
        "ok": True,
        "status": "query_succeeded",
        "source_system": "kubernetes",
        "dimension": "topology",
        "entity": {
            "kind": "Pod",
            "namespace": namespace,
            "pod": pod,
            "pod_uid": pod_uid,
            "node": "x" * 253,
        },
        "purpose": "verify maximum legal Kubernetes identifiers remain auditable",
        "coverage": "present",
        "directness": "direct",
        "query": {
            "relationship_types": list(topology.RELATIONSHIP_TYPES),
            "scope": {
                "kind": "Pod",
                "namespace": namespace,
                "pod": pod,
            },
            "time_range": {
                "start": "2026-07-23T11:30:00Z",
                "end": "2026-07-23T12:00:00Z",
            },
        },
        "facts": facts,
        "samples": [],
        "entities": entities,
        "edges": edges,
        "topology_summary": summary,
        "evidence_refs": [fact["ref"] for fact in facts],
        "truncated": False,
        "limitations": [],
        "limits": {
            "max_window_seconds": 7200,
            "max_relationships": 5,
            "max_owner_relationships": 2,
            "max_service_relationships": 1,
            "max_container_relationships": 1,
        },
    }


class TopologyQueryTests(unittest.TestCase):
    def test_execute_returns_source_backed_kubernetes_relationships(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "api-abc",
            "pod_uid": "pod-uid",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node-1",
            "containers": ["api", "sidecar"],
        }
        pod_json = {
            "metadata": {
                "name": "api-abc",
                "namespace": "ns",
                "uid": "pod-uid",
                "labels": {"app": "api", "tier": "backend"},
            },
            "spec": {
                "nodeName": "node-1",
                "containers": [{"name": "api"}, {"name": "sidecar"}],
            },
        }
        owner_chain = [
            {"kind": "ReplicaSet", "name": "api-abc", "uid": "rs-uid"},
            {"kind": "Deployment", "name": "api", "uid": "deployment-uid"},
        ]
        services = [
            {
                "name": "api",
                "uid": "service-uid",
                "selector": {"app": "api"},
                "cluster_ip": "10.96.0.20",
                "ports": [{"port": 8080, "targetPort": 8080}],
            }
        ]

        with (
            patch.object(
                topology,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch.object(topology, "get_pod", return_value=(pod_json, None)),
            patch.object(
                topology,
                "resolve_owner_chain_with_status",
                return_value=(owner_chain, {"status": "complete"}),
            ) as owners,
            patch.object(
                topology,
                "matching_services_with_status",
                return_value=(services, {"status": "complete"}),
            ) as service_query,
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
            ),
        ):
            result = topology.execute(
                {
                    "namespace": "ns",
                    "pod": "api-abc",
                    "pod_uid": "pod-uid",
                    "purpose": "identify workload and service relationships",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["source_system"], "kubernetes")
        self.assertEqual(result["dimension"], "topology")
        self.assertEqual(result["coverage"], "present")
        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-23T11:30:00Z",
                "end": "2026-07-23T12:00:00Z",
            },
        )
        self.assertEqual(
            {fact["relation"] for fact in result["facts"]},
            {"owned_by", "selects", "scheduled_on", "owns_container"},
        )
        self.assertTrue(all(fact["source_system"] == "kubernetes" for fact in result["facts"]))
        self.assertTrue(all(fact["directness"] == "direct" for fact in result["facts"]))
        self.assertTrue(all(fact["ref"] for fact in result["facts"]))
        for fact in result["facts"]:
            value = fact["value"]
            self.assertEqual(value["relation"], fact["relation"])
            self.assertIn(f"--{fact['relation']}-->", value["relationship"])
            self.assertTrue(value["source"])
            self.assertTrue(value["target"])
            self.assertTrue(value["source_field"])
        self.assertEqual(
            result["evidence_refs"],
            [fact["ref"] for fact in result["facts"]],
        )
        self.assertEqual(
            {edge["evidence_ref"] for edge in result["edges"]},
            set(result["evidence_refs"]),
        )
        self.assertTrue(all(edge["source_system"] == "kubernetes" for edge in result["edges"]))
        self.assertEqual(
            result["topology_summary"]["relations"],
            {
                "owned_by": 2,
                "selects": 1,
                "scheduled_on": 1,
                "owns_container": 1,
            },
        )
        self.assertEqual(result["topology_summary"]["edge_count"], 5)
        self.assertEqual(result["topology_summary"]["entity_count"], 6)
        self.assertTrue(all(entity["entity_id"] for entity in result["entities"]))
        service_fact = next(fact for fact in result["facts"] if fact["relation"] == "selects")
        self.assertEqual(service_fact["value"]["selector"], {"app": "api"})
        self.assertEqual(
            service_fact["value"]["matched_pod_labels"],
            {"app": "api"},
        )
        owners.assert_called_once_with(pod_json, "ns", timeout=30)
        service_query.assert_called_once_with(
            "ns",
            {"app": "api", "tier": "backend"},
            timeout=30,
        )

    def test_execute_caps_each_relationship_source(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "large-pod",
            "pod_uid": "pod-uid",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node-1",
            "containers": [f"container-{index}" for index in range(20)],
        }
        pod_json = {
            "metadata": {
                "name": "large-pod",
                "namespace": "ns",
                "uid": "pod-uid",
                "labels": {"app": "large"},
            },
            "spec": {
                "nodeName": "node-1",
                "containers": [{"name": f"container-{index}"} for index in range(20)],
            },
        }
        services = [
            {
                "name": f"service-{index}",
                "uid": f"service-{index}-uid",
                "selector": {"app": "large"},
                "cluster_ip": f"10.96.0.{index + 1}",
                "ports": [],
            }
            for index in range(20)
        ]

        with (
            patch.object(
                topology,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch.object(topology, "get_pod", return_value=(pod_json, None)),
            patch.object(
                topology,
                "resolve_owner_chain_with_status",
                return_value=([], {"status": "empty"}),
            ),
            patch.object(
                topology,
                "matching_services_with_status",
                return_value=(services, {"status": "complete"}),
            ),
        ):
            result = topology.execute(
                {
                    "namespace": "ns",
                    "pod": "large-pod",
                    "purpose": "bound topology breadth",
                }
            )

        self.assertTrue(result["ok"])
        self.assertLessEqual(
            len(result["facts"]),
            result["limits"]["max_relationships"],
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(
            sum(fact["relation"] == "owns_container" for fact in result["facts"]),
            result["limits"]["max_container_relationships"],
        )
        self.assertEqual(
            sum(fact["relation"] == "selects" for fact in result["facts"]),
            result["limits"]["max_service_relationships"],
        )

    def test_live_snapshot_is_not_backdated_to_explicit_context_window(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "api-abc",
            "pod_uid": "pod-uid",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node-1",
            "containers": ["api"],
        }
        pod_json = {
            "metadata": {
                "name": "api-abc",
                "namespace": "ns",
                "uid": "pod-uid",
                "labels": {},
            },
            "spec": {"nodeName": "node-1", "containers": [{"name": "api"}]},
        }

        with (
            patch.object(
                topology,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch.object(topology, "get_pod", return_value=(pod_json, None)),
            patch.object(
                topology,
                "resolve_owner_chain_with_status",
                return_value=([], {"status": "empty"}),
            ),
            patch.object(
                topology,
                "matching_services_with_status",
                return_value=([], {"status": "empty"}),
            ),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
            ),
        ):
            result = topology.execute(
                {
                    "namespace": "ns",
                    "pod": "api-abc",
                    "purpose": "compare current topology with a prior incident window",
                    "start": "2026-07-22T10:00:00Z",
                    "end": "2026-07-22T10:30:00Z",
                }
            )

        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-22T10:00:00Z",
                "end": "2026-07-22T10:30:00Z",
            },
        )
        self.assertTrue(result["facts"])
        self.assertTrue(all(fact["observed_at"] == "2026-07-23T12:00:00Z" for fact in result["facts"]))

    def test_call_tool_keeps_topology_result_under_six_kib(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "large-pod",
            "pod_uid": "pod-uid",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node-with-a-long-but-valid-name",
            "containers": [f"container-{index}" for index in range(20)],
        }
        pod_json = {
            "metadata": {
                "name": "large-pod",
                "namespace": "ns",
                "uid": "pod-uid",
                "labels": {"app": "large", "component": "observability-test"},
            },
            "spec": {
                "nodeName": "node-with-a-long-but-valid-name",
                "containers": [{"name": f"container-{index}"} for index in range(20)],
            },
        }
        services = [
            {
                "name": f"service-{index}",
                "uid": f"service-{index}-uid",
                "selector": {
                    "app": "large",
                    "component": "observability-test",
                },
                "cluster_ip": f"10.96.0.{index + 1}",
                "ports": [{"port": 8080 + index, "targetPort": f"http-{index}"}],
            }
            for index in range(20)
        ]
        owners = [
            {
                "kind": "ReplicaSet" if index == 0 else "Deployment",
                "name": f"owner-{index}-with-a-long-but-valid-name",
                "uid": f"owner-{index}-uid",
            }
            for index in range(8)
        ]

        with (
            patch.object(
                topology,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch.object(topology, "get_pod", return_value=(pod_json, None)),
            patch.object(
                topology,
                "resolve_owner_chain_with_status",
                return_value=(owners, {"status": "complete"}),
            ),
            patch.object(
                topology,
                "matching_services_with_status",
                return_value=(services, {"status": "complete"}),
            ),
        ):
            raw = aiops_observability_query.call_tool(
                "query_pod_topology",
                {
                    "namespace": "ns",
                    "pod": "large-pod",
                    "purpose": "verify the serialized topology budget",
                },
            )

        self.assertIsNotNone(raw)
        result = json.loads(raw)
        self.assertFalse(result["serialization"]["response_truncated"])
        self.assertTrue(result["truncated"])
        self.assertEqual(
            result["evidence_refs"],
            [fact["ref"] for fact in result["facts"]],
        )
        self.assertEqual(
            result["query"]["relationship_types"],
            ["owned_by", "selects", "scheduled_on", "owns_container"],
        )
        self.assertEqual(
            {fact["relation"] for fact in result["facts"]},
            {"owned_by", "selects", "scheduled_on", "owns_container"},
        )
        self.assertEqual(
            {edge["relation"] for edge in result["edges"]},
            {"owned_by", "selects", "scheduled_on", "owns_container"},
        )
        self.assertEqual(
            {edge["evidence_ref"] for edge in result["edges"]},
            set(result["evidence_refs"]),
        )

    def test_lossless_serialization_does_not_mutate_long_evidence_strings(self):
        long_value = "x" * 7000
        payload = {
            "dimension": "logging",
            "facts": [{"ref": "log-long", "value": long_value}],
            "samples": [],
        }

        result = json.loads(common.dump_bounded_result(payload))

        self.assertEqual(payload["facts"][0]["value"], long_value)
        self.assertEqual(result["facts"][0]["value"], long_value)
        self.assertFalse(result["serialization"]["response_truncated"])

    def test_genuinely_oversized_topology_finishes_with_coherent_four_relation_projection(self):
        payload = _oversized_topology_payload()
        self.assertGreater(common.serialized_bytes(payload), 6144)

        started = time.monotonic()
        raw = common.dump_bounded_result(payload)
        elapsed = time.monotonic() - started
        result = json.loads(raw)

        self.assertLess(elapsed, 1.0)
        self.assertGreater(len(raw.encode("utf-8")), 6144)
        self.assertFalse(result["serialization"]["response_truncated"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["truncated"], payload["truncated"])
        self.assertEqual(
            {fact["relation"] for fact in result["facts"]},
            {"owned_by", "selects", "scheduled_on", "owns_container"},
        )
        retained_refs = {fact["ref"] for fact in result["facts"]}
        self.assertEqual(
            {edge["evidence_ref"] for edge in result["edges"]},
            retained_refs,
        )
        self.assertEqual(set(result["evidence_refs"]), retained_refs)
        entity_ids = {entity["entity_id"] for entity in result["entities"]}
        self.assertTrue(
            all(edge["source"] in entity_ids and edge["target"] in entity_ids for edge in result["edges"])
        )
        self.assertEqual(
            result["topology_summary"]["relations"],
            dict(Counter(edge["relation"] for edge in result["edges"])),
        )
        self.assertEqual(
            result["topology_summary"]["edge_count"],
            len(result["edges"]),
        )
        self.assertEqual(
            result["topology_summary"]["entity_count"],
            len(result["entities"]),
        )

    def test_maximum_legal_identifiers_remain_lossless_and_auditable(self):
        payload = _max_identifier_topology_payload()
        self.assertGreater(common.serialized_bytes(payload), 18 * 1024)

        started = time.monotonic()
        raw = common.dump_bounded_result(payload)
        elapsed = time.monotonic() - started
        repeated_raw = common.dump_bounded_result(payload)
        result = json.loads(raw)

        self.assertLess(elapsed, 1.0)
        self.assertEqual(raw, repeated_raw)
        self.assertGreater(len(raw.encode("utf-8")), 18 * 1024)
        self.assertFalse(result["serialization"]["response_truncated"])
        self.assertEqual(
            result["serialization"]["serialized_bytes"],
            len(raw.encode("utf-8")),
        )
        self.assertIn(result["status"], {"query_succeeded", "query_partial"})
        self.assertNotEqual(
            (result.get("error") or {}).get("code"),
            "result_budget_exceeded",
        )
        self.assertEqual(result["truncated"], payload["truncated"])
        self.assertEqual(result["query"], payload["query"])
        self.assertEqual(
            {fact["relation"] for fact in result["facts"]},
            {"owned_by", "selects", "scheduled_on", "owns_container"},
        )

        entities = {entity["entity_id"]: entity for entity in result["entities"]}
        self.assertEqual(
            {(entity["kind"], len(entity["name"])) for entity in entities.values()},
            {
                ("Pod", 253),
                ("ReplicaSet", 253),
                ("Deployment", 253),
                ("Service", 63),
                ("Node", 253),
                ("Container", 63),
            },
        )
        self.assertTrue(all("...[truncated]" not in entity["name"] for entity in entities.values()))
        self.assertEqual(result["entity"], payload["entity"])

        fact_refs = {fact["ref"] for fact in result["facts"]}
        self.assertEqual(set(result["evidence_refs"]), fact_refs)
        self.assertEqual(fact_refs, {fact["ref"] for fact in payload["facts"]})
        self.assertTrue(
            all(
                isinstance(fact["value"]["source"], dict) and isinstance(fact["value"]["target"], dict)
                for fact in result["facts"]
            )
        )
        self.assertTrue(
            all(
                edge["source"] in entities
                and edge["target"] in entities
                and edge["evidence_ref"] in fact_refs
                for edge in result["edges"]
            )
        )
        self.assertEqual(
            result["topology_summary"]["relations"],
            dict(Counter(edge["relation"] for edge in result["edges"])),
        )
        self.assertEqual(
            result["topology_summary"]["edge_count"],
            len(result["edges"]),
        )
        self.assertEqual(
            result["topology_summary"]["entity_count"],
            len(result["entities"]),
        )

    def test_lookup_errors_return_partial_coverage_and_limitations(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "api-abc",
            "pod_uid": "pod-uid",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node-1",
            "containers": ["api"],
        }
        pod_json = {
            "metadata": {
                "name": "api-abc",
                "namespace": "ns",
                "uid": "pod-uid",
                "labels": {"app": "api"},
            },
            "spec": {"nodeName": "node-1", "containers": [{"name": "api"}]},
        }
        owner_chain = [{"kind": "ReplicaSet", "name": "api-abc", "uid": "rs-uid"}]

        with (
            patch.object(
                topology,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch.object(topology, "get_pod", return_value=(pod_json, None)),
            patch.object(
                topology,
                "resolve_owner_chain_with_status",
                return_value=(
                    owner_chain,
                    {
                        "status": "partial",
                        "error": "failed to fetch ReplicaSet ns/api-abc",
                    },
                ),
            ),
            patch.object(
                topology,
                "matching_services_with_status",
                return_value=(
                    [],
                    {
                        "status": "error",
                        "error": "services is forbidden",
                    },
                ),
            ),
        ):
            result = topology.execute(
                {
                    "namespace": "ns",
                    "pod": "api-abc",
                    "purpose": "surface incomplete topology discovery",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "query_partial")
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["directness"], "related_context")
        self.assertTrue(result["facts"])
        self.assertEqual(
            result["query"]["source_status"],
            {
                "owners": "partial",
                "services": "error",
            },
        )
        self.assertEqual(
            {(item["source"], item["status"]) for item in result["limitations"]},
            {
                ("owners", "partial"),
                ("services", "error"),
            },
        )
        self.assertIn(
            "services is forbidden",
            {item["message"] for item in result["limitations"]},
        )
        self.assertEqual(result["topology_summary"]["coverage"], "partial")

    def test_execute_rejects_results_when_pod_lifecycle_changes(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "api-abc",
            "pod_uid": "pod-uid",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node-1",
            "containers": ["api"],
        }
        pod_json = {
            "metadata": {
                "name": "api-abc",
                "namespace": "ns",
                "uid": "pod-uid",
                "labels": {"app": "api"},
            },
            "spec": {"nodeName": "node-1", "containers": [{"name": "api"}]},
        }
        with (
            patch.object(
                topology,
                "resolve_pod_scope",
                side_effect=[
                    (entity, None),
                    (
                        None,
                        {
                            "code": "pod_uid_conflict",
                            "message": (
                                "current Pod UID replacement-uid does not match supplied UID pod-uid"
                            ),
                        },
                    ),
                ],
            ),
            patch.object(topology, "get_pod", return_value=(pod_json, None)),
            patch.object(
                topology,
                "resolve_owner_chain_with_status",
                return_value=([], {"status": "empty"}),
            ),
            patch.object(
                topology,
                "matching_services_with_status",
                return_value=([], {"status": "empty"}),
            ),
        ):
            result = topology.execute(
                {
                    "namespace": "ns",
                    "pod": "api-abc",
                    "purpose": "avoid mixing pod incarnations",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "pod_lifecycle_changed")
        self.assertEqual(result["facts"], [])
        self.assertTrue(result["entity"]["lifecycle_changed"])


if __name__ == "__main__":
    unittest.main()
