import json
import unittest
from unittest.mock import patch

from servers.aiops_observability.query import common, logs
from servers.holmes_tools import aiops_observability_query


class LoggingQueryTests(unittest.TestCase):
    def test_requested_fields_are_appended_to_mandatory_source_fields(self):
        body = logs.build_query_body(
            namespace="ns",
            pod="pod",
            pod_uid="uid-1",
            start="2026-07-21T00:00:00Z",
            end="2026-07-21T00:30:00Z",
            container=None,
            keywords=[],
            keyword_mode="any",
            levels=[],
            trace_id=None,
            max_records=20,
            identity_basis="pod_uid",
            fields=["severity"],
        )

        self.assertIn("severity", body["_source"])
        for mandatory in (
            "@timestamp",
            "message",
            "log",
            "kubernetes.pod.uid",
            "kubernetes.pod.name",
            "kubernetes.namespace",
            "kubernetes.container.name",
            "trace.id",
            "log.level",
        ):
            self.assertIn(mandatory, body["_source"])

    def test_uid_query_contains_structured_model_selected_filters(self):
        body = logs.build_query_body(
            namespace="ns",
            pod="pod",
            pod_uid="uid-1",
            start="2026-07-21T00:00:00Z",
            end="2026-07-21T00:30:00Z",
            container="api",
            keywords=["required config", "missing"],
            keyword_mode="all",
            levels=["error"],
            trace_id="0123456789abcdef0123456789abcdef",
            max_records=20,
            identity_basis="pod_uid",
        )
        encoded = str(body)

        self.assertIn("kubernetes.pod.uid", encoded)
        self.assertIn("uid-1", encoded)
        self.assertIn("kubernetes.container.name", encoded)
        self.assertIn("required config", encoded)
        self.assertIn("minimum_should_match", encoded)
        self.assertIn("trace.id", encoded)
        self.assertEqual(body["size"], logs.MAX_BACKEND_RECORDS)

    def test_rejects_wildcard_and_unsafe_source_fields(self):
        for fields in (["*"], ["event.*"], ["../secret"], ["field[0]"]):
            with self.subTest(fields=fields), patch.object(
                logs,
                "_query_elasticsearch",
            ) as backend:
                result = logs.execute(
                    {
                        "namespace": "ns",
                        "pod": "pod",
                        "purpose": "read selected safe fields",
                        "fields": fields,
                    }
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "invalid_fields")
            backend.assert_not_called()

    def test_accepts_safe_optional_source_fields_without_losing_message(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        hit = {
            "_source": {
                "@timestamp": "2026-07-21T00:10:00Z",
                "message": "required config PAYMENT_GATEWAY_TOKEN is missing",
                "event": {"action": "startup_validation"},
                "error_code": "CONFIG_MISSING",
                "missing_config": "PAYMENT_GATEWAY_TOKEN",
                "exit_code": 78,
                "memory": {"working_set_bytes": 73400320},
                "kubernetes": {
                    "namespace": "ns",
                    "pod": {"name": "pod", "uid": "uid-1"},
                    "container": {"name": "api"},
                },
            }
        }
        calls = []

        def fake_query(body):
            calls.append(body)
            return {"hits": {"hits": [hit]}}

        requested = [
            "event",
            "error_code",
            "missing_config",
            "exit_code",
            "memory",
        ]
        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(logs, "_query_elasticsearch", side_effect=fake_query),
        ):
            result = logs.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "read optional structured fields and decisive message",
                    "fields": requested,
                    "allow_name_fallback": False,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "query_succeeded")
        self.assertEqual(len(calls), 1)
        for field in requested:
            self.assertIn(field, calls[0]["_source"])
        self.assertIn("message", calls[0]["_source"])
        self.assertEqual(
            result["samples"][0]["message"],
            "required config PAYMENT_GATEWAY_TOKEN is missing",
        )
        self.assertEqual(
            result["samples"][0]["fields"]["error_code"],
            "CONFIG_MISSING",
        )
        self.assertEqual(
            result["samples"][0]["fields"]["missing_config"],
            "PAYMENT_GATEWAY_TOKEN",
        )

    def test_execute_falls_back_to_name_and_deduplicates_messages(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        empty = {"hits": {"hits": []}}
        hit = {
            "_source": {
                "@timestamp": "2026-07-21T00:10:00Z",
                "message": "required config PAYMENT_GATEWAY_TOKEN is missing",
                "log": {"level": "error"},
                "kubernetes": {
                    "namespace": "ns",
                    "pod": {"name": "pod"},
                    "container": {"name": "api"},
                },
            }
        }
        calls = []

        def fake_query(body):
            calls.append(body)
            return empty if len(calls) == 1 else {"hits": {"hits": [hit, hit]}}

        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(logs, "_query_elasticsearch", side_effect=fake_query),
        ):
            result = logs.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "find the exact configuration validation failure",
                    "keywords": ["required config", "missing"],
                    "keyword_mode": "all",
                    "window_minutes": 30,
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["query"]["identity_basis"], "name_time_fallback")
        self.assertEqual(result["query"]["uid_query_hits"], 0)
        self.assertEqual(result["query"]["dsl"], calls[-1])
        self.assertEqual(result["coverage"], "present")
        self.assertEqual(len(result["samples"]), 1)
        self.assertEqual(
            result["samples"][0]["message"],
            "required config PAYMENT_GATEWAY_TOKEN is missing",
        )
        self.assertEqual(result["samples"][0]["directness"], "related_context")
        self.assertEqual(result["facts"][0]["directness"], "related_context")
        self.assertEqual(result["directness"], "related_context")

    def test_name_fallback_only_marks_current_uid_records_direct(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        empty = {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}
        old_hit = {
            "_source": {
                "@timestamp": "2026-07-21T00:10:00Z",
                "message": "old pod incarnation",
                "kubernetes": {
                    "namespace": "ns",
                    "pod": {"name": "pod", "uid": "uid-old"},
                    "container": {"name": "api"},
                },
            }
        }
        current_hit = {
            "_source": {
                "@timestamp": "2026-07-21T00:11:00Z",
                "message": "current pod incarnation",
                "kubernetes": {
                    "namespace": "ns",
                    "pod": {"name": "pod", "uid": "uid-1"},
                    "container": {"name": "api"},
                },
            }
        }
        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(
                logs,
                "_query_elasticsearch",
                side_effect=[
                    empty,
                    {
                        "hits": {
                            "total": {"value": 2, "relation": "eq"},
                            "hits": [old_hit, current_hit],
                        }
                    },
                ],
            ),
        ):
            result = logs.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "separate old and current pod logs",
                }
            )

        by_message = {
            sample["message"]: sample
            for sample in result["samples"]
        }
        self.assertEqual(
            by_message["old pod incarnation"]["directness"],
            "related_context",
        )
        self.assertEqual(
            by_message["current pod incarnation"]["directness"],
            "direct",
        )

    def test_old_uid_hit_is_not_published_in_current_pod_fact_ledger(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-current",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        old_hit = {
            "_index": "filebeat-2026.07.21",
            "_id": "old-pod-log",
            "_source": {
                "@timestamp": "2026-07-21T00:10:00Z",
                "message": "log from an old pod incarnation",
                "kubernetes": {
                    "namespace": "ns",
                    "pod": {"name": "pod", "uid": "uid-old"},
                    "container": {"name": "api"},
                },
            },
        }
        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(
                logs,
                "_query_elasticsearch",
                return_value={"hits": {"hits": [old_hit]}},
            ),
        ):
            result = logs.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "exclude old pod lifecycle evidence",
                    "allow_name_fallback": False,
                }
            )

        self.assertEqual(result["samples"][0]["pod_uid"], "uid-old")
        self.assertEqual(result["facts"][0].get("pod_uid"), "uid-old")
        published = json.loads(common.dump_bounded_result(result))
        self.assertEqual(
            published["fact_ledger"]["scope_entity_ids"],
            ["k8s.pod:ns/pod:uid-current"],
        )
        self.assertEqual(published["fact_ledger"]["records"], [])

    def test_custom_field_request_does_not_remove_log_message(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        hit = {
            "_source": {
                "@timestamp": "2026-07-21T00:10:00Z",
                "message": "required config PAYMENT_GATEWAY_TOKEN is missing",
                "severity": "ERROR",
                "kubernetes": {
                    "namespace": "ns",
                    "pod": {"name": "pod", "uid": "uid-1"},
                    "container": {"name": "api"},
                },
            }
        }
        calls = []

        def fake_query(body):
            calls.append(body)
            return {"hits": {"hits": [hit]}}

        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(logs, "_query_elasticsearch", side_effect=fake_query),
        ):
            result = logs.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "read the decisive startup error",
                    "fields": ["severity"],
                }
            )

        self.assertIn("message", calls[0]["_source"])
        self.assertEqual(
            result["samples"][0]["message"],
            "required config PAYMENT_GATEWAY_TOKEN is missing",
        )
        self.assertEqual(result["query"]["dsl"], calls[0])
        self.assertEqual(result["samples"][0]["fields"]["severity"], "ERROR")

    def test_window_is_clamped_to_target_pod_creation_time(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "creation_timestamp": "2026-07-21T00:10:00Z",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        calls = []

        def fake_query(body):
            calls.append(body)
            return {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}

        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(logs, "_query_elasticsearch", side_effect=fake_query),
        ):
            result = logs.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "avoid logs from an older pod incarnation",
                    "start": "2026-07-21T00:00:00Z",
                    "end": "2026-07-21T00:30:00Z",
                    "allow_name_fallback": False,
                }
            )

        time_filter = calls[0]["query"]["bool"]["filter"][0]["range"]["@timestamp"]
        self.assertEqual(time_filter["gte"], "2026-07-21T00:10:00Z")
        self.assertEqual(result["query"]["time_range"]["start"], "2026-07-21T00:10:00Z")

    def test_revalidates_uid_after_backend_query(self):
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
            patch.object(
                logs,
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
            patch.object(
                logs,
                "_query_elasticsearch",
                return_value={"hits": {"hits": []}},
            ),
        ):
            result = logs.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "avoid mixing pod incarnations",
                    "allow_name_fallback": False,
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "pod_lifecycle_changed")
        self.assertTrue(result["entity"]["lifecycle_changed"])
        self.assertEqual(resolver.call_count, 2)

    def test_rejects_too_many_keywords(self):
        result = logs.execute(
            {
                "namespace": "ns",
                "pod": "pod",
                "purpose": "search",
                "keywords": [str(index) for index in range(9)],
            }
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "too_many_keywords")

    def test_rejects_oversized_generated_dsl_before_backend_execution(self):
        entity = {
            "kind": "Pod",
            "namespace": "n" * 63,
            "pod": "p" * 253,
            "pod_uid": "u" * 64,
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(logs, "_query_elasticsearch") as query_backend,
        ):
            result = logs.execute(
                {
                    "namespace": entity["namespace"],
                    "pod": entity["pod"],
                    "purpose": "verify whether selected log evidence changes the diagnosis",
                    "container": "api",
                    "keywords": [
                        f"keyword-{index}-" + ("k" * 108)
                        for index in range(8)
                    ],
                    "keyword_mode": "all",
                    "levels": [
                        f"level-{index}-" + ("l" * 24)
                        for index in range(6)
                    ],
                    "trace_id": "0123456789abcdef0123456789abcdef",
                    "max_records": 50,
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "query_metadata_too_large")
        self.assertFalse(result["query"]["preserved"])
        self.assertFalse(result["query"]["executed"])
        query_backend.assert_not_called()

    def test_compacts_prior_uid_query_metadata_before_name_fallback(self):
        entity = {
            "kind": "Pod",
            "namespace": "n" * 63,
            "pod": "p" * 253,
            "pod_uid": "u" * 64,
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        empty = {"hits": {"hits": []}}

        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(
                logs,
                "_query_elasticsearch",
                side_effect=[empty, empty],
            ) as query_backend,
        ):
            result = logs.execute(
                {
                    "namespace": entity["namespace"],
                    "pod": entity["pod"],
                    "purpose": "verify fallback query budgeting",
                    "container": "api",
                    "levels": ["l" * 32] * 6,
                    "trace_id": "0123456789abcdef0123456789abcdef",
                }
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "query_succeeded")
        self.assertEqual(result["coverage"], "empty")
        self.assertEqual(query_backend.call_count, 2)
        self.assertEqual(result["query"]["identity_basis"], "name_time_fallback")
        self.assertEqual(result["query"]["dsl"], query_backend.call_args_list[1].args[0])
        self.assertEqual(
            result["query"]["executions"][0]["identity_basis"],
            "pod_uid",
        )
        self.assertEqual(result["query"]["executions"][0]["hit_count"], 0)
        self.assertTrue(result["query"]["executions"][0]["query"]["executed"])
        self.assertFalse(result["query"]["executions"][0]["query"]["preserved"])
        self.assertIn("sha256", result["query"]["executions"][0]["query"])
        self.assertNotIn("dsl", result["query"]["executions"][0])
        self.assertEqual(
            result["query"]["executions"][1]["identity_basis"],
            "name_time_fallback",
        )
        self.assertEqual(
            result["query"]["executions"][1]["dsl_ref"],
            "query.dsl",
        )

    def test_elasticsearch_backend_error_preserves_attempted_dsl(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        calls = []

        def fail_query(body):
            calls.append(body)
            return {"error": "connection refused"}

        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(logs, "_query_elasticsearch", side_effect=fail_query),
        ):
            result = logs.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "preserve the failed Elasticsearch query",
                    "keywords": ["required config"],
                }
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "elasticsearch_error")
        self.assertEqual(result["query"]["dsl"], calls[0])
        self.assertEqual(
            result["query"]["executions"][0]["identity_basis"],
            "pod_uid",
        )
        self.assertEqual(
            result["query"]["executions"][0]["error_ref"],
            "error.message",
        )
        self.assertEqual(result["error"]["message"], "connection refused")

    def test_long_initial_backend_error_keeps_dsl_through_mcp_budget(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        calls = []

        def fail_query(body):
            calls.append(body)
            return {"error": "x" * 7000}

        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(logs, "_query_elasticsearch", side_effect=fail_query),
        ):
            raw = aiops_observability_query.call_tool(
                "query_pod_logs",
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "preserve the initial failed query under budget",
                    "keywords": ["fatal"],
                },
            )

        result = __import__("json").loads(raw)
        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "elasticsearch_error")
        self.assertEqual(result["query"]["dsl"], calls[0])
        self.assertEqual(
            result["query"]["executions"][0]["error_ref"],
            "error.message",
        )

    def test_long_fallback_backend_error_keeps_query_chain_through_mcp_budget(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        calls = []

        def fallback_failure(body):
            calls.append(body)
            if len(calls) == 1:
                return {"hits": {"hits": []}}
            return {"error": "x" * 7000}

        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(
                logs,
                "_query_elasticsearch",
                side_effect=fallback_failure,
            ),
        ):
            raw = aiops_observability_query.call_tool(
                "query_pod_logs",
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "preserve the fallback query chain under budget",
                    "keywords": ["fatal"],
                },
            )

        result = __import__("json").loads(raw)
        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "elasticsearch_error")
        self.assertEqual(result["query"]["dsl"], calls[1])
        self.assertEqual(
            result["query"]["executions"][-1]["error_ref"],
            "error.message",
        )

    def test_preserves_full_log_message_and_immutable_raw_reference_before_compaction(self):
        message = "decisive-prefix " + ("x" * 1800) + " decisive-suffix"
        hit = {
            "_index": "filebeat-2026.07.21",
            "_id": "doc-123",
            "_source": {
                "@timestamp": "2026-07-21T00:10:00Z",
                "message": message,
                "kubernetes": {
                    "namespace": "ns",
                    "pod": {"name": "pod", "uid": "uid-1"},
                    "container": {"name": "api"},
                },
            },
        }

        records = logs._dedupe_records([hit], requested_fields=[])

        self.assertEqual(records[0]["message"], message)
        self.assertEqual(records[0]["message_original_length"], len(message))
        self.assertEqual(
            records[0]["raw_ref"],
            {
                "source_system": "elasticsearch",
                "index": "filebeat-2026.07.21",
                "document_id": "doc-123",
            },
        )

    def test_bounded_mcp_result_marks_log_excerpt_and_keeps_raw_reference(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        message = "decisive-prefix " + ("x" * 7000) + " decisive-suffix"
        hit = {
            "_index": "filebeat-2026.07.21",
            "_id": "doc-long",
            "_source": {
                "@timestamp": "2026-07-21T00:10:00Z",
                "message": message,
                "kubernetes": {
                    "namespace": "ns",
                    "pod": {"name": "pod", "uid": "uid-1"},
                    "container": {"name": "api"},
                },
            },
        }

        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(
                logs,
                "_query_elasticsearch",
                return_value={"hits": {"hits": [hit]}},
            ),
        ):
            raw = aiops_observability_query.call_tool(
                "query_pod_logs",
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "preserve decisive log evidence within the model budget",
                },
            )

        result = __import__("json").loads(raw)
        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        sample = result["samples"][0]
        fact = result["facts"][0]
        self.assertTrue(sample["message_truncated"])
        self.assertEqual(sample["message_original_length"], len(message))
        self.assertEqual(sample["raw_ref"]["document_id"], "doc-long")
        self.assertTrue(fact["value_truncated"])
        self.assertEqual(fact["value_original_length"], len(message))
        self.assertEqual(fact["raw_ref"]["document_id"], "doc-long")

    def test_backend_total_and_limit_signal_drive_truncated(self):
        entity = {
            "kind": "Pod",
            "namespace": "ns",
            "pod": "pod",
            "pod_uid": "uid-1",
            "pod_ip": "10.0.0.2",
            "node": "node1",
            "containers": ["api"],
        }
        hit = {
            "_source": {
                "@timestamp": "2026-07-21T00:10:00Z",
                "message": "one retained record",
                "kubernetes": {
                    "namespace": "ns",
                    "pod": {"name": "pod", "uid": "uid-1"},
                },
            }
        }
        calls = []

        def fake_query(body):
            calls.append(body)
            return {
                "hits": {
                    "total": {"value": 100, "relation": "gte"},
                    "hits": [hit],
                }
            }

        with (
            patch.object(logs, "resolve_pod_scope", return_value=(entity, None)),
            patch.object(logs, "_query_elasticsearch", side_effect=fake_query),
        ):
            result = logs.execute(
                {
                    "namespace": "ns",
                    "pod": "pod",
                    "purpose": "report incomplete backend coverage",
                    "max_records": 20,
                }
            )

        self.assertEqual(calls[0]["size"], logs.MAX_BACKEND_RECORDS)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["query"]["backend_total"]["value"], 100)
        self.assertEqual(result["query"]["backend_total"]["relation"], "gte")


if __name__ == "__main__":
    unittest.main()
