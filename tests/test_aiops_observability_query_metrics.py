import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from servers.aiops_observability.query import metrics


class MetricsScopeTests(unittest.TestCase):
    def test_rejects_unscoped_vector_selector(self):
        error = metrics.validate_promql_scope(
            'container_memory_working_set_bytes{namespace="ns",pod="pod"} or up',
            "ns",
            "pod",
        )
        self.assertEqual(error["error_code"], "missing_pod_scope")

    def test_rejects_regex_pod_matcher(self):
        error = metrics.validate_promql_scope(
            'kube_pod_status_phase{namespace="ns",pod=~"pod.*"}',
            "ns",
            "pod",
        )
        self.assertEqual(error["error_code"], "missing_pod_scope")

    def test_accepts_multiple_exact_scoped_selectors(self):
        error = metrics.validate_promql_scope(
            (
                'max_over_time(container_memory_working_set_bytes'
                '{namespace="ns",pod="pod",container="api"}[30m]) '
                '/ kube_pod_container_resource_limits'
                '{namespace="ns",pod="pod",container="api",resource="memory",unit="byte"}'
            ),
            "ns",
            "pod",
        )
        self.assertIsNone(error)

    def test_accepts_aggregation_label_list(self):
        error = metrics.validate_promql_scope(
            (
                "sum by (container) ("
                'container_memory_working_set_bytes{namespace="ns",pod="pod"}'
                ")"
            ),
            "ns",
            "pod",
        )
        self.assertIsNone(error)

    def test_accepts_group_aggregation_operator(self):
        error = metrics.validate_promql_scope(
            (
                "group by (container) ("
                'http_requests_total{namespace="ns",pod="pod"}'
                ")"
            ),
            "ns",
            "pod",
        )
        self.assertIsNone(error)

    def test_accepts_pod_scoped_subquery_and_at_modifier(self):
        error = metrics.validate_promql_scope(
            (
                "max_over_time("
                'http_requests_total{namespace="ns",pod="pod"}[30m:1m]'
                ") @ start()"
            ),
            "ns",
            "pod",
        )
        self.assertIsNone(error)

    def test_subquery_does_not_hide_an_unscoped_vector(self):
        error = metrics.validate_promql_scope(
            "max_over_time(up[30m:1m])",
            "ns",
            "pod",
        )
        self.assertEqual(error["error_code"], "missing_pod_scope")

    def test_accepts_vector_matching_label_list(self):
        error = metrics.validate_promql_scope(
            (
                'container_memory_working_set_bytes{namespace="ns",pod="pod"} '
                "/ on(namespace,pod,container) "
                'kube_pod_container_resource_limits{namespace="ns",pod="pod"}'
            ),
            "ns",
            "pod",
        )
        self.assertIsNone(error)

    def test_rejects_internal_range_over_window_budget(self):
        error = metrics.validate_promql_scope(
            'max_over_time(up{namespace="ns",pod="pod"}[3h])',
            "ns",
            "pod",
        )

        self.assertEqual(error["error_code"], "query_too_expensive")

    def test_rejects_subquery_resolution_over_point_budget(self):
        error = metrics.validate_promql_scope(
            'max_over_time(up{namespace="ns",pod="pod"}[30m:1s])',
            "ns",
            "pod",
        )

        self.assertEqual(error["error_code"], "query_too_expensive")

    def test_combined_outer_and_subquery_points_exceed_budget(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(metrics, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(metrics, "_request_prometheus") as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "bound nested range evaluation work",
                    "promql": (
                        "max_over_time("
                        'up{namespace="ns",pod="pod"}[30m:1m]'
                        ")"
                    ),
                    "query_type": "range",
                    "start": "2026-07-23T10:00:00Z",
                    "end": "2026-07-23T10:30:00Z",
                    "step": "1m",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "query_too_expensive")
        self.assertIn("combined", result["error"]["message"])
        backend.assert_not_called()

    def test_rejects_offset_over_window_budget(self):
        error = metrics.validate_promql_scope(
            'up{namespace="ns",pod="pod"} offset 3h',
            "ns",
            "pod",
        )

        self.assertEqual(error["error_code"], "query_too_expensive")

    def test_rejects_absolute_at_modifier(self):
        error = metrics.validate_promql_scope(
            'up{namespace="ns",pod="pod"} @ 1753056000',
            "ns",
            "pod",
        )

        self.assertEqual(error["error_code"], "unsafe_time_modifier")


class MetricsResultTests(unittest.TestCase):
    def test_deduplicates_scrapers_without_summing(self):
        payload = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "__name__": "container_memory_working_set_bytes",
                            "namespace": "ns",
                            "pod": "pod",
                            "container": "api",
                            "job": "kubelet-a",
                            "instance": "10.0.0.1:10250",
                        },
                        "values": [[1, "10"], [2, "20"], [3, "30"]],
                    },
                    {
                        "metric": {
                            "__name__": "container_memory_working_set_bytes",
                            "namespace": "ns",
                            "pod": "pod",
                            "container": "api",
                            "job": "kubelet-b",
                            "instance": "10.0.0.2:10250",
                        },
                        "values": [[1, "10"], [2, "20"], [3, "30"]],
                    },
                ],
            },
        }

        result = metrics.compact_prometheus_result(payload)

        self.assertEqual(result["raw_series_count"], 2)
        self.assertEqual(result["series_count"], 1)
        self.assertEqual(result["series"][0]["values"][-1], [3, "30"])
        self.assertEqual(result["series"][0]["stats"]["max"], 30.0)

    def test_execute_preserves_query_purpose_and_numeric_fact(self):
        prom_payload = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {
                            "__name__": "kube_pod_container_status_waiting_reason",
                            "namespace": "ns",
                            "pod": "pod",
                            "container": "api",
                            "reason": "ImagePullBackOff",
                        },
                        "value": [1753056000, "1"],
                    }
                ],
            },
        }
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
            patch.object(metrics, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(metrics, "_request_prometheus", return_value=(prom_payload, 200)),
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "verify the current image pull waiting reason",
                    "promql": (
                        'kube_pod_container_status_waiting_reason'
                        '{namespace="ns",pod="pod",reason="ImagePullBackOff"}'
                    ),
                    "query_type": "instant",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["purpose"], "verify the current image pull waiting reason")
        self.assertEqual(result["query"]["promql"].split("{", 1)[0], "kube_pod_container_status_waiting_reason")
        self.assertEqual(result["coverage"], "present")
        self.assertEqual(result["facts"][0]["value"], "1")
        self.assertEqual(result["facts"][0]["labels"]["reason"], "ImagePullBackOff")

    def test_execute_marks_metric_units_sample_quality_and_series_role(self):
        prom_payload = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {
                            "__name__": "container_memory_working_set_bytes",
                            "namespace": "ns",
                            "pod": "pod",
                            "container": "business-api",
                            "image": "example/business-api:1.0",
                        },
                        "value": [1753056000, "75497472"],
                    },
                    {
                        "metric": {
                            "__name__": "container_memory_working_set_bytes",
                            "namespace": "ns",
                            "pod": "pod",
                            "image": "registry.k8s.io/pause:3.9",
                        },
                        "value": [1753056000, "188416"],
                    },
                    {
                        "metric": {
                            "__name__": "container_memory_working_set_bytes",
                            "namespace": "ns",
                            "pod": "pod",
                        },
                        "value": [1753056000, "245760"],
                    },
                ],
            },
        }
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["business-api"],
        }
        with (
            patch.object(metrics, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(metrics, "_request_prometheus", return_value=(prom_payload, 200)),
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "verify whether application memory approached its limit",
                    "promql": (
                        'container_memory_working_set_bytes'
                        '{namespace="ns",pod="pod"}'
                    ),
                    "query_type": "instant",
                }
            )

        facts = result["facts"]
        self.assertEqual(facts[0]["labels"]["container"], "business-api")
        self.assertEqual(facts[0]["unit"], "bytes")
        self.assertEqual(facts[0]["series_role"], "application_container")
        self.assertEqual(facts[0]["directness"], "direct")
        self.assertEqual(facts[0]["sample_count"], 1)
        self.assertFalse(facts[0]["trend_evaluable"])

        by_role = {fact["series_role"]: fact for fact in facts}
        self.assertEqual(by_role["infrastructure_container"]["directness"], "related_context")
        self.assertEqual(by_role["pod_aggregate"]["directness"], "related_context")
        self.assertEqual(result["query"]["value_unit"], "bytes")
        self.assertFalse(result["query"]["trend_evaluable"])

    def test_range_query_rejects_outer_step_over_backend_point_budget(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(metrics, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(metrics, "_request_prometheus") as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect a bounded range",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "range",
                    "start": "2026-07-21T00:00:00Z",
                    "end": "2026-07-21T02:00:00Z",
                    "step": "1s",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "query_too_expensive")
        backend.assert_not_called()

    def test_range_query_synthesizes_default_window_when_bounds_are_omitted(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
            ),
            patch.object(
                metrics,
                "_request_prometheus",
                return_value=(prom_payload, 200),
            ) as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect a recent bounded range",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "range",
                }
            )

        self.assertTrue(result["ok"])
        backend.assert_called_once_with(
            "api/v1/query_range",
            {
                "query": 'up{namespace="ns",pod="pod"}',
                "start": "2026-07-23T11:30:00Z",
                "end": "2026-07-23T12:00:00Z",
                "step": "30s",
            },
        )
        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-23T11:30:00Z",
                "end": "2026-07-23T12:00:00Z",
            },
        )
        self.assertEqual(result["query"]["step"], "30s")

    def test_range_query_preserves_explicit_window_and_step(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch.object(
                metrics,
                "_request_prometheus",
                return_value=(prom_payload, 200),
            ) as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect an explicit bounded range",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "range",
                    "start": "2026-07-23T10:00:00Z",
                    "end": "2026-07-23T10:20:00Z",
                    "step": "1m",
                }
            )

        self.assertTrue(result["ok"])
        backend.assert_called_once_with(
            "api/v1/query_range",
            {
                "query": 'up{namespace="ns",pod="pod"}',
                "start": "2026-07-23T10:00:00Z",
                "end": "2026-07-23T10:20:00Z",
                "step": "1m",
            },
        )

    def test_range_query_preserves_raw_offset_request_and_executes_utc_window(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        promql = 'up{namespace="ns",pod="pod"}'
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch.object(
                metrics,
                "_request_prometheus",
                return_value=(prom_payload, 200),
            ) as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "preserve the caller time zone provenance",
                    "promql": promql,
                    "query_type": "range",
                    "start": "2026-07-23T18:00:00+08:00",
                    "end": "2026-07-23T18:20:00+08:00",
                    "step": "1m",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["query"]["requested_time_range"],
            {
                "start": "2026-07-23T18:00:00+08:00",
                "end": "2026-07-23T18:20:00+08:00",
            },
        )
        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-23T10:00:00Z",
                "end": "2026-07-23T10:20:00Z",
            },
        )
        self.assertNotIn("time_range_adjustment", result["query"])
        backend.assert_called_once_with(
            "api/v1/query_range",
            {
                "query": promql,
                "start": "2026-07-23T10:00:00Z",
                "end": "2026-07-23T10:20:00Z",
                "step": "1m",
            },
        )

    def test_range_query_interprets_relative_start_as_recent_lookback(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T07:25:45Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        promql = 'up{namespace="ns",pod="pod"}'
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(
                    2026,
                    7,
                    23,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            ),
            patch.object(
                metrics,
                "_request_prometheus",
                return_value=(prom_payload, 200),
            ) as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect the most recent hour",
                    "promql": promql,
                    "query_type": "range",
                    "start": "1h",
                    "window_minutes": 30,
                    "step": "1m",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["query"]["promql"], promql)
        self.assertEqual(
            result["query"]["requested_time_range"],
            {"start": "1h"},
        )
        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-23T11:00:00Z",
                "end": "2026-07-23T12:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range_adjustment"],
            {
                "reason": "relative_lookback",
                "action": "converted_duration_to_bounded_window",
            },
        )
        self.assertEqual(
            result["query"]["pod_creation_timestamp"],
            "2026-07-23T07:25:45Z",
        )
        backend.assert_called_once_with(
            "api/v1/query_range",
            {
                "query": promql,
                "start": "2026-07-23T11:00:00Z",
                "end": "2026-07-23T12:00:00Z",
                "step": "1m",
            },
        )

    def test_range_query_replaces_future_window_with_current_bounded_window(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T07:25:45Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        promql = 'up{namespace="ns",pod="pod"}'
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(
                    2026,
                    7,
                    23,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            ),
            patch.object(
                metrics,
                "_request_prometheus",
                return_value=(prom_payload, 200),
            ) as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "recover a future model-generated range",
                    "promql": promql,
                    "query_type": "range",
                    "start": "2026-07-23T13:00:00Z",
                    "end": "2026-07-23T14:00:00Z",
                    "window_minutes": 45,
                    "step": "1m",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["query"]["promql"], promql)
        self.assertEqual(
            result["query"]["requested_time_range"],
            {
                "start": "2026-07-23T13:00:00Z",
                "end": "2026-07-23T14:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-23T11:15:00Z",
                "end": "2026-07-23T12:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range_adjustment"],
            {
                "reason": "future_time_range",
                "action": "replaced_with_current_bounded_window",
            },
        )
        backend.assert_called_once_with(
            "api/v1/query_range",
            {
                "query": promql,
                "start": "2026-07-23T11:15:00Z",
                "end": "2026-07-23T12:00:00Z",
                "step": "1m",
            },
        )

    def test_range_query_replaces_reversed_window_with_current_bounded_window(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T07:25:45Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        promql = 'up{namespace="ns",pod="pod"}'
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(
                    2026,
                    7,
                    23,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            ),
            patch.object(
                metrics,
                "_request_prometheus",
                return_value=(prom_payload, 200),
            ) as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "recover a reversed model-generated range",
                    "promql": promql,
                    "query_type": "range",
                    "start": "2026-07-23T10:30:00Z",
                    "end": "2026-07-23T10:00:00Z",
                    "window_minutes": 20,
                    "step": "1m",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["query"]["promql"], promql)
        self.assertEqual(
            result["query"]["requested_time_range"],
            {
                "start": "2026-07-23T10:30:00Z",
                "end": "2026-07-23T10:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-23T11:40:00Z",
                "end": "2026-07-23T12:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range_adjustment"],
            {
                "reason": "reversed_time_range",
                "action": "replaced_with_current_bounded_window",
            },
        )
        backend.assert_called_once_with(
            "api/v1/query_range",
            {
                "query": promql,
                "start": "2026-07-23T11:40:00Z",
                "end": "2026-07-23T12:00:00Z",
                "step": "1m",
            },
        )

    def test_range_query_keeps_unrecognized_start_invalid(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T07:25:45Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(metrics, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(metrics, "_request_prometheus") as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "reject an unrecognized time expression",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "range",
                    "start": "sometime-recently",
                    "step": "1m",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_time_range")
        self.assertEqual(
            result["query"]["requested_time_range"],
            {"start": "sometime-recently"},
        )
        backend.assert_not_called()

    def test_range_query_rejects_relative_lookback_over_two_hours(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T07:25:45Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(metrics, "resolve_pod_scope", return_value=(entity, None)),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(
                    2026,
                    7,
                    23,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            ),
            patch.object(metrics, "_request_prometheus") as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "reject an oversized relative lookback",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "range",
                    "start": "3h",
                    "step": "1m",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "range_too_large")
        self.assertEqual(
            result["query"]["requested_time_range"],
            {"start": "3h"},
        )
        backend.assert_not_called()

    def test_range_query_rejects_sub_microsecond_lookback_after_rounding(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(metrics, "resolve_pod_scope", return_value=(entity, None)),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(
                    2026,
                    7,
                    23,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            ),
            patch.object(metrics, "_request_prometheus") as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "reject a lookback rounded to zero duration",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "range",
                    "start": "0.0001ms",
                    "step": "1m",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_time_range")
        self.assertEqual(
            result["query"]["requested_time_range"],
            {"start": "0.0001ms"},
        )
        self.assertNotIn("time_range", result["query"])
        backend.assert_not_called()

    def test_range_query_rejects_naive_absolute_window_with_provenance(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(metrics, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(metrics, "_request_prometheus") as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "reject host-timezone-dependent timestamps",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "range",
                    "start": "2026-07-23T10:00:00",
                    "end": "2026-07-23T10:20:00",
                    "step": "1m",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_time_range")
        self.assertEqual(
            result["query"]["requested_time_range"],
            {
                "start": "2026-07-23T10:00:00",
                "end": "2026-07-23T10:20:00",
            },
        )
        backend.assert_not_called()

    def test_range_window_normalizes_window_minutes_boundaries(self):
        expected = {
            "start": "2026-07-23T11:59:00Z",
            "end": "2026-07-23T12:00:00Z",
        }
        with patch(
            "servers.aiops_observability.schemas.utc_now",
            return_value=datetime(
                2026,
                7,
                23,
                12,
                0,
                tzinfo=timezone.utc,
            ),
        ):
            for window_minutes in (0, -5):
                with self.subTest(window_minutes=window_minutes):
                    window, requested, adjustment, error = (
                        metrics._range_window(
                            {"window_minutes": window_minutes},
                            not_before=None,
                        )
                    )
                    self.assertEqual(window, expected)
                    self.assertEqual(requested, {})
                    self.assertIsNone(adjustment)
                    self.assertIsNone(error)

            try:
                window, requested, adjustment, error = (
                    metrics._range_window(
                        {"window_minutes": 10**400},
                        not_before=None,
                    )
                )
            except OverflowError as exc:
                self.fail(f"window_minutes overflow escaped normalization: {exc}")

        self.assertIsNone(window)
        self.assertEqual(requested, {})
        self.assertIsNone(adjustment)
        self.assertEqual(error["code"], "invalid_time_range")

    def test_range_query_recovers_fully_stale_explicit_window_with_current_lifecycle_window(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T07:25:45Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(
                    2026,
                    7,
                    23,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            ),
            patch.object(
                metrics,
                "_request_prometheus",
                return_value=(prom_payload, 200),
            ) as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect current pod memory",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "range",
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2024-01-01T01:00:00Z",
                    "window_minutes": 60,
                    "step": "1m",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["query"]["requested_time_range"],
            {
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-01-01T01:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-23T11:00:00Z",
                "end": "2026-07-23T12:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range_adjustment"],
            {
                "reason": "range_precedes_pod_lifecycle",
                "action": "replaced_with_current_bounded_window",
            },
        )
        self.assertEqual(
            result["query"]["pod_creation_timestamp"],
            "2026-07-23T07:25:45Z",
        )
        backend.assert_called_once_with(
            "api/v1/query_range",
            {
                "query": 'up{namespace="ns",pod="pod"}',
                "start": "2026-07-23T11:00:00Z",
                "end": "2026-07-23T12:00:00Z",
                "step": "1m",
            },
        )

    def test_fully_stale_range_normalizes_zero_and_negative_fallback_minutes(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T07:25:45Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        promql = 'up{namespace="ns",pod="pod"}'
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        for window_minutes in (0, -5):
            with self.subTest(window_minutes=window_minutes):
                with (
                    patch.object(
                        metrics,
                        "resolve_pod_scope",
                        side_effect=[(entity, None), (entity, None)],
                    ),
                    patch(
                        "servers.aiops_observability.schemas.utc_now",
                        return_value=datetime(
                            2026,
                            7,
                            23,
                            12,
                            0,
                            tzinfo=timezone.utc,
                        ),
                    ),
                    patch.object(
                        metrics,
                        "_request_prometheus",
                        return_value=(prom_payload, 200),
                    ) as backend,
                ):
                    result = metrics.execute(
                        {
                            "namespace": "ns",
                            "pod": "pod",
                            "purpose": "normalize a stale fallback window",
                            "promql": promql,
                            "query_type": "range",
                            "start": "2024-01-01T00:00:00Z",
                            "end": "2024-01-01T01:00:00Z",
                            "window_minutes": window_minutes,
                            "step": "1m",
                        }
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(
                    result["query"]["requested_time_range"],
                    {
                        "start": "2024-01-01T00:00:00Z",
                        "end": "2024-01-01T01:00:00Z",
                    },
                )
                self.assertEqual(
                    result["query"]["time_range"],
                    {
                        "start": "2026-07-23T11:59:00Z",
                        "end": "2026-07-23T12:00:00Z",
                    },
                )
                self.assertEqual(
                    result["query"]["time_range_adjustment"],
                    {
                        "reason": "range_precedes_pod_lifecycle",
                        "action": "replaced_with_current_bounded_window",
                    },
                )
                backend.assert_called_once_with(
                    "api/v1/query_range",
                    {
                        "query": promql,
                        "start": "2026-07-23T11:59:00Z",
                        "end": "2026-07-23T12:00:00Z",
                        "step": "1m",
                    },
                )

    def test_fully_stale_range_rejects_overflowing_fallback_minutes(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T07:25:45Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(metrics, "resolve_pod_scope", return_value=(entity, None)),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(
                    2026,
                    7,
                    23,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            ),
            patch.object(metrics, "_request_prometheus") as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "reject an overflowing stale fallback window",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "range",
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2024-01-01T01:00:00Z",
                    "window_minutes": 10**400,
                    "step": "1m",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_time_range")
        self.assertEqual(
            result["query"]["requested_time_range"],
            {
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-01-01T01:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range_adjustment"],
            {
                "reason": "range_precedes_pod_lifecycle",
                "action": "replaced_with_current_bounded_window",
            },
        )
        backend.assert_not_called()

    def test_synthesized_range_may_clamp_to_pod_creation(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T11:45:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
            ),
            patch.object(
                metrics,
                "_request_prometheus",
                return_value=(prom_payload, 200),
            ) as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect the current pod lifetime",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "range",
                }
            )

        self.assertTrue(result["ok"])
        backend.assert_called_once_with(
            "api/v1/query_range",
            {
                "query": 'up{namespace="ns",pod="pod"}',
                "start": "2026-07-23T11:45:00Z",
                "end": "2026-07-23T12:00:00Z",
                "step": "30s",
            },
        )
        self.assertEqual(
            result["query"]["requested_time_range"],
            {},
        )
        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-23T11:45:00Z",
                "end": "2026-07-23T12:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range_adjustment"],
            {
                "reason": "range_precedes_pod_lifecycle",
                "action": "clamped_start_to_pod_creation",
            },
        )

    def test_relative_range_preserves_adjustment_when_clamped_to_pod_creation(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T11:45:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        promql = 'up{namespace="ns",pod="pod"}'
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(
                    2026,
                    7,
                    23,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            ),
            patch.object(
                metrics,
                "_request_prometheus",
                return_value=(prom_payload, 200),
            ) as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect the current pod portion of a lookback",
                    "promql": promql,
                    "query_type": "range",
                    "start": "1h",
                    "step": "1m",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["query"]["requested_time_range"],
            {"start": "1h"},
        )
        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-23T11:45:00Z",
                "end": "2026-07-23T12:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range_adjustment"],
            {
                "reason": "relative_lookback",
                "action": "converted_duration_to_bounded_window",
                "pod_lifecycle_adjustment": {
                    "reason": "range_precedes_pod_lifecycle",
                    "action": "clamped_start_to_pod_creation",
                },
            },
        )
        backend.assert_called_once_with(
            "api/v1/query_range",
            {
                "query": promql,
                "start": "2026-07-23T11:45:00Z",
                "end": "2026-07-23T12:00:00Z",
                "step": "1m",
            },
        )

    def test_future_range_preserves_adjustment_when_clamped_to_pod_creation(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-23T11:45:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        promql = 'up{namespace="ns",pod="pod"}'
        prom_payload = {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[(entity, None), (entity, None)],
            ),
            patch(
                "servers.aiops_observability.schemas.utc_now",
                return_value=datetime(
                    2026,
                    7,
                    23,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            ),
            patch.object(
                metrics,
                "_request_prometheus",
                return_value=(prom_payload, 200),
            ) as backend,
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "recover a future range within the pod lifetime",
                    "promql": promql,
                    "query_type": "range",
                    "start": "2026-07-23T13:00:00Z",
                    "end": "2026-07-23T14:00:00Z",
                    "window_minutes": 30,
                    "step": "1m",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["query"]["requested_time_range"],
            {
                "start": "2026-07-23T13:00:00Z",
                "end": "2026-07-23T14:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range"],
            {
                "start": "2026-07-23T11:45:00Z",
                "end": "2026-07-23T12:00:00Z",
            },
        )
        self.assertEqual(
            result["query"]["time_range_adjustment"],
            {
                "reason": "future_time_range",
                "action": "replaced_with_current_bounded_window",
                "pod_lifecycle_adjustment": {
                    "reason": "range_precedes_pod_lifecycle",
                    "action": "clamped_start_to_pod_creation",
                },
            },
        )
        backend.assert_called_once_with(
            "api/v1/query_range",
            {
                "query": promql,
                "start": "2026-07-23T11:45:00Z",
                "end": "2026-07-23T12:00:00Z",
                "step": "1m",
            },
        )

    def test_compaction_preserves_original_peak_and_sample_count(self):
        values = [[index, str(index)] for index in range(25)]
        values[11] = [11, "999"]
        payload = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "__name__": "container_memory_working_set_bytes",
                            "namespace": "ns",
                            "pod": "pod",
                            "container": "api",
                        },
                        "values": values,
                    }
                ],
            },
        }

        compact = metrics.compact_prometheus_result(payload)
        series = metrics._annotate_series(
            compact["series"],
            {"containers": ["api"]},
            "range",
        )[0]

        self.assertEqual(series["sample_count"], 25)
        self.assertEqual(series["stats"]["max"], 999.0)
        self.assertIn([11, "999"], series["values"])
        self.assertLessEqual(len(series["values"]), metrics.MAX_SAMPLES)

    def test_compaction_orders_retained_points_and_preserves_boundary_extrema(self):
        values = [[index, str(index + 100)] for index in range(257)]
        values[73] = [73, "-999"]
        values[181] = [181, "9999"]
        values[-1] = [256, "777"]
        payload = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "__name__": "container_memory_working_set_bytes",
                            "namespace": "ns",
                            "pod": "pod",
                            "container": "api",
                        },
                        "values": values,
                    }
                ],
            },
        }

        compact = metrics.compact_prometheus_result(payload)
        series = compact["series"][0]
        retained = series["values"]
        facts = metrics._facts([series], "ns", "pod")

        self.assertEqual(
            [point[0] for point in retained],
            sorted(point[0] for point in retained),
        )
        self.assertEqual(retained[0], [0, "100"])
        self.assertEqual(retained[-1], [256, "777"])
        self.assertIn([73, "-999"], retained)
        self.assertIn([181, "9999"], retained)
        self.assertEqual(
            series["stats"],
            {
                "first": 100.0,
                "min": -999.0,
                "max": 9999.0,
                "last": 777.0,
            },
        )
        self.assertEqual(facts[0]["value"], "777")

    def test_compaction_sorts_unordered_samples_before_stats_bounding_and_latest_fact(self):
        values = [[timestamp, str(timestamp)] for timestamp in range(30, 0, -1)]
        values[0] = [30, "777"]
        values[9] = [21, "9999"]
        values[23] = [7, "-999"]
        values[-1] = [1, "100"]
        payload = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "__name__": "container_memory_working_set_bytes",
                            "namespace": "ns",
                            "pod": "pod",
                            "container": "api",
                        },
                        "values": values,
                    }
                ],
            },
        }

        compact = metrics.compact_prometheus_result(payload)
        series = compact["series"][0]
        retained = series["values"]
        facts = metrics._facts([series], "ns", "pod")

        self.assertEqual(
            [point[0] for point in retained],
            sorted(point[0] for point in retained),
        )
        self.assertEqual(retained[0], [1, "100"])
        self.assertEqual(retained[-1], [30, "777"])
        self.assertIn([7, "-999"], retained)
        self.assertIn([21, "9999"], retained)
        self.assertEqual(
            series["stats"],
            {
                "first": 100.0,
                "min": -999.0,
                "max": 9999.0,
                "last": 777.0,
            },
        )
        self.assertEqual(facts[0]["value"], "777")
        self.assertEqual(facts[0]["observed_at"], "1970-01-01T00:00:30Z")

    def test_http_200_success_with_missing_result_shape_is_rejected(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        malformed_payloads = [
            {"status": "success"},
            {"status": "success", "data": {}},
            {"status": "success", "data": {"result": {}}},
        ]

        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                with (
                    patch.object(
                        metrics,
                        "resolve_pod_scope",
                        side_effect=[(entity, None), (entity, None)],
                    ),
                    patch.object(
                        metrics,
                        "_request_prometheus",
                        return_value=(payload, 200),
                    ),
                ):
                    result = metrics.execute(
                        {
                            "namespace": "ns",
                            "pod": "pod",
                            "purpose": "reject malformed backend success",
                            "promql": 'up{namespace="ns",pod="pod"}',
                            "query_type": "instant",
                        }
                    )

                self.assertFalse(result["ok"])
                self.assertEqual(
                    result["error"]["code"],
                    "malformed_prometheus_response",
                )
                self.assertEqual(result["coverage"], "error")

    def test_http_200_success_rejects_missing_or_unknown_result_type(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        malformed_payloads = [
            {"status": "success", "data": {"result": []}},
            {
                "status": "success",
                "data": {"resultType": "histogram", "result": []},
            },
        ]

        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                with (
                    patch.object(
                        metrics,
                        "resolve_pod_scope",
                        side_effect=[(entity, None), (entity, None)],
                    ),
                    patch.object(
                        metrics,
                        "_request_prometheus",
                        return_value=(payload, 200),
                    ),
                ):
                    result = metrics.execute(
                        {
                            "namespace": "ns",
                            "pod": "pod",
                            "purpose": "reject an unsupported result type",
                            "promql": 'up{namespace="ns",pod="pod"}',
                            "query_type": "instant",
                        }
                    )

                self.assertFalse(result["ok"])
                self.assertEqual(
                    result["error"]["code"],
                    "malformed_prometheus_response",
                )

    def test_http_200_success_rejects_result_type_incompatible_with_query_path(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-21T00:00:00Z",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        cases = [
            ("instant", "matrix"),
            ("range", "vector"),
        ]

        for query_type, result_type in cases:
            with self.subTest(query_type=query_type, result_type=result_type):
                arguments = {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "reject a path-incompatible result type",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": query_type,
                }
                if query_type == "range":
                    arguments.update(
                        {
                            "start": "2026-07-23T10:00:00Z",
                            "end": "2026-07-23T10:20:00Z",
                            "step": "1m",
                        }
                    )
                payload = {
                    "status": "success",
                    "data": {
                        "resultType": result_type,
                        "result": [],
                    },
                }
                with (
                    patch.object(
                        metrics,
                        "resolve_pod_scope",
                        side_effect=[(entity, None), (entity, None)],
                    ),
                    patch.object(
                        metrics,
                        "_request_prometheus",
                        return_value=(payload, 200),
                    ),
                ):
                    result = metrics.execute(arguments)

                self.assertFalse(result["ok"])
                self.assertEqual(
                    result["error"]["code"],
                    "malformed_prometheus_response",
                )

    def test_derived_metric_unit_and_top_level_directness_follow_series(self):
        prom_payload = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {
                            "namespace": "ns",
                            "pod": "pod",
                            "container": "POD",
                        },
                        "value": [1753056000, "1"],
                    }
                ],
            },
        }
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(metrics, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(metrics, "_request_prometheus", return_value=(prom_payload, 200)),
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "inspect a derived pod aggregate",
                    "promql": 'sum(up{namespace="ns",pod="pod"})',
                    "query_type": "instant",
                }
            )

        self.assertEqual(result["facts"][0]["unit"], "unknown")
        self.assertEqual(result["query"]["value_unit"], "unknown")
        self.assertEqual(result["directness"], "related_context")

    def test_execute_rejects_result_when_pod_lifecycle_changes(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.8",
            "node": "node1",
            "containers": ["api"],
        }
        prom_payload = {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        }
        with (
            patch.object(
                metrics,
                "resolve_pod_scope",
                side_effect=[
                    (entity, None),
                    (
                        None,
                        {
                            "code": "pod_uid_conflict",
                            "message": "current Pod UID uid-2 does not match supplied UID uid-1",
                        },
                    ),
                ],
            ) as resolver,
            patch.object(metrics, "_request_prometheus", return_value=(prom_payload, 200)),
        ):
            result = metrics.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "avoid mixing pod incarnations",
                    "promql": 'up{namespace="ns",pod="pod"}',
                    "query_type": "instant",
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "pod_lifecycle_changed")
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(resolver.call_args_list[1].args, ("ns", "pod", "uid-1"))


if __name__ == "__main__":
    unittest.main()
