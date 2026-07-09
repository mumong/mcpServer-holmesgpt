"""Foundational grouped tests for AIOps observability source collectors (T016).

These are pure unit tests: every external boundary (kubectl subprocess,
Prometheus/Elasticsearch/DeepFlow HTTP) is mocked, so the suite runs with no
cluster, no data-project dependency, and asserts the SourceResult contract:
coverage states (present/empty/absent/error), honest error handling without
stack traces, and key evidence invariants.
"""

import json
import unittest
from unittest.mock import patch

from servers.aiops_observability.collectors import (
    deepflow,
    elasticsearch,
    kubernetes,
    prometheus,
)
from servers.aiops_observability.topology import build_topology


class _Resp:
    def __init__(self, payload, raise_exc=None):
        self._payload = payload
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._payload


class KubernetesCollectorTests(unittest.TestCase):
    def test_pod_not_found_returns_error_coverage_without_raising(self):
        with patch.object(kubernetes, "_run_kubectl", return_value=(False, "NotFound")):
            result = kubernetes.collect_kubernetes("aiops-temp", "missing-pod")
        self.assertEqual(result.coverage, "error")
        self.assertEqual(result.dimension, "k8s")
        self.assertIn("NotFound", result.error or "")

    def test_success_extracts_last_terminated_and_prefers_uid(self):
        pod_json = {
            "metadata": {"name": "oom-pod", "namespace": "aiops-temp", "uid": "uid-123", "labels": {"app": "x"}},
            "spec": {"nodeName": "node-1", "containers": [{"name": "app"}]},
            "status": {
                "phase": "Running",
                "podIP": "10.0.0.5",
                "containerStatuses": [
                    {
                        "name": "app",
                        "restartCount": 3,
                        "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "t"}},
                    }
                ],
            },
        }

        def fake_run(args, timeout=30):
            return True, "ok-output"

        with patch.object(kubernetes, "get_pod", return_value=(pod_json, None)), patch.object(
            kubernetes, "_run_kubectl", side_effect=fake_run
        ):
            result = kubernetes.collect_kubernetes("aiops-temp", "oom-pod")

        self.assertEqual(result.coverage, "present")
        self.assertEqual(result.records[0]["uid"], "uid-123")
        summaries = [e.summary for e in result.evidence]
        self.assertTrue(any("Last terminated" in s and "OOMKilled" in s for s in summaries))


class PrometheusCollectorTests(unittest.TestCase):
    def test_absent_when_url_missing(self):
        result = prometheus.collect_prometheus(
            prometheus_url="", namespace="ns", pod="p", pod_uid=None, timeout=5
        )
        self.assertEqual(result.coverage, "absent")
        self.assertEqual(result.dimension, "metrics")

    def test_present_when_series_returned(self):
        payload = {"status": "success", "data": {"result": [{"metric": {}, "value": [0, "1"]}]}}
        with patch.object(prometheus, "_query", return_value=payload):
            result = prometheus.collect_prometheus(
                prometheus_url="http://prom:9090", namespace="ns", pod="p", pod_uid="uid", timeout=5
            )
        self.assertEqual(result.coverage, "present")
        self.assertTrue(result.evidence)

    def test_empty_when_success_but_no_series(self):
        payload = {"status": "success", "data": {"result": []}}
        with patch.object(prometheus, "_query", return_value=payload):
            result = prometheus.collect_prometheus(
                prometheus_url="http://prom:9090", namespace="ns", pod="p", pod_uid=None, timeout=5
            )
        self.assertEqual(result.coverage, "empty")

    def test_error_when_all_queries_fail(self):
        payload = {"status": "error", "error": "boom"}
        with patch.object(prometheus, "_query", return_value=payload):
            result = prometheus.collect_prometheus(
                prometheus_url="http://prom:9090", namespace="ns", pod="p", pod_uid=None, timeout=5
            )
        self.assertEqual(result.coverage, "error")
        self.assertIn("boom", result.error or "")


