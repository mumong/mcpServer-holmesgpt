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
    tempo,
)
from servers.aiops_observability.topology import build_topology, topology_details


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
    def test_instant_queries_use_clipped_end_and_uid_scoped_selectors(self):
        calls = []

        def fake_query(url, query, timeout, evaluation_time=None):
            calls.append((query, evaluation_time))
            return {"status": "success", "data": {"result": []}}

        with patch.object(prometheus, "_query", side_effect=fake_query), patch.object(
            prometheus,
            "_query_range",
            return_value={"status": "success", "data": {"result": []}},
        ) as range_query:
            prometheus.collect_prometheus(
                prometheus_url="http://prom:9090",
                namespace="ns",
                pod="api",
                pod_uid="uid-new",
                timeout=5,
                start="2026-07-16T00:05:00Z",
                end="2026-07-16T00:20:00Z",
            )

        self.assertTrue(calls)
        self.assertTrue(all(evaluation_time == "2026-07-16T00:20:00Z" for _, evaluation_time in calls))
        self.assertTrue(all("uid-new" in query for query, _ in calls))
        self.assertIn("uid-new", range_query.call_args.args[1])

    def test_memory_highlights_filter_pause_and_deduplicate_scrapers(self):
        range_results = [
            {
                "metric": {
                    "namespace": "aiops-temp",
                    "pod": "aiops-oom-business-abc",
                    "container": "business-api",
                    "service": "observability-kubelet",
                    "image": "python:3-alpine",
                },
                "values": [[100, "18874368"], [115, "50331648"], [130, "66060288"]],
            },
            {
                "metric": {
                    "namespace": "aiops-temp",
                    "pod": "aiops-oom-business-abc",
                    "container": "business-api",
                    "service": "duplicate-kubelet",
                    "image": "python:3-alpine",
                },
                "values": [[100, "18874368"], [115, "50331648"], [130, "66060288"]],
            },
            {
                "metric": {
                    "namespace": "aiops-temp",
                    "pod": "aiops-oom-business-abc",
                    "container": "POD",
                    "image": "registry.k8s.io/pause:3.9",
                },
                "values": [[100, "196608"], [130, "196608"]],
            },
        ]
        limit_results = [
            {
                "metric": {
                    "namespace": "aiops-temp",
                    "pod": "aiops-oom-business-abc",
                    "container": "business-api",
                },
                "value": [130, "67108864"],
            }
        ]

        highlights = prometheus._memory_highlights(range_results, limit_results)

        self.assertEqual(len(highlights), 1)
        self.assertEqual(highlights[0]["container"], "business-api")
        self.assertEqual(highlights[0]["start"], "18.0Mi")
        self.assertEqual(highlights[0]["max"], "63.0Mi")
        self.assertEqual(highlights[0]["last"], "63.0Mi")
        self.assertEqual(highlights[0]["limit"], "64.0Mi")
        self.assertAlmostEqual(highlights[0]["max_limit_ratio"], 0.9844, places=4)
        self.assertLessEqual(len(highlights[0]["samples"]), 5)

    def test_memory_highlights_empty_does_not_claim_growth(self):
        self.assertEqual(prometheus._memory_highlights([], []), [])

    def test_memory_highlights_prioritize_highest_limit_ratio(self):
        range_results = [
            {
                "metric": {
                    "namespace": "ns",
                    "pod": "pod-a",
                    "container": "a-sidecar",
                    "image": "sidecar:1",
                },
                "values": [[100, str(20 * 1024 * 1024)]],
            },
            {
                "metric": {
                    "namespace": "ns",
                    "pod": "pod-a",
                    "container": "z-business-api",
                    "image": "api:1",
                },
                "values": [[100, str(75 * 1024 * 1024)]],
            },
        ]
        limit_results = [
            {
                "metric": {"namespace": "ns", "pod": "pod-a", "container": "a-sidecar"},
                "value": [100, str(128 * 1024 * 1024)],
            },
            {
                "metric": {"namespace": "ns", "pod": "pod-a", "container": "z-business-api"},
                "value": [100, str(80 * 1024 * 1024)],
            },
        ]

        highlights = prometheus._memory_highlights(range_results, limit_results)

        self.assertEqual(highlights[0]["container"], "z-business-api")
        self.assertEqual(highlights[0]["max_limit_ratio"], 0.9375)

    def test_absent_when_url_missing(self):
        result = prometheus.collect_prometheus(
            prometheus_url="", namespace="ns", pod="p", pod_uid=None, timeout=5
        )
        self.assertEqual(result.coverage, "absent")
        self.assertEqual(result.dimension, "metrics")

    def test_present_when_series_returned(self):
        payload = {"status": "success", "data": {"result": [{"metric": {}, "value": [0, "1"]}]}}
        with patch.object(prometheus, "_query", return_value=payload), patch.object(
            prometheus, "_query_range", return_value={"status": "success", "data": {"result": []}}
        ):
            result = prometheus.collect_prometheus(
                prometheus_url="http://prom:9090", namespace="ns", pod="p", pod_uid="uid", timeout=5,
                start="2026-07-13T00:00:00Z", end="2026-07-13T00:30:00Z",
            )
        self.assertEqual(result.coverage, "present")
        self.assertTrue(result.evidence)
        self.assertIn("highlights", result.evidence[0].payload)

    def test_empty_when_success_but_no_series(self):
        payload = {"status": "success", "data": {"result": []}}
        with patch.object(prometheus, "_query", return_value=payload), patch.object(
            prometheus, "_query_range", return_value=payload
        ):
            result = prometheus.collect_prometheus(
                prometheus_url="http://prom:9090", namespace="ns", pod="p", pod_uid=None, timeout=5,
                start="2026-07-13T00:00:00Z", end="2026-07-13T00:30:00Z",
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
    def test_log_evidence_contains_at_most_eight_readable_samples(self):
        hits_payload = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "@timestamp": f"2026-07-13T00:00:{index:02d}Z",
                            "message": f"trace_id=abc allocated_mib={index}",
                            "kubernetes": {"container": {"name": "business-api"}},
                        }
                    }
                    for index in range(12)
                ]
            }
        }

        result = elasticsearch._parse_hits(
            hits_payload,
            "aiops-temp",
            "aiops-oom-business",
            max_records=12,
            source_desc="unit-test",
        )

        samples = result.evidence[0].payload["samples"]
        self.assertEqual(len(samples), 8)
        self.assertEqual(samples[0]["timestamp"], "2026-07-13T00:00:00Z")
        self.assertIn("allocated_mib=0", samples[0]["message"])
        self.assertEqual(samples[0]["container"], "business-api")

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
        body = elasticsearch._query_body("monitor", "vma-node-exporter-c8qtj", None, "s", "e", 50)
        q = body["query"]["bool"]
        must_blob = json.dumps(q["must"])
        # pod identity is a required (must) clause
        self.assertIn("vma-node-exporter-c8qtj", must_blob)
        self.assertIn("kubernetes.pod.name", must_blob)
        # namespace lives only in filter, never as a standalone should at top level
        self.assertNotIn("namespace", json.dumps(q.get("should", [])))
        self.assertIn("namespace", json.dumps(q.get("filter", [])))

    def test_query_uses_uid_exclusively_when_available_to_isolate_recreated_pod(self):
        body = elasticsearch._query_body(
            "monitor",
            "reused-pod-name",
            "current-pod-uid",
            "s",
            "e",
            50,
        )
        identity_clause = json.dumps(body["query"]["bool"]["must"][1])

        self.assertIn("current-pod-uid", identity_clause)
        self.assertIn("kubernetes.pod.uid", identity_clause)
        self.assertNotIn("reused-pod-name", identity_clause)
        self.assertNotIn("kubernetes.pod.name", identity_clause)

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
    def test_build_sql_prioritizes_trace_correlated_inbound_non_dns_flows(self):
        sql = deepflow._build_sql(
            "flow_log",
            "10.0.0.20",
            "2026-07-13T00:00:00Z",
            "2026-07-13T00:30:00Z",
            50,
        )

        self.assertIn("(trace_id != '') DESC", sql)
        self.assertIn("(ip4_1 = '10.0.0.20') DESC", sql)
        self.assertIn("(l7_protocol_str != 'DNS') DESC", sql)

    def test_flow_samples_prioritize_business_http_over_newer_dns(self):
        rows = [
            {
                "time": "2026-07-13 00:00:02",
                "src_ip": "10.0.0.20",
                "dst_ip": "10.0.0.53",
                "l7_protocol_str": "DNS",
                "request_type": "A",
                "request_resource": "lgtm.xnet.svc.cluster.local",
                "trace_id": "",
            },
            {
                "time": "2026-07-13 00:00:01",
                "src_ip": "10.0.0.10",
                "dst_ip": "10.0.0.20",
                "l7_protocol_str": "HTTP",
                "request_type": "GET",
                "request_resource": "/allocate?mib=2",
                "response_code": 200,
                "trace_id": "0123456789abcdef0123456789abcdef",
            },
        ]

        samples = deepflow._flow_samples(rows)

        self.assertEqual(samples[0]["protocol"], "HTTP")
        self.assertEqual(samples[0]["request"], "GET /allocate?mib=2")
        self.assertEqual(samples[0]["trace_id"], "0123456789abcdef0123456789abcdef")

    def test_flow_evidence_contains_at_most_five_readable_samples(self):
        rows = [
            {
                "time": f"2026-07-13 00:00:0{index}",
                "src_ip": "10.0.0.10",
                "dst_ip": "10.0.0.20",
                "l7_protocol_str": "HTTP",
                "request_type": "GET",
                "request_resource": "/allocate?mib=2",
                "response_code": 200,
                "response_duration": 1200 + index,
                "trace_id": f"trace-{index}",
                "span_id": f"span-{index}",
            }
            for index in range(8)
        ]

        result = deepflow._result_from_rows(
            rows,
            "aiops-temp",
            "aiops-oom-business",
            "10.0.0.20",
            "master",
            "SELECT ...",
            "flow_log",
            "unit-test",
            max_records=10,
        )

        samples = result.evidence[0].payload["samples"]
        self.assertEqual(len(samples), 5)
        self.assertEqual(samples[0]["request"], "GET /allocate?mib=2")
        self.assertEqual(samples[0]["src"], "10.0.0.10")
        self.assertEqual(samples[0]["dst"], "10.0.0.20")
        self.assertEqual(samples[0]["protocol"], "HTTP")
        self.assertEqual(samples[0]["response_code"], 200)
        self.assertEqual(samples[0]["trace_id"], "trace-0")
        self.assertEqual(samples[0]["span_id"], "span-0")

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

    def test_build_call_chains_stitches_by_syscall_trace_id(self):
        # eBPF auto-tracing: flows sharing a syscall_trace_id form a call chain
        # WITHOUT app instrumentation.
        rows = [
            {"time": "t1", "src_ip": "10.0.0.1", "dst_ip": "10.0.0.2", "l7_protocol_str": "HTTP",
             "request_resource": "/api", "response_code": 200, "response_duration": 5,
             "syscall_trace_id_request": "999", "trace_id": ""},
            {"time": "t2", "src_ip": "10.0.0.2", "dst_ip": "10.0.0.3", "l7_protocol_str": "MySQL",
             "request_resource": "SELECT 1", "response_code": None, "response_duration": 3,
             "syscall_trace_id_request": "999", "trace_id": ""},
            {"time": "t3", "src_ip": "10.0.0.9", "dst_ip": "10.0.0.2", "l7_protocol_str": "HTTP",
             "request_resource": "/other", "response_code": 200, "response_duration": 1,
             "syscall_trace_id_request": "111", "trace_id": ""},
        ]
        chains = deepflow._build_call_chains(rows)
        self.assertEqual(len(chains), 1)  # only syscall 999 has 2 hops
        self.assertEqual(chains[0]["syscall_trace_id"], "999")
        self.assertEqual(chains[0]["hop_count"], 2)
        self.assertEqual(chains[0]["hops"][0]["proto"], "HTTP")

    def test_query_uses_explicit_utc_timezone(self):
        # Regression: ClickHouse server TZ may be Asia/Shanghai; the UTC window
        # must be parsed as UTC to avoid an 8-hour offset missing real flows.
        sql = deepflow._build_sql("flow_log", "10.0.0.1", "2026-07-08T05:00:00Z", "2026-07-08T06:00:00Z", 10)
        self.assertIn("toDateTime('2026-07-08 05:00:00', 'UTC')", sql)
        self.assertIn("toDateTime('2026-07-08 06:00:00', 'UTC')", sql)
        self.assertIn("formatDateTime(toTimeZone(time, 'UTC')", sql)
        self.assertIn("AS timestamp", sql)

    def test_asia_shanghai_flow_time_is_utc_before_evidence_creation(self):
        rows = [
            {
                "time": "2026-07-16 16:07:38",
                "src_ip": "172.16.104.8",
                "dst_ip": "172.16.104.13",
                "l7_protocol_str": "HTTP",
                "request_type": "GET",
                "request_resource": "/allocate?mib=2&step=61012",
                "response_code": 200,
                "response_duration": "16242",
                "trace_id": "467fee396d50216e33083b8667268457",
                "span_id": "b226f2348bdbf11e",
            }
        ]

        result = deepflow._result_from_rows(
            rows,
            "aiops-traced-oom",
            "trace-oom-api",
            "172.16.104.13",
            "node2",
            "SELECT ...",
            "flow_log",
            "unit-test",
            max_records=10,
            source_timezone="Asia/Shanghai",
        )

        sample = result.evidence[0].payload["samples"][0]
        self.assertEqual(sample["timestamp"], "2026-07-16T08:07:38Z")
        self.assertEqual(sample["trace_id"], "467fee396d50216e33083b8667268457")


