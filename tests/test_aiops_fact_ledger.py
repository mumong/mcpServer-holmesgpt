import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from servers.aiops_observability import case_builder
from servers.aiops_observability.schemas import EvidenceItem, SourceResult, TimeWindow
from servers.aiops_observability.topology import build_topology
from servers.holmes_tools import aiops_case


def _fact_ledger_module():
    try:
        return importlib.import_module("servers.aiops_observability.fact_ledger")
    except ModuleNotFoundError:
        return None


def _case(**overrides):
    payload = {
        "case_id": "auto-ns-api",
        "primary_entity": {
            "kind": "Pod",
            "namespace": "ns",
            "name": "api",
            "uid": "uid-new",
            "pod_ip": "10.0.0.20",
            "node": "node-a",
        },
        "time_window": {
            "start": "2026-07-16T00:05:00Z",
            "end": "2026-07-16T00:30:00Z",
        },
        "coverage": {
            "k8s": "present",
            "metrics": "present",
            "logs": "empty",
            "tracing": "weak",
            "trace": "absent",
            "topology": "present",
        },
    }
    payload.update(overrides)
    return payload


def _evidence_records():
    return [
        {
            "evidence_id": "k8s-pod-snapshot",
            "source_system": "kubernetes",
            "dimension": "k8s",
            "summary": "pod snapshot",
            "confidence": "high",
            "directness": "direct",
            "payload": {
                "uid": "uid-new",
                "phase": "Running",
                "node": "node-a",
                "last_terminated": [
                    {
                        "container": "api",
                        "reason": "Error",
                        "exitCode": 78,
                        "finishedAt": "2026-07-16T00:10:00Z",
                    }
                ],
            },
        },
        {
            "evidence_id": "metric-memory",
            "source_system": "prometheus",
            "dimension": "metrics",
            "summary": "memory samples",
            "confidence": "high",
            "directness": "direct",
            "payload": {
                "pod_uid": "uid-new",
                "highlights": [
                    {
                        "metric": "container_memory_working_set_bytes",
                        "container": "api",
                        "start_time": "2026-07-16T00:09:00Z",
                        "end_time": "2026-07-16T00:10:00Z",
                        "max": "63.2Mi",
                        "limit": "64.0Mi",
                        "max_limit_ratio": 0.9875,
                    }
                ],
            },
        },
    ]