class ElasticsearchCollectorTests(unittest.TestCase):
    def test_absent_when_url_missing_and_exec_disabled(self):
        import os
        with patch.dict(os.environ, {"AIOPS_ES_EXEC_ENABLED": "false", "ELASTICSEARCH_URL": "", "ES_URL": ""}):
            result = elasticsearch.collect_elasticsearch(
                elasticsearch_url="", index="filebeat-*", namespace="ns", pod="p",
                pod_uid=None, start="s", end="e", timeout=5, max_records=10,
            )
        self.assertEqual(result.coverage, "absent")

    def test_exec_mode_auto_fetches_creds_and_returns_present(self):
        import os
        hits = {"hits": {"hits": [{"_source": {"@timestamp": "t", "message": "oom growth"}}]}}
        with patch.dict(os.environ, {"AIOPS_ES_EXEC_ENABLED": "true", "ELASTICSEARCH_URL": "", "ES_URL": ""}), \
             patch.object(elasticsearch._kube, "secret_value", side_effect=["elastic", "pw"]), \
             patch.object(elasticsearch._kube, "exec_in_pod", return_value=(True, json.dumps(hits), "")):
            result = elasticsearch.collect_elasticsearch(
                elasticsearch_url="", index="filebeat-*", namespace="aiops-temp", pod="oom-pod",
                pod_uid="uid", start="s", end="e", timeout=5, max_records=10,
            )
        self.assertEqual(result.coverage, "present")
        self.assertIn("kubectl exec", result.evidence[0].summary)

    def test_query_requires_pod_identity_not_namespace_only(self):
        # Regression: a namespace-only match must NOT return other pods' logs.
        body = elasticsearch._query_body("monitor", "vma-node-exporter-c8qtj", "uid-1", "s", "e", 50)
        q = body["query"]["bool"]
        must_blob = json.dumps(q["must"])
        # pod identity is a required (must) clause
        self.assertIn("vma-node-exporter-c8qtj", must_blob)
        self.assertIn("kubernetes.pod.name", must_blob)
        # namespace lives only in filter, never as a standalone should at top level
        self.assertNotIn("namespace", json.dumps(q.get("should", [])))
        self.assertIn("namespace", json.dumps(q.get("filter", [])))

    def test_exec_mode_absent_when_no_credentials(self):
        import os
        with patch.dict(os.environ, {"AIOPS_ES_EXEC_ENABLED": "true", "ELASTICSEARCH_URL": "", "ES_URL": ""}), \
             patch.object(elasticsearch._kube, "secret_value", return_value=None):
            result = elasticsearch.collect_elasticsearch(
                elasticsearch_url="", index="filebeat-*", namespace="ns", pod="p",
                pod_uid=None, start="s", end="e", timeout=5, max_records=10,
            )
        self.assertEqual(result.coverage, "absent")
        self.assertIn("凭证", result.error or "")

    def test_present_and_empty_paths(self):
        hits_payload = {"hits": {"hits": [{"_source": {"@timestamp": "t", "message": "oom"}}]}}
        with patch.object(elasticsearch.requests, "post", return_value=_Resp(hits_payload)):
            present = elasticsearch.collect_elasticsearch(
                elasticsearch_url="http://es:9200", index="filebeat-*", namespace="ns", pod="p",
                pod_uid="uid", start="s", end="e", timeout=5, max_records=10,
            )
        self.assertEqual(present.coverage, "present")
        self.assertTrue(present.evidence)

        with patch.object(elasticsearch.requests, "post", return_value=_Resp({"hits": {"hits": []}})):
            empty = elasticsearch.collect_elasticsearch(
                elasticsearch_url="http://es:9200", index="filebeat-*", namespace="ns", pod="p",
                pod_uid=None, start="s", end="e", timeout=5, max_records=10,
            )
        self.assertEqual(empty.coverage, "empty")

    def test_error_when_request_raises(self):
        with patch.object(elasticsearch.requests, "post", side_effect=RuntimeError("conn refused")):
            result = elasticsearch.collect_elasticsearch(
                elasticsearch_url="http://es:9200", index="filebeat-*", namespace="ns", pod="p",
                pod_uid=None, start="s", end="e", timeout=5, max_records=10,
            )
        self.assertEqual(result.coverage, "error")
        self.assertIn("conn refused", result.error or "")


