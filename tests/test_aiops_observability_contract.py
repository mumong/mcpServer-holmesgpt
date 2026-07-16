"""Coarse AIOps case MCP contract + no-data-dependency assertions (T021/T022).

Complements test_aiops_case_tool.py (anti-leak / path-safety / builder wiring)
by pinning the FR-006 summary contract and proving, at the source level, that
the coarse tools carry no runtime dependency on the `data` project.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from servers.holmes_tools import aiops_case


PKG = Path(aiops_case.__file__).resolve().parents[2] / "servers"

FR006_SUMMARY_FIELDS = [
    "coverage",
    "signals_summary",
    "timeline_summary",
    "topology_summary",
    "evidence_inventory",
    "evidence_refs",
    "recommended_refs_by_dimension",
    "dimension_details",
    "fact_ledger",
]

FORBIDDEN_DATA_REFS = [
    "data/scripts/collect_case",
    "../data",
    "AIOPS_DATASET_ROOT",
    "AIOPS_COLLECT_SCRIPT",
    "combine-aiops-mcp/data",
]


def _write_min_case(root: Path) -> str:
    case_id = "auto-aiops-temp-demo"
    case_dir = root / case_id
    (case_dir / "evidence").mkdir(parents=True)
    (case_dir / "case.yaml").write_text(
        "case_id: auto-aiops-temp-demo\n"
        "abnormal_type: oomkilled\n"
        "scenario: auto\n"
        "coverage:\n  k8s: present\n  metrics: empty\n  tracing: weak\n"
        "recommended_refs_by_dimension:\n  k8s:\n    - k8s-demo-ref\n",
        encoding="utf-8",
    )
    for name in ("signals.jsonl", "timeline.jsonl", "entities.jsonl", "topology.jsonl"):
        (case_dir / name).write_text("", encoding="utf-8")
    return case_id


class CoarseContractTests(unittest.TestCase):
    def test_coarse_tools_are_registered(self):
        names = {tool.name for tool in aiops_case.TOOLS}
        self.assertEqual(
            names,
            {"collect_aiops_case", "get_aiops_case", "get_aiops_case_evidence", "search_aiops_cases"},
        )
        collect = next(t for t in aiops_case.TOOLS if t.name == "collect_aiops_case")
        self.assertEqual(collect.inputSchema["required"], ["pod"])  # namespace+pod primary, pod mandatory

    def test_collect_aiops_case_description_marks_preferred_live_entrypoint(self):
        collect = next(tool for tool in aiops_case.TOOLS if tool.name == "collect_aiops_case")
        description = collect.description.lower()
        for term in (
            "preferred",
            "prometheus",
            "logging",
            "deepflow",
            "tempo",
            "topology",
            "abnormal kubernetes pod",
        ):
            self.assertIn(term, description)

    def test_get_aiops_case_summary_satisfies_fr006_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            case_id = _write_min_case(root)
            with patch.dict(os.environ, {"AIOPS_CASES_DIR": str(root)}):
                payload = json.loads(aiops_case.call_tool("get_aiops_case", {"case_id": case_id}))

        self.assertTrue(payload["ok"])
        for field in FR006_SUMMARY_FIELDS:
            self.assertIn(field, payload, f"FR-006 requires '{field}' in the coarse case summary")
        self.assertEqual(payload["coverage"]["tracing"], "weak")  # honest weak state survives
        self.assertEqual(payload["fact_ledger"]["contract_version"], "aiops.fact-ledger.v1")
        self.assertFalse(payload["fact_ledger"]["legacy_contract"])


class NoDataDependencyTests(unittest.TestCase):
    def _source_files(self):
        files = [PKG / "holmes_tools" / "aiops_case.py"]
        files += sorted((PKG / "aiops_observability").glob("*.py"))
        files += sorted((PKG / "aiops_observability" / "collectors").glob("*.py"))
        return files

    def test_coarse_source_has_no_data_project_runtime_dependency(self):
        offenders = []
        for path in self._source_files():
            text = path.read_text(encoding="utf-8")
            for needle in FORBIDDEN_DATA_REFS:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        self.assertEqual(offenders, [], f"forbidden data-project references found: {offenders}")

    def test_collect_aiops_case_reports_no_data_dependency(self):
        fake = {"ok": True, "case_id": "auto-x", "runtime_status": {"data_dependency": False}}
        with patch.object(aiops_case, "collect_case", return_value=fake):
            payload = json.loads(
                aiops_case.call_tool("collect_aiops_case", {"namespace": "ns", "pod": "p", "scenario": "auto"})
            )
        self.assertFalse(payload["runtime_status"]["data_dependency"])


if __name__ == "__main__":
    unittest.main()
