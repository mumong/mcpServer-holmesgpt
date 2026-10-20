"""Additive lifecycle API and generic record operations, including MCP JSON arguments."""

import asyncio
import copy
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers"))
import k8s_core_server
from holmes_tools import pod_lifecycle
from holmes_tools.record_query import select_records, validate_query


def fixture():
    return {
        "kind": "PodList",
        "metadata": {"resourceVersion": "v1", "continue": "next", "remainingItemCount": 7},
        "items": [
            {
                "metadata": {"namespace": "test", "name": "p", "uid": "u"},
                "spec": {"nodeName": "n", "containers": [{"name": "app"}, {"name": "sidecar"}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "name": "app",
                            "ready": True,
                            "restartCount": 7,
                            "state": {"running": {}},
                            "lastState": {
                                "terminated": {
                                    "reason": "OOMKilled",
                                    "exitCode": 137,
                                    "finishedAt": "2026-10-09T00:09:00Z",
                                }
                            },
                        },
                        {
                            "name": "sidecar",
                            "ready": False,
                            "restartCount": 0,
                            "state": {"waiting": {}},
                            "lastState": {},
                        },
                    ],
                },
            }
        ],
    }


class RecordQueryTests(unittest.TestCase):
    def call(self, args, payload=None):
        with patch.object(
            pod_lifecycle.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload or fixture()), stderr=""),
        ):
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
        args = {
            "record_type": "container",
            "where": [
                {"field": "last_termination_reason", "op": "eq", "value": "OOMKilled"},
                {"field": "ready", "op": "eq", "value": True},
            ],
        }
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
        args = {
            "record_type": "container",
            "fields": ["ready"],
            "where": [{"field": "last_termination_reason", "op": "ne", "value": None}],
        }
        original = copy.deepcopy(args)
        with (
            patch.object(k8s_core_server.kubernetes_core, "call_tool", return_value="ok") as call,
            patch.object(k8s_core_server, "log_tool_call"),
            patch.object(k8s_core_server, "log_tool_result"),
        ):
            asyncio.run(k8s_core_server.call_tool("query_pod_lifecycle", args))
            call.assert_called_once_with("query_pod_lifecycle", args)
            self.assertEqual(args, original)

    def test_invalid_query_fails_before_collection(self):
        invalid = [
            {"fields": []},
            {"where": "bad"},
            {"fields": ["nonexistent"]},
            {"group_by": "state"},
            {"record_type": "other"},
            {"where": [{"field": "node", "op": "gt", "value": "x"}]},
            {"where": [{"field": "node", "op": "eq", "value": float("nan")}]},
            {"match": "xor"},
        ]
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
        with patch.object(
            pod_lifecycle.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="denied"),
        ):
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
        rows = [{"id": i, "value": i} for i in range(100)] + [
            {"id": 100, "value": None},
            {"id": 101, "value": False},
        ]
        query = validate_query(
            {"where": [{"field": "value", "op": "eq", "value": 0}]}, ["id", "value"], ["id", "value"]
        )
        result = select_records(rows, query)
        self.assertEqual(result["rows"], [{"id": 0, "value": 0}])
        self.assertEqual(result["unknown_filter_rows"], 1)
        query = validate_query(
            {"where": [{"field": "id", "op": "gte", "value": 99}]}, ["id", "value"], ["id", "value"]
        )
        self.assertEqual(len(select_records(rows, query)["rows"]), 3)

    def test_all_container_roles_keep_identity(self):
        payload = fixture()
        payload["items"][0]["spec"]["initContainers"] = [{"name": "setup"}]
        result = self.call({"record_type": "container", "fields": ["ready"]}, payload)
        self.assertEqual([r["role"] for r in result["rows"]], ["app", "app", "init"])
        self.assertIsNone(result["rows"][-1]["ready"])


class LenientFieldTests(unittest.TestCase):
    """Live 2026-10-19: small models mix Pod- and container-level names on record_type=pod
    (b9df540f, 411cbba3, 5854968c, 3c052423, 7e55538b); the whole query failed and the
    healthy check never got a complete inventory."""

    call = RecordQueryTests.call

    def test_container_fields_on_pod_rows_promote_to_container_rows(self):
        result = self.call({"record_type": "pod", "fields": ["namespace", "pod", "phase", "restart_count_in_window", "last_exit_code"]})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["record_unit"], "container")
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual({r["container"] for r in result["rows"]}, {"app", "sidecar"})
        self.assertIn("record_type=container", " ".join(result["query_adjustments"]))

    def test_container_filter_on_pod_rows_promotes_too(self):
        result = self.call({"record_type": "pod", "where": [{"field": "last_exit_code", "op": "eq", "value": 137}]})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["record_unit"], "container")
        self.assertEqual(result["matched_rows"], 1)

    def test_unknown_names_are_dropped_with_a_note_when_others_remain(self):
        result = self.call({"record_type": "pod", "fields": ["namespace", "pod", "phase", "no_such_field"]})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["record_unit"], "pod")
        self.assertNotIn("no_such_field", result["rows"][0])
        self.assertIn("no_such_field", " ".join(result["query_adjustments"]))

    def test_plain_valid_query_has_no_adjustments(self):
        result = self.call({"record_type": "pod", "group_by": "node"})
        self.assertNotIn("query_adjustments", result)


