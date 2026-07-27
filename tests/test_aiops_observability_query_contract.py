import asyncio
import json
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

_SERVERS_DIR = pathlib.Path(__file__).resolve().parents[1] / "servers"
if str(_SERVERS_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVERS_DIR))

from servers import aiops_observability_query_server
from servers.aiops_observability.query.common import dump_bounded_result
from servers.holmes_tools import aiops_observability_query


class AutonomousObservabilityContractTests(unittest.TestCase):
    def test_registers_exactly_four_generic_tools_without_scenario(self):
        self.assertEqual(
            [tool.name for tool in aiops_observability_query.TOOLS],
            [
                "execute_pod_promql",
                "query_pod_logs",
                "query_pod_tracing",
                "query_pod_topology",
            ],
        )
        for tool in aiops_observability_query.TOOLS:
            schema = tool.inputSchema
            self.assertIn("namespace", schema["required"])
            self.assertIn("pod", schema["required"])
            self.assertIn("purpose", schema["required"])
            self.assertNotIn("scenario", schema["properties"])
            self.assertIn("model", tool.description.lower())
            self.assertIn("pod", tool.description.lower())

    def test_unknown_tool_returns_none(self):
        self.assertIsNone(aiops_observability_query.call_tool("unknown", {}))

    def test_tool_schemas_match_approved_defaults_and_string_bounds(self):
        tools = {
            tool.name: tool.inputSchema
            for tool in aiops_observability_query.TOOLS
        }

        for schema in tools.values():
            properties = schema["properties"]
            self.assertEqual(properties["namespace"]["minLength"], 1)
            self.assertEqual(properties["pod"]["minLength"], 1)
            self.assertEqual(properties["purpose"]["minLength"], 1)

        metrics = tools["execute_pod_promql"]["properties"]
        self.assertEqual(metrics["promql"]["minLength"], 1)
        self.assertEqual(metrics["window_minutes"]["default"], 30)
        self.assertEqual(metrics["step"]["default"], "30s")

        logging = tools["query_pod_logs"]["properties"]
        self.assertEqual(logging["window_minutes"]["default"], 30)
        self.assertEqual(logging["keyword_mode"]["default"], "any")
        self.assertEqual(logging["max_records"]["default"], 20)
        self.assertIs(logging["allow_name_fallback"]["default"], True)
        self.assertEqual(logging["fields"]["items"]["maxLength"], 128)
        self.assertEqual(logging["levels"]["items"]["maxLength"], 32)
        self.assertEqual(logging["trace_id"]["maxLength"], 32)

        tracing = tools["query_pod_tracing"]["properties"]
        self.assertEqual(tracing["window_minutes"]["default"], 30)
        self.assertEqual(tracing["direction"]["default"], "any")
        self.assertIs(tracing["include_tempo"]["default"], True)
        self.assertEqual(tracing["max_records"]["default"], 20)
        self.assertEqual(tracing["protocols"]["items"]["maxLength"], 32)
        self.assertEqual(tracing["response_statuses"]["items"]["maxLength"], 32)
        self.assertEqual(tracing["trace_id"]["maxLength"], 32)

        topology = tools["query_pod_topology"]["properties"]
        self.assertEqual(topology["window_minutes"]["default"], 30)

    def test_bounded_result_preserves_contract_and_stays_under_six_kib(self):
        payload = {
            "ok": True,
            "status": "query_succeeded",
            "source_system": "elasticsearch",
            "dimension": "logging",
            "entity": {"kind": "Pod", "namespace": "ns", "pod": "pod"},
            "purpose": "find the decisive startup error",
            "coverage": "present",
            "directness": "direct",
            "query": {"index": "filebeat-*", "identity_basis": "pod_uid"},
            "facts": [
                {
                    "ref": f"log-{index}",
                    "name": "log.message",
                    "value": "required config PAYMENT_GATEWAY_TOKEN is missing " + ("x" * 400),
                }
                for index in range(20)
            ],
            "samples": [
                {
                    "timestamp": f"2026-07-21T00:00:{index:02d}Z",
                    "message": "sample " + ("y" * 900),
                }
                for index in range(20)
            ],
            "evidence_refs": ["log-0"],
            "truncated": False,
            "limits": {"max_serialized_bytes": 6144},
        }

        raw = dump_bounded_result(payload)
        result = json.loads(raw)

        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        for key in (
            "entity",
            "purpose",
            "coverage",
            "query",
            "facts",
            "samples",
            "evidence_refs",
        ):
            self.assertIn(key, result)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["facts"])
        self.assertTrue(result["samples"])

    def test_bounded_result_never_changes_query_and_removes_dangling_refs(self):
        sql = "SELECT " + ("very_long_column_name," * 90) + "trace_id FROM flow_log"
        facts = [
            {
                "ref": f"trace-{index}",
                "name": "l7_flow",
                "value": "fact " + ("x" * 1800),
            }
            for index in range(12)
        ]
        payload = {
            "ok": True,
            "status": "query_succeeded",
            "source_system": "deepflow+tempo",
            "dimension": "tracing",
            "entity": {"kind": "Pod", "namespace": "ns", "pod": "pod"},
            "purpose": "preserve the exact executed query",
            "coverage": "present",
            "directness": "direct",
            "query": {
                "sql": sql,
                "time_range": {
                    "start": "2026-07-21T00:00:00Z",
                    "end": "2026-07-21T00:30:00Z",
                },
            },
            "facts": facts,
            "samples": [
                {"ref": fact["ref"], "message": "sample " + ("y" * 1800)}
                for fact in facts
            ],
            "flows": [
                {"ref": fact["ref"], "request_resource": "/resource/" + ("z" * 1200)}
                for fact in facts
            ],
            "spans": [],
            "correlations": [],
            "evidence_refs": [fact["ref"] for fact in facts],
            "truncated": False,
            "limits": {},
        }

        raw = dump_bounded_result(payload)
        result = json.loads(raw)

        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertEqual(result["query"], payload["query"])
        self.assertEqual(
            result["evidence_refs"],
            [fact["ref"] for fact in result["facts"]],
        )

    def test_bounded_result_keeps_both_facts_for_retained_correlation(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        correlated_facts = [
            {
                "ref": "deepflow-correlated",
                "source_system": "deepflow",
                "trace_id": trace_id,
                "name": "l7_flow",
                "value": "flow " + ("f" * 1800),
            },
            {
                "ref": "tempo-correlated",
                "source_system": "tempo",
                "trace_id": trace_id,
                "name": "application_span",
                "value": "span " + ("s" * 1800),
            },
        ]
        payload = {
            "ok": True,
            "status": "query_succeeded",
            "source_system": "deepflow+tempo",
            "dimension": "tracing",
            "entity": {"kind": "Pod", "namespace": "ns", "pod": "pod"},
            "purpose": "retain the evidence behind exact trace correlation",
            "coverage": "present",
            "directness": "direct",
            "query": {"sql": "SELECT trace_id FROM flow_log"},
            "facts": correlated_facts
            + [
                {
                    "ref": f"unrelated-{index}",
                    "source_system": "deepflow",
                    "trace_id": f"{index:032x}",
                    "name": "l7_flow",
                    "value": "unrelated " + ("x" * 1800),
                }
                for index in range(10)
            ],
            "samples": [{"message": "sample " + ("y" * 1800)}],
            "flows": [{"trace_id": trace_id, "payload": "z" * 1800}],
            "spans": [{"trace_id": trace_id, "payload": "w" * 1800}],
            "correlations": [
                {
                    "trace_id": trace_id,
                    "flow_count": 1,
                    "span_count": 1,
                    "basis": "exact_trace_id",
                }
            ],
            "evidence_refs": [
                fact["ref"]
                for fact in correlated_facts
            ],
            "truncated": False,
            "limits": {},
        }

        result = json.loads(dump_bounded_result(payload))

        retained = result["correlations"][0]["trace_id"]
        sources = {
            fact["source_system"]
            for fact in result["facts"]
            if fact.get("trace_id") == retained
        }
        self.assertEqual(sources, {"deepflow", "tempo"})

    def test_oversized_input_is_rejected_before_handler_and_response_is_bounded(self):
        handler = Mock(side_effect=AssertionError("backend handler must not run"))
        arguments = {
            "namespace": "ns",
            "pod": "pod",
            "purpose": "inspect selected log fields",
            "fields": ["field_" + ("x" * 7000)],
        }

        with patch.dict(
            aiops_observability_query._HANDLERS,
            {"query_pod_logs": handler},
        ):
            raw = aiops_observability_query.call_tool("query_pod_logs", arguments)

        self.assertIsNotNone(raw)
        result = json.loads(raw)
        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "input_budget_exceeded")
        self.assertFalse(result["query"]["preserved"])
        self.assertFalse(result["query"]["executed"])
        self.assertEqual(result["query"]["serialized_bytes"], len(json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")))
        handler.assert_not_called()

    def test_fail_safe_bounds_query_metadata_that_alone_exceeds_result_budget(self):
        query = {"dsl": {"_source": ["field_" + ("x" * 7000)]}}
        payload = {
            "ok": False,
            "status": "query_rejected",
            "source_system": "elasticsearch",
            "dimension": "logging",
            "entity": {"kind": "Pod", "namespace": "ns", "pod": "pod"},
            "purpose": "verify the hard result budget",
            "coverage": "error",
            "directness": "direct",
            "query": query,
            "facts": [],
            "samples": [],
            "evidence_refs": [],
            "truncated": False,
            "limits": {},
            "error": {
                "code": "result_budget_exceeded",
                "message": "query metadata is too large",
            },
        }

        raw = dump_bounded_result(payload)
        result = json.loads(raw)

        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertFalse(result["query"]["preserved"])
        self.assertEqual(
            result["query"]["serialized_bytes"],
            len(json.dumps(
                query,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")),
        )
        self.assertNotIn("x" * 100, raw)

    def test_real_mcp_entrypoint_preserves_structured_array_filters(self):
        arguments = {
            "namespace": "ns",
            "pod": "pod",
            "purpose": "verify selected structured filters",
            "keywords": ["required config", "missing"],
            "levels": ["error", "fatal"],
            "protocols": ["HTTP", "HTTP2"],
            "response_codes": [500, 503],
        }

        with (
            patch.object(
                aiops_observability_query_server.aiops_observability_query,
                "call_tool",
                return_value='{"ok":true}',
            ) as handler,
            patch.object(aiops_observability_query_server, "log_tool_call"),
            patch.object(aiops_observability_query_server, "log_tool_result"),
        ):
            asyncio.run(
                aiops_observability_query_server.call_tool(
                    "query_pod_logs",
                    arguments,
                )
            )

        self.assertEqual(handler.call_args.args[1], arguments)

    def test_real_mcp_entrypoint_returns_bounded_structured_internal_error(self):
        arguments = {
            "namespace": "ns",
            "pod": "pod",
            "purpose": "exercise the server error boundary",
        }

        with (
            patch.object(
                aiops_observability_query_server.aiops_observability_query,
                "call_tool",
                side_effect=RuntimeError("backend failed " + ("x" * 7000)),
            ),
            patch.object(aiops_observability_query_server, "log_tool_call"),
            patch.object(aiops_observability_query_server, "log_tool_result"),
        ):
            content = asyncio.run(
                aiops_observability_query_server.call_tool(
                    "query_pod_logs",
                    arguments,
                )
            )

        raw = content[0].text
        result = json.loads(raw)
        self.assertLessEqual(len(raw.encode("utf-8")), 6144)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "internal_error")


if __name__ == "__main__":
    unittest.main()