class DeepflowCollectorTests(unittest.TestCase):
    def test_absent_when_url_missing_and_exec_disabled(self):
        import os
        with patch.dict(os.environ, {"AIOPS_CLICKHOUSE_EXEC_ENABLED": "false", "DEEPFLOW_CLICKHOUSE_URL": "", "CLICKHOUSE_URL": ""}):
            absent = deepflow.collect_deepflow(
                clickhouse_url="", clickhouse_database="flow_log", namespace="ns", pod="p",
                pod_ip="10.0.0.1", node="n", start="2026-07-08T05:00:00Z", end="2026-07-08T06:00:00Z",
                timeout=5, max_records=10,
            )
        self.assertEqual(absent.coverage, "absent")

    def test_empty_without_pod_ip(self):
        empty = deepflow.collect_deepflow(
            clickhouse_url="http://ch:8123", clickhouse_database="flow_log", namespace="ns", pod="p",
            pod_ip=None, node="n", start="2026-07-08T05:00:00Z", end="2026-07-08T06:00:00Z",
            timeout=5, max_records=10,
        )
        self.assertEqual(empty.coverage, "empty")

    def test_exec_mode_queries_clickhouse_and_returns_present(self):
        import os
        rows = {"data": [{"trace_id": "abc", "l7_protocol_str": "HTTP", "ip4_0": "10.0.0.10"}]}
        with patch.dict(os.environ, {"AIOPS_CLICKHOUSE_EXEC_ENABLED": "true", "DEEPFLOW_CLICKHOUSE_URL": "", "CLICKHOUSE_URL": ""}), \
             patch.object(deepflow._kube, "exec_in_pod", return_value=(True, json.dumps(rows), "")):
            result = deepflow.collect_deepflow(
                clickhouse_url="", clickhouse_database="flow_log", namespace="aiops-temp", pod="pod",
                pod_ip="10.0.0.10", node="master", start="2026-07-08T05:00:00Z", end="2026-07-08T06:00:00Z",
                timeout=5, max_records=10,
            )
        self.assertEqual(result.coverage, "present")
        self.assertEqual(result.evidence[0].confidence, "medium")  # weak, never high
        self.assertIn("kubectl exec", result.evidence[0].summary)

    def test_present_marks_weak_pod_ip_trace_correlation(self):
        payload = {"data": [{"trace_id": "abc", "l7_protocol": "HTTP"}]}
        with patch.object(deepflow.requests, "post", return_value=_Resp(payload)):
            result = deepflow.collect_deepflow(
                clickhouse_url="http://ch:8123", clickhouse_database="flow_log", namespace="ns", pod="p",
                pod_ip="10.0.0.10", node="n", start="2026-07-08T05:00:00Z", end="2026-07-08T06:00:00Z",
                timeout=5, max_records=10,
            )
        self.assertEqual(result.coverage, "present")
        self.assertEqual(result.evidence[0].confidence, "medium")  # pod-ip flow is weak, never high
        self.assertEqual(result.evidence[0].trace_correlation["pod_ip"], "10.0.0.10")

    def test_clickhouse_datetime_and_identifier_safety(self):
        self.assertEqual(deepflow._clickhouse_datetime("2026-07-08T05:00:00.123456Z"), "2026-07-08 05:00:00")
        self.assertEqual(deepflow._safe_identifier("flow_log"), "flow_log")
        self.assertEqual(deepflow._safe_identifier("bad; DROP"), "flow_log")  # injection-ish → safe default


class TopologyTests(unittest.TestCase):
    def test_builds_owner_service_and_peer_topology(self):
        primary = {"namespace": "aiops-temp", "name": "web-1", "uid": "u1", "pod_ip": "10.0.0.5", "node": "node1"}
        k8s_record = {
            "containers": ["app"],
            "owner_chain": [{"kind": "ReplicaSet", "name": "web-abc"}, {"kind": "Deployment", "name": "web"}],
            "services": [{"name": "web-svc", "selector": {"app": "web"}, "cluster_ip": "10.96.0.1", "ports": [{"port": 80}]}],
        }
        entities, edges, summary = build_topology(
            primary=primary,
            k8s_record=k8s_record,
            evidence_refs_by_dimension={"k8s": ["k8s-ref"], "tracing": ["tr-ref"]},
            deepflow_coverage="present",
            deepflow_peers=["10.0.0.9", "10.0.0.5", "10.0.0.9"],  # self ip + dup must be skipped
        )
        relations = {e["relation"] for e in edges}
        self.assertIn("owned_by", relations)   # Pod -> ReplicaSet -> Deployment
        self.assertIn("selects", relations)    # Service -> Pod
        self.assertIn("communicates_with", relations)  # peer IP -> Pod (weak)

        self.assertEqual(sum(1 for e in edges if e["relation"] == "owned_by"), 2)

        svc_edges = [e for e in edges if e["relation"] == "selects"]
        self.assertEqual(svc_edges[0]["source"], "service:aiops-temp/web-svc")
        self.assertEqual(svc_edges[0]["target"], "pod:aiops-temp/web-1")

        peer_edges = [e for e in edges if e["relation"] == "communicates_with"]
        self.assertEqual(len(peer_edges), 1)               # self ip + dup excluded
        self.assertEqual(peer_edges[0]["confidence"], "weak")
        self.assertEqual(peer_edges[0]["directness"], "related_context")

        self.assertIn("Service", summary["entity_kinds"])
        self.assertIn("Deployment", summary["entity_kinds"])
        self.assertIn("owned_by", summary["relations"])


if __name__ == "__main__":
    unittest.main()
