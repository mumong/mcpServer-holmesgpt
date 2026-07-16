import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from servers.aiops_observability.case_builder import (
    _align_correlated_samples,
    _fit_dimension_details,
    _infer_abnormal_type,
    _rank_log_samples,
    _write_package,
)
from servers.aiops_observability.schemas import EvidenceItem, SourceResult
from servers.holmes_tools import aiops_case


class AiopsCaseToolTests(unittest.TestCase):
    def _write_case(self, root: Path) -> Path:
        case_dir = root / "cases" / "oomkilled-aiops-temp-aiops-oom-business"
        evidence_dir = case_dir / "evidence"
        evidence_dir.mkdir(parents=True)
        (case_dir / "case.yaml").write_text(
            """
case_id: oomkilled-aiops-temp-aiops-oom-business
abnormal_type: oomkilled
scenario: auto
primary_entity:
  kind: Pod
  namespace: aiops-temp
  name: aiops-oom-business
  uid: pod-uid
  node: master
  pod_ip: 10.244.0.10
coverage:
  k8s: present
  metrics: present
  logs: present
  tracing: weak
  topology: present
recommended_refs_by_dimension:
  k8s:
    - k8s-aiops-temp-aiops-oom-business-pod-yaml
  metrics:
    - metric-aiops-temp-aiops-oom-business-prometheus
""".strip(),
            encoding="utf-8",
        )
        (case_dir / "signals.jsonl").write_text(
            json.dumps(
                {
                    "signal_id": "k8s-present",
                    "dimension": "k8s",
                    "role": "root_cause_state",
                    "strength": "strong",
                    "observed": "lastState.terminated.reason is OOMKilled",
                    "interpretation": "pre_failure_behavior request_driver",
                    "evidence_refs": ["k8s-aiops-temp-aiops-oom-business-pod-yaml"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (case_dir / "timeline.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": "2026-07-08T05:00:00Z",
                    "dimension": "k8s",
                    "summary": "pod OOMKilled",
                    "evidence_refs": ["k8s-aiops-temp-aiops-oom-business-pod-yaml"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (case_dir / "entities.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "entity_id": "service:aiops-temp/aiops-oom-business",
                            "kind": "Service",
                            "namespace": "aiops-temp",
                            "name": "aiops-oom-business",
                        }
                    ),
                    json.dumps(
                        {
                            "entity_id": "pod:aiops-temp/aiops-oom-business",
                            "kind": "Pod",
                            "namespace": "aiops-temp",
                            "name": "aiops-oom-business",
                        }
                    ),
                    json.dumps(
                        {
                            "entity_id": "node:cluster/master",
                            "kind": "Node",
                            "name": "master",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (case_dir / "topology.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "source": "service:aiops-temp/aiops-oom-business",
                            "relation": "selects",
                            "target": "pod:aiops-temp/aiops-oom-business",
                            "source_system": "kubernetes",
                            "directness": "direct",
                            "confidence": "high",
                            "evidence_refs": ["k8s-aiops-temp-aiops-oom-business-pod-yaml"],
                        }
                    ),
                    json.dumps(
                        {
                            "source": "pod:aiops-temp/aiops-oom-business",
                            "relation": "scheduled_on",
                            "target": "node:cluster/master",
                            "source_system": "kubernetes",
                            "directness": "direct",
                            "confidence": "high",
                            "evidence_refs": ["k8s-aiops-temp-aiops-oom-business-pod-yaml"],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (case_dir / "labels.yaml").write_text(
            "root_cause_label: should_not_leak\nexpected_remediation: should_not_leak\n",
            encoding="utf-8",
        )
        (evidence_dir / "k8s_events.jsonl").write_text(
            json.dumps(
                {
                    "evidence_id": "k8s-aiops-temp-aiops-oom-business-pod-yaml",
                    "source_system": "kubernetes",
                    "dimension": "k8s",
                    "summary": "OOMKilled exit 137",
                    "confidence": "high",
                    "directness": "direct",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "metrics.jsonl").write_text(
            json.dumps(
                {
                    "evidence_id": "metric-aiops-temp-aiops-oom-business-prometheus",
                    "source_system": "prometheus",
                    "dimension": "metrics",
                    "summary": "memory reached limit",
                    "confidence": "high",
                    "directness": "direct",
                    "payload": {
                        "highlights": [
                            {
                                "metric": "container_memory_working_set_bytes",
                                "container": "business-api",
                                "start": "18.4Mi",
                                "max": "63.2Mi",
                                "last": "0.3Mi",
                                "limit": "64.0Mi",
                                "max_limit_ratio": 0.9875,
                                "samples": ["00:00:00=18.4Mi", "00:01:00=63.2Mi"],
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "logs.jsonl").write_text(
            json.dumps(
                {
                    "evidence_id": "log-aiops-temp-aiops-oom-business-elasticsearch",
                    "source_system": "elasticsearch",
                    "dimension": "logs",
                    "summary": "business allocation logs",
                    "payload": {
                        "samples": [
                            {
                                "timestamp": "2026-07-08T05:00:00Z",
                                "message": "trace_id=abc123 span_id=span01 allocated_mib=50",
                                "container": "business-api",
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "deepflow_l4.jsonl").write_text("", encoding="utf-8")
        (evidence_dir / "deepflow_l7.jsonl").write_text(
            json.dumps(
                {
                    "evidence_id": "deepflow-aiops-temp-aiops-oom-business-pod-ip-flow",
                    "source_system": "deepflow",
                    "dimension": "tracing",
                    "summary": "real HTTP flow",
                    "payload": {
                        "samples": [
                            {
                                "timestamp": "2026-07-08T05:00:00Z",
                                "src": "10.244.0.20",
                                "dst": "10.244.0.10",
                                "protocol": "HTTP",
                                "request": "GET /allocate?mib=2",
                                "response_code": 200,
                                "duration_us": 1820,
                                "trace_id": "abc123",
                                "span_id": "span01",
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "tempo_traces.jsonl").write_text(
            json.dumps(
                {
                    "evidence_id": "tempo-aiops-temp-aiops-oom-business-spans",
                    "source_system": "tempo",
                    "dimension": "trace",
                    "summary": "real OTLP span",
                    "payload": {
                        "spans": [
                            {
                                "trace_id": "abc123",
                                "service": "aiops-oom-business",
                                "name": "GET /allocate",
                                "start": "2026-07-08T05:00:00Z",
                                "end": "2026-07-08T05:00:00.001820Z",
                                "attributes": {"aiops.allocated_mib.after": 50},
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "k8s_describe.txt").write_text("Last State: OOMKilled", encoding="utf-8")
        (evidence_dir / "k8s_pod.yaml").write_text("kind: Pod\n", encoding="utf-8")
        return case_dir

    def test_get_aiops_case_returns_compact_agent_safe_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_case(root)

            with patch.dict(os.environ, {"AIOPS_CASES_DIR": str(root / "cases")}):
                payload = json.loads(aiops_case.call_tool("get_aiops_case", {"case_id": "oomkilled-aiops-temp-aiops-oom-business"}))

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["case_id"], "oomkilled-aiops-temp-aiops-oom-business")
            self.assertEqual(payload["coverage"]["metrics"], "present")
            self.assertEqual(payload["topology_summary"]["directness"]["direct"], 2)
            self.assertEqual(payload["recommended_refs_by_dimension"]["metrics"], ["metric-aiops-temp-aiops-oom-business-prometheus"])
            self.assertEqual(payload["fact_ledger"]["contract_version"], "aiops.fact-ledger.v1")
            self.assertEqual(payload["fact_ledger"]["scope_entity_ids"], ["k8s.pod:aiops-temp/aiops-oom-business:pod-uid"])
            details = payload["dimension_details"]
            self.assertEqual(details["metrics"]["highlights"][0]["max"], "63.2Mi")
            self.assertEqual(details["logs"]["samples"][0]["container"], "business-api")
            self.assertIn("allocated_mib=50", details["logs"]["samples"][0]["message"])
            self.assertEqual(details["tracing"]["flows"][0]["request"], "GET /allocate?mib=2")
            self.assertEqual(details["tracing"]["spans"][0]["attributes"]["aiops.allocated_mib.after"], 50)
            self.assertEqual(details["topology"]["edges"][0]["relationship"], "Service --selects--> Pod")
            topology_entity_ids = {
                entity["entity_id"]
                for entity in details["topology"]["entities"]
            }
            self.assertIn(
                "k8s.pod:aiops-temp/aiops-oom-business:pod-uid",
                topology_entity_ids,
            )
            self.assertNotIn(
                "pod:aiops-temp/aiops-oom-business",
                topology_entity_ids,
            )
            self.assertTrue(details["metrics"]["highlights"][0]["evidence_ref"])
            self.assertTrue(details["logs"]["samples"][0]["evidence_ref"])
            self.assertTrue(details["tracing"]["flows"][0]["evidence_ref"])
            self.assertLessEqual(len(json.dumps(details, ensure_ascii=False)), 6000)
            rendered = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("root_cause_label", rendered)
            self.assertNotIn("expected_remediation", rendered)
            self.assertNotIn("should_not_leak", rendered)
            self.assertNotIn("root_cause_state", json.dumps(payload["fact_ledger"], ensure_ascii=False))
            self.assertNotIn("pre_failure_behavior", json.dumps(payload["fact_ledger"], ensure_ascii=False))
            self.assertNotIn("request_driver", json.dumps(payload["fact_ledger"], ensure_ascii=False))

    def test_dimension_details_budget_trims_large_log_payloads(self):
        details = {
            "metrics": {"coverage": "present", "highlights": []},
            "logs": {
                "coverage": "present",
                "samples": [
                    {
                        "timestamp": "2026-07-13T00:00:00Z",
                        "message": "x" * 2000,
                        "container": "business-api",
                        "evidence_ref": "log-ref",
                    }
                    for _ in range(8)
                ],
            },
            "tracing": {"coverage": "empty", "flows": [], "spans": [], "call_chains": []},
            "topology": {"coverage": "present", "entities": [], "edges": []},
        }

        bounded = _fit_dimension_details(details, max_chars=6000)

        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False)), 6000)
        self.assertTrue(bounded["logs"]["samples"])
        self.assertIn("message", bounded["logs"]["samples"][0])

    def test_log_samples_prioritize_previous_high_watermark_before_restart_startup(self):
        samples = [
            {
                "message": '{"event":"allocate","allocated_mib":2}',
                "evidence_ref": "log-current",
                "role": "target",
            },
            {
                "message": '{"event":"allocate","allocated_mib":62}',
                "evidence_ref": "log-previous",
                "role": "target",
            },
            {
                "message": '{"event":"request_succeeded","allocated_mib":40}',
                "evidence_ref": "caller-log",
                "role": "request_driver",
            },
        ]

        ranked = _rank_log_samples(samples)

        self.assertEqual(ranked[0]["evidence_ref"], "log-previous")
        self.assertIn('"allocated_mib":62', ranked[0]["message"])

    def test_write_package_persists_caller_logs_as_agent_visible_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            case_dir = Path(temp) / "case"
            caller = SourceResult(
                "elasticsearch",
                "caller_logs",
                "present",
                records=[{"message": "request_succeeded trace_id=abc"}],
                evidence=[
                    EvidenceItem(
                        evidence_id="caller-log-ns-driver",
                        source_system="elasticsearch",
                        dimension="caller_logs",
                        summary="caller request",
                        payload={
                            "samples": [
                                {
                                    "message": "request_succeeded trace_id=abc",
                                    "container": "driver",
                                }
                            ]
                        },
                    )
                ],
                query_meta={"caller_pod": "ns/driver", "caller_ip": "10.0.0.10"},
            )

            _write_package(
                case_dir,
                "case",
                "auto",
                {
                    "kind": "Pod",
                    "namespace": "ns",
                    "name": "api",
                    "uid": "uid",
                    "pod_ip": "10.0.0.20",
                    "node": "node1",
                },
                {"start": "2026-07-13T00:00:00Z", "end": "2026-07-13T00:30:00Z"},
                [caller],
            )

            persisted = (case_dir / "evidence" / "logs.jsonl").read_text(encoding="utf-8")
            self.assertIn("caller-log-ns-driver", persisted)
            self.assertIn('"dimension": "caller_logs"', persisted)

    def test_get_aiops_case_evidence_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_case(root)

            with patch.dict(os.environ, {"AIOPS_CASES_DIR": str(root / "cases")}):
                payload = json.loads(
                    aiops_case.call_tool(
                        "get_aiops_case_evidence",
                        {"case_id": "oomkilled-aiops-temp-aiops-oom-business", "evidence_ref": "../labels.yaml"},
                    )
                )

            self.assertFalse(payload["ok"])
            self.assertIn("invalid evidence_ref", payload["error"])

    def test_get_aiops_case_evidence_rejects_evaluator_only_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_case(root)

            with patch.dict(os.environ, {"AIOPS_CASES_DIR": str(root / "cases")}):
                payload = json.loads(
                    aiops_case.call_tool(
                        "get_aiops_case_evidence",
                        {"case_id": "oomkilled-aiops-temp-aiops-oom-business", "evidence_ref": "labels.yaml"},
                    )
                )

            self.assertFalse(payload["ok"])
            self.assertIn("not agent-visible", payload["error"])
            rendered = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("should_not_leak", rendered)

    def test_collect_aiops_case_uses_internal_builder_not_dataset_script(self):
        fake_payload = {
            "ok": True,
            "case_id": "auto-aiops-temp-aiops-oom-business",
            "runtime_status": {"data_dependency": False},
            "recommended_refs_by_dimension": {"k8s": ["k8s-ref"]},
            "topology_summary": {"directness": {"direct": 1}, "confidence": {"high": 1}},
        }
        with patch.object(aiops_case, "collect_case", return_value=fake_payload) as collect:
            payload = json.loads(
                aiops_case.call_tool(
                    "collect_aiops_case",
                    {"namespace": "aiops-temp", "pod": "aiops-oom-business", "scenario": "auto"},
                )
            )

        collect.assert_called_once()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["runtime_status"]["data_dependency"])

    def test_infers_imagepullbackoff_from_k8s_waiting_reason(self):
        source = SourceResult(
            "kubernetes",
            "k8s",
            "present",
            records=[
                {
                    "container_statuses": [
                        {
                            "state": {
                                "waiting": {
                                    "reason": "ImagePullBackOff",
                                    "message": "Back-off pulling image",
                                }
                            }
                        }
                    ]
                }
            ],
            evidence=[
                EvidenceItem(
                    evidence_id="k8s-event",
                    source_system="kubernetes",
                    dimension="k8s",
                    summary="Warning Failed Error: ImagePullBackOff",
                )
            ],
        )

        self.assertEqual(_infer_abnormal_type([source]), "imagepullbackoff")

    def test_infers_crashloopbackoff_from_full_k8s_event_payload_during_restart_race(self):
        source = SourceResult(
            "kubernetes",
            "k8s",
            "present",
            records=[
                {
                    "container_statuses": [
                        {
                            "restartCount": 4,
                            "state": {"running": {"startedAt": "2026-07-13T06:48:00Z"}},
                            "lastState": {
                                "terminated": {
                                    "reason": "Error",
                                    "exitCode": 78,
                                }
                            },
                        }
                    ],
                    "last_terminated": [
                        {
                            "container": "business-api",
                            "reason": "Error",
                            "exitCode": 78,
                        }
                    ],
                }
            ],
            evidence=[
                EvidenceItem(
                    evidence_id="k8s-event",
                    source_system="kubernetes",
                    dimension="k8s",
                    summary="Normal Started container business-api",
                    payload={
                        "events_preview": (
                            "Warning BackOff Back-off restarting failed container "
                            "business-api"
                        )
                    },
                )
            ],
        )

        self.assertEqual(_infer_abnormal_type([source]), "crashloopbackoff")

    def test_log_ranking_prefers_diagnostic_json_over_unavailable_previous_log(self):
        samples = [
            {
                "message": (
                    "unable to retrieve container logs for "
                    "containerd://deadbeef"
                ),
                "role": "target",
                "evidence_ref": "log-pod-previous",
            },
            {
                "message": json.dumps(
                    {
                        "event": "config_missing",
                        "error_code": "CONFIG_MISSING",
                        "http_status": 500,
                        "trace_id": "abc123",
                    }
                ),
                "role": "target",
                "evidence_ref": "log-pod-elasticsearch",
            },
        ]

        ranked = _rank_log_samples(samples)

        self.assertIn("CONFIG_MISSING", ranked[0]["message"])
        self.assertIn("unable to retrieve", ranked[-1]["message"])

    def test_correlated_samples_prefer_same_trace_across_logs_deepflow_and_tempo(self):
        logs = [
            {
                "message": json.dumps({"event": "config_missing", "trace_id": "log-only"}),
                "source_system": "kubernetes",
                "role": "target",
                "evidence_ref": "log-previous",
            },
            {
                "message": json.dumps({"event": "config_missing", "trace_id": "shared-trace"}),
                "source_system": "elasticsearch",
                "role": "target",
                "evidence_ref": "log-elasticsearch",
            },
        ]
        flows = [
            {"trace_id": "shared-trace", "request": "GET /checkout?order_id=42"},
        ]
        spans = [
            {"trace_id": "shared-trace", "name": "GET /checkout"},
        ]

        aligned_logs, aligned_flows, aligned_spans = _align_correlated_samples(
            logs,
            flows,
            spans,
        )

        self.assertEqual(aligned_logs[0]["source_system"], "elasticsearch")
        self.assertIn("shared-trace", aligned_logs[0]["message"])
        self.assertEqual(aligned_flows[0]["trace_id"], "shared-trace")
        self.assertEqual(aligned_spans[0]["trace_id"], "shared-trace")


if __name__ == "__main__":
    unittest.main()