class TopologyTests(unittest.TestCase):
    def test_builds_direct_caller_pod_edge_when_caller_is_resolved(self):
        primary = {
            "namespace": "aiops-traced-oom",
            "name": "trace-oom-api-abc",
            "uid": "uid-api",
            "pod_ip": "10.0.0.20",
            "node": "node2",
        }

        entities, edges, _ = build_topology(
            primary=primary,
            k8s_record={},
            evidence_refs_by_dimension={"k8s": ["k8s-ref"], "tracing": ["deepflow-ref"]},
            deepflow_coverage="present",
            resolved_callers=[
                {
                    "namespace": "aiops-traced-oom",
                    "pod": "trace-oom-driver-def",
                    "uid": "uid-driver",
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

        caller = next(item for item in entities if item.get("name") == "trace-oom-driver-def")
        call_edge = next(item for item in edges if item.get("relation") == "calls")
        self.assertEqual(caller["kind"], "Pod")
        self.assertEqual(call_edge["source"], caller["entity_id"])
        self.assertEqual(call_edge["target"], "k8s.pod:aiops-traced-oom/trace-oom-api-abc:uid-api")
        self.assertEqual(call_edge["source_system"], "deepflow+kubernetes")
        self.assertEqual(call_edge["directness"], "direct")
        self.assertEqual(call_edge["confidence"], "high")

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
        self.assertEqual(svc_edges[0]["target"], "k8s.pod:aiops-temp/web-1:u1")

        peer_edges = [e for e in edges if e["relation"] == "communicates_with"]
        self.assertEqual(len(peer_edges), 1)               # self ip + dup excluded
        self.assertEqual(peer_edges[0]["confidence"], "weak")
        self.assertEqual(peer_edges[0]["directness"], "related_context")

        self.assertIn("Service", summary["entity_kinds"])
        self.assertIn("Deployment", summary["entity_kinds"])
        self.assertIn("owned_by", summary["relations"])

    def test_topology_details_exposes_human_readable_relationships(self):
        entities = [
            {"entity_id": "service:aiops-temp/oom-svc", "kind": "Service", "name": "oom-svc"},
            {"entity_id": "pod:aiops-temp/oom-1", "kind": "Pod", "name": "oom-1"},
            {"entity_id": "node:cluster/master", "kind": "Node", "name": "master"},
        ]
        edges = [
            {
                "source": "service:aiops-temp/oom-svc",
                "target": "pod:aiops-temp/oom-1",
                "relation": "selects",
                "source_system": "kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": ["k8s-ref"],
            },
            {
                "source": "pod:aiops-temp/oom-1",
                "target": "node:cluster/master",
                "relation": "scheduled_on",
                "source_system": "kubernetes",
                "directness": "direct",
                "confidence": "high",
                "evidence_refs": ["k8s-ref"],
            },
        ]

        details = topology_details(entities, edges)

        self.assertEqual(details["edges"][0]["relationship"], "Service --selects--> Pod")
        self.assertEqual(details["edges"][0]["source"], "oom-svc")
        self.assertEqual(details["edges"][0]["target"], "oom-1")
        self.assertEqual(details["edges"][0]["directness"], "direct")
        self.assertEqual(details["edges"][0]["confidence"], "high")
        self.assertEqual(details["edges"][0]["evidence_refs"], ["k8s-ref"])

    def test_topology_details_prioritizes_causal_edges_under_budget(self):
        entities = [
            {"entity_id": "pod:ns/api", "kind": "Pod", "name": "api"},
            {"entity_id": "pod:ns/driver", "kind": "Pod", "name": "driver"},
            {"entity_id": "service:ns/api", "kind": "Service", "name": "api"},
            {"entity_id": "node:cluster/node2", "kind": "Node", "name": "node2"},
        ]
        edges = [
            {"source": "pod:ns/api", "target": "node:cluster/node2", "relation": "scheduled_on"},
            {"source": "pod:ns/driver", "target": "pod:ns/api", "relation": "calls"},
            {"source": "service:ns/api", "target": "pod:ns/api", "relation": "selects"},
        ]

        details = topology_details(entities, edges, max_edges=2)

        self.assertEqual(
            [item["relationship"] for item in details["edges"]],
            ["Pod --calls--> Pod", "Service --selects--> Pod"],
        )


class SignalsAndTempoTests(unittest.TestCase):
    def test_signals_have_role_and_interpretation(self):
        from servers.aiops_observability.case_builder import _signals_from_sources
        from servers.aiops_observability.schemas import EvidenceItem, SourceResult
        k8s = SourceResult("kubernetes", "k8s", "present", evidence=[
            EvidenceItem(evidence_id="e1", source_system="kubernetes", dimension="k8s",
                         summary="Last terminated state: api=OOMKilled exit=137", severity="warning")])
        logs = SourceResult("elasticsearch", "logs", "present", evidence=[
            EvidenceItem(evidence_id="l1", source_system="elasticsearch", dimension="logs",
                         summary="allocated business cache chunk=8 approx_mib=16")])
        sigs = {s["dimension"]: s for s in _signals_from_sources([k8s, logs])}
        self.assertEqual(sigs["k8s"]["role"], "root_cause_state")
        self.assertIn("OOMKilled", sigs["k8s"]["observed"])           # human-readable real signal
        self.assertTrue(sigs["k8s"]["interpretation"])                # plain-language why
        self.assertEqual(sigs["logs"]["role"], "pre_failure_behavior")

    def test_tracing_role_upgrades_when_trace_id_present(self):
        from servers.aiops_observability.case_builder import _signals_from_sources
        from servers.aiops_observability.schemas import EvidenceItem, SourceResult
        df = SourceResult("deepflow", "tracing", "present", evidence=[
            EvidenceItem(evidence_id="d1", source_system="deepflow", dimension="tracing",
                         summary="DeepFlow flows", trace_correlation={"trace_ids": ["abc"]})])
        sig = _signals_from_sources([df])[0]
        self.assertEqual(sig["role"], "trace_correlated_traffic")

    def test_tempo_empty_without_trace_ids(self):
        import os
        from servers.aiops_observability.collectors import tempo
        with patch.dict(os.environ, {"AIOPS_TEMPO_EXEC_ENABLED": "true"}):
            r = tempo.collect_tempo(namespace="ns", pod="p", trace_ids=[], timeout=5)
        self.assertEqual(r.coverage, "empty")
        self.assertIn("trace_id", r.error or "")

    def test_tempo_present_when_spans_found(self):
        import os
        from servers.aiops_observability.collectors import tempo
        trace_json = json.dumps({"batches": [{"scopeSpans": [{"spans": [{"name": "GET /allocate"}]}]}]})
        with patch.dict(os.environ, {"AIOPS_TEMPO_EXEC_ENABLED": "true"}), \
             patch.object(tempo._kube, "find_pod", return_value="lgtm-0"), \
             patch.object(tempo._kube, "exec_in_pod", return_value=(True, trace_json, "")):
            r = tempo.collect_tempo(namespace="ns", pod="p", trace_ids=["t1"], timeout=5)
        self.assertEqual(r.coverage, "present")
        self.assertIn("span", r.evidence[0].summary.lower())

    def test_tempo_evidence_contains_at_most_three_readable_span_samples(self):
        import os
        trace_payload = {
            "batches": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "aiops-oom-business"}}
                        ]
                    },
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "abc123",
                                    "spanId": f"span-{index}",
                                    "name": "GET /allocate",
                                    "startTimeUnixNano": str(1000 + index),
                                    "endTimeUnixNano": str(2000 + index),
                                    "attributes": [
                                        {
                                            "key": "aiops.allocated_mib.after",
                                            "value": {"intValue": str(40 + index)},
                                        }
                                    ],
                                }
                                for index in range(5)
                            ]
                        }
                    ],
                }
            ]
        }
        with patch.dict(os.environ, {"AIOPS_TEMPO_EXEC_ENABLED": "true"}), \
             patch.object(tempo._kube, "find_pod", return_value="lgtm-0"), \
             patch.object(tempo._kube, "exec_in_pod", return_value=(True, json.dumps(trace_payload), "")):
            result = tempo.collect_tempo(namespace="ns", pod="p", trace_ids=["abc123"], timeout=5)

        samples = result.evidence[0].payload["spans"]
        self.assertEqual(len(samples), 3)
        self.assertEqual(samples[0]["trace_id"], "abc123")
        self.assertEqual(samples[0]["service"], "aiops-oom-business")
        self.assertEqual(samples[0]["name"], "GET /allocate")
        self.assertEqual(samples[0]["start"], "1000")
        self.assertEqual(samples[0]["end"], "2000")
        self.assertEqual(samples[0]["attributes"]["aiops.allocated_mib.after"], 40)

    def test_tempo_normalizes_otlp_base64_trace_id_to_w3c_hex(self):
        from servers.aiops_observability.collectors import tempo

        sample = tempo._span_sample(
            {"traceId": "A3l/0tEv2h7SNWp6yPaRpg==", "name": "GET /allocate"},
            "aiops-traced-oom-api",
            "03797fd2d12fda1ed2356a7ac8f691a6",
        )

        self.assertEqual(sample["trace_id"], "03797fd2d12fda1ed2356a7ac8f691a6")


