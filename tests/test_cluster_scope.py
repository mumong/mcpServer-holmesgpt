"""Per-call cluster scope for MCP tools (specs/008-multi-cluster FR-004/007/012)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp.types import Tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers"))

import cluster_scope  # noqa: E402

MULTI = """
clusters:
  center: vc-xnet-management
  items:
    - name: vc-xnet-management
      aliases: [center]
      access: local
      observability: victoria
    - name: vc-942nkkozc8o5fyb9q3bznb1ws
      aliases: [edge-88]
      access: xnet
      observability: victoria
    - name: legacy-a
      access: local
      observability: legacy
  access:
    xnet:
      proxy_url: https://x/proxy
      runner_url: http://r:8090
observability_profiles:
  victoria:
    metrics: {type: victoriametrics, url: "http://vm:8481/select/0/prometheus", cluster_label: cluster}
    logs: {type: victorialogs_events, url: "http://vl:9428", cluster_field: cluster}
    traces: {type: none}
"""


class ScopeTestBase(unittest.TestCase):
    def use_config(self, text):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        tmp.write(text)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        patcher = mock.patch.dict(os.environ, {"AIOPS_CLUSTERS_CONFIG": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        cluster_scope.reset()
        self.addCleanup(cluster_scope.reset)


class SingleClusterTest(ScopeTestBase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"AIOPS_CLUSTERS_CONFIG": "/nonexistent/clusters.yaml"})
        patcher.start()
        self.addCleanup(patcher.stop)
        cluster_scope.reset()
        self.addCleanup(cluster_scope.reset)

    def test_absent_config_is_local_legacy_and_unchanged(self):
        args = {"namespace": "a"}
        with cluster_scope.activate(args) as scope:
            self.assertIsNone(scope.error)
            self.assertEqual(scope.cluster.name, "local")
            self.assertEqual(cluster_scope.backend("metrics"), "prometheus")
            self.assertIsNone(cluster_scope.subprocess_env())
            self.assertEqual(cluster_scope.prometheus_params(), {})
        self.assertEqual(args, {"namespace": "a"})

    def test_schemas_unchanged_in_single_cluster(self):
        tools = [Tool(name="t", description="d", inputSchema={"type": "object", "properties": {}})]
        self.assertIs(cluster_scope.with_cluster_param(tools), tools)

    def test_annotate_is_noop(self):
        self.assertEqual(cluster_scope.annotate({"ok": True}), {"ok": True})


class MultiClusterTest(ScopeTestBase):
    def setUp(self):
        self.use_config(MULTI)

    def test_cluster_argument_is_consumed_and_resolved(self):
        args = {"namespace": "a", "cluster": "edge-88"}
        with cluster_scope.activate(args) as scope:
            self.assertIsNone(scope.error)
            self.assertEqual(scope.cluster.name, "vc-942nkkozc8o5fyb9q3bznb1ws")
            self.assertEqual(cluster_scope.subprocess_env()["AIOPS_CLUSTER"], "vc-942nkkozc8o5fyb9q3bznb1ws")
        self.assertNotIn("cluster", args)
        self.assertIsNone(cluster_scope.current())

    def test_missing_cluster_is_an_error_in_multi_mode(self):
        with cluster_scope.activate({}) as scope:
            self.assertEqual(scope.error["code"], "cluster_required")
            self.assertIn("edge-88", scope.error["message"])

    def test_unknown_cluster(self):
        with cluster_scope.activate({"cluster": "edge-99"}) as scope:
            self.assertEqual(scope.error["code"], "cluster_unknown")

    def test_victoria_metrics_are_forced_to_the_cluster(self):
        with cluster_scope.activate({"cluster": "center"}):
            self.assertEqual(cluster_scope.prometheus_url(), "http://vm:8481/select/0/prometheus")
            self.assertEqual(cluster_scope.prometheus_params(), {"extra_label": "cluster=vc-xnet-management"})
            self.assertEqual(cluster_scope.backend("metrics"), "victoriametrics")
            self.assertEqual(cluster_scope.backend("logs"), "victorialogs_events")
            self.assertEqual(cluster_scope.backend("traces"), "none")

    def test_local_center_needs_no_shim(self):
        with cluster_scope.activate({"cluster": "center"}):
            self.assertIsNone(cluster_scope.subprocess_env())

    def test_legacy_profile_uses_environment(self):
        with mock.patch.dict(os.environ, {"PROMETHEUS_URL": "http://prom:9090"}):
            with cluster_scope.activate({"cluster": "legacy-a"}):
                self.assertEqual(cluster_scope.backend("metrics"), "prometheus")
                self.assertEqual(cluster_scope.prometheus_url(), "http://prom:9090")
                self.assertEqual(cluster_scope.prometheus_params(), {})

    def test_annotate_adds_cluster_and_backend(self):
        with cluster_scope.activate({"cluster": "edge-88"}):
            out = cluster_scope.annotate({"ok": True, "dimension": "metrics"}, dimension="metrics")
        self.assertEqual(out["cluster"], "vc-942nkkozc8o5fyb9q3bznb1ws")
        self.assertEqual(out["backend"], "victoriametrics")

    def test_schemas_gain_optional_cluster(self):
        tools = [
            Tool(name="t", description="d", inputSchema={"type": "object", "properties": {}, "required": []})
        ]
        out = cluster_scope.with_cluster_param(tools)
        self.assertIn("cluster", out[0].inputSchema["properties"])
        self.assertNotIn("cluster", out[0].inputSchema.get("required", []))
        self.assertNotIn("cluster", tools[0].inputSchema["properties"])  # original untouched

    def test_scopes_are_isolated_between_threads(self):
        import threading

        seen = {}

        def worker(name):
            with cluster_scope.activate({"cluster": name}):
                seen[name] = cluster_scope.current().name

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("center", "edge-88")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(seen, {"center": "vc-xnet-management", "edge-88": "vc-942nkkozc8o5fyb9q3bznb1ws"})


if __name__ == "__main__":
    unittest.main()