def _pod(ns, name, *, phase="Running", ready=True, deleting=False, state=None, restarts=0, finished=None):
    meta = {"namespace": ns, "name": name, "uid": name}
    if deleting:
        meta.update(deletionTimestamp="2026-10-20T03:04:45Z", finalizers=["aiops.lab/hold"])
    status = {"name": "app", "ready": ready, "restartCount": restarts, "state": state or {"running": {}}, "lastState": {}}
    if finished:
        status["lastState"] = {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": finished}}
    return {
        "metadata": meta,
        "spec": {"nodeName": "n", "containers": [{"name": "app"}]},
        "status": {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                   "containerStatuses": [status]},
    }


def cluster_fixture():
    """Healthy Pods sort first alphabetically; the anomalies live in the last namespaces."""
    items = [_pod("aaa-system", f"healthy-{i:02d}") for i in range(30)]
    items += [
        _pod("zz-case-04", "pending", phase="Pending", ready=False, state={"waiting": {"reason": "CreateContainerConfigError"}}),
        _pod("zz-case-06", "crash", ready=False, restarts=7, state={"waiting": {"reason": "CrashLoopBackOff"}}),
        _pod("zz-case-09", "notready", ready=False),
        _pod("zz-case-11", "deleting", ready=False, deleting=True, state={"terminated": {"exitCode": 137}}),
    ]
    return {"kind": "PodList", "metadata": {"resourceVersion": "v1"}, "items": items}


class StableClusterScanTests(unittest.TestCase):
    """Live runs 52821169883547df / d852a86dcead4dee: a cluster-wide page held 22 of 109 rows,
    the model read only page one (alphabetical) and missed anomalies in later namespaces;
    ``group_by: "ns"`` / ``""`` errored without saying what is available."""

    call = RecordQueryTests.call

    def test_first_page_holds_the_anomalies_before_the_healthy_rows(self):
        with patch.dict(os.environ, {"AIOPS_RECORD_RESULT_MAX_BYTES": "4000"}):
            page = self.call({"record_type": "container"}, cluster_fixture())
        self.assertEqual(page["status"], "success")
        self.assertEqual(page["coverage"], "partial")
        self.assertLess(page["pagination"]["returned_rows"], 34)
        names = [row["pod"] for row in page["rows"]]
        self.assertEqual(names[:4], ["pending", "crash", "notready", "deleting"])
        self.assertIn("abnormal", " ".join(page["source_metadata"]["limitations"]).lower())
        self.assertEqual(page["row_order"], "abnormal_first")

    def test_completed_init_containers_and_succeeded_pods_are_healthy(self):
        # Live 7.0.48: calico-node's finished init containers (terminated, exit 0) sorted
        # ahead of healthy rows and pushed real anomalies' neighbours off page one.
        items = cluster_fixture()["items"]
        calico = _pod("kube-system", "calico-node", ready=True)
        calico["spec"]["initContainers"] = [{"name": "install-cni"}]
        calico["status"]["initContainerStatuses"] = [
            {"name": "install-cni", "ready": True, "restartCount": 0,
             "state": {"terminated": {"reason": "Completed", "exitCode": 0,
                                      "finishedAt": datetime.now(timezone.utc).isoformat()}},
             "lastState": {}}
        ]  # finished a moment ago (termination_within_window) and still healthy
        job = _pod("batch", "backup-job", phase="Succeeded", ready=False,
                   state={"terminated": {"reason": "Completed", "exitCode": 0}})
        fixture = {"kind": "PodList", "metadata": {"resourceVersion": "v1"}, "items": [calico, job, *items]}
        page = self.call({"record_type": "container"}, fixture)
        names = [row["pod"] for row in page["rows"][:4]]
        self.assertEqual(names, ["pending", "crash", "notready", "deleting"])
        self.assertNotIn("calico-node", names)
        self.assertNotIn("backup-job", names)

    def test_pod_rows_are_ordered_the_same_way(self):
        page = self.call({"record_type": "pod", "fields": ["namespace", "pod", "phase", "ready"]}, cluster_fixture())
        self.assertEqual([r["pod"] for r in page["rows"][:4]], ["pending", "crash", "notready", "deleting"])
        self.assertEqual(page["matched_rows"], 34)

    def test_field_aliases_are_accepted_and_reported(self):
        result = self.call({"record_type": "pod", "group_by": "ns"}, cluster_fixture())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result_type"], "groups")
        self.assertEqual({g["namespace"] for g in result["groups"]}, {"aaa-system", "zz-case-04", "zz-case-06", "zz-case-09", "zz-case-11"})
        self.assertIn("ns", " ".join(result["query_adjustments"]))
        rows = self.call({"record_type": "pod", "fields": ["ns", "name"], "where": [{"field": "name", "op": "eq", "value": "crash"}]}, cluster_fixture())
        self.assertEqual(rows["matched_rows"], 1)
        self.assertEqual(rows["rows"][0]["namespace"], "zz-case-06")

    def test_empty_group_by_is_ignored_and_unknown_group_by_lists_scalar_fields(self):
        result = self.call({"record_type": "container", "group_by": ""}, cluster_fixture())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result_type"], "records")
        self.assertIn("group_by", " ".join(result["query_adjustments"]))
        bad = self.call({"record_type": "container", "group_by": ":string:1:1:1"}, cluster_fixture())
        self.assertEqual(bad["status"], "error")
        self.assertIn("namespace", bad["error"])
        self.assertIn("node", bad["error"])
        self.assertNotIn("condition_states", bad["error"])

    def test_phase_only_filters_get_a_kubernetes_caveat(self):
        result = self.call({"record_type": "pod", "where": [{"field": "phase", "op": "ne", "value": "Running"}]}, cluster_fixture())
        self.assertEqual(result["matched_rows"], 1)
        self.assertIn("NotReady", " ".join(result["query_adjustments"]))
        grouped = self.call({"record_type": "pod", "group_by": "phase"}, cluster_fixture())
        self.assertIn("NotReady", " ".join(grouped["query_adjustments"]))
        fine = self.call({"record_type": "pod", "where": [{"field": "ready", "op": "ne", "value": True}]}, cluster_fixture())
        self.assertNotIn("query_adjustments", fine)
