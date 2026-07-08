"""Foundational grouped tests for AIOps observability source collectors (T016).

These are pure unit tests: every external boundary (kubectl subprocess,
Prometheus/Elasticsearch/DeepFlow HTTP) is mocked, so the suite runs with no
cluster, no data-project dependency, and asserts the SourceResult contract:
coverage states (present/empty/absent/error), honest error handling without
stack traces, and key evidence invariants.
"""

import unittest
from unittest.mock import patch

from servers.aiops_observability.collectors import (
    deepflow,
    elasticsearch,
    kubernetes,
    prometheus,
)


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
    def test_absent_when_url_missing(self):
        result = elasticsearch.collect_elasticsearch(
            elasticsearch_url="", index="filebeat-*", namespace="ns", pod="p",
            pod_uid=None, start="s", end="e", timeout=5, max_records=10,
        )
        self.assertEqual(result.coverage, "absent")

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
    def test_absent_without_url_and_empty_without_pod_ip(self):
        absent = deepflow.collect_deepflow(
            clickhouse_url="", clickhouse_database="flow_log", namespace="ns", pod="p",
            pod_ip="10.0.0.1", node="n", start="2026-07-08T05:00:00Z", end="2026-07-08T06:00:00Z",
            timeout=5, max_records=10,
        )
        self.assertEqual(absent.coverage, "absent")

        empty = deepflow.collect_deepflow(
            clickhouse_url="http://ch:8123", clickhouse_database="flow_log", namespace="ns", pod="p",
            pod_ip=None, node="n", start="2026-07-08T05:00:00Z", end="2026-07-08T06:00:00Z",
            timeout=5, max_records=10,
        )
        self.assertEqual(empty.coverage, "empty")

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


if __name__ == "__main__":
    unittest.main()
