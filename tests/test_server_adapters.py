"""Characterize the existing MCP wire contracts before sharing adapters.

The digests cover the ordered MCP wire serialization (including
descriptions and schemas), using the SDK wire settings by_alias=True and
exclude_none=True. Optional unset SDK fields are not transmitted. Update
the digests only for an intentional API change.
Runbook's import-time directory listing is fixed to the pre-refactor fixture;
adding documentation must not make a contract test depend on the checkout.
All handlers and logging boundaries are mocked; no tool command is executed.
"""

import asyncio
import hashlib
import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers"))

TOOL_DIGESTS = {
    "runbook": "511016a7b9be5cc6d5cac42afefb72963426d63407872ae78cf51d376f21d945",
    "internet": "c6e178d9ec7a1362081976e88c3206acb62f1414eb08e27f15a3064ac0d2063d",
    "connectivity": "dfcb5244181ab4c22ba61c7d7d9980e17e678c9ba868256759584e44f3c1804b",
    # Intentional lifecycle-only addition: compact state fields, Pod Ready,
    # continuation descriptions. test_pod_lifecycle pins every OTHER tool unchanged.
    "k8s_core": "1c9dcd5b2e56dc9ccdc1a073ced42df6991ead0fa4505a7fb12a065e9366d9fb",
    "core_investigation": "7f6de113428773e638b42f7c5edc295a4af2bd1d0d71d3740a7891dbd78fa3eb",
    # Full rule catalog deliberately disabled; all seven remaining schemas unchanged.
    "prometheus": "71b7e127e7db27b8309aeb8c36da53a9f9813dfa1032681544f136bdad6caefe",
    "aiops_case": "5cd12701f0e76879be0b2c2cae32394b5a443bd95f33bb71a170c94ff80867cc",
    "aiops_observability_fine": "458f3ba25d7b9456cc3cf86e9199961446b02d38fb7f265224ed63d309fa07a3",
}
CAUGHT_ERRORS = {"prometheus", "aiops_case", "aiops_observability_fine"}


