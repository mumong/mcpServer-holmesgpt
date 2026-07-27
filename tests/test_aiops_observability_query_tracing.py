import unittest
from unittest.mock import patch

from servers.aiops_observability.query import tracing
from servers.aiops_observability.schemas import SourceResult
from servers.holmes_tools import aiops_observability_query


class TracingQueryTests(unittest.TestCase):
    def test_facts_interleave_deepflow_and_tempo_for_budget_fairness(self):
        trace_a = "0123456789abcdef0123456789abcdea"
        trace_b = "0123456789abcdef0123456789abcdeb"
        flows = [
            {
                "timestamp": "2026-07-21 00:10:00",
                "request_resource": f"/allocate?step={index}",
                "trace_id": trace_a if index < 2 else trace_b,
            }
            for index in range(3)
        ]
        spans = [
            {
                "trace_id": trace_id,
                "service": "api",
                "name": f"GET /allocate {index}",
            }
            for index, trace_id in enumerate([trace_b, trace_a])
        ]

        facts = tracing._facts("ns", "pod", flows, spans)

        self.assertEqual(
            [fact["source_system"] for fact in facts[:4]],
            ["deepflow", "tempo", "deepflow", "tempo"],
        )
        self.assertEqual(facts[0]["trace_id"], facts[1]["trace_id"])
        self.assertEqual(facts[2]["trace_id"], facts[3]["trace_id"])
        self.assertEqual(facts[0]["trace_id"], trace_a)
        self.assertEqual(facts[2]["trace_id"], trace_b)

    def test_correlations_preserve_first_matching_flow_order(self):
        trace_a = "0123456789abcdef0123456789abcdea"
        trace_b = "0123456789abcdef0123456789abcdeb"
        flows = [
            {"trace_id": trace_b},
            {"trace_id": trace_a},
        ]
        spans = [
            {"trace_id": trace_a},
            {"trace_id": trace_b},
        ]

        correlations = tracing._correlations(flows, spans)

        self.assertEqual(
            [item["trace_id"] for item in correlations],
            [trace_b, trace_a],
        )

    def test_bounded_tool_result_preserves_flow_span_and_exact_correlation(self):
        trace_ids = [
            "0123456789abcdef0123456789abcde0",
            "0123456789abcdef0123456789abcde1",
            "0123456789abcdef0123456789abcde2",
        ]
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        flows = [
            {
                "time": f"2026-07-21 00:10:{index:02d}",
                "src_ip": "10.0.0.3",
                "dst_ip": "10.0.0.2",
                "l7_protocol_str": "HTTP",
                "request_type": "GET",
                "request_resource": f"/allocate?mib=2&step={index}",
                "response_code": 200,
                "response_status": "Ok",
                "response_duration": 8000 + index,
                "trace_id": trace_ids[index % len(trace_ids)],
                "span_id": f"{index:016x}",
                "syscall_trace_id_request": str(index),
                "syscall_trace_id_response": str(index),
            }
            for index in range(10)
        ]
        spans = [
            {
                "trace_id": trace_id,
                "service": "api",
                "name": "GET /allocate",
                "start": f"2026-07-21T00:10:0{index}Z",
                "end": f"2026-07-21T00:10:0{index + 1}Z",
                "attributes": {
                    "aiops.allocated_mib.before": index * 2,
                    "aiops.allocated_mib.after": index * 2 + 2,
                    "aiops.alloc_mib": 2,
                    "k8s.pod.name": "pod",
                    "url.path": f"/allocate?mib=2&step={index}",
                },
            }
            for index, trace_id in enumerate(trace_ids)
        ]
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(tracing, "_query_deepflow", return_value={"data": flows}),
            patch.object(
                tracing,
                "_query_tempo",
                return_value={
                    "coverage": "present",
                    "spans": spans,
                    "query_meta": {"queried_trace_ids": trace_ids},
                    "error": None,
                    "executed": True,
                },
            ),
        ):
            raw = aiops_observability_query.call_tool(
                "query_pod_tracing",
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "pod_uid": "uid-1",
                    "purpose": "preserve exact cross-source trace evidence",
                    "request_resource": "/allocate",
                    "max_records": 10,
                },
            )

        self.assertIsNotNone(raw)
        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        result = __import__("json").loads(raw)
        fact_sources = {
            fact["source_system"]
            for fact in result["facts"]
        }
        self.assertIn("deepflow", fact_sources)
        self.assertIn("tempo", fact_sources)
        self.assertTrue(result["correlations"])
        self.assertEqual(result["correlations"][0]["basis"], "exact_trace_id")
        retained_correlation = result["correlations"][0]["trace_id"]
        self.assertIn(
            retained_correlation,
            {
                fact["trace_id"]
                for fact in result["facts"]
                if fact["source_system"] == "deepflow"
            },
        )
        self.assertIn(
            retained_correlation,
            {
                fact["trace_id"]
                for fact in result["facts"]
                if fact["source_system"] == "tempo"
            },
        )
        self.assertEqual(
            result["evidence_refs"],
            [fact["ref"] for fact in result["facts"]],
        )

    def test_sql_qualifies_time_column_to_avoid_clickhouse_alias_shadowing(self):
        sql = tracing.build_deepflow_sql(
            database="flow_log",
            pod_ip="10.0.0.2",
            start="2026-07-21T00:00:00Z",
            end="2026-07-21T00:30:00Z",
            direction="any",
            protocols=[],
            response_codes=[],
            response_statuses=[],
            min_duration_us=None,
            peer_ip=None,
            trace_id=None,
            request_resource=None,
            max_records=20,
        )

        self.assertIn("SELECT toString(flow.time) AS time", sql)
        self.assertIn("FROM flow_log.l7_flow_log AS flow", sql)
        self.assertIn("flow.time >= toDateTime(", sql)
        self.assertIn("flow.time <= toDateTime(", sql)
        self.assertIn("flow.time DESC", sql)
        self.assertNotIn("WHERE time >=", sql)

    def test_sql_keeps_pod_ip_scope_and_model_selected_filters(self):
        sql = tracing.build_deepflow_sql(
            database="flow_log",
            pod_ip="10.0.0.2",
            start="2026-07-21T00:00:00Z",
            end="2026-07-21T00:30:00Z",
            direction="inbound",
            protocols=["HTTP"],
            response_codes=[500, 503],
            response_statuses=["Server Error"],
            min_duration_us=50000,
            peer_ip="10.0.0.3",
            trace_id="0123456789abcdef0123456789abcdef",
            request_resource="/allocate",
            max_records=20,
        )

        self.assertIn("ip4_1 = '10.0.0.2'", sql)
        self.assertIn("ip4_0 = '10.0.0.3'", sql)
        self.assertIn("l7_protocol_str IN ('HTTP')", sql)
        self.assertIn("response_code IN (500,503)", sql)
        self.assertIn("response_status IN (3)", sql)
        self.assertNotIn("'Server Error'", sql)
        self.assertIn("response_duration >= 50000", sql)
        self.assertIn("trace_id = '0123456789abcdef0123456789abcdef'", sql)
        self.assertIn("positionCaseInsensitive(request_resource, '/allocate') > 0", sql)
        self.assertIn("LIMIT 20 FORMAT JSON", sql)

    def test_sql_maps_generic_error_status_to_all_non_ok_deepflow_statuses(self):
        sql = tracing.build_deepflow_sql(
            database="flow_log",
            pod_ip="10.0.0.2",
            start="2026-07-21T00:00:00Z",
            end="2026-07-21T00:30:00Z",
            direction="any",
            protocols=[],
            response_codes=[],
            response_statuses=["error"],
            min_duration_us=None,
            peer_ip=None,
            trace_id=None,
            request_resource=None,
            max_records=20,
        )

        self.assertIn("response_status != 0", sql)
        self.assertNotIn("'error'", sql)

    def test_sql_maps_specific_symbolic_deepflow_statuses_to_numeric_values(self):
        sql = tracing.build_deepflow_sql(
            database="flow_log",
            pod_ip="10.0.0.2",
            start="2026-07-21T00:00:00Z",
            end="2026-07-21T00:30:00Z",
            direction="any",
            protocols=[],
            response_codes=[],
            response_statuses=["server_error", "client error", "timeout"],
            min_duration_us=None,
            peer_ip=None,
            trace_id=None,
            request_resource=None,
            max_records=20,
        )

        self.assertIn("response_status IN (3,4,5)", sql)
        self.assertNotIn("'server_error'", sql)

    def test_sql_rejects_unknown_symbolic_deepflow_status(self):
        with self.assertRaisesRegex(ValueError, "unsupported response_status"):
            tracing.build_deepflow_sql(
                database="flow_log",
                pod_ip="10.0.0.2",
                start="2026-07-21T00:00:00Z",
                end="2026-07-21T00:30:00Z",
                direction="any",
                protocols=[],
                response_codes=[],
                response_statuses=["mystery"],
                min_duration_us=None,
                peer_ip=None,
                trace_id=None,
                request_resource=None,
                max_records=20,
            )

    def test_execute_returns_empty_without_pod_ip(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": None,
            "node": "node1",
            "containers": ["api"],
        }
        with patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "check application requests during image pull failure",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["coverage"], "empty")
        self.assertEqual(result["directness"], "direct")
        self.assertIn("pod_ip", result["limitations"][0])
        self.assertEqual(result["flows"], [])
        self.assertEqual(result["spans"], [])

    def test_explicit_trace_id_queries_tempo_without_pod_ip(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": None,
            "node": "node1",
            "containers": ["api"],
        }
        spans = [
            {
                "trace_id": trace_id,
                "service": "checkout",
                "name": "POST /pay",
            }
        ]
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(tracing, "_query_deepflow") as deepflow_query,
            patch.object(
                tracing,
                "_query_tempo",
                return_value={
                    "coverage": "present",
                    "spans": spans,
                    "query_meta": {"queried_trace_ids": [trace_id]},
                    "error": None,
                },
            ) as tempo_query,
        ):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect an independently supplied trace",
                    "trace_id": trace_id,
                }
            )

        deepflow_query.assert_not_called()
        tempo_query.assert_called_once_with(
            namespace="ns",
            pod="pod",
            trace_ids=[trace_id],
            service=None,
        )
        self.assertEqual(result["coverage"], "present")
        self.assertEqual(result["directness"], "related_context")
        self.assertEqual(result["flows"], [])
        self.assertEqual(result["spans"], spans)
        self.assertTrue(
            all(
                fact["directness"] == "related_context"
                for fact in result["facts"]
            )
        )
        self.assertIn("Pod IP", result["limitations"][0])

    def test_explicit_trace_id_preserves_tempo_absent_coverage(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": None,
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(
                tracing,
                "_query_tempo",
                return_value={
                    "coverage": "absent",
                    "spans": [],
                    "query_meta": {"queried_trace_ids": [trace_id]},
                    "error": "Tempo is unavailable",
                },
            ),
        ):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "verify an explicit trace while Tempo is unavailable",
                    "trace_id": trace_id,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["coverage"], "absent")
        self.assertEqual(result["telemetry"]["tempo"]["coverage"], "absent")
        self.assertIn("Tempo is unavailable", result["limitations"])

    def test_explicit_trace_id_reports_tempo_backend_error(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": None,
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(
                tracing,
                "_query_tempo",
                return_value={
                    "coverage": "error",
                    "spans": [],
                    "query_meta": {"queried_trace_ids": [trace_id]},
                    "error": "Tempo query failed",
                },
            ),
        ):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "verify an explicit trace when Tempo query fails",
                    "trace_id": trace_id,
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["coverage"], "error")
        self.assertEqual(result["error"]["code"], "tempo_error")
        self.assertEqual(result["telemetry"]["tempo"]["coverage"], "error")

    def test_explicit_trace_id_preserves_true_empty_tempo_lookup(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": None,
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(
                tracing,
                "_query_tempo",
                return_value={
                    "coverage": "empty",
                    "spans": [],
                    "query_meta": {"queried_trace_ids": [trace_id]},
                    "error": "no spans matched",
                },
            ),
        ):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "verify an explicit trace with no matching spans",
                    "trace_id": trace_id,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["coverage"], "empty")
        self.assertEqual(result["telemetry"]["tempo"]["coverage"], "empty")

    def test_invalid_explicit_trace_id_never_reaches_tempo(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": None,
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(tracing, "_query_tempo") as tempo_query,
        ):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "reject an unsafe trace lookup",
                    "trace_id": "../search?limit=100",
                }
            )

        tempo_query.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_trace_id")

    def test_invalid_deepflow_trace_id_never_reaches_tempo(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        flow_payload = {
            "data": [
                {
                    "time": "2026-07-21 00:10:00",
                    "src_ip": "10.0.0.3",
                    "dst_ip": "10.0.0.2",
                    "l7_protocol_str": "HTTP",
                    "request_type": "GET",
                    "request_resource": "/health",
                    "response_code": 200,
                    "response_status": "Ok",
                    "response_duration": 1000,
                    "trace_id": "../search?limit=100",
                }
            ]
        }
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(tracing, "_query_deepflow", return_value=flow_payload),
            patch.object(tracing, "_query_tempo") as tempo_query,
        ):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect Pod flows without trusting invalid IDs",
                }
            )

        tempo_query.assert_not_called()
        self.assertEqual(result["query"]["tempo_trace_ids"], [])
        self.assertEqual(result["spans"], [])
        self.assertEqual(result["flows"][0]["trace_id"], "../search?limit=100")

    def test_execute_keeps_flows_spans_and_exact_trace_correlation_separate(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        flow = {
            "time": "2026-07-21 00:10:00",
            "src_ip": "10.0.0.3",
            "dst_ip": "10.0.0.2",
            "l7_protocol_str": "HTTP",
            "request_type": "GET",
            "request_resource": "/allocate?mib=2",
            "response_code": 200,
            "response_status": "Ok",
            "response_duration": 8123,
            "trace_id": trace_id,
            "span_id": "abcdef0123456789",
            "syscall_trace_id_request": "123",
            "syscall_trace_id_response": "123",
        }
        flow_payload = {"data": [flow, dict(flow)]}
        spans = [
            {
                "trace_id": trace_id,
                "service": "trace-oom-api",
                "name": "GET /allocate",
                "attributes": {"allocated_before": 60, "allocated_after": 62},
            },
            {
                "trace_id": trace_id,
                "service": "trace-oom-api",
                "name": "GET /allocate",
                "attributes": {"allocated_before": 60, "allocated_after": 62},
            },
        ]
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(tracing, "_query_deepflow", return_value=flow_payload),
            patch.object(
                tracing,
                "_query_tempo",
                return_value={
                    "coverage": "present",
                    "spans": spans,
                    "query_meta": {"queried_trace_ids": [trace_id]},
                    "error": None,
                },
            ),
        ):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "identify the request that drives memory allocation",
                    "request_resource": "/allocate",
                }
            )

        self.assertEqual(result["coverage"], "present")
        self.assertEqual(len(result["flows"]), 1)
        self.assertEqual(len(result["spans"]), 1)
        self.assertEqual(result["correlations"][0]["trace_id"], trace_id)
        self.assertEqual(result["flows"][0]["request_resource"], "/allocate?mib=2")
        self.assertEqual(result["spans"][0]["attributes"]["allocated_after"], 62)

    def test_filtered_empty_query_uses_bounded_pod_baseline_as_related_context(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        baseline_flow = {
            "time": "2026-07-21 00:10:00",
            "src_ip": "10.0.0.3",
            "dst_ip": "10.0.0.2",
            "l7_protocol_str": "HTTP",
            "request_type": "GET",
            "request_resource": "/allocate?mib=2",
            "response_code": 200,
            "response_status": "Ok",
            "response_duration": 8123,
            "trace_id": trace_id,
            "span_id": "abcdef0123456789",
            "syscall_trace_id_request": "123",
            "syscall_trace_id_response": "123",
        }
        spans = [{
            "trace_id": trace_id,
            "service": "trace-oom-api",
            "name": "GET /allocate",
            "attributes": {
                "aiops.allocated_mib.before": 60,
                "aiops.alloc_mib": 2,
                "aiops.allocated_mib.after": 62,
                "k8s.pod.name": "pod",
            },
        }]
        deepflow_calls = []

        def query_deepflow(sql):
            deepflow_calls.append(sql)
            if len(deepflow_calls) == 1:
                return {"data": []}
            return {"data": [baseline_flow]}

        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(
                tracing,
                "_query_deepflow",
                side_effect=query_deepflow,
            ),
            patch.object(
                tracing,
                "_query_tempo",
                return_value={
                    "coverage": "present",
                    "spans": spans,
                    "query_meta": {"queried_trace_ids": [trace_id]},
                    "error": None,
                    "executed": True,
                },
            ) as tempo_query,
        ):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": (
                        "check whether failed requests explain the Pod restart"
                    ),
                    "response_codes": [500, 502, 503, 504],
                    "response_statuses": ["error"],
                    "max_records": 20,
                }
            )

        self.assertEqual(len(deepflow_calls), 2)
        self.assertIn("response_code IN (500,502,503,504)", deepflow_calls[0])
        self.assertIn("response_status != 0", deepflow_calls[0])
        self.assertNotIn("response_code IN", deepflow_calls[1])
        self.assertNotIn("response_status", deepflow_calls[1].split(" WHERE ", 1)[1])
        self.assertIn("LIMIT 8 FORMAT JSON", deepflow_calls[1])
        tempo_query.assert_called_once_with(
            namespace="ns",
            pod="pod",
            trace_ids=[trace_id],
            service=None,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "query_partial")
        self.assertEqual(result["coverage"], "weak")
        self.assertEqual(result["directness"], "related_context")
        self.assertEqual(result["query"]["filtered_match_count"], 0)
        self.assertEqual(result["query"]["baseline_match_count"], 1)
        self.assertEqual(result["query"]["sql"], deepflow_calls[0])
        self.assertEqual(result["query"]["baseline_sql"], deepflow_calls[1])
        self.assertEqual(result["flows"][0]["request_resource"], "/allocate?mib=2")
        self.assertEqual(result["spans"], spans)
        self.assertEqual(result["correlations"][0]["basis"], "exact_trace_id")
        self.assertTrue(
            all(
                fact["directness"] == "related_context"
                for fact in result["facts"]
            )
        )
        self.assertIn("original predicate", result["limitations"][0])

    def test_query_tempo_propagates_collector_coverage_query_and_error(self):
        source = SourceResult(
            "tempo",
            "trace",
            "absent",
            query_meta={"queried_trace_ids": ["abc"]},
            error="Tempo disabled",
        )
        with patch.object(tracing, "collect_tempo", return_value=source):
            result = tracing._query_tempo(
                namespace="ns",
                pod="pod",
                trace_ids=["abc"],
                service=None,
            )

        self.assertEqual(result["coverage"], "absent")
        self.assertEqual(result["query_meta"]["queried_trace_ids"], ["abc"])
        self.assertEqual(result["error"], "Tempo disabled")
        self.assertEqual(result["spans"], [])

    def test_query_tempo_recomputes_coverage_after_service_filter(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        source = SourceResult(
            "tempo",
            "trace",
            "present",
            evidence=[
                __import__(
                    "servers.aiops_observability.schemas",
                    fromlist=["EvidenceItem"],
                ).EvidenceItem(
                    evidence_id="tempo-spans",
                    source_system="tempo",
                    dimension="trace",
                    summary="one span",
                    payload={
                        "spans": [
                            {
                                "trace_id": trace_id,
                                "service": "service-a",
                                "name": "GET /ready",
                            }
                        ]
                    },
                )
            ],
            query_meta={"queried_trace_ids": [trace_id]},
        )
        with patch.object(tracing, "collect_tempo", return_value=source):
            result = tracing._query_tempo(
                namespace="ns",
                pod="pod",
                trace_ids=[trace_id],
                service="service-b",
            )

        self.assertEqual(result["coverage"], "empty")
        self.assertEqual(result["spans"], [])
        self.assertEqual(result["filtered_out_count"], 1)
        self.assertIn("service-b", result["error"])

    def test_include_tempo_false_is_not_reported_as_verified_empty(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(tracing, "_query_deepflow", return_value={"data": []}),
            patch.object(tracing, "_query_tempo") as tempo_query,
        ):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect flows without querying Tempo",
                    "include_tempo": False,
                }
            )

        tempo_query.assert_not_called()
        self.assertEqual(result["telemetry"]["tempo"]["coverage"], "absent")
        self.assertFalse(result["telemetry"]["tempo"]["executed"])
        self.assertEqual(
            result["telemetry"]["tempo"]["reason"],
            "include_tempo=false",
        )

    def test_no_trace_ids_is_not_reported_as_verified_empty_tempo_lookup(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(tracing, "_query_deepflow", return_value={"data": []}),
            patch.object(tracing, "_query_tempo") as tempo_query,
        ):
            result = tracing.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect flows that have no trace IDs",
                }
            )

        tempo_query.assert_not_called()
        self.assertEqual(result["telemetry"]["tempo"]["coverage"], "absent")
        self.assertFalse(result["telemetry"]["tempo"]["executed"])
        self.assertEqual(
            result["telemetry"]["tempo"]["reason"],
            "no_valid_trace_ids",
        )

    def test_pod_ip_path_propagates_tempo_error_when_no_flow_exists(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(tracing, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(
                tracing,
                "_query_deepflow",
                return_value={
                    "data": [{
                        "time": "2026-07-21 00:10:00",
                        "src_ip": "10.0.0.3",
                        "dst_ip": "10.0.0.2",
                        "trace_id": "0123456789abcdef0123456789abcdef",
                    }]
                },
            ),
            patch.object(
                tracing,
                "_query_tempo",
                return_value={
                    "coverage": "error",
                    "spans": [],
                    "query_meta": {},
                    "error": "Tempo query failed",
                    "executed": True,
                },
            ),
        ):
            result = tracing.execute({
                "namespace": "ns",
                "pod": "pod",
                "purpose": "verify trace backend availability",
            })

        self.assertEqual(result["coverage"], "weak")
        self.assertEqual(result["telemetry"]["tempo"]["coverage"], "error")
        self.assertIn("Tempo query failed", result["limitations"])

    def test_non_target_pod_span_is_related_context(self):
        facts = tracing._facts(
            "ns",
            "pod",
            [],
            [{
                "trace_id": "0123456789abcdef0123456789abcdef",
                "service": "peer",
                "name": "GET /peer",
                "attributes": {"k8s.pod.name": "other-pod"},
            }],
            pod_uid="uid-1",
        )

        self.assertEqual(facts[0]["directness"], "related_context")


if __name__ == "__main__":
    unittest.main()
