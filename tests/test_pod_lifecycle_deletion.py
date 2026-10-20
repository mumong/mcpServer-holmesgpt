"""Deleting Pods expose finalizers, grace period and how long they have been terminating.

Live run cdaf41032a33484b: a Pod stuck 19 minutes behind a finalizer was diagnosed as
"external deletion, not a fault" because no tool output ever contained the finalizer.
"""

import unittest
from datetime import datetime, timezone

from holmes_tools import pod_lifecycle, pod_record_query

OBSERVED = datetime(2026, 10, 16, 11, 29, 19, tzinfo=timezone.utc)


def _pod(deleting=True):
    meta = {"namespace": "aiops-case-11", "name": "workload", "uid": "u1", "creationTimestamp": "2026-10-16T11:00:00Z"}
    if deleting:
        meta.update(deletionTimestamp="2026-10-16T11:10:19Z", deletionGracePeriodSeconds=0, finalizers=["aiops.lab/hold"])
    return {"metadata": meta, "spec": {"nodeName": "node1"}, "status": {"phase": "Running", "conditions": [],
            "containerStatuses": [{"name": "app", "ready": False, "restartCount": 0,
                                   "state": {"terminated": {"reason": "Error", "exitCode": 137}}}]}}


class DeletionFactsTests(unittest.TestCase):
    def test_project_pod_exposes_deletion_facts(self):
        row = pod_lifecycle.project_pod(_pod(), OBSERVED, 30)
        self.assertEqual(row["finalizers"], ["aiops.lab/hold"])
        self.assertEqual(row["deletion_grace_period_seconds"], 0)
        self.assertEqual(row["terminating_seconds"], 19 * 60)

    def test_live_pod_has_empty_deletion_facts(self):
        row = pod_lifecycle.project_pod(_pod(deleting=False), OBSERVED, 30)
        self.assertEqual(row["finalizers"], [])
        self.assertIsNone(row["terminating_seconds"])
        self.assertIsNone(row["deletion_grace_period_seconds"])

    def test_record_defaults_include_deletion_facts(self):
        for kind in ("pod", "container"):
            self.assertIn("terminating_seconds", pod_record_query.DEFAULT_FIELDS[kind])
            self.assertIn("finalizers", pod_record_query.DEFAULT_FIELDS[kind])
        self.assertIn("finalizers", pod_record_query.NON_SCALAR_FIELDS)
        self.assertIn("terminating_seconds", pod_record_query.POD_FIELDS)

    def _records(self, arguments, pod):
        prepared = pod_record_query.prepare(arguments)
        payload = {"status": "success", "coverage": "present",
                   "pods": [pod_lifecycle.project_pod(pod, OBSERVED, 30)]}
        return pod_record_query.project(payload, prepared, arguments)

    def test_deletion_facts_stay_when_the_caller_picks_other_fields(self):
        # Live run f8e56239b1b74313: fields=[state, last_exit_code, ...] hid the deletion,
        # the model saw only exit 137 and argued OOM / application exit.
        pod = _pod()
        pod["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"] = "2026-10-16T11:10:29Z"
        out = self._records({"namespace": "aiops-case-11", "pod": "workload", "record_type": "container",
                             "fields": ["container", "state", "last_exit_code"]}, pod)
        row = out["rows"][0]
        self.assertEqual(row["deletion_timestamp"], "2026-10-16T11:10:19Z")
        self.assertEqual(row["finalizers"], ["aiops.lab/hold"])
        self.assertEqual(row["terminating_seconds"], 19 * 60)
        self.assertIs(row["stopped_by_pod_deletion"], True)
        pod_rows = self._records({"record_type": "pod", "fields": ["namespace", "pod", "phase"]}, _pod())
        self.assertEqual(pod_rows["rows"][0]["finalizers"], ["aiops.lab/hold"])

    def test_termination_before_the_deletion_is_not_attributed_to_it(self):
        pod = _pod()
        pod["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"] = "2026-10-16T11:05:00Z"
        out = self._records({"namespace": "aiops-case-11", "pod": "workload", "record_type": "container",
                             "fields": ["container", "state"]}, pod)
        self.assertIs(out["rows"][0]["stopped_by_pod_deletion"], False)

    def test_every_row_matches_the_columns_when_one_pod_is_deleting(self):
        # Live run e2c569f8c44746ea: only the deleting Pod's row got the sticky keys, the
        # consumer's row/column contract check failed and the page was treated as an error.
        prepared_args = {"record_type": "container"}
        live = _pod(deleting=False)
        live["metadata"]["name"] = "other"
        payload = {"status": "success", "coverage": "present",
                   "pods": [pod_lifecycle.project_pod(_pod(), OBSERVED, 30),
                            pod_lifecycle.project_pod(live, OBSERVED, 30)]}
        out = pod_record_query.project(payload, pod_record_query.prepare(prepared_args), prepared_args)
        self.assertIn("stopped_by_pod_deletion", out["columns"])
        for row in out["rows"]:
            self.assertEqual(set(row), set(out["columns"]))

    def test_live_pods_do_not_grow_sticky_columns(self):
        out = self._records({"record_type": "container", "fields": ["container", "state"]}, _pod(deleting=False))
        self.assertNotIn("deletion_timestamp", out["columns"])
        self.assertNotIn("stopped_by_pod_deletion", out["rows"][0])


if __name__ == "__main__":
    unittest.main()
