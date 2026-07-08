import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from servers.aiops_observability.case_builder import _infer_abnormal_type
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
                    "strength": "strong",
                    "observed": "lastState.terminated.reason is OOMKilled",
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
            json.dumps({"entity_id": "pod:aiops-temp/aiops-oom-business", "kind": "Pod"}) + "\n",
            encoding="utf-8",
        )
        (case_dir / "topology.jsonl").write_text(
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
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "logs.jsonl").write_text("", encoding="utf-8")
        (evidence_dir / "deepflow_l4.jsonl").write_text("", encoding="utf-8")
        (evidence_dir / "deepflow_l7.jsonl").write_text("", encoding="utf-8")
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
            self.assertEqual(payload["topology_summary"]["directness"]["direct"], 1)
            self.assertEqual(payload["recommended_refs_by_dimension"]["metrics"], ["metric-aiops-temp-aiops-oom-business-prometheus"])
            rendered = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("root_cause_label", rendered)
            self.assertNotIn("expected_remediation", rendered)
            self.assertNotIn("should_not_leak", rendered)

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


if __name__ == "__main__":
    unittest.main()