class FactLedgerTests(unittest.TestCase):
    def _build(self, *, case=None, evidence_records=None, entities=None, topology=None, **kwargs):
        module = _fact_ledger_module()
        self.assertIsNotNone(module, "canonical fact ledger module is missing")
        return module.build_fact_ledger(
            case=case or _case(),
            evidence_records=evidence_records or [],
            entities=entities or [],
            topology=topology or [],
            **kwargs,
        )

    def test_fact_ids_are_stable_under_reordering_and_duplicates_collapse(self):
        evidence = _evidence_records()
        topology = [
            {
                "source": "service:ns/api",
                "target": "pod:ns/api",
                "relation": "selects",
                "source_system": "kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": ["k8s-service", "k8s-pod-snapshot"],
            }
        ]

        first = self._build(
            evidence_records=evidence + [evidence[0]],
            topology=topology,
        )
        second = self._build(
            evidence_records=list(reversed(evidence)),
            topology=[
                {
                    **topology[0],
                    "evidence_refs": list(reversed(topology[0]["evidence_refs"])),
                }
            ],
        )

        self.assertEqual(first["records"], second["records"])
        self.assertEqual(
            len({record["fact_id"] for record in first["records"]}),
            first["record_count"],
        )

    def test_conflicting_duplicate_same_authority_is_rejected(self):
        records = [
            {
                "evidence_id": "duplicate-ref",
                "source_system": "elasticsearch",
                "dimension": "logs",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "pod_uid": "uid-new",
                    "samples": [
                        {
                            "timestamp": "2026-07-16T00:10:00Z",
                            "message": "first payload",
                        }
                    ],
                },
            },
            {
                "evidence_id": "duplicate-ref",
                "source_system": "elasticsearch",
                "dimension": "logs",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "pod_uid": "uid-new",
                    "samples": [
                        {
                            "timestamp": "2026-07-16T00:11:00Z",
                            "message": "conflicting payload",
                        }
                    ],
                },
            },
        ]
        topology = [
            {
                "source": "evidence:ns/logs",
                "target": "k8s.pod:ns/api:uid-new",
                "relation": "observes",
                "source_system": "logs",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": ["duplicate-ref"],
            }
        ]

        first = self._build(
            case=_case(coverage={**_case()["coverage"], "logs": "present"}),
            evidence_records=records,
            topology=topology,
        )
        second = self._build(
            case=_case(coverage={**_case()["coverage"], "logs": "present"}),
            evidence_records=list(reversed(records)),
            topology=topology,
        )

        self.assertEqual(first, second)
        self.assertNotIn("duplicate-ref", json.dumps(first, ensure_ascii=False))
        logs_coverage = next(
            record
            for record in first["records"]
            if record["attribute"] == "logs.coverage"
        )
        self.assertEqual(logs_coverage["evidence_refs"], [])

    def test_root_cause_only_duplicate_conflict_is_rejected_direct(self):
        records = [
            {
                "evidence_id": "root-cause-conflict",
                "source_system": "elasticsearch",
                "dimension": "logs",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "pod_uid": "uid-new",
                    "root_cause": root_cause,
                    "samples": [
                        {
                            "timestamp": "2026-07-16T00:10:00Z",
                            "message": "same safe payload",
                        }
                    ],
                },
            }
            for root_cause in ("first hidden cause", "second hidden cause")
        ]

        ledger = self._build(
            case=_case(coverage={**_case()["coverage"], "logs": "present"}),
            evidence_records=records,
        )

        self.assertNotIn(
            "root-cause-conflict",
            json.dumps(ledger, ensure_ascii=False),
        )

    def test_root_cause_only_duplicate_conflict_is_rejected_when_reversed(self):
        records = [
            {
                "evidence_id": "root-cause-order-conflict",
                "source_system": "elasticsearch",
                "dimension": "logs",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "pod_uid": "uid-new",
                    "root_cause": root_cause,
                    "samples": [
                        {
                            "timestamp": "2026-07-16T00:10:00Z",
                            "message": "same safe payload",
                        }
                    ],
                },
            }
            for root_cause in ("first hidden cause", "second hidden cause")
        ]
        case = _case(coverage={**_case()["coverage"], "logs": "present"})

        forward = self._build(case=case, evidence_records=records)
        reversed_order = self._build(
            case=case,
            evidence_records=list(reversed(records)),
        )

        self.assertEqual(forward, reversed_order)
        self.assertNotIn(
            "root-cause-order-conflict",
            json.dumps(reversed_order, ensure_ascii=False),
        )

    def test_nested_forbidden_only_duplicate_conflict_is_rejected(self):
        records = [
            {
                "evidence_id": "nested-forbidden-conflict",
                "source_system": "prometheus",
                "dimension": "metrics",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "pod_uid": "uid-new",
                    "highlights": [
                        {
                            "metric": "custom",
                            "start_time": "2026-07-16T00:10:00Z",
                            "end_time": "2026-07-16T00:11:00Z",
                            "metadata": {
                                "safe_value": "preserved",
                                "diagnosis": {
                                    "label": diagnosis_label,
                                },
                            },
                        }
                    ],
                },
            }
            for diagnosis_label in ("hidden-a", "hidden-b")
        ]

        ledger = self._build(evidence_records=records)

        self.assertNotIn(
            "nested-forbidden-conflict",
            json.dumps(ledger, ensure_ascii=False),
        )

    def test_exact_duplicate_same_authority_remains_accepted_once(self):
        item = {
            "evidence_id": "same-ref",
            "source_system": "elasticsearch",
            "dimension": "logs",
            "confidence": "high",
            "directness": "direct",
            "payload": {
                "pod_uid": "uid-new",
                "samples": [
                    {
                        "timestamp": "2026-07-16T00:10:00Z",
                        "message": "same payload",
                    }
                ],
            },
        }

        ledger = self._build(
            case=_case(coverage={**_case()["coverage"], "logs": "present"}),
            evidence_records=[item, dict(item)],
        )

        facts = [
            record
            for record in ledger["records"]
            if record["attribute"] == "log.sample"
            and record["evidence_refs"] == ["same-ref"]
        ]
        self.assertEqual(len(facts), 1)

    def test_semantic_duplicates_merge_refs_without_weak_authority_upgrade(self):
        termination = {
            "container": "api",
            "reason": "Error",
            "exitCode": 78,
            "finishedAt": "2026-07-16T00:10:00Z",
        }
        ledger = self._build(
            evidence_records=[
                {
                    "evidence_id": "current-k8s-termination",
                    "source_system": "kubernetes",
                    "dimension": "k8s",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "uid": "uid-new",
                        "last_terminated": [termination],
                    },
                },
                {
                    "evidence_id": "temporal-k8s-termination",
                    "source_system": "kubernetes",
                    "dimension": "k8s",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {"last_terminated": [termination]},
                },
            ]
        )

        facts = [
            record
            for record in ledger["records"]
            if record["attribute"] == "container.last_termination"
        ]
        self.assertEqual(len(facts), 1)
        self.assertEqual(
            facts[0]["evidence_refs"],
            ["current-k8s-termination", "temporal-k8s-termination"],
        )
        self.assertEqual(facts[0]["directness"], "direct")
        self.assertEqual(facts[0]["confidence"], "high")

    def test_unreferenced_duplicate_cannot_upgrade_weak_authority(self):
        termination = {
            "container": "api",
            "reason": "Error",
            "exitCode": 78,
            "finishedAt": "2026-07-16T00:10:00Z",
        }
        ledger = self._build(
            evidence_records=[
                {
                    "evidence_id": "temporal-k8s-termination",
                    "source_system": "kubernetes",
                    "dimension": "k8s",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {"last_terminated": [termination]},
                },
                {
                    "source_system": "kubernetes",
                    "dimension": "k8s",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "uid": "uid-new",
                        "last_terminated": [termination],
                    },
                },
            ]
        )

        fact = next(
            record
            for record in ledger["records"]
            if record["attribute"] == "container.last_termination"
        )
        self.assertEqual(fact["evidence_refs"], ["temporal-k8s-termination"])
        self.assertEqual(fact["directness"], "related_context")
        self.assertEqual(fact["confidence"], "weak")

    def test_uid_scope_rejects_facts_from_recreated_same_name_pod(self):
        case = _case(
            coverage={
                **_case()["coverage"],
                "logs": "present",
            }
        )
        evidence = _evidence_records() + [
            {
                "evidence_id": "stale-old-instance-log",
                "source_system": "elasticsearch",
                "dimension": "logs",
                "summary": "old instance",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "records": [
                        {
                            "kubernetes": {
                                "pod": {
                                    "name": "api",
                                    "uid": "uid-old",
                                }
                            }
                        }
                    ],
                    "samples": [
                        {
                            "timestamp": "2026-07-15T23:55:00Z",
                            "message": "stale log from recreated Pod",
                        }
                    ],
                },
            }
        ]

        ledger = self._build(case=case, evidence_records=evidence)

        self.assertEqual(ledger["scope_entity_ids"], ["k8s.pod:ns/api:uid-new"])
        rendered = json.dumps(ledger, ensure_ascii=False)
        self.assertIn("metric-memory", rendered)
        self.assertNotIn("stale-old-instance-log", rendered)
        self.assertNotIn("uid-old", rendered)

    def test_mixed_current_and_old_uids_do_not_authorize_stale_sibling_records(self):
        ledger = self._build(
            case=_case(coverage={**_case()["coverage"], "logs": "present"}),
            evidence_records=[
                {
                    "evidence_id": "mixed-pod-log",
                    "source_system": "elasticsearch",
                    "dimension": "logs",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "pod_uid": "uid-new",
                        "records": [
                            {"kubernetes": {"pod": {"uid": "uid-old"}}},
                        ],
                        "samples": [
                            {
                                "timestamp": "2026-07-16T00:10:00Z",
                                "message": "stale sibling leaked",
                            }
                        ],
                    },
                }
            ],
        )

        rendered = json.dumps(ledger, ensure_ascii=False)
        self.assertNotIn("mixed-pod-log", rendered)
        self.assertNotIn("stale sibling leaked", rendered)

    def test_mixed_uid_evidence_ref_cannot_authorize_topology(self):
        ledger = self._build(
            case=_case(coverage={**_case()["coverage"], "logs": "present"}),
            evidence_records=[
                {
                    "evidence_id": "mixed-pod-log",
                    "source_system": "elasticsearch",
                    "dimension": "logs",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "pod_uid": "uid-new",
                        "records": [
                            {"kubernetes": {"pod": {"uid": "uid-old"}}},
                        ],
                        "samples": [
                            {
                                "timestamp": "2026-07-16T00:10:00Z",
                                "message": "mixed identity",
                            }
                        ],
                    },
                }
            ],
            topology=[
                {
                    "source": "evidence:ns/logs",
                    "target": "k8s.pod:ns/api:uid-new",
                    "relation": "observes",
                    "source_system": "logs",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["mixed-pod-log"],
                }
            ],
        )

        self.assertFalse(
            any(
                record["attribute"] == "topology.observes"
                for record in ledger["records"]
            )
        )
        self.assertNotIn("mixed-pod-log", json.dumps(ledger, ensure_ascii=False))

    def test_phantom_evidence_ref_cannot_authorize_topology(self):
        ledger = self._build(
            topology=[
                {
                    "source": "service:ns/api",
                    "target": "k8s.pod:ns/api:uid-new",
                    "relation": "selects",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["does-not-exist"],
                }
            ]
        )

        self.assertFalse(
            any(
                record["attribute"] == "topology.selects"
                for record in ledger["records"]
            )
        )
        self.assertNotIn("does-not-exist", json.dumps(ledger, ensure_ascii=False))

    def test_unrelated_topology_edge_is_rejected_even_with_current_ref(self):
        ledger = self._build(
            evidence_records=[
                {
                    "evidence_id": "current-k8s",
                    "source_system": "kubernetes",
                    "dimension": "k8s",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {"uid": "uid-new", "phase": "Running"},
                }
            ],
            topology=[
                {
                    "source": "service:ns/other",
                    "target": "k8s.pod:ns/other:uid-other",
                    "relation": "selects",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["current-k8s"],
                }
            ],
        )

        self.assertFalse(
            any(
                record["attribute"] == "topology.selects"
                for record in ledger["records"]
            )
        )

    def test_old_uid_topology_endpoint_is_rejected(self):
        ledger = self._build(
            evidence_records=[
                {
                    "evidence_id": "current-k8s",
                    "source_system": "kubernetes",
                    "dimension": "k8s",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {"uid": "uid-new", "phase": "Running"},
                }
            ],
            topology=[
                {
                    "source": "service:ns/api",
                    "target": "k8s.pod:ns/api:uid-old",
                    "relation": "selects",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["current-k8s"],
                }
            ],
        )

        self.assertFalse(
            any(
                record["attribute"] == "topology.selects"
                for record in ledger["records"]
            )
        )
        self.assertNotIn("uid-old", json.dumps(ledger, ensure_ascii=False))

    def test_valid_current_primary_topology_relationships_are_retained(self):
        primary_id = "k8s.pod:ns/api:uid-new"
        ledger = self._build(
            evidence_records=[
                {
                    "evidence_id": "current-k8s",
                    "source_system": "kubernetes",
                    "dimension": "k8s",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {"uid": "uid-new", "phase": "Running"},
                }
            ],
            topology=[
                {
                    "source": "service:ns/api",
                    "target": primary_id,
                    "relation": "selects",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["current-k8s"],
                },
                {
                    "source": primary_id,
                    "target": "node:cluster/node-a",
                    "relation": "scheduled_on",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["current-k8s"],
                },
                {
                    "source": primary_id,
                    "target": "ip:cluster/10.0.0.20",
                    "relation": "assigned_to",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["current-k8s"],
                },
                {
                    "source": "k8s.pod:ns/caller:uid-caller",
                    "target": primary_id,
                    "relation": "calls",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["current-k8s"],
                },
            ],
        )

        topology_facts = [
            record
            for record in ledger["records"]
            if record["dimension"] == "topology"
            and record["fact_type"] == "relationship"
        ]
        self.assertEqual(
            {record["attribute"] for record in topology_facts},
            {
                "topology.assigned_to",
                "topology.calls",
                "topology.scheduled_on",
                "topology.selects",
            },
        )
        self.assertTrue(
            all(
                primary_id
                in {record["value"]["source"], record["value"]["target"]}
                for record in topology_facts
            )
        )

    def test_no_uid_persisted_replay_outside_case_window_is_not_a_current_fact(self):
        with tempfile.TemporaryDirectory() as temp:
            case_dir = Path(temp)
            evidence_dir = case_dir / "evidence"
            evidence_dir.mkdir()
            (case_dir / "case.yaml").write_text(
                "\n".join(
                    [
                        "case_id: replay-case",
                        "primary_entity:",
                        "  kind: Pod",
                        "  namespace: ns",
                        "  name: api",
                        "  uid: uid-new",
                        "time_window:",
                        "  start: 2026-07-16T00:05:00Z",
                        "  end: 2026-07-16T00:30:00Z",
                        "coverage:",
                        "  logs: present",
                    ]
                ),
                encoding="utf-8",
            )
            for name in ("signals.jsonl", "timeline.jsonl", "entities.jsonl", "topology.jsonl"):
                (case_dir / name).write_text("", encoding="utf-8")
            (evidence_dir / "logs.jsonl").write_text(
                json.dumps(
                    {
                        "evidence_id": "persisted-no-uid-log",
                        "source_system": "elasticsearch",
                        "dimension": "logs",
                        "confidence": "high",
                        "directness": "direct",
                        "payload": {
                            "samples": [
                                {
                                    "timestamp": "2026-07-16T00:01:00Z",
                                    "message": "pre-creation stale replay",
                                }
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            summary = case_builder.case_summary(case_dir)

        facts = summary["fact_ledger"]["records"]
        self.assertFalse(
            any(
                record.get("fact_type") != "coverage"
                and record.get("directness") == "direct"
                for record in facts
            )
        )
        self.assertNotIn("pre-creation stale replay", json.dumps(facts, ensure_ascii=False))

    def test_stale_no_uid_replay_ref_cannot_authorize_topology(self):
        with tempfile.TemporaryDirectory() as temp:
            case_dir = Path(temp)
            evidence_dir = case_dir / "evidence"
            evidence_dir.mkdir()
            (case_dir / "case.yaml").write_text(
                "\n".join(
                    [
                        "case_id: replay-case",
                        "primary_entity:",
                        "  kind: Pod",
                        "  namespace: ns",
                        "  name: api",
                        "  uid: uid-new",
                        "time_window:",
                        "  start: 2026-07-16T00:05:00Z",
                        "  end: 2026-07-16T00:30:00Z",
                        "coverage:",
                        "  logs: present",
                        "  topology: present",
                    ]
                ),
                encoding="utf-8",
            )
            for name in ("signals.jsonl", "timeline.jsonl", "entities.jsonl"):
                (case_dir / name).write_text("", encoding="utf-8")
            (case_dir / "topology.jsonl").write_text(
                json.dumps(
                    {
                        "source": "evidence:ns/logs",
                        "target": "k8s.pod:ns/api:uid-new",
                        "relation": "observes",
                        "source_system": "logs",
                        "directness": "direct",
                        "confidence": "high",
                        "evidence_refs": ["persisted-no-uid-log"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (evidence_dir / "logs.jsonl").write_text(
                json.dumps(
                    {
                        "evidence_id": "persisted-no-uid-log",
                        "source_system": "elasticsearch",
                        "dimension": "logs",
                        "confidence": "high",
                        "directness": "direct",
                        "payload": {
                            "samples": [
                                {
                                    "timestamp": "2026-07-16T00:01:00Z",
                                    "message": "stale replay",
                                }
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            summary = case_builder.case_summary(case_dir)

        rendered = json.dumps(summary["fact_ledger"], ensure_ascii=False)
        self.assertNotIn("topology.observes", rendered)
        self.assertNotIn("persisted-no-uid-log", rendered)

    def test_no_uid_coverage_remains_non_authoritative(self):
        ledger = self._build(
            case=_case(coverage={**_case()["coverage"], "logs": "present"}),
            evidence_records=[
                {
                    "evidence_id": "temporal-log",
                    "source_system": "elasticsearch",
                    "dimension": "logs",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "samples": [
                            {
                                "timestamp": "2026-07-16T00:10:00Z",
                                "message": "in-window context",
                            }
                        ]
                    },
                }
            ],
        )

        coverage = next(
            record
            for record in ledger["records"]
            if record["attribute"] == "logs.coverage"
        )
        observation = next(
            record
            for record in ledger["records"]
            if record["attribute"] == "log.sample"
        )
        self.assertEqual(coverage["evidence_refs"], ["temporal-log"])
        self.assertEqual(coverage["directness"], "related_context")
        self.assertEqual(coverage["confidence"], "weak")
        self.assertEqual(observation["directness"], "related_context")
        self.assertEqual(observation["confidence"], "weak")

    def test_primary_without_uid_cannot_be_direct_high(self):
        primary = dict(_case()["primary_entity"])
        primary.pop("uid")
        ledger = self._build(
            case=_case(
                primary_entity=primary,
                coverage={
                    **_case()["coverage"],
                    "logs": "present",
                    "topology": "present",
                },
            ),
            evidence_records=[
                {
                    "evidence_id": "legacy-log",
                    "source_system": "elasticsearch",
                    "dimension": "logs",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "samples": [
                            {
                                "timestamp": "2026-07-16T00:10:00Z",
                                "message": "legacy",
                            }
                        ]
                    },
                }
            ],
            topology=[
                {
                    "source": "evidence:ns/logs",
                    "target": "k8s.pod:ns/api",
                    "relation": "observes",
                    "source_system": "logs",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["legacy-log"],
                }
            ],
        )

        observations = [
            record
            for record in ledger["records"]
            if record["fact_type"] != "coverage"
        ]
        self.assertTrue(observations)
        self.assertTrue(
            all(
                (record["directness"], record["confidence"])
                == ("related_context", "weak")
                for record in observations
            )
        )
        logs_coverage = next(
            record
            for record in ledger["records"]
            if record["attribute"] == "logs.coverage"
        )
        self.assertEqual(
            (logs_coverage["directness"], logs_coverage["confidence"]),
            ("related_context", "weak"),
        )
        self.assertFalse(
            any(
                record["directness"] == "direct"
                and record["confidence"] == "high"
                for record in ledger["records"]
            )
        )

    def test_no_uid_partial_interval_overlap_is_rejected(self):
        ledger = self._build(
            evidence_records=[
                {
                    "evidence_id": "partial-no-uid-metric",
                    "source_system": "prometheus",
                    "dimension": "metrics",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "highlights": [
                            {
                                "metric": "container_memory_working_set_bytes",
                                "start_time": "2026-07-16T00:01:00Z",
                                "end_time": "2026-07-16T00:10:00Z",
                                "max": "63.2Mi",
                            }
                        ]
                    },
                }
            ]
        )

        rendered = json.dumps(ledger, ensure_ascii=False)
        self.assertNotIn("partial-no-uid-metric", rendered)
        self.assertFalse(
            any(
                record["attribute"] == "metric.container_memory_working_set_bytes"
                for record in ledger["records"]
            )
        )

    def test_no_uid_interval_missing_end_is_rejected(self):
        ledger = self._build(
            evidence_records=[
                {
                    "evidence_id": "missing-end",
                    "source_system": "prometheus",
                    "dimension": "metrics",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "highlights": [
                            {
                                "metric": "cpu",
                                "start_time": "2026-07-16T00:10:00Z",
                                "max": 1,
                            }
                        ]
                    },
                }
            ]
        )

        self.assertNotIn("missing-end", json.dumps(ledger, ensure_ascii=False))

    def test_no_uid_interval_requires_both_valid_unambiguous_endpoints(self):
        records = [
            {
                "evidence_id": "missing-start",
                "source_system": "tempo",
                "dimension": "trace",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "spans": [
                        {
                            "trace_id": "trace-missing-start",
                            "name": "missing start",
                            "end": "2026-07-16T00:11:00Z",
                        }
                    ]
                },
            },
            {
                "evidence_id": "ambiguous-start",
                "source_system": "prometheus",
                "dimension": "metrics",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "highlights": [
                        {
                            "metric": "ambiguous",
                            "start_time": "2026-07-16T00:10:00",
                            "end_time": "2026-07-16T00:11:00Z",
                            "max": 1,
                        }
                    ]
                },
            },
            {
                "evidence_id": "reversed-interval",
                "source_system": "prometheus",
                "dimension": "metrics",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "highlights": [
                        {
                            "metric": "reversed",
                            "start_time": "2026-07-16T00:12:00Z",
                            "end_time": "2026-07-16T00:11:00Z",
                            "max": 1,
                        }
                    ]
                },
            },
            {
                "evidence_id": "timeless-interval",
                "source_system": "prometheus",
                "dimension": "metrics",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "highlights": [
                        {
                            "metric": "timeless",
                            "max": 1,
                        }
                    ]
                },
            },
        ]

        ledger = self._build(evidence_records=records)
        rendered = json.dumps(ledger, ensure_ascii=False)

        for evidence_ref in (
            "missing-start",
            "ambiguous-start",
            "reversed-interval",
            "timeless-interval",
        ):
            self.assertNotIn(evidence_ref, rendered)

    def test_no_uid_complete_intervals_are_retained_as_weak_context(self):
        ledger = self._build(
            case=_case(
                coverage={
                    **_case()["coverage"],
                    "metrics": "present",
                    "trace": "present",
                }
            ),
            evidence_records=[
                {
                    "evidence_id": "complete-metric",
                    "source_system": "prometheus",
                    "dimension": "metrics",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "highlights": [
                            {
                                "metric": "cpu",
                                "start_time": "2026-07-16T00:10:00Z",
                                "end_time": "2026-07-16T00:11:00Z",
                                "max": 1,
                            }
                        ]
                    },
                },
                {
                    "evidence_id": "complete-span",
                    "source_system": "tempo",
                    "dimension": "trace",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "spans": [
                            {
                                "trace_id": "trace-complete",
                                "name": "complete",
                                "start": "2026-07-16T00:12:00Z",
                                "end": "2026-07-16T00:13:00Z",
                            }
                        ]
                    },
                },
            ],
        )

        observations = [
            record
            for record in ledger["records"]
            if set(record["evidence_refs"]) & {"complete-metric", "complete-span"}
            and record["fact_type"] != "coverage"
        ]
        self.assertEqual(len(observations), 2)
        self.assertTrue(
            all(
                (record["directness"], record["confidence"])
                == ("related_context", "weak")
                for record in observations
            )
        )

    def test_persisted_replay_applies_the_same_ref_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            case_dir = Path(temp)
            evidence_dir = case_dir / "evidence"
            evidence_dir.mkdir()
            (case_dir / "case.yaml").write_text(
                "\n".join(
                    [
                        "case_id: replay-authority",
                        "primary_entity:",
                        "  kind: Pod",
                        "  namespace: ns",
                        "  name: api",
                        "  uid: uid-new",
                        "time_window:",
                        "  start: 2026-07-16T00:05:00Z",
                        "  end: 2026-07-16T00:30:00Z",
                        "coverage:",
                        "  k8s: present",
                        "  topology: present",
                    ]
                ),
                encoding="utf-8",
            )
            for name in ("signals.jsonl", "timeline.jsonl"):
                (case_dir / name).write_text("", encoding="utf-8")
            (case_dir / "entities.jsonl").write_text(
                json.dumps(
                    {
                        "entity_id": "k8s.pod:ns/api:uid-new",
                        "kind": "Pod",
                        "namespace": "ns",
                        "name": "api",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            topology_rows = [
                {
                    "source": "service:ns/api",
                    "target": "k8s.pod:ns/api:uid-new",
                    "relation": "selects",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["current-k8s"],
                },
                {
                    "source": "service:ns/phantom",
                    "target": "k8s.pod:ns/api:uid-new",
                    "relation": "selects",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["phantom-k8s"],
                },
            ]
            (case_dir / "topology.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in topology_rows),
                encoding="utf-8",
            )
            (evidence_dir / "k8s_events.jsonl").write_text(
                json.dumps(
                    {
                        "evidence_id": "current-k8s",
                        "source_system": "kubernetes",
                        "dimension": "k8s",
                        "confidence": "high",
                        "directness": "direct",
                        "payload": {"uid": "uid-new", "phase": "Running"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            first = case_builder.case_summary(case_dir)["fact_ledger"]
            second = case_builder.case_summary(case_dir)["fact_ledger"]

        topology_facts = [
            record
            for record in first["records"]
            if record["attribute"] == "topology.selects"
        ]
        self.assertEqual(first, second)
        self.assertEqual(len(topology_facts), 1)
        self.assertEqual(topology_facts[0]["evidence_refs"], ["current-k8s"])
        self.assertNotIn("phantom-k8s", json.dumps(first, ensure_ascii=False))

    def test_persisted_replay_rejects_evaluator_answer_only_conflict_deterministically(self):
        with tempfile.TemporaryDirectory() as temp:
            case_dir = Path(temp)
            evidence_dir = case_dir / "evidence"
            evidence_dir.mkdir()
            (case_dir / "case.yaml").write_text(
                "\n".join(
                    [
                        "case_id: replay-conflict",
                        "primary_entity:",
                        "  kind: Pod",
                        "  namespace: ns",
                        "  name: api",
                        "  uid: uid-new",
                        "time_window:",
                        "  start: 2026-07-16T00:05:00Z",
                        "  end: 2026-07-16T00:30:00Z",
                        "coverage:",
                        "  logs: present",
                        "  topology: present",
                    ]
                ),
                encoding="utf-8",
            )
            for name in ("signals.jsonl", "timeline.jsonl", "entities.jsonl"):
                (case_dir / name).write_text("", encoding="utf-8")
            (case_dir / "topology.jsonl").write_text(
                json.dumps(
                    {
                        "source": "evidence:ns/logs",
                        "target": "pod:ns/api",
                        "relation": "observes",
                        "source_system": "logs",
                        "directness": "direct",
                        "confidence": "high",
                        "evidence_refs": ["duplicate-ref"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            rows = [
                {
                    "evidence_id": "duplicate-ref",
                    "source_system": "elasticsearch",
                    "dimension": "logs",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "pod_uid": "uid-new",
                        "samples": [
                            {
                                "timestamp": "2026-07-16T00:10:00Z",
                                "message": "same persisted payload",
                                "evaluator": {
                                    "answer": "first hidden answer",
                                },
                            }
                        ],
                    },
                },
                {
                    "evidence_id": "duplicate-ref",
                    "source_system": "elasticsearch",
                    "dimension": "logs",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "pod_uid": "uid-new",
                        "samples": [
                            {
                                "timestamp": "2026-07-16T00:10:00Z",
                                "message": "same persisted payload",
                                "evaluator": {
                                    "answer": "second hidden answer",
                                },
                            }
                        ],
                    },
                },
            ]
            (evidence_dir / "logs.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            first = case_builder.case_summary(case_dir)["fact_ledger"]
            second = case_builder.case_summary(case_dir)["fact_ledger"]

        self.assertEqual(first, second)
        self.assertNotIn("duplicate-ref", json.dumps(first, ensure_ascii=False))

    def test_persisted_replay_scopes_topology_after_alias_normalization(self):
        with tempfile.TemporaryDirectory() as temp:
            case_dir = Path(temp)
            evidence_dir = case_dir / "evidence"
            evidence_dir.mkdir()
            (case_dir / "case.yaml").write_text(
                "\n".join(
                    [
                        "case_id: replay-topology-scope",
                        "primary_entity:",
                        "  kind: Pod",
                        "  namespace: ns",
                        "  name: api",
                        "  uid: uid-new",
                        "time_window:",
                        "  start: 2026-07-16T00:05:00Z",
                        "  end: 2026-07-16T00:30:00Z",
                        "coverage:",
                        "  k8s: present",
                        "  topology: present",
                    ]
                ),
                encoding="utf-8",
            )
            for name in ("signals.jsonl", "timeline.jsonl", "entities.jsonl"):
                (case_dir / name).write_text("", encoding="utf-8")
            topology_rows = [
                {
                    "source": "service:ns/api",
                    "target": "pod:ns/api",
                    "relation": "selects",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["current-k8s"],
                },
                {
                    "source": "service:ns/other",
                    "target": "k8s.pod:ns/other:uid-other",
                    "relation": "selects",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["current-k8s"],
                },
                {
                    "source": "service:ns/api",
                    "target": "k8s.pod:ns/api:uid-old",
                    "relation": "selects",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["current-k8s"],
                },
            ]
            (case_dir / "topology.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in topology_rows),
                encoding="utf-8",
            )
            (evidence_dir / "k8s_events.jsonl").write_text(
                json.dumps(
                    {
                        "evidence_id": "current-k8s",
                        "source_system": "kubernetes",
                        "dimension": "k8s",
                        "confidence": "high",
                        "directness": "direct",
                        "payload": {"uid": "uid-new", "phase": "Running"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            summary = case_builder.case_summary(case_dir)

        topology_facts = [
            record
            for record in summary["fact_ledger"]["records"]
            if record["attribute"] == "topology.selects"
        ]
        self.assertEqual(len(topology_facts), 1)
        self.assertEqual(
            topology_facts[0]["value"]["target"],
            "k8s.pod:ns/api:uid-new",
        )
        rendered = json.dumps(summary, ensure_ascii=False, default=str)
        self.assertNotIn("uid-old", rendered)
        self.assertNotIn("uid-other", rendered)

    def test_ledger_does_not_inherit_diagnostic_or_causal_roles(self):
        case = _case(
            signals_summary=[
                {"role": "root_cause_state"},
                {"role": "pre_failure_behavior"},
                {"role": "request_driver"},
            ]
        )
        evidence = [
            {
                "evidence_id": "log-sample",
                "source_system": "elasticsearch",
                "dimension": "caller_logs",
                "summary": "caller sample",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "pod_uid": "uid-new",
                    "root_cause": "must not leak",
                    "expected_remediation": "must not leak",
                    "label": "must not leak",
                    "samples": [
                        {
                            "timestamp": "2026-07-16T00:10:00Z",
                            "message": "request completed",
                            "role": "request_driver",
                        }
                    ],
                },
            }
        ]

        ledger = self._build(case=case, evidence_records=evidence)
        rendered = json.dumps(ledger, ensure_ascii=False)

        for forbidden in (
            "root_cause",
            "expected_remediation",
            '"label"',
            "root_cause_state",
            "pre_failure_behavior",
            "request_driver",
        ):
            self.assertNotIn(forbidden, rendered)
        caller_fact = next(
            record
            for record in ledger["records"]
            if "log-sample" in record["evidence_refs"]
        )
        self.assertEqual(caller_fact["directness"], "related_context")
        self.assertNotEqual(caller_fact["confidence"], "high")

    def test_budget_preserves_coverage_states_and_is_evaluator_safe(self):
        evidence = [
            {
                "evidence_id": f"log-{index:03d}",
                "source_system": "elasticsearch",
                "dimension": "logs",
                "summary": f"log sample {index}",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "samples": [
                        {
                            "timestamp": f"2026-07-16T00:{index % 60:02d}:00Z",
                            "message": "x" * 600,
                        }
                    ]
                },
            }
            for index in range(70)
        ]

        ledger = self._build(evidence_records=evidence, max_records=48)

        self.assertLessEqual(ledger["record_count"], 48)
        self.assertTrue(ledger["truncated"])
        coverage = {
            (record["attribute"], record["value"])
            for record in ledger["records"]
            if record["fact_type"] == "coverage"
        }
        for dimension, state in _case()["coverage"].items():
            self.assertIn((f"{dimension}.coverage", state), coverage)
        self.assertLessEqual(len(json.dumps(ledger, ensure_ascii=False)), 12000)

    def test_real_mcp_json_budget_holds_for_many_small_records(self):
        evidence = [
            {
                "evidence_id": f"log-{index:03d}",
                "source_system": "elasticsearch",
                "dimension": "logs",
                "summary": f"log sample {index}",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "pod_uid": "uid-new",
                    "samples": [
                        {
                            "timestamp": f"2026-07-16T00:{index % 25 + 5:02d}:00Z",
                            "message": f"small record {index:03d}",
                        }
                    ],
                },
            }
            for index in range(80)
        ]

        ledger = self._build(evidence_records=evidence, max_records=48)
        wire = aiops_case._json({"fact_ledger": ledger})

        self.assertLessEqual(len(wire), 12000)
        self.assertTrue(ledger["truncated"])

    def test_live_shaped_budget_retains_each_present_observation_dimension(self):
        case = _case(
            coverage={
                "k8s": "present",
                "metrics": "present",
                "logs": "present",
                "tracing": "present",
                "trace": "present",
                "caller_logs": "present",
                "topology": "present",
            }
        )
        evidence = [
            {
                "evidence_id": "k8s-live",
                "source_system": "kubernetes",
                "dimension": "k8s",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "uid": "uid-new",
                    "phase": "Running",
                    "last_terminated": [
                        {
                            "container": "api",
                            "reason": "OOMKilled",
                            "exitCode": 137,
                            "finishedAt": "2026-07-16T00:12:00Z",
                        }
                    ],
                },
            },
            {
                "evidence_id": "metric-live",
                "source_system": "prometheus",
                "dimension": "metrics",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "pod_uid": "uid-new",
                    "highlights": [
                        {
                            "metric": "container_memory_working_set_bytes",
                            "container": "api",
                            "start": "3.2Mi",
                            "max": "70.9Mi",
                            "last": "70.9Mi",
                            "limit": "80.0Mi",
                            "max_limit_ratio": 0.8867,
                            "start_time": "2026-07-16T00:06:00Z",
                            "end_time": "2026-07-16T00:12:00Z",
                        }
                    ],
                },
            },
            {
                "evidence_id": "deepflow-live",
                "source_system": "deepflow",
                "dimension": "tracing",
                "confidence": "medium",
                "directness": "direct",
                "payload": {
                    "samples": [
                        {
                            "timestamp": "2026-07-16T00:11:58Z",
                            "src": "10.0.0.10",
                            "dst": "10.0.0.20",
                            "protocol": "HTTP",
                            "request": "GET /allocate?mib=2",
                            "response_code": 200,
                            "duration_us": "16242",
                            "trace_id": "467fee396d50216e33083b8667268457",
                            "span_id": "b226f2348bdbf11e",
                        }
                    ]
                },
            },
            {
                "evidence_id": "tempo-live",
                "source_system": "tempo",
                "dimension": "trace",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "spans": [
                        {
                            "trace_id": "467fee396d50216e33083b8667268457",
                            "service": "api",
                            "name": "GET /allocate",
                            "start": "2026-07-16T00:11:58Z",
                            "end": "2026-07-16T00:11:59Z",
                            "attributes": {"http.route": "/allocate"},
                        }
                    ]
                },
            },
        ]
        evidence.extend(
            {
                "evidence_id": f"log-live-{index:02d}",
                "source_system": "elasticsearch",
                "dimension": "logs",
                "confidence": "high",
                "directness": "direct",
                "payload": {
                    "pod_uid": "uid-new",
                    "samples": [
                        {
                            "timestamp": f"2026-07-16T00:{10 + index % 10:02d}:00Z",
                            "message": f"log-{index:02d} " + ("x" * 520),
                            "container": "api",
                        }
                    ],
                },
            }
            for index in range(24)
        )
        topology = [
            {
                "source": "service:ns/api",
                "target": "k8s.pod:ns/api:uid-new",
                "relation": "selects",
                "source_system": "kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": ["k8s-live"],
            },
            {
                "source": "k8s.pod:ns/caller:uid-caller",
                "target": "k8s.pod:ns/api:uid-new",
                "relation": "calls",
                "source_system": "deepflow+kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": ["deepflow-live"],
            },
        ]

        ledger = self._build(
            case=case,
            evidence_records=evidence,
            topology=topology,
        )
        records = ledger["records"]
        wire = aiops_case._json({"fact_ledger": ledger})
        coverage = {
            (record["attribute"], record["value"])
            for record in records
            if record["fact_type"] == "coverage"
        }

        for dimension, state in case["coverage"].items():
            self.assertIn((f"{dimension}.coverage", state), coverage)
        self.assertTrue(
            any(record["attribute"] == "container.last_termination" for record in records)
        )
        self.assertTrue(
            any(
                record["attribute"] == "metric.container_memory_working_set_bytes"
                for record in records
            )
        )
        self.assertTrue(any(record["attribute"] == "log.sample" for record in records))
        self.assertTrue(
            any(
                record["attribute"] in {"network.flow", "trace.span"}
                for record in records
            )
        )
        self.assertTrue(
            any(record["attribute"].startswith("topology.") for record in records)
        )
        self.assertTrue(ledger["truncated"])
        self.assertLessEqual(len(wire), 12000)

    def test_absent_empty_and_error_sources_do_not_create_direct_high_observes(self):
        _, edges, _ = build_topology(
            primary={
                "namespace": "ns",
                "name": "api",
                "uid": "uid-new",
                "pod_ip": "10.0.0.20",
                "node": "node-a",
            },
            k8s_record={},
            evidence_refs_by_dimension={
                "metrics": ["prometheus-metrics-empty"],
                "logs": ["elasticsearch-logs-absent"],
                "tracing": ["deepflow-tracing-error"],
            },
            deepflow_coverage="error",
            coverage_by_dimension={
                "metrics": "empty",
                "logs": "absent",
                "tracing": "error",
            },
        )

        invalid = [
            edge
            for edge in edges
            if edge.get("relation") == "observes"
            and edge.get("directness") == "direct"
            and edge.get("confidence") == "high"
        ]
        self.assertEqual(invalid, [])

        ledger = self._build(
            case=_case(
                coverage={
                    **_case()["coverage"],
                    "metrics": "empty",
                }
            ),
            topology=[
                {
                    "source": "evidence:ns/prometheus-metrics",
                    "target": "pod:ns/api",
                    "relation": "observes",
                    "source_system": "prometheus",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": ["prometheus-metrics-empty"],
                }
            ],
        )
        invalid_facts = [
            record
            for record in ledger["records"]
            if record["attribute"] == "topology.observes"
            and record["directness"] == "direct"
            and record["confidence"] == "high"
        ]
        self.assertEqual(invalid_facts, [])

    def test_unverified_caller_ip_mapping_is_only_related_context(self):
        _, edges, _ = build_topology(
            primary={
                "namespace": "ns",
                "name": "api",
                "uid": "uid-new",
                "pod_ip": "10.0.0.20",
                "node": "node-a",
            },
            k8s_record={},
            evidence_refs_by_dimension={"tracing": ["deepflow-flow"]},
            deepflow_coverage="present",
            resolved_callers=[
                {
                    "namespace": "ns",
                    "pod": "caller",
                    "pod_ip": "10.0.0.10",
                }
            ],
        )

        call_edge = next(edge for edge in edges if edge.get("relation") == "calls")
        self.assertEqual(call_edge["directness"], "related_context")
        self.assertEqual(call_edge["confidence"], "weak")

    def test_identity_verified_boolean_without_uid_ip_and_lifetime_proof_is_weak(self):
        _, edges, _ = build_topology(
            primary={
                "namespace": "ns",
                "name": "api",
                "uid": "uid-new",
                "pod_ip": "10.0.0.20",
                "node": "node-a",
            },
            k8s_record={},
            evidence_refs_by_dimension={"tracing": ["deepflow-flow"]},
            deepflow_coverage="present",
            resolved_callers=[
                {
                    "namespace": "ns",
                    "pod": "caller",
                    "pod_ip": "10.0.0.10",
                    "identity_verified": True,
                }
            ],
        )

        call_edge = next(edge for edge in edges if edge.get("relation") == "calls")
        self.assertEqual(call_edge["directness"], "related_context")
        self.assertEqual(call_edge["confidence"], "weak")

    def test_verified_caller_requires_uid_matching_ip_and_in_lifetime_proof(self):
        entities, edges, _ = build_topology(
            primary={
                "namespace": "ns",
                "name": "api",
                "uid": "uid-new",
                "pod_ip": "10.0.0.20",
                "node": "node-a",
            },
            k8s_record={},
            evidence_refs_by_dimension={"tracing": ["deepflow-flow"]},
            deepflow_coverage="present",
            resolved_callers=[
                {
                    "namespace": "ns",
                    "pod": "caller",
                    "uid": "uid-caller",
                    "caller_ip": "10.0.0.10",
                    "pod_ip": "10.0.0.10",
                    "creation_timestamp": "2026-07-16T00:00:00Z",
                    "deletion_timestamp": "2026-07-16T00:20:00Z",
                    "proof_start": "2026-07-16T00:05:00Z",
                    "proof_end": "2026-07-16T00:10:00Z",
                    "identity_verified": True,
                }
            ],
        )

        caller = next(entity for entity in entities if entity.get("name") == "caller")
        call_edge = next(edge for edge in edges if edge.get("relation") == "calls")
        self.assertEqual(caller["entity_id"], "k8s.pod:ns/caller:uid-caller")
        self.assertEqual(call_edge["directness"], "direct")
        self.assertEqual(call_edge["confidence"], "high")

    def test_verified_caller_without_creation_timestamp_remains_weak(self):
        _, edges, _ = build_topology(
            primary={
                "namespace": "ns",
                "name": "api",
                "uid": "uid-new",
                "pod_ip": "10.0.0.20",
                "node": "node-a",
            },
            k8s_record={},
            evidence_refs_by_dimension={"tracing": ["deepflow-flow"]},
            deepflow_coverage="present",
            resolved_callers=[
                {
                    "namespace": "ns",
                    "pod": "caller",
                    "uid": "uid-caller",
                    "caller_ip": "10.0.0.10",
                    "pod_ip": "10.0.0.10",
                    "deletion_timestamp": "2026-07-16T00:20:00Z",
                    "proof_start": "2026-07-16T00:05:00Z",
                    "proof_end": "2026-07-16T00:10:00Z",
                    "identity_verified": True,
                }
            ],
        )

        call_edge = next(edge for edge in edges if edge.get("relation") == "calls")
        self.assertEqual(call_edge["directness"], "related_context")
        self.assertEqual(call_edge["confidence"], "weak")

    def test_collection_window_is_clipped_to_current_pod_lifetime(self):
        clip = getattr(case_builder, "_clip_window_to_pod_lifetime", None)
        self.assertTrue(callable(clip), "Pod lifetime window helper is missing")
        requested = TimeWindow(
            start="2026-07-16T00:00:00Z",
            end="2026-07-16T00:30:00Z",
        )

        clipped = clip(
            requested,
            {
                "creation_timestamp": "2026-07-16T00:05:00Z",
                "deletion_timestamp": "2026-07-16T00:20:00Z",
            },
        )

        self.assertEqual(clipped.start, "2026-07-16T00:05:00Z")
        self.assertEqual(clipped.end, "2026-07-16T00:20:00Z")
        self.assertFalse(getattr(clipped, "empty", False))

    def test_disjoint_lifetime_windows_are_explicitly_empty(self):
        windows = [
            (
                TimeWindow(
                    start="2026-07-16T00:00:00Z",
                    end="2026-07-16T00:01:00Z",
                ),
                {
                    "creation_timestamp": "2026-07-16T00:05:00Z",
                    "deletion_timestamp": "2026-07-16T00:20:00Z",
                },
            ),
            (
                TimeWindow(
                    start="2026-07-16T00:21:00Z",
                    end="2026-07-16T00:30:00Z",
                ),
                {
                    "creation_timestamp": "2026-07-16T00:05:00Z",
                    "deletion_timestamp": "2026-07-16T00:20:00Z",
                },
            ),
            (
                TimeWindow(
                    start="2026-07-16T00:00:00Z",
                    end="2026-07-16T00:02:00Z",
                ),
                {
                    "creation_timestamp": "2026-07-16T00:10:00Z",
                    "deletion_timestamp": "2026-07-16T00:12:00Z",
                },
            ),
        ]

        for requested, lifetime in windows:
            with self.subTest(requested=requested):
                clipped = case_builder._clip_window_to_pod_lifetime(requested, lifetime)
                self.assertTrue(clipped.empty)

    def test_disjoint_window_skips_all_downstream_collectors_and_trace_discovery(self):
        cfg = SimpleNamespace(
            cases_dir=Path(tempfile.gettempdir()) / "aiops-fact-ledger-empty-window-tests",
            timeout_seconds=5,
            prometheus_url="http://prometheus",
            elasticsearch_url="http://elasticsearch",
            elasticsearch_index="filebeat-*",
            max_records=20,
            deepflow_clickhouse_url="http://clickhouse",
            deepflow_clickhouse_database="flow_log",
        )
        k8s = SourceResult(
            "kubernetes",
            "k8s",
            "present",
            records=[
                {
                    "namespace": "ns",
                    "pod": "api",
                    "uid": "uid-new",
                    "pod_ip": "10.0.0.20",
                    "node": "node-a",
                    "creation_timestamp": "2026-07-16T00:05:00Z",
                    "deletion_timestamp": "2026-07-16T00:20:00Z",
                }
            ],
        )

        for start, end in (
            ("2026-07-16T00:00:00Z", "2026-07-16T00:01:00Z"),
            ("2026-07-16T00:21:00Z", "2026-07-16T00:30:00Z"),
            ("2026-07-16T00:01:00Z", "2026-07-16T00:02:00Z"),
        ):
            with (
                self.subTest(start=start, end=end),
                patch.object(case_builder, "get_config", return_value=cfg),
                patch.object(case_builder, "collect_kubernetes", return_value=k8s),
                patch.object(case_builder, "collect_prometheus") as metrics,
                patch.object(case_builder, "collect_elasticsearch") as logs,
                patch.object(case_builder, "collect_deepflow") as flows,
                patch.object(case_builder, "discover_correlation") as correlation,
                patch.object(case_builder, "collect_tempo") as tempo,
                patch.object(case_builder, "_collect_caller_logs") as caller_logs,
                patch.object(case_builder, "_write_package") as write_package,
                patch.object(case_builder, "case_summary", return_value={"ok": True}),
            ):
                result = case_builder.collect_case(
                    {
                        "namespace": "ns",
                        "pod": "api",
                        "start": start,
                        "end": end,
                    }
                )

            self.assertTrue(result["ok"])
            metrics.assert_not_called()
            logs.assert_not_called()
            flows.assert_not_called()
            correlation.assert_not_called()
            tempo.assert_not_called()
            caller_logs.assert_not_called()
            written_window = write_package.call_args.args[4]
            self.assertTrue(written_window["empty"])

    def test_non_coverage_facts_without_real_evidence_refs_are_dropped(self):
        ledger = self._build(
            case=_case(coverage={**_case()["coverage"], "logs": "present"}),
            evidence_records=[
                {
                    "source_system": "elasticsearch",
                    "dimension": "logs",
                    "summary": "unreferenced log",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "pod_uid": "uid-new",
                        "samples": [
                            {
                                "timestamp": "2026-07-16T00:10:00Z",
                                "message": "must be dropped",
                            }
                        ],
                    },
                }
            ],
            topology=[
                {
                    "source": "service:ns/api",
                    "target": "k8s.pod:ns/api:uid-new",
                    "relation": "selects",
                    "source_system": "kubernetes",
                    "directness": "direct",
                    "confidence": "high",
                    "evidence_refs": [],
                }
            ],
        )

        non_coverage = [
            record for record in ledger["records"] if record["fact_type"] != "coverage"
        ]
        self.assertEqual(non_coverage, [])

    def test_forbidden_nested_keys_are_normalized_across_case_and_punctuation(self):
        ledger = self._build(
            evidence_records=[
                {
                    "evidence_id": "metric-normalized-keys",
                    "source_system": "prometheus",
                    "dimension": "metrics",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "pod_uid": "uid-new",
                        "highlights": [
                            {
                                "metric": "custom",
                                "start_time": "2026-07-16T00:10:00Z",
                                "end_time": "2026-07-16T00:11:00Z",
                                "rootCause": "hidden",
                                "Expected-Remediation": "hidden",
                                "GROUND.TRUTH": "hidden",
                                "evaluatorLabel": "hidden",
                                "root-cause-state": "hidden",
                                "Expected.Remediation.Plan": "hidden",
                                "groundTruthLabel": "hidden",
                                "diagnosisReason": "hidden",
                                "preFailureBehavior": "hidden",
                                "safe_value": "visible",
                            }
                        ],
                    },
                }
            ]
        )

        rendered = json.dumps(ledger, ensure_ascii=False)
        for forbidden in (
            "rootCause",
            "Expected-Remediation",
            "GROUND.TRUTH",
            "evaluatorLabel",
            "root-cause-state",
            "Expected.Remediation.Plan",
            "groundTruthLabel",
            "diagnosisReason",
            "preFailureBehavior",
            "hidden",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertIn("safe_value", rendered)

    def test_topology_ledger_and_details_share_uid_scoped_primary_id(self):
        primary = {
            "namespace": "ns",
            "name": "api",
            "uid": "uid-new",
            "pod_ip": "10.0.0.20",
            "node": "node-a",
        }
        entities, edges, _ = build_topology(
            primary=primary,
            k8s_record={},
            evidence_refs_by_dimension={"k8s": ["k8s-ref"]},
            deepflow_coverage="absent",
        )
        ledger = self._build(
            entities=entities,
            topology=edges,
            evidence_records=[
                {
                    "evidence_id": "k8s-ref",
                    "source_system": "kubernetes",
                    "dimension": "k8s",
                    "payload": {"uid": "uid-new", "phase": "Running"},
                }
            ],
        )
        details = case_builder.topology_details(entities, edges)
        primary_id = "k8s.pod:ns/api:uid-new"

        self.assertIn(primary_id, {entity["entity_id"] for entity in entities})
        self.assertIn(primary_id, ledger["scope_entity_ids"])
        self.assertTrue(
            any(
                primary_id in (edge.get("source"), edge.get("target"))
                for edge in edges
            )
        )
        self.assertIn(primary_id, json.dumps(details, ensure_ascii=False))

    def test_selected_fact_values_are_not_silently_truncated(self):
        message = "m" * 1400
        ledger = self._build(
            case=_case(coverage={**_case()["coverage"], "logs": "present"}),
            evidence_records=[
                {
                    "evidence_id": "exact-log-value",
                    "source_system": "elasticsearch",
                    "dimension": "logs",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "pod_uid": "uid-new",
                        "samples": [
                            {
                                "timestamp": "2026-07-16T00:10:00Z",
                                "message": message,
                            }
                        ],
                    },
                }
            ],
        )

        fact = next(
            record
            for record in ledger["records"]
            if "exact-log-value" in record["evidence_refs"]
            and record["attribute"] == "log.sample"
        )
        self.assertEqual(fact["value"]["message"], message)


    def test_collect_case_uses_lifetime_window_for_logs_metrics_and_flows(self):
        cfg = SimpleNamespace(
            cases_dir=Path(tempfile.gettempdir()) / "aiops-fact-ledger-tests",
            timeout_seconds=5,
            prometheus_url="http://prometheus",
            elasticsearch_url="http://elasticsearch",
            elasticsearch_index="filebeat-*",
            max_records=20,
            deepflow_clickhouse_url="http://clickhouse",
            deepflow_clickhouse_database="flow_log",
        )
        k8s = SourceResult(
            "kubernetes",
            "k8s",
            "present",
            records=[
                {
                    "namespace": "ns",
                    "pod": "api",
                    "uid": "uid-new",
                    "pod_ip": "10.0.0.20",
                    "node": "node-a",
                    "creation_timestamp": "2026-07-16T00:05:00Z",
                    "deletion_timestamp": "2026-07-16T00:20:00Z",
                }
            ],
            evidence=[
                EvidenceItem(
                    evidence_id="k8s-pod",
                    source_system="kubernetes",
                    dimension="k8s",
                    summary="pod snapshot",
                )
            ],
        )
        empty_metrics = SourceResult("prometheus", "metrics", "empty")
        empty_logs = SourceResult("elasticsearch", "logs", "empty")
        empty_flows = SourceResult("deepflow", "tracing", "empty")
        empty_traces = SourceResult("tempo", "trace", "empty")

        with (
            patch.object(case_builder, "get_config", return_value=cfg),
            patch.object(case_builder, "collect_kubernetes", return_value=k8s),
            patch.object(case_builder, "collect_prometheus", return_value=empty_metrics) as metrics,
            patch.object(case_builder, "collect_elasticsearch", return_value=empty_logs) as logs,
            patch.object(case_builder, "collect_deepflow", return_value=empty_flows) as flows,
            patch.object(case_builder, "discover_correlation", return_value={"trace_ids": [], "caller_ips": []}) as correlation,
            patch.object(case_builder, "collect_tempo", return_value=empty_traces),
            patch.object(case_builder, "_collect_caller_logs", return_value=None),
            patch.object(case_builder, "_write_package"),
            patch.object(case_builder, "case_summary", return_value={"ok": True}),
        ):
            result = case_builder.collect_case(
                {
                    "namespace": "ns",
                    "pod": "api",
                    "start": "2026-07-16T00:00:00Z",
                    "end": "2026-07-16T00:30:00Z",
                }
            )

        self.assertTrue(result["ok"])
        for collector in (metrics, logs, flows):
            self.assertEqual(collector.call_args.kwargs["start"], "2026-07-16T00:05:00Z")
            self.assertEqual(collector.call_args.kwargs["end"], "2026-07-16T00:20:00Z")
        self.assertEqual(correlation.call_args.args[3], "2026-07-16T00:05:00Z")
        self.assertEqual(correlation.call_args.args[4], "2026-07-16T00:20:00Z")


if __name__ == "__main__":
    unittest.main()