class ServerAdapterTests(unittest.TestCase):
    def modules(self):
        for name in TOOL_DIGESTS:
            server = importlib.import_module(name + "_server")
            tool = getattr(server, "kubernetes_core" if name == "k8s_core" else name)
            if name == "runbook":
                with (
                    patch.object(
                        tool.os, "listdir", return_value=["CLAUDE.md", "README-aiops.md", "README.md"]
                    ),
                    patch.dict(tool.os.environ, {"RUNBOOK_SEARCH_PATH": ""}),
                ):
                    importlib.reload(tool)
            yield name, server, tool

    def test_runbook_examples_keep_dynamic_sorted_unique_markdown_names(self):
        server = importlib.import_module("runbook_server")
        with (
            patch.object(server.runbook, "_get_search_paths", return_value=["/fixture/one", "/fixture/two"]),
            patch.object(
                server.runbook.os, "listdir", side_effect=[["z.md", "ignore.txt", "a.md"], ["a.md", "b.md"]]
            ),
        ):
            self.assertEqual(server.runbook._runbook_list_for_description(), "a.md, b.md, z.md")

    def test_ordered_tools_descriptions_and_schemas_are_unchanged(self):
        for name, server, _ in self.modules():
            with self.subTest(server=name):
                tools = asyncio.run(server.list_tools())
                serialized = json.dumps(
                    [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.assertEqual(hashlib.sha256(serialized.encode()).hexdigest(), TOOL_DIGESTS[name])

    def test_success_preserves_text_and_parameter_policy(self):
        arguments = {"none": None, "number": 1, "flag": False, "text": "x", "todos": [{"id": "1"}]}
        for name, server, tool in self.modules():
            with (
                self.subTest(server=name),
                patch.object(tool, "call_tool", return_value=" exact\n输出 ") as handler,
                patch.object(server, "log_tool_call") as started,
                patch.object(server, "log_tool_result") as ended,
            ):
                result = asyncio.run(server.call_tool("example", arguments))
                self.assertEqual(
                    [item.model_dump() for item in result],
                    [{"type": "text", "text": " exact\n输出 ", "annotations": None, "meta": None}],
                )
                expected = (
                    arguments if name == "core_investigation" else {**arguments, "none": "", "todos": ""}
                )
                handler.assert_called_once_with("example", expected)
                started.assert_called_once_with(server._SERVER, "example", arguments)
                self.assertEqual(ended.call_args.args[:3], (server._SERVER, "example", " exact\n输出 "))
                self.assertEqual(arguments["todos"], [{"id": "1"}])

    def test_unknown_tool_text_and_error_logging_policy(self):
        for name, server, tool in self.modules():
            with (
                self.subTest(server=name),
                patch.object(tool, "call_tool", return_value=None),
                patch.object(server, "log_tool_call"),
                patch.object(server, "log_tool_result") as ended,
            ):
                self.assertEqual(asyncio.run(server.call_tool("missing", {}))[0].text, "未知工具: missing")
                error = ended.call_args.kwargs.get("error")
                if name in CAUGHT_ERRORS:
                    self.assertIsInstance(error, ValueError)
                    self.assertEqual(str(error), "unknown tool")
                else:
                    self.assertIsNone(error)

    def test_exception_conversion_is_not_applied_to_propagating_servers(self):
        for name, server, tool in self.modules():
            error = RuntimeError("fixture failure")
            with (
                self.subTest(server=name),
                patch.object(tool, "call_tool", side_effect=error),
                patch.object(server, "log_tool_call"),
                patch.object(server, "log_tool_result") as ended,
            ):
                if name in CAUGHT_ERRORS:
                    self.assertEqual(
                        asyncio.run(server.call_tool("example", {}))[0].text, "工具调用异常: fixture failure"
                    )
                    self.assertIs(ended.call_args.kwargs["error"], error)
                else:
                    with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                        asyncio.run(server.call_tool("example", {}))
                    ended.assert_not_called()

    def test_empty_text_is_not_unknown_tool(self):
        for name, server, tool in self.modules():
            with (
                self.subTest(server=name),
                patch.object(tool, "call_tool", return_value=""),
                patch.object(server, "log_tool_call"),
                patch.object(server, "log_tool_result"),
            ):
                self.assertEqual(asyncio.run(server.call_tool("example", None))[0].text, "")


class AdapterLoggingOrderTests(unittest.TestCase):
    """Logging errors keep the same try/except boundary as legacy entrypoints."""

    def setUp(self):
        self.adapter = importlib.import_module("holmes_tools.server_adapter")

    def test_call_clock_handler_clock_result_order(self):
        for catch_errors in (False, True):
            for output in ("success", None):
                with self.subTest(catch_errors=catch_errors, output=output):
                    events = []
                    ticks = iter((10.0, 12.5))

                    def clock(events=events, ticks=ticks):
                        events.append("clock")
                        return next(ticks)

                    def handler(name, arguments, events=events, output=output):
                        events.append("handler")
                        return output

                    def log_result(*args, events=events, catch_errors=catch_errors, output=output, **kwargs):
                        events.append("result")
                        self.assertEqual(args[3], 2.5)
                        self.assertEqual("error" in kwargs, catch_errors and output is None)

                    with patch.object(self.adapter.time, "monotonic", clock):
                        self.adapter.call_text_tool(
                            "fixture",
                            "example",
                            {},
                            handler,
                            log_call=lambda *args, events=events: events.append("call"),
                            log_result=log_result,
                            catch_errors=catch_errors,
                        )
                    self.assertEqual(events, ["call", "clock", "handler", "clock", "result"])

    def test_initial_log_failure_is_outside_catch_boundary(self):
        for catch_errors in (False, True):
            with self.subTest(catch_errors=catch_errors):
                handler, ended = Mock(), Mock()
                with patch.object(self.adapter.time, "monotonic") as clock:
                    with self.assertRaisesRegex(RuntimeError, "initial logging"):
                        self.adapter.call_text_tool(
                            "fixture",
                            "example",
                            {},
                            handler,
                            log_call=Mock(side_effect=RuntimeError("initial logging")),
                            log_result=ended,
                            catch_errors=catch_errors,
                        )
                clock.assert_not_called()
                handler.assert_not_called()
                ended.assert_not_called()

    def test_result_log_failure_is_converted_only_by_catching_policy(self):
        for catch_errors in (False, True):
            with self.subTest(catch_errors=catch_errors):
                error = RuntimeError("result logging")
                ended = Mock(side_effect=[error, None])
                with patch.object(self.adapter.time, "monotonic", side_effect=[10, 12, 15]):
                    if catch_errors:
                        result = self.adapter.call_text_tool(
                            "fixture",
                            "example",
                            {},
                            Mock(return_value="success"),
                            log_call=Mock(),
                            log_result=ended,
                            catch_errors=True,
                        )
                        self.assertEqual(result[0].text, "工具调用异常: result logging")
                        self.assertEqual(ended.call_count, 2)
                        self.assertEqual(ended.call_args.args, ("fixture", "example", None, 5))
                        self.assertIs(ended.call_args.kwargs["error"], error)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "result logging"):
                            self.adapter.call_text_tool(
                                "fixture",
                                "example",
                                {},
                                Mock(return_value="success"),
                                log_call=Mock(),
                                log_result=ended,
                                catch_errors=False,
                            )
                        self.assertEqual(ended.call_count, 1)

    def test_error_logging_failure_propagates_without_recursive_logging(self):
        ended = Mock(side_effect=RuntimeError("error logging"))
        with self.assertRaisesRegex(RuntimeError, "error logging"):
            self.adapter.call_text_tool(
                "fixture",
                "example",
                {},
                Mock(side_effect=ValueError("handler failure")),
                log_call=Mock(),
                log_result=ended,
                catch_errors=True,
            )
        self.assertEqual(ended.call_count, 1)
        self.assertEqual(str(ended.call_args.kwargs["error"]), "handler failure")

    def test_threaded_adapter_runs_handler_off_the_event_loop_thread(self):
        import threading

        seen = []

        def handler(name, arguments):
            seen.append(threading.get_ident())
            return "ok"

        result = asyncio.run(
            self.adapter.call_text_tool_in_thread(
                "fixture",
                "example",
                {},
                handler,
                log_call=Mock(),
                log_result=Mock(),
            )
        )
        self.assertEqual(result[0].text, "ok")
        self.assertNotEqual(seen, [threading.get_ident()])
