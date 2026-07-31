import copy
import hashlib
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from servers.aiops_observability.query import common, topology
from servers.holmes_tools import aiops_observability_query
from tests.test_aiops_observability_query_topology import (
    _max_identifier_topology_payload,
)

try:
    from servers.aiops_observability.query import fact_ledger
except (ImportError, ModuleNotFoundError):
    fact_ledger = None


_OFFICIAL_LEDGER_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "specs"
    / "004-canonical-fact-ledger"
    / "contracts"
    / "fact-ledger.schema.json"
)
_LEDGER_SCHEMA = json.loads(
    _OFFICIAL_LEDGER_SCHEMA_PATH.read_text(encoding="utf-8")
)


def _metric_fact(ref="metric-ref", **overrides):
    value = {
        "ref": ref,
        "source_system": "prometheus",
        "dimension": "metrics",
        "entity": "ns/api",
        "observed_at": "2026-07-30T00:00:00Z",
        "name": "container_cpu_usage_seconds_total",
        "value": "1.25",
        "unit": "seconds",
        "directness": "direct",
    }
    value.update(overrides)
    return value


def _result(
    *,
    dimension="metrics",
    source_system=None,
    facts=None,
    status="query_succeeded",
    coverage="present",
    truncated=False,
):
    sources = {
        "metrics": "prometheus",
        "logging": "elasticsearch",
        "tracing": "deepflow+tempo",
        "topology": "kubernetes",
    }
    return {
        "ok": status == "query_succeeded",
        "status": status,
        "source_system": source_system or sources[dimension],
        "dimension": dimension,
        "entity": {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "api",
            "pod_uid": "uid-current",
        },
        "purpose": "inspect source-backed Pod evidence",
        "coverage": coverage,
        "directness": "direct",
        "query": {"scope": "ns/api"},
        "facts": list(facts if facts is not None else [_metric_fact()]),
        "samples": [],
        "evidence_refs": [
            fact["ref"]
            for fact in (facts if facts is not None else [_metric_fact()])
            if isinstance(fact, dict) and fact.get("ref")
        ],
        "truncated": truncated,
        "limits": {},
    }


def _topology_result():
    source = {
        "kind": "Pod",
        "namespace": "ns",
        "name": "api",
        "uid": "uid-current",
    }
    target = {"kind": "Node", "name": "node-a"}
    fact = {
        "ref": "topology-ref",
        "source_system": "kubernetes",
        "dimension": "topology",
        "name": "kubernetes.relationship",
        "relation": "scheduled_on",
        "value": {
            "relation": "scheduled_on",
            "source": source,
            "target": target,
            "source_field": "pod.spec.nodeName",
        },
        "observed_at": "2026-07-30T00:00:00Z",
        "directness": "direct",
        "confidence": "high",
    }
    result = _result(dimension="topology", facts=[fact])
    entities, edges, summary = topology._topology_graph(
        [fact],
        coverage="present",
    )
    result.update(
        {"entities": entities, "edges": edges, "topology_summary": summary}
    )
    return result


def _all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_keys(item)


class CanonicalFactLedgerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            fact_ledger,
            "canonical Fact Ledger module has not been implemented",
        )

    def test_success_with_exact_uid_publishes_schema_valid_ledger(self):
        ledger = fact_ledger.build_fact_ledger(_result())

        self.assertEqual(
            _LEDGER_SCHEMA["$id"],
            "https://aiops.local/contracts/aiops.fact-ledger.v1.schema.json",
        )
        Draft202012Validator(_LEDGER_SCHEMA).validate(ledger)
        self.assertEqual(
            ledger["scope_entity_ids"],
            ["k8s.pod:ns/api:uid-current"],
        )
        self.assertEqual(ledger["record_count"], len(ledger["records"]))
        self.assertEqual(ledger["source"], "mcp_canonical")
        self.assertFalse(ledger["legacy_contract"])

    def test_null_optional_strength_is_omitted_from_canonical_record(self):
        ledger = fact_ledger.build_fact_ledger(_result())

        self.assertNotIn("strength", ledger["records"][0])

    def test_same_result_builds_identical_ledger_one_hundred_times(self):
        result = _result(
            facts=[
                _metric_fact("metric-b", value="2"),
                _metric_fact("metric-a", value="1"),
            ]
        )

        ledgers = [fact_ledger.build_fact_ledger(result) for _ in range(100)]

        self.assertTrue(all(item == ledgers[0] for item in ledgers))
        self.assertEqual(
            [item["fact_id"] for item in ledgers[0]["records"]],
            sorted(item["fact_id"] for item in ledgers[0]["records"]),
        )

    def test_fact_id_is_sha256_of_full_normalized_record(self):
        record = fact_ledger.build_fact_ledger(_result())["records"][0]
        normalized = copy.deepcopy(record)
        normalized.pop("fact_id")
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertEqual(
            record["fact_id"],
            "fact-" + hashlib.sha256(encoded).hexdigest()[:12],
        )
        self.assertEqual(
            fact_ledger.canonical_fact_id(normalized),
            record["fact_id"],
        )
        changed = copy.deepcopy(normalized)
        changed["strength"] = "context"
        self.assertNotEqual(
            fact_ledger.canonical_fact_id(changed),
            record["fact_id"],
        )

    def test_ledger_recursively_removes_forbidden_diagnostic_fields(self):
        polluted = _metric_fact(
            value={
                "sample": 3,
                "diagnosis": "leak",
                "nested": {
                    "root_cause": "leak",
                    "groundTruth": "leak",
                    "safe": "retained",
                },
            },
            evaluator_label="leak",
            remediation={"command": "kubectl delete pod api"},
        )

        ledger = fact_ledger.build_fact_ledger(_result(facts=[polluted]))
        normalized_keys = {
            re.sub(r"[^a-z0-9]", "", key.lower())
            for key in _all_keys(ledger)
        }

        self.assertTrue(
            normalized_keys.isdisjoint(
                {"diagnosis", "rootcause", "remediation", "groundtruth"}
            )
        )
        self.assertFalse(any(key.startswith("evaluator") for key in normalized_keys))
        self.assertEqual(ledger["records"][0]["value"]["sample"], 3)
        self.assertEqual(
            ledger["records"][0]["value"]["nested"]["safe"],
            "retained",
        )

    def test_ledger_recursively_removes_all_forbidden_key_families(self):
        polluted = _metric_fact(
            value={
                "safe": "retained",
                "diagnosis_label": "leak",
                "nested": [{
                    "root_cause_label": "leak",
                    "remediation_plan": "leak",
                    "ground_truth_label": "leak",
                    "evaluator_score": "leak",
                }],
            },
            fields={
                "safe_metadata": "retained",
                "nested": {
                    "diagnosisSummary": "leak",
                    "rootCauseHint": "leak",
                    "remediationSteps": ["leak"],
                    "groundTruthSource": "leak",
                    "evaluatorVerdict": "leak",
                },
            },
        )

        ledger = fact_ledger.build_fact_ledger(_result(facts=[polluted]))
        normalized_keys = [
            re.sub(r"[^a-z0-9]", "", key.lower())
            for key in _all_keys(ledger)
        ]

        self.assertFalse(
            any(
                key.startswith(
                    ("diagnosis", "rootcause", "remediation", "groundtruth", "evaluator")
                )
                for key in normalized_keys
            )
        )
        self.assertEqual(ledger["records"][0]["value"]["safe"], "retained")
        self.assertEqual(
            ledger["records"][0]["metadata"]["fields"]["safe_metadata"],
            "retained",
        )

    def test_blank_source_or_missing_ref_is_not_published(self):
        facts = [
            _metric_fact("valid"),
            _metric_fact("blank-source", source_system="   "),
            _metric_fact(None),
        ]

        ledger = fact_ledger.build_fact_ledger(_result(facts=facts))

        self.assertEqual(ledger["record_count"], 1)
        self.assertEqual(ledger["records"][0]["evidence_refs"], ["valid"])

    def test_related_context_is_never_high_confidence(self):
        fact = _metric_fact(
            directness="related_context",
            confidence="high",
        )

        record = fact_ledger.build_fact_ledger(_result(facts=[fact]))["records"][0]

        self.assertEqual(record["directness"], "related_context")
        self.assertNotEqual(record["confidence"], "high")

    def test_partial_keeps_source_backed_records_and_adds_coverage_limitation(self):
        result = _result(
            status="query_partial",
            coverage="partial",
            facts=[_metric_fact("retained"), _metric_fact(None)],
        )
        result["limitations"] = ["service lookup incomplete"]

        ledger = fact_ledger.build_fact_ledger(result)

        self.assertEqual(
            [record["dimension"] for record in ledger["records"]],
            ["metrics", "coverage"],
        )
        self.assertEqual(ledger["records"][0]["evidence_refs"], ["retained"])
        self.assertEqual(ledger["records"][1]["fact_type"], "coverage")

    def test_empty_and_rejected_publish_no_direct_high_observation(self):
        cases = [
            _result(facts=[], coverage="empty"),
            _result(facts=[], coverage="absent"),
            _result(facts=[], coverage="error"),
            _result(facts=[], coverage="error", status="query_rejected"),
        ]

        for result in cases:
            with self.subTest(status=result["status"], coverage=result["coverage"]):
                ledger = fact_ledger.build_fact_ledger(result)
                records = [] if ledger is None else ledger["records"]
                self.assertFalse(
                    any(
                        record["directness"] == "direct"
                        and record["confidence"] == "high"
                        for record in records
                    )
                )

    def test_nonpresent_coverage_rejects_complete_direct_high_facts(self):
        fact = _metric_fact(confidence="high")

        for coverage in ("empty", "absent", "error"):
            with self.subTest(coverage=coverage):
                ledger = fact_ledger.build_fact_ledger(
                    _result(facts=[fact], coverage=coverage)
                )

                self.assertIsNone(ledger)

    def test_lifecycle_changed_scope_publishes_no_canonical_ledger(self):
        result = _result()
        result["entity"]["lifecycle_changed"] = True

        ledger = fact_ledger.build_fact_ledger(result)

        self.assertIsNone(ledger)

    def test_trace_sources_correlate_only_on_exact_shared_trace_id(self):
        shared = "0123456789abcdef0123456789abcdef"
        tempo_only = "fedcba9876543210fedcba9876543210"
        facts = [
            {
                "ref": "deepflow-shared",
                "source_system": "deepflow",
                "dimension": "tracing",
                "name": "l7_flow",
                "value": {"trace_id": shared},
                "trace_id": shared,
                "directness": "direct",
            },
            {
                "ref": "tempo-shared",
                "source_system": "tempo",
                "dimension": "tracing",
                "name": "application_span",
                "value": {"trace_id": shared},
                "trace_id": shared,
                "directness": "direct",
            },
            {
                "ref": "tempo-only",
                "source_system": "tempo",
                "dimension": "tracing",
                "name": "application_span",
                "value": {"trace_id": tempo_only},
                "trace_id": tempo_only,
                "directness": "direct",
            },
        ]
        result = _result(dimension="tracing", facts=facts)
        result["correlations"] = [
            {"trace_id": shared, "basis": "exact_trace_id"},
            {"trace_id": tempo_only, "basis": "exact_trace_id"},
        ]

        published = json.loads(common.dump_bounded_result(result))

        self.assertEqual(
            published["correlations"],
            [{"trace_id": shared, "basis": "exact_trace_id"}],
        )
        shared_records = [
            record
            for record in published["fact_ledger"]["records"]
            if (record.get("metadata") or {}).get("trace_id") == shared
        ]
        self.assertEqual(
            {record["source_system"] for record in shared_records},
            {"deepflow", "tempo"},
        )

    def test_unresolvable_topology_endpoint_is_rejected(self):
        result = _result(
            dimension="topology",
            facts=[
                {
                    "ref": "dangling-topology-ref",
                    "source_system": "kubernetes",
                    "name": "kubernetes.relationship",
                    "value": {
                        "relation": "scheduled_on",
                        "source": "missing-source",
                        "target": "missing-target",
                    },
                    "directness": "direct",
                }
            ],
        )
        result.update({"entities": [], "edges": [], "topology_summary": {}})

        ledger = fact_ledger.build_fact_ledger(result)

        self.assertEqual(ledger["records"], [])

    def test_topology_rejects_model_summary_and_unknown_sources(self):
        for source_system in ("model_summary", "unknown_source"):
            for source_location in ("fact", "endpoint"):
                with self.subTest(
                    source_system=source_system,
                    source_location=source_location,
                ):
                    result = _topology_result()
                    if source_location == "fact":
                        result["facts"][0]["source_system"] = source_system
                    else:
                        result["facts"][0]["value"]["target"][
                            "source_system"
                        ] = source_system

                    ledger = fact_ledger.build_fact_ledger(result)

                    self.assertEqual(ledger["records"], [])

    def test_bounded_topology_does_not_launder_endpoint_provenance(self):
        payload = _max_identifier_topology_payload()
        facts_by_relation = {
            fact["relation"]: fact
            for fact in payload["facts"]
        }
        invalid_fact = facts_by_relation["selects"]
        invalid_ref = invalid_fact["ref"]
        invalid_endpoint = copy.deepcopy(invalid_fact["value"]["target"])
        invalid_endpoint["source_system"] = "model_summary"
        invalid_fact["value"]["target"] = invalid_endpoint
        facts_by_relation["scheduled_on"]["value"]["target"][
            "source_system"
        ] = "deepflow"
        facts_by_relation["owns_container"]["value"]["target"][
            "source_system"
        ] = "kubernetes"

        raw = common.dump_bounded_result(payload)
        published = json.loads(raw)
        records = published["fact_ledger"]["records"]
        relations = {
            record["value"]["relation"]
            for record in records
            if record["dimension"] == "topology"
        }
        retained_refs = {
            ref
            for record in records
            for ref in record["evidence_refs"]
        }

        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertTrue(published["truncated"])
        self.assertTrue(published["entity"]["entity_id"].startswith("e:"))
        self.assertNotIn("selects", relations)
        self.assertNotIn(invalid_ref, retained_refs)
        self.assertIn("scheduled_on", relations)
        self.assertIn("owns_container", relations)

    def test_rejected_fact_source_cannot_consume_exact_scope_anchor(self):
        payload = _max_identifier_topology_payload()
        rejected_fact = next(
            fact
            for fact in payload["facts"]
            if fact["relation"] == "selects"
        )
        rejected_ref = rejected_fact["ref"]
        rejected_fact["source_system"] = "model_summary"
        original = copy.deepcopy(payload)

        raw = common.dump_bounded_result(payload)
        published = json.loads(raw)
        ledger = published["fact_ledger"]
        scope_id = ledger["scope_entity_ids"][0]
        records = [
            record
            for record in ledger["records"]
            if record["dimension"] == "topology"
        ]
        relations = {record["value"]["relation"] for record in records}
        retained_refs = {
            ref
            for record in records
            for ref in record["evidence_refs"]
        }
        endpoints = {
            record["value"][key]
            for record in records
            for key in ("source", "target")
        }
        legal_relations = {"owned_by", "owns_container", "scheduled_on"}
        fact_relations = {
            fact["relation"] for fact in published["facts"]
        }
        fact_refs = {fact["ref"] for fact in published["facts"]}
        edge_relations = {
            edge["relation"] for edge in published["edges"]
        }
        edge_refs = {
            edge["evidence_ref"] for edge in published["edges"]
        }
        summary_relations = set(
            published["topology_summary"]["relations"]
        )

        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertEqual(payload, original)
        self.assertEqual(fact_relations, legal_relations)
        self.assertEqual(edge_relations, legal_relations)
        self.assertEqual(summary_relations, legal_relations)
        self.assertEqual(relations, legal_relations)
        self.assertNotIn(rejected_ref, fact_refs)
        self.assertNotIn(rejected_ref, published["evidence_refs"])
        self.assertNotIn(rejected_ref, edge_refs)
        self.assertNotIn(rejected_ref, retained_refs)
        self.assertEqual(
            {fact["source_system"] for fact in published["facts"]},
            {"kubernetes"},
        )
        self.assertTrue(records)
        self.assertEqual(
            {record["source_system"] for record in records},
            {"kubernetes"},
        )
        self.assertIn("k8s.pod:scope/current", endpoints)

    def test_shrink_resynchronizes_facts_refs_correlations_edges_and_ledger(self):
        shared = "0123456789abcdef0123456789abcdef"
        tracing_result = _result(
            dimension="tracing",
            facts=[
                {
                    "ref": "deepflow-ref",
                    "source_system": "deepflow",
                    "dimension": "tracing",
                    "name": "l7_flow",
                    "value": {"trace_id": shared, "payload": "x" * 3000},
                    "trace_id": shared,
                    "directness": "direct",
                },
                {
                    "ref": "tempo-ref",
                    "source_system": "tempo",
                    "dimension": "tracing",
                    "name": "application_span",
                    "value": {"trace_id": shared, "payload": "y" * 3000},
                    "trace_id": shared,
                    "directness": "direct",
                },
                *[
                    _metric_fact(f"extra-{index}", value="z" * 1500)
                    for index in range(5)
                ],
            ],
        )
        tracing_result["correlations"] = [
            {"trace_id": shared, "basis": "exact_trace_id"}
        ]

        tracing_published = json.loads(common.dump_bounded_result(tracing_result))
        retained_refs = [fact["ref"] for fact in tracing_published["facts"]]
        ledger_refs = {
            ref
            for record in tracing_published["fact_ledger"]["records"]
            for ref in record["evidence_refs"]
        }
        self.assertEqual(tracing_published["evidence_refs"], retained_refs)
        self.assertEqual(ledger_refs, set(retained_refs))
        for correlation in tracing_published["correlations"]:
            sources = {
                fact["source_system"]
                for fact in tracing_published["facts"]
                if fact.get("trace_id") == correlation["trace_id"]
            }
            self.assertEqual(sources, {"deepflow", "tempo"})

        topology_published = json.loads(
            common.dump_bounded_result(_max_identifier_topology_payload())
        )
        topology_refs = {fact["ref"] for fact in topology_published["facts"]}
        self.assertEqual(set(topology_published["evidence_refs"]), topology_refs)
        self.assertEqual(
            {edge["evidence_ref"] for edge in topology_published["edges"]},
            topology_refs,
        )
        self.assertEqual(
            {
                ref
                for record in topology_published["fact_ledger"]["records"]
                for ref in record["evidence_refs"]
            },
            topology_refs,
        )

    def test_maximum_legal_topology_identifiers_with_ledger_stay_under_6144_bytes(self):
        payload = _max_identifier_topology_payload()

        raw = common.dump_bounded_result(payload)
        result = json.loads(raw)

        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertNotEqual(
            (result.get("error") or {}).get("code"),
            "result_budget_exceeded",
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(
            result["fact_ledger"]["scope_entity_ids"],
            [
                "k8s.pod:"
                + ("n" * 63)
                + "/"
                + ("p" * 253)
                + ":12345678-1234-1234-1234-123456789012"
            ],
        )
        primary = result["entity"].get("entity_id")
        self.assertTrue(
            any(
                primary in {edge["source"], edge["target"]}
                for edge in result["edges"]
            )
        )
        self.assertTrue(result["fact_ledger"]["records"])

    def test_compacted_topology_ledger_preserves_exact_scope_with_typed_alias(self):
        raw = common.dump_bounded_result(_max_identifier_topology_payload())
        result = json.loads(raw)
        scope_id = result["fact_ledger"]["scope_entity_ids"][0]
        records = [
            record
            for record in result["fact_ledger"]["records"]
            if record["dimension"] == "topology"
        ]
        endpoints = {
            endpoint
            for record in records
            for endpoint in (
                record["value"].get("source"),
                record["value"].get("target"),
            )
        }
        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertIn("k8s.pod:scope/current", endpoints)
        self.assertTrue(
            all(endpoint.startswith("k8s.") for endpoint in endpoints)
        )
        self.assertEqual(
            {record["value"]["relation"] for record in records},
            {"owned_by", "selects", "scheduled_on", "owns_container"},
        )
        self.assertEqual(len(records), 4)

    def test_compacted_topology_ledger_uses_typed_scope_connected_entities(self):
        result = json.loads(
            common.dump_bounded_result(_max_identifier_topology_payload())
        )
        scope_id = result["fact_ledger"]["scope_entity_ids"][0]
        records = result["fact_ledger"]["records"]

        adjacency = {}
        for record in records:
            source = record["value"]["source"]
            target = record["value"]["target"]
            self.assertTrue(source.startswith("k8s."), source)
            self.assertTrue(target.startswith("k8s."), target)
            adjacency.setdefault(source, set()).add(target)
            adjacency.setdefault(target, set()).add(source)

        reachable = {scope_id, "k8s.pod:scope/current"}
        pending = list(reachable)
        while pending:
            current = pending.pop()
            for peer in adjacency.get(current, set()):
                if peer not in reachable:
                    reachable.add(peer)
                    pending.append(peer)

        self.assertTrue(records)
        self.assertTrue(
            all(
                record["value"]["source"] in reachable
                and record["value"]["target"] in reachable
                for record in records
            )
        )

    def test_compaction_does_not_rebind_scope_to_same_name_stale_uid(self):
        payload = _max_identifier_topology_payload()
        namespace = payload["entity"]["namespace"]
        pod = payload["entity"]["pod"]
        current_uid = payload["entity"]["pod_uid"]
        stale_fact = topology._relationship_fact(
            namespace=namespace,
            pod=pod,
            relation="stale_peer",
            source=topology._entity("Pod", namespace, pod, uid="uid-old"),
            target=topology._entity("Node", None, "stale-node"),
            source_field="stale.audit",
            observed_at="2026-07-23T12:00:00Z",
        )
        payload["facts"].append(stale_fact)
        payload["evidence_refs"].append(stale_fact["ref"])
        entities, edges, summary = topology._topology_graph(
            payload["facts"],
            coverage="present",
        )
        payload.update(
            entities=entities,
            edges=edges,
            topology_summary=summary,
        )

        raw = common.dump_bounded_result(payload)
        result = json.loads(raw)

        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertEqual(
            result["fact_ledger"]["scope_entity_ids"],
            [f"k8s.pod:{namespace}/{pod}:{current_uid}"],
        )
        primary = next(
            entity
            for entity in result["entities"]
            if entity["entity_id"] == result["entity"]["entity_id"]
        )
        self.assertEqual(primary.get("uid"), current_uid)

    def _assert_tool_publication(self, tool_name, result):
        with patch.dict(aiops_observability_query._HANDLERS, {tool_name: lambda _: result}):
            raw = aiops_observability_query.call_tool(
                tool_name,
                {"namespace": "ns", "pod": "api", "purpose": "publish facts"},
            )
        published = json.loads(raw)
        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertEqual(
            published["fact_ledger"]["contract_version"],
            "aiops.fact-ledger.v1",
        )
        self.assertEqual(published["fact_ledger"]["source"], "mcp_canonical")
        self.assertFalse(published["fact_ledger"]["legacy_contract"])

    def test_execute_pod_promql_handler_publishes_canonical_ledger(self):
        self._assert_tool_publication("execute_pod_promql", _result())

    def test_query_pod_logs_handler_publishes_canonical_ledger(self):
        fact = _metric_fact(
            source_system="elasticsearch",
            dimension="logging",
            name="log.message",
            value="source-backed log line",
        )
        self._assert_tool_publication(
            "query_pod_logs",
            _result(dimension="logging", facts=[fact]),
        )

    def test_query_pod_tracing_handler_publishes_canonical_ledger(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        fact = _metric_fact(
            source_system="deepflow",
            dimension="tracing",
            name="l7_flow",
            value={"trace_id": trace_id},
            trace_id=trace_id,
        )
        self._assert_tool_publication(
            "query_pod_tracing",
            _result(dimension="tracing", facts=[fact]),
        )

    def test_query_pod_topology_handler_publishes_canonical_ledger(self):
        self._assert_tool_publication("query_pod_topology", _topology_result())


if __name__ == "__main__":
    unittest.main()
