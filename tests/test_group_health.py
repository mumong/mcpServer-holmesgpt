"""Grouped Pod counts must not hide unhealthy Pods (run 97f67069325c471c).

group_by=phase returned {"phase": "Running", "count": 26} while two of those Pods were
crash-looping (phase stays Running). Each group now carries counted health facts.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "servers"))

from holmes_tools import record_query  # noqa: E402
from holmes_tools.pod_record_query import group_health  # noqa: E402

POD_ROWS = [
    {"namespace": "a", "pod": "ok-1", "phase": "Running", "ready": True, "deletion_timestamp": None},
    {"namespace": "b", "pod": "crash-1", "phase": "Running", "ready": False, "deletion_timestamp": None},
    {"namespace": "c", "pod": "oom-1", "phase": "Running", "ready": False, "deletion_timestamp": None},
    {
        "namespace": "d",
        "pod": "gone-1",
        "phase": "Running",
        "ready": True,
        "deletion_timestamp": "2026-10-21T00:00:00Z",
    },
    {"namespace": "e", "pod": "pull-1", "phase": "Pending", "ready": None, "deletion_timestamp": None},
]
CONTAINER_ROWS = [
    {
        "namespace": "b",
        "pod": "crash-1",
        "container": "app",
        "phase": "Running",
        "ready": False,
        "current_state": {"state": "waiting", "reason": "CrashLoopBackOff"},
        "restart_count_in_window": 2,
        "last_termination_reason": "Error",
    },
    {
        "namespace": "c",
        "pod": "oom-1",
        "container": "app",
        "phase": "Running",
        "ready": False,
        "current_state": {"state": "waiting", "reason": "CrashLoopBackOff"},
        "restart_count_in_window": 1,
        "last_termination_reason": "OOMKilled",
    },
    {
        "namespace": "a",
        "pod": "ok-1",
        "container": "app",
        "phase": "Running",
        "ready": True,
        "current_state": {"state": "running"},
        "restart_count_in_window": 0,
        "last_termination_reason": None,
    },
]


def query(group_by="phase"):
    return {"fields": ["namespace"], "where": [], "match": "all", "group_by": group_by}


class GroupHealthTest(unittest.TestCase):
    def test_running_group_reports_not_ready_and_terminating(self):
        out = record_query.select_records(POD_ROWS, query(), group_facts=group_health("pod"))
        running = next(g for g in out["groups"] if g["phase"] == "Running")
        self.assertEqual(running["count"], 4)
        self.assertEqual(running["not_ready"], 2)
        self.assertEqual(running["terminating"], 1)
        self.assertEqual(running["not_ready_examples"], ["b/crash-1", "c/oom-1"])

    def test_unknown_readiness_is_counted_separately(self):
        out = record_query.select_records(POD_ROWS, query(), group_facts=group_health("pod"))
        pending = next(g for g in out["groups"] if g["phase"] == "Pending")
        self.assertEqual(pending["not_ready"], 0)
        self.assertEqual(pending["ready_unknown"], 1)

    def test_container_groups_count_reasons_and_restarts(self):
        out = record_query.select_records(CONTAINER_ROWS, query(), group_facts=group_health("container"))
        running = out["groups"][0]
        self.assertEqual(running["not_ready"], 2)
        self.assertEqual(running["restarted_in_window"], 2)
        self.assertEqual(running["waiting_reasons"], {"CrashLoopBackOff": 2})
        self.assertEqual(running["last_termination_reasons"], {"Error": 1, "OOMKilled": 1})

    def test_examples_are_bounded(self):
        rows = [
            {"namespace": "n", "pod": f"p{i}", "phase": "Running", "ready": False, "deletion_timestamp": None}
            for i in range(12)
        ]
        out = record_query.select_records(rows, query(), group_facts=group_health("pod"))
        self.assertEqual(len(out["groups"][0]["not_ready_examples"]), 5)

    def test_healthy_groups_stay_compact(self):
        rows = [POD_ROWS[0]]
        out = record_query.select_records(rows, query(), group_facts=group_health("pod"))
        self.assertEqual(out["groups"], [{"phase": "Running", "count": 1, "not_ready": 0}])

    def test_without_callback_the_generic_contract_is_unchanged(self):
        out = record_query.select_records(POD_ROWS, query())
        self.assertEqual(sorted(out["groups"][0]), ["count", "phase"])


if __name__ == "__main__":
    unittest.main()
