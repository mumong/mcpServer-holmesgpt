"""Lifecycle facts preserve recovered containers without diagnosing them."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers"))
from holmes_tools import kubernetes_core
from holmes_tools import pod_lifecycle as lifecycle

NOW = datetime(2026, 10, 9, 2, 48, 44, tzinfo=timezone.utc)


def pod():
    return {
        "kind": "Pod", "metadata": {"namespace": "test", "name": "example", "uid": "uid1"},
        "spec": {"containers": [{"name": "app", "env": [{"name": "SECRET", "value": "private"}]}]},
        "status": {"phase": "Running", "containerStatuses": [{
            "name": "app", "ready": True, "restartCount": 2,
            "state": {"running": {"startedAt": "2026-10-09T02:48:18Z"}},
            "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137,
                                         "finishedAt": "2026-10-09T02:48:17Z"}},
        }]},
    }


class LifecycleTests(unittest.TestCase):
    def test_raw_request_uses_rotating_service_account_without_exposing_token(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "token").write_text("test-secret")
            with patch.object(lifecycle, "SERVICE_ACCOUNT", root), \
                    patch.object(lifecycle.Path, "home", return_value=root), \
                    patch.dict(lifecycle.os.environ, {"KUBERNETES_SERVICE_HOST": "172.30.0.1"}, clear=True):
                with lifecycle._kubectl_config() as flags:
                    path = Path(flags[0].split("=", 1)[1])
                    text = path.read_text()
                    config = json.loads(text)
                    self.assertNotIn("test-secret", text)
                    self.assertEqual(config["users"][0]["user"]["tokenFile"], str(root / "token"))
                    self.assertEqual(config["clusters"][0]["cluster"]["server"], "https://172.30.0.1:443")
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertFalse(path.exists())
            with patch.dict(lifecycle.os.environ, {"KUBECONFIG": "/external/config"}):
                with lifecycle._kubectl_config() as flags:
                    self.assertEqual(flags, [])

    def test_existing_tool_definitions_unchanged(self):
        existing = [t.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for t in kubernetes_core.TOOLS if t.name != "query_pod_lifecycle"]
        serialized = json.dumps(existing, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(serialized.encode()).hexdigest(),
                         "21e708424ad185dfd3344b30faf1b4e9bf687821656373757f9c3311b0a3e504")

    def test_recovered_oom_still_has_evidence(self):
        source = pod()
        before = copy.deepcopy(source)
        result = lifecycle.project_pod(source, NOW, 30)
        c = result["containers"][0]
        self.assertEqual(result["phase"], "Running")
        self.assertTrue(c["ready"])
        self.assertEqual(c["last_state"]["terminated"]["reason"], "OOMKilled")
        self.assertTrue(c["termination_within_window"])
        self.assertEqual(c["restart_count_total"], 2)
        self.assertIsNone(c["restart_count_in_window"])
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("root_cause", result)
        self.assertEqual(source, before)

    def test_time_boundaries_old_unknown_and_future(self):
        for age, expected in [(27, True), (599, True), (600, True), (601, False), (1200, False), (86400, False), (-1, None)]:
            with self.subTest(age=age):
                p = pod()
                p["status"]["containerStatuses"][0]["lastState"]["terminated"]["finishedAt"] = (NOW - timedelta(seconds=age)).isoformat()
                c = lifecycle.project_pod(p, NOW, 10)["containers"][0]
                self.assertIs(c["termination_within_window"], expected)
        for timestamp in [None, "", "bad", "2026-10-09T02:48:17"]:
            p = pod()
            p["status"]["containerStatuses"][0]["lastState"]["terminated"]["finishedAt"] = timestamp
            self.assertIsNone(lifecycle.project_pod(p, NOW, 30)["containers"][0]["termination_within_window"])

    def test_healthy_no_termination_and_missing_status(self):
        p = pod()
        p["status"]["containerStatuses"][0].update(lastState={}, restartCount=0)
        c = lifecycle.project_pod(p, NOW, 30)["containers"][0]
        self.assertEqual(c["last_state"], {})
        self.assertIsNone(c["termination_within_window"])
        p["status"] = {}
        c = lifecycle.project_pod(p, NOW, 30)["containers"][0]
        self.assertFalse(c["status_observed"])
        self.assertIsNone(c["ready"])
        self.assertIsNone(c["restart_count_total"])

    def test_all_container_roles_and_completed_jobs(self):
        p = pod()
        p["spec"]["initContainers"] = [{"name": "setup"}]
        p["spec"]["ephemeralContainers"] = [{"name": "debug"}]
        p["status"].update(phase="Succeeded", initContainerStatuses=[{
            "name": "setup", "state": {"terminated": {"reason": "Completed", "exitCode": 0,
                                                       "finishedAt": NOW.isoformat()}}}])
        result = lifecycle.project_pod(p, NOW, 30)
        self.assertEqual([c["role"] for c in result["containers"]], ["app", "init", "ephemeral"])
        self.assertEqual(result["containers"][1]["state"]["terminated"]["reason"], "Completed")
        self.assertNotIn("abnormal", result)

    def run_query(self, payload, args=None, code=0, stderr=""):
        with patch.object(lifecycle.subprocess, "run", return_value=SimpleNamespace(
            returncode=code, stdout=json.dumps(payload), stderr=stderr
        )) as run:
            result = json.loads(kubernetes_core.call_tool("query_pod_lifecycle", args or {}))
        return result, run.call_args

    def test_paging_and_scope_preserved_without_health_filter(self):
        result, call = self.run_query({"kind": "PodList", "metadata": {
            "continue": "opaque+/=", "remainingItemCount": 20, "resourceVersion": "123"
        }, "items": [pod()]}, {"namespace": "test", "label_selector": "app in (api,web)",
                               "continue_token": "old+/=", "page_size": 1})
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(result["next_continue_token"], "opaque+/=")
        self.assertFalse(result["history_complete"])
        args = call.args[0]
        self.assertNotIn("shell", call.kwargs)
        path = urlparse(args[-1])
        self.assertEqual(path.path, "/api/v1/namespaces/test/pods")
        self.assertEqual(parse_qs(path.query), {"limit": ["1"], "continue": ["old+/="], "labelSelector": ["app in (api,web)"]})

    def test_single_empty_error_and_uid_identity(self):
        result, call = self.run_query(pod(), {"namespace": "test", "pod": "example"})
        self.assertEqual(result["pods"][0]["uid"], "uid1")
        self.assertEqual(call.args[0][-1], "/api/v1/namespaces/test/pods/example")
        result, _ = self.run_query({"kind": "PodList", "metadata": {}, "items": []})
        self.assertEqual(result["coverage"], "empty")
        result, _ = self.run_query({}, code=1, stderr="Forbidden")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["coverage"], "unknown")
        result, _ = self.run_query({"kind": "Status", "status": "Failure"})
        self.assertEqual(result["status"], "error")

    def test_default_ten_minutes_and_explicit_override(self):
        self.assertEqual(lifecycle.TOOL.inputSchema["properties"]["window_minutes"]["default"], 10)
        self.assertIn("10 minutes", lifecycle.TOOL.description)
        with patch.object(lifecycle, "datetime") as clock:
            clock.now.return_value = NOW
            clock.fromisoformat.side_effect = datetime.fromisoformat
            p = pod()
            p["status"]["containerStatuses"][0]["lastState"]["terminated"]["finishedAt"] = (NOW - timedelta(minutes=20)).isoformat()
            result, _ = self.run_query({"kind": "PodList", "items": [p]})
            self.assertEqual(result["window"]["minutes"], 10)
            self.assertFalse(result["pods"][0]["containers"][0]["termination_within_window"])
            result, _ = self.run_query({"kind": "PodList", "items": [p]}, {"window_minutes": 30})
            self.assertEqual(result["window"]["minutes"], 30)
            self.assertTrue(result["pods"][0]["containers"][0]["termination_within_window"])
            self.assertEqual(result["pods"][0]["containers"][0]["restart_count_total"], 2)

    def test_invalid_parameters_never_execute(self):
        for args in [{"pod": "example"}, {"namespace": "x/../y"}, {"page_size": 101},
                     {"window_minutes": 0}, {"window_minutes": True}, {"page_size": 1.5},
                     {"namespace": "x", "pod": "p", "continue_token": "t"}]:
            with self.subTest(args=args), patch.object(lifecycle.subprocess, "run") as run:
                self.assertEqual(json.loads(lifecycle.query(args))["status"], "error")
                run.assert_not_called()

    def test_timeout_is_error_not_empty(self):
        with patch.object(lifecycle.subprocess, "run", side_effect=lifecycle.subprocess.TimeoutExpired("kubectl", 30)):
            result = json.loads(lifecycle.query({}))
        self.assertEqual(result["coverage"], "unknown")
        self.assertEqual(result["status"], "error")
