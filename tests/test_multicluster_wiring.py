"""Cluster scope reaches every backend call site (specs/008-multi-cluster P1)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "servers"))
sys.path.insert(0, str(ROOT))

import cluster_scope  # noqa: E402

EDGE = "vc-942nkkozc8o5fyb9q3bznb1ws"
MULTI = """
clusters:
  center: vc-xnet-management
  items:
    - {name: vc-xnet-management, aliases: [center], access: local, observability: victoria}
    - {name: vc-942nkkozc8o5fyb9q3bznb1ws, aliases: [edge-88], access: xnet, observability: victoria}
  access:
    xnet: {proxy_url: "https://x/proxy", runner_url: "http://r:8090"}
observability_profiles:
  victoria:
    metrics: {type: victoriametrics, url: "http://vm:8481/select/0/prometheus", cluster_label: cluster}
    logs: {type: victorialogs_events, url: "http://vl:9428", cluster_field: cluster}
    traces: {type: none}
"""

EVENT_LINES = [
    {
        "_time": "2026-10-21T08:00:02Z",
        "cluster": EDGE,
        "_msg": "event name: web-1.18e1,count: 7,involvedObject_namespace: shop,involvedObject_kind: Pod,"
        "involvedObject_name: web-1,reasoni: BackOff,type: Warning,"
        "message: Back-off restarting failed container web in pod web-1_shop(uid), retry",
    },
    {
        "_time": "2026-10-21T08:00:01Z",
        "cluster": EDGE,
        "_msg": "event name: web-1-x.18e2,count: 1,involvedObject_namespace: shop,involvedObject_kind: Pod,"
        "involvedObject_name: web-1-x,reasoni: Pulled,type: Normal,message: other pod with a longer name",
    },
    {
        "_time": "2026-10-21T07:59:00Z",
        "cluster": EDGE,
        "_msg": "event name: web-1.18e0,count: 1,involvedObject_namespace: shop,involvedObject_kind: Pod,"
        "involvedObject_name: web-1,reasoni: Scheduled,type: Normal,message: Successfully assigned shop/web-1 to n1",
    },
]


class Multi(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        tmp.write(MULTI)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        p = mock.patch.dict(os.environ, {"AIOPS_CLUSTERS_CONFIG": tmp.name})
        p.start()
        self.addCleanup(p.stop)
        cluster_scope.reset()
        self.addCleanup(cluster_scope.reset)


class MetricsWiringTest(Multi):
    def test_observability_promql_goes_to_vm_with_extra_label(self):
        from servers.aiops_observability.query import metrics

        response = mock.Mock(status_code=200)
        response.json.return_value = {"status": "success", "data": {"resultType": "vector", "result": []}}
        with mock.patch.object(metrics._http.SESSION, "get", return_value=response) as get:
            with cluster_scope.activate({"cluster": "edge-88"}):
                metrics._request_prometheus("api/v1/query", {"query": "up"})
        url = get.call_args.args[0]
        params = get.call_args.kwargs["params"]
        self.assertEqual(url, "http://vm:8481/select/0/prometheus/api/v1/query")
        self.assertEqual(params["extra_label"], f"cluster={EDGE}")
        self.assertEqual(params["query"], "up")

    def test_prometheus_tool_goes_to_vm_with_extra_label(self):
        from holmes_tools import prometheus

        response = mock.Mock(status_code=200, text="{}")
        with mock.patch.object(prometheus._SESSION, "get", return_value=response) as get:
            with cluster_scope.activate({"cluster": "center"}):
                prometheus._do_get("api/v1/labels", {"match[]": "up"})
        self.assertTrue(get.call_args.args[0].startswith("http://vm:8481/select/0/prometheus/"))
        self.assertEqual(get.call_args.kwargs["params"]["extra_label"], "cluster=vc-xnet-management")


class KubectlWiringTest(Multi):
    def _run_capture(self, func):
        completed = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run:
            with cluster_scope.activate({"cluster": "edge-88"}):
                func()
        return run.call_args

    def test_observability_kubectl_targets_cluster(self):
        from servers.aiops_observability.collectors import _kube

        call = self._run_capture(lambda: _kube.run_kubectl(["get", "pods"]))
        self.assertEqual(call.kwargs["env"]["AIOPS_CLUSTER"], EDGE)

    def test_collectors_kubernetes_targets_cluster(self):
        from servers.aiops_observability.collectors import kubernetes

        call = self._run_capture(lambda: kubernetes._run_kubectl(["get", "pods"]))
        self.assertEqual(call.kwargs["env"]["AIOPS_CLUSTER"], EDGE)

    def test_command_runner_targets_cluster(self):
        from holmes_tools import _command_runner

        call = self._run_capture(lambda: _command_runner.run_command("kubectl get pods", {}))
        self.assertEqual(call.kwargs["env"]["AIOPS_CLUSTER"], EDGE)

    def test_local_cluster_inherits_environment(self):
        from holmes_tools import _command_runner

        completed = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run:
            with cluster_scope.activate({"cluster": "center"}):
                _command_runner.run_command("kubectl get pods", {})
        self.assertIsNone(run.call_args.kwargs.get("env"))

    def test_lifecycle_drops_in_cluster_kubeconfig_for_remote(self):
        from holmes_tools import pod_lifecycle

        with cluster_scope.activate({"cluster": "edge-88"}):
            with pod_lifecycle._kubectl_config() as flags:
                self.assertEqual(flags, [])


class ObservabilityToolTest(Multi):
    def call(self, name, arguments):
        from holmes_tools import aiops_observability_query as q

        return json.loads(q.call_tool(name, arguments))

    def test_missing_cluster_is_reported(self):
        out = self.call("query_pod_topology", {"namespace": "a", "pod": "b", "purpose": "p"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "cluster_required")

    def test_tracing_is_capability_missing_on_victoria(self):
        out = self.call(
            "query_pod_tracing", {"cluster": "edge-88", "namespace": "a", "pod": "b", "purpose": "p"}
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "capability_missing")
        self.assertEqual(out["cluster"], EDGE)
        self.assertEqual(out["backend"], "none")

    def test_logs_return_pod_events_on_victoria(self):
        from servers.aiops_observability.query import logs

        entity = {
            "namespace": "shop",
            "pod": "web-1",
            "pod_uid": "u1",
            "containers": ["web"],
            "creation_timestamp": "2026-10-21T07:00:00Z",
        }
        with (
            mock.patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            mock.patch(
                "servers.aiops_observability.collectors.victorialogs_events._fetch",
                return_value=(EVENT_LINES, None),
            ) as fetch,
        ):
            out = self.call(
                "query_pod_logs",
                {"cluster": "edge-88", "namespace": "shop", "pod": "web-1", "purpose": "why restarting"},
            )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["log_source"], "k8s_events")
        self.assertEqual(out["coverage"], "partial")
        self.assertEqual(out["backend"], "victorialogs_events")
        self.assertEqual([s["reason"] for s in out["samples"]], ["BackOff", "Scheduled"])  # web-1-x filtered
        self.assertEqual(out["samples"][0]["type"], "Warning")
        self.assertEqual(out["samples"][0]["count"], 7)
        self.assertTrue(any("容器日志" in note or "container logs" in note for note in out["limitations"]))
        query = fetch.call_args.args[1]
        self.assertIn(f'cluster:="{EDGE}"', query)
        self.assertIn('"involvedObject_name: web-1"', query)


class AdapterTest(Multi):
    def test_text_tools_activate_scope_and_report_errors(self):
        from holmes_tools.server_adapter import call_text_tool

        seen = {}

        def handler(name, params):
            seen["cluster"] = cluster_scope.current().name
            seen["params"] = dict(params)
            return "ok"

        noop = lambda *a, **k: None  # noqa: E731
        out = call_text_tool(
            "s", "kubectl_get", {"cluster": "edge-88", "kind": "pod"}, handler, log_call=noop, log_result=noop
        )
        self.assertEqual(out[0].text, "ok")
        self.assertEqual(seen, {"cluster": EDGE, "params": {"kind": "pod"}})
        out = call_text_tool("s", "kubectl_get", {"kind": "pod"}, handler, log_call=noop, log_result=noop)
        self.assertIn("cluster_required", out[0].text)


class ParsingTest(unittest.TestCase):
    def test_event_message_parsing(self):
        from servers.aiops_observability.collectors.victorialogs_events import parse_event

        event = parse_event(EVENT_LINES[0]["_msg"])
        self.assertEqual(event["involved_name"], "web-1")
        self.assertEqual(event["reason"], "BackOff")
        self.assertEqual(event["type"], "Warning")
        self.assertEqual(event["count"], 7)
        self.assertTrue(
            event["message"].startswith("Back-off restarting failed container web in pod web-1_shop")
        )
        self.assertTrue(event["message"].endswith(", retry"))  # commas inside the message are kept

    def test_non_event_lines_are_ignored(self):
        from servers.aiops_observability.collectors.victorialogs_events import parse_event

        self.assertIsNone(parse_event("event vmstorage.18e0 has been removed from Prometheus"))


if __name__ == "__main__":
    unittest.main()


class RemoteBashRulesTest(unittest.TestCase):
    def test_remote_shell_allows_only_kubectl_pipelines(self):
        from cluster_scope import remote_shell_error

        self.assertIsNone(remote_shell_error("kubectl get pods -A"))
        self.assertIsNone(remote_shell_error("kubectl get pods -o json | jq '.items|length' | head -5"))
        for bad in (
            "cat /etc/hosts",
            "kubectl get pods; rm -rf /",
            "kubectl get pods && ls",
            "kubectl get pods > /tmp/x",
            "echo $(kubectl get pods)",
            "kubectl get pods | sh",
            "ls | kubectl apply -f -",
        ):
            with self.subTest(command=bad):
                self.assertIsNotNone(remote_shell_error(bad))


class ListClustersTest(Multi):
    def test_listing_reports_aliases_backends_and_status(self):
        with mock.patch.object(cluster_scope, "_probe", side_effect=lambda c: (True, "") if c.remote else (True, "")):
            data = json.loads(cluster_scope.list_clusters_text())
        names = {c["name"]: c for c in data["clusters"]}
        self.assertEqual(names[EDGE]["aliases"], ["edge-88"])
        self.assertEqual(names[EDGE]["access"], "xnet")
        self.assertEqual(names[EDGE]["capabilities"]["traces"], False)
        self.assertEqual(names[EDGE]["capabilities"]["logs"], "k8s_events")
        self.assertTrue(names[EDGE]["online"])
        self.assertEqual(data["center"], "vc-xnet-management")

    def test_offline_cluster_reported_with_reason(self):
        with mock.patch.object(
            cluster_scope, "_probe", side_effect=lambda c: (False, "cluster_unreachable") if c.remote else (True, "")
        ):
            data = json.loads(cluster_scope.list_clusters_text())
        edge = next(c for c in data["clusters"] if c["name"] == EDGE)
        self.assertFalse(edge["online"])
        self.assertEqual(edge["reason"], "cluster_unreachable")
