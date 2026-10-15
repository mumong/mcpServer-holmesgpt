import json
import unittest
from unittest.mock import patch

from servers.aiops_observability.collectors import deepflow
from servers.holmes_tools import aiops_observability_fine


class AiopsFineToolTests(unittest.TestCase):
    def test_fine_tools_are_pod_scoped_and_registered(self):
        names = {tool.name for tool in aiops_observability_fine.TOOLS}
        self.assertIn("resolve_aiops_entity", names)
        self.assertIn("query_aiops_metrics", names)
        self.assertIn("query_aiops_logs", names)
        self.assertIn("query_aiops_deepflow_flows", names)
        self.assertIn("build_aiops_topology", names)
        for tool in aiops_observability_fine.TOOLS:
            props = tool.inputSchema.get("properties", {})
            self.assertIn("pod", props)
            self.assertNotIn("query", props)
            self.assertNotIn("sql", props)

    def test_build_topology_returns_compact_summary(self):
        with patch.object(
            aiops_observability_fine,
            "collect_case",
            return_value={
                "ok": True,
                "case_id": "fine-topology-aiops-temp-pod",
                "coverage": {"topology": "present"},
                "topology_summary": {"edge_count": 2, "directness": {"direct": 2}},
                "recommended_refs_by_dimension": {"topology": ["topology-primary"]},
                "package_ref": "/tmp/case",
            },
        ):
            payload = json.loads(
                aiops_observability_fine.call_tool(
                    "build_aiops_topology",
                    {"namespace": "aiops-temp", "pod": "pod"},
                )
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["dimension"], "topology")
        self.assertEqual(payload["topology_summary"]["directness"]["direct"], 2)

    def test_deepflow_uses_configured_database_and_current_columns(self):
        captured = {}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": []}

        def fake_post(url, data, timeout):
            captured["url"] = url
            captured["sql"] = data.decode("utf-8")
            captured["timeout"] = timeout
            return Response()

        with patch.object(deepflow._http.SESSION, "post", side_effect=fake_post):
            result = deepflow.collect_deepflow(
                clickhouse_url="http://clickhouse:8123",
                clickhouse_database="flow_log",
                namespace="aiops-temp",
                pod="pod",
                pod_ip="10.0.0.10",
                node="master",
                start="2026-07-08T05:00:00Z",
                end="2026-07-08T06:00:00Z",
                timeout=5,
                max_records=10,
            )

        self.assertEqual(result.coverage, "empty")
        self.assertIn("FROM flow_log.l7_flow_log", captured["sql"])
        # timezone-explicit UTC parsing (fix for Asia/Shanghai server TZ offset)
        self.assertIn("toDateTime('2026-07-08 05:00:00', 'UTC')", captured["sql"])
        self.assertNotIn("l4_protocol", captured["sql"])


if __name__ == "__main__":
    unittest.main()
