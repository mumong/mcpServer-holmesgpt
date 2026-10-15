"""Additive lifecycle API and generic record operations, including MCP JSON arguments."""
import asyncio
import copy
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers"))
import k8s_core_server
from holmes_tools import pod_lifecycle
from holmes_tools.record_query import select_records, validate_query


def fixture():
    return {"kind": "PodList", "metadata": {"resourceVersion": "v1", "continue": "next", "remainingItemCount": 7},
            "items": [{"metadata": {"namespace": "test", "name": "p", "uid": "u"},
                       "spec": {"nodeName": "n", "containers": [{"name": "app"}, {"name": "sidecar"}]},
                       "status": {"phase": "Running", "containerStatuses": [
                           {"name": "app", "ready": True, "restartCount": 7,
                            "state": {"running": {}}, "lastState": {"terminated": {
                                "reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-10-09T00:09:00Z"}}},
                           {"name": "sidecar", "ready": False, "restartCount": 0,
                            "state": {"waiting": {}}, "lastState": {}},
                       ]}}]}


class RecordQueryTests(unittest.TestCase):
    def call(self, args, payload=None):
        with patch.object(pod_lifecycle.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(payload or fixture()), stderr="")):
            return json.loads(pod_lifecycle.query(args))

    def test_old_call_still_full_lifecycle(self):
        result = self.call({})
        self.assertEqual(result["contract_version"], "pod-lifecycle.v1")
        self.assertIn("pods", result)
        self.assertNotIn("rows", result)

    def test_exact_group_count_and_page_scope(self):
        result = self.call({"record_type": "pod", "group_by": "node"})
        self.assertEqual(result["contract_version"], "evidence-records.v1")
        self.assertEqual(result["groups"], [{"node": "n", "count": 1}])
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["source_metadata"]["next_continue_token"], "next")
        self.assertEqual(result["source_metadata"]["remaining_item_count"], 7)

    def test_filter_same_container_preserves_recovered_facts(self):
        args = {"record_type": "container", "where": [
            {"field": "last_termination_reason", "op": "eq", "value": "OOMKilled"},
            {"field": "ready", "op": "eq", "value": True}]}
        result = self.call(args)
        self.assertEqual(result["matched_rows"], 1)
        self.assertEqual(result["rows"][0]["container"], "app")
        self.assertEqual(result["rows"][0]["uid"], "u")
        self.assertEqual(result["rows"][0]["last_exit_code"], 137)
        self.assertIsNone(result["rows"][0]["restart_count_in_window"])
        args["where"][1]["value"] = False
        result = self.call(args)
        self.assertEqual(result["matched_rows"], 0)
        self.assertEqual(result["unknown_filter_rows"], 1)
        self.assertEqual(result["status"], "success")

    def test_mcp_entry_preserves_lists_and_nested_null(self):
        args = {"record_type": "container", "fields": ["ready"],
                "where": [{"field": "last_termination_reason", "op": "ne", "value": None}]}
        original = copy.deepcopy(args)
        with patch.object(k8s_core_server.kubernetes_core, "call_tool", return_value="ok") as call, \
                patch.object(k8s_core_server, "log_tool_call"), patch.object(k8s_core_server, "log_tool_result"):
            asyncio.run(k8s_core_server.call_tool("query_pod_lifecycle", args))
            call.assert_called_once_with("query_pod_lifecycle", args)
            self.assertEqual(args, original)

    def test_invalid_query_fails_before_collection(self):
        invalid = [{"fields": []}, {"where": "bad"}, {"fields": ["nonexistent"]},
                   {"group_by": "state"}, {"record_type": "other"},
                   {"where": [{"field": "node", "op": "gt", "value": "x"}]},
                   {"where": [{"field": "node", "op": "eq", "value": float("nan")}]},
                   {"match": "xor"}]
        for args in invalid:
            with self.subTest(args=args), patch.object(pod_lifecycle.subprocess, "run") as run:
                result = json.loads(pod_lifecycle.query(args))
                self.assertEqual(result["status"], "error")
                run.assert_not_called()

    def test_disable_is_explicit_old_call_available(self):
        with patch.dict(os.environ, {"AIOPS_RECORD_QUERY_ENABLED": "false"}):
            self.assertEqual(self.call({})["contract_version"], "pod-lifecycle.v1")
            with patch.object(pod_lifecycle.subprocess, "run") as run:
                self.assertEqual(json.loads(pod_lifecycle.query({"group_by": "node"}))["status"], "error")
                run.assert_not_called()

    def test_empty_and_source_error_not_conflated(self):
        empty = {"kind": "PodList", "items": [], "metadata": {}}
        self.assertEqual(self.call({"group_by": "node"}, empty)["groups"], [])
        with patch.object(pod_lifecycle.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="denied")):
            result = json.loads(pod_lifecycle.query({"record_type": "pod"}))
        self.assertEqual(result["status"], "error")
        self.assertNotIn("matched_rows", result)

    def test_overflow_no_silent_prefix(self):
        with patch.dict(os.environ, {"AIOPS_RECORD_RESULT_MAX_BYTES": "1024"}):
            result = self.call({"record_type": "container"})
        self.assertEqual(result["status"], "error")
        self.assertIn("record_too_large", result["error"])
        self.assertNotIn("rows", result)

    def test_generic_numeric_zero_false_null_and_late_match(self):
        rows = [{"id": i, "value": i} for i in range(100)] + [{"id": 100, "value": None}, {"id": 101, "value": False}]
        query = validate_query({"where": [{"field": "value", "op": "eq", "value": 0}]}, ["id", "value"], ["id", "value"])
        result = select_records(rows, query)
        self.assertEqual(result["rows"], [{"id": 0, "value": 0}])
        self.assertEqual(result["unknown_filter_rows"], 1)
        query = validate_query({"where": [{"field": "id", "op": "gte", "value": 99}]}, ["id", "value"], ["id", "value"])
        self.assertEqual(len(select_records(rows, query)["rows"]), 3)

    def test_all_container_roles_keep_identity(self):
        payload = fixture()
        payload["items"][0]["spec"]["initContainers"] = [{"name": "setup"}]
        result = self.call({"record_type": "container", "fields": ["ready"]}, payload)
        self.assertEqual([r["role"] for r in result["rows"]], ["app", "app", "init"])
        self.assertIsNone(result["rows"][-1]["ready"])