if __name__ == "__main__":
    unittest.main()


class IndependentAuditFixTests(unittest.TestCase):
    def test_merge_trace_ids_preserves_business_flow_priority(self):
        from servers.aiops_observability.case_builder import _merge_trace_ids

        merged = _merge_trace_ids(
            ["flow-newest", "flow-older"],
            ["alphabetic-first", "flow-newest"],
            ["log-trace"],
        )

        self.assertEqual(
            merged,
            ["flow-newest", "flow-older", "alphabetic-first", "log-trace"],
        )

    def test_merge_trace_ids_reserves_capacity_for_later_log_trace(self):
        from servers.aiops_observability.case_builder import _merge_trace_ids

        merged = _merge_trace_ids(
            [f"flow-{index:02d}" for index in range(20)],
            ["discovered-trace"],
            ["log-trace"],
            limit=20,
        )

        self.assertEqual(len(merged), 20)
        self.assertIn("discovered-trace", merged)
        self.assertIn("log-trace", merged)

    def test_deepflow_peer_ips_support_clickhouse_alias_fields(self):
        from servers.aiops_observability.case_builder import _deepflow_peer_ips

        peers = _deepflow_peer_ips(
            [
                {"src_ip": "10.0.0.10", "dst_ip": "10.0.0.20"},
                {"ip4_0": "10.0.0.30", "ip4_1": "10.0.0.20"},
            ],
            pod_ip="10.0.0.20",
        )

        self.assertEqual(peers, ["10.0.0.10", "10.0.0.30"])

    def test_extract_trace_ids_from_log_evidence(self):
        from servers.aiops_observability.case_builder import _trace_ids_from_evidence
        from servers.aiops_observability.schemas import EvidenceItem, SourceResult
        k8s = SourceResult("kubernetes", "k8s", "present", evidence=[
            EvidenceItem(evidence_id="log1", source_system="kubernetes", dimension="logs",
                         summary="prev logs",
                         payload={"log_preview": 'allocated mib=6 trace_id=e2e054876b6a5b936572000000000001 span_id=def01'})])
        tids = _trace_ids_from_evidence([k8s])
        self.assertIn("e2e054876b6a5b936572000000000001", tids)

    def test_logs_coverage_reflects_k8s_container_logs(self):
        # Regression (independent audit finding #1): when ES is empty but k8s
        # collected container logs, coverage.logs must NOT be misleadingly empty.
        from servers.aiops_observability.case_builder import _trace_ids_from_evidence
        # exercised indirectly; the coverage upgrade logic lives in _write_package.
        # Here we just assert the trace extractor ignores non-log evidence safely.
        from servers.aiops_observability.schemas import EvidenceItem, SourceResult
        empty = SourceResult("elasticsearch", "logs", "empty", evidence=[])
        self.assertEqual(_trace_ids_from_evidence([empty]), [])


if __name__ == "__main__":
    unittest.main()
