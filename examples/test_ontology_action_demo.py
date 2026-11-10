import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

from ontology_action_demo import (
    ACTION_ID, FUNCTION_ID, Conflict, FakeTaskGateway, FollowupParams,
    Forbidden, Principal, canonical, demo_runtime, is_overdue, sample_issue,
)


class OntologyActionTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeTaskGateway()
        self.runtime = demo_runtime(gateway=self.gateway)
        self.issue = sample_issue()
        self.runtime.seed_issue(self.issue)
        self.params = FollowupParams("alice", date(2026, 10, 30))

    def tearDown(self):
        self.runtime.db.close()

    def request(self, key="request-1"):
        return self.runtime.request("alice", self.issue.id, self.params, key)

    def approved(self):
        run_id = self.request()
        self.runtime.approve("bob", run_id)
        return run_id

    def replace_issue(self, **changes):
        issue = replace(self.issue, **changes)
        self.runtime.db.execute("UPDATE issues SET body=? WHERE tenant_id=? AND id=?",
                                (canonical(asdict(issue)), issue.tenant_id, issue.id))

    def test_function_is_pure_and_observes_date_and_status(self):
        original = self.runtime.issue("alice", self.issue.id)
        self.assertTrue(self.runtime.invoke_function("alice", FUNCTION_ID, self.issue.id, date(2026, 10, 28)))
        self.assertFalse(is_overdue(self.issue, date(2026, 10, 25)))
        self.assertFalse(is_overdue(replace(self.issue, status="done"), date(2026, 10, 28)))
        self.assertEqual(original, self.runtime.issue("alice", self.issue.id))

    def test_definition_binds_to_type_not_single_record(self):
        second = replace(self.issue, id="issue-1002")
        self.runtime.seed_issue(second)
        self.assertTrue(self.runtime.invoke_function("alice", FUNCTION_ID, second.id, date(2026, 10, 28)))
        self.replace_issue(type_id="sales.Order@1")
        with self.assertRaises(Conflict):
            self.runtime.invoke_function("alice", FUNCTION_ID, self.issue.id, date(2026, 10, 28))
        with self.assertRaises(Conflict):
            self.request()

    def test_normal_flow_and_duplicate_request_and_execution(self):
        run_id = self.approved()
        first = self.runtime.execute("bob", run_id)
        self.assertEqual(first["state"], "SUCCEEDED")
        self.assertEqual(run_id, self.request())
        self.assertEqual(first["external_id"], self.runtime.execute("bob", run_id)["external_id"])
        self.assertEqual(self.gateway.calls, 1)
        events = [row[0] for row in self.runtime.db.execute("SELECT event FROM audit ORDER BY seq")]
        self.assertEqual(events, ["requested", "approved", "dispatch_started", "remote_creation_confirmed"])

    def test_same_key_different_parameters_are_rejected(self):
        self.request()
        with self.assertRaises(Conflict):
            self.runtime.request("alice", self.issue.id,
                                 replace(self.params, due_date=date(2026, 11, 1)), "request-1")

    def test_self_approval_and_dispatch_before_approval_are_rejected(self):
        self.runtime.principals["alice"] = replace(
            self.runtime.principals["alice"],
            permissions=frozenset({"issue:read", "followup:request", "followup:approve"}))
        run_id = self.request()
        with self.assertRaises(Forbidden):
            self.runtime.approve("alice", run_id)
        with self.assertRaises(Conflict):
            self.runtime.execute("bob", run_id)
        self.assertEqual(self.gateway.calls, 0)

    def test_tenant_project_and_operation_permissions(self):
        run_id = self.approved()
        self.runtime.principals["outsider"] = Principal(
            "outsider", "enterprise-b", frozenset({"project-1"}),
            frozenset({"issue:read", "followup:execute"}))
        with self.assertRaises(Forbidden):
            self.runtime.get_run("outsider", run_id)
        self.runtime.principals["outsider"] = replace(
            self.runtime.principals["outsider"], tenant_id="enterprise-a", projects=frozenset({"project-2"}))
        with self.assertRaises(Forbidden):
            self.runtime.issue("outsider", self.issue.id)
        with self.assertRaises(Forbidden):
            self.runtime.execute("alice", run_id)
        self.assertEqual(self.gateway.calls, 0)

    def test_object_revision_change_invalidates_approval(self):
        run_id = self.approved()
        self.replace_issue(revision=2, title="修改后的事项")
        with self.assertRaises(Conflict):
            self.runtime.execute("bob", run_id)
        self.assertEqual(self.gateway.calls, 0)

    def test_changed_payload_or_definition_is_not_dispatched(self):
        run_id = self.approved()
        self.runtime.db.execute("UPDATE runs SET payload=? WHERE id=?", ('{"title":"changed"}', run_id))
        with self.assertRaises(Conflict):
            self.runtime.execute("bob", run_id)
        self.runtime.db.execute("UPDATE runs SET action_id=? WHERE id=?", (ACTION_ID + "-changed", run_id))
        with self.assertRaises(Conflict):
            self.runtime.execute("bob", run_id)
        self.assertEqual(self.gateway.calls, 0)

    def test_permission_revocation_is_checked_at_execution(self):
        run_id = self.approved()
        self.runtime.principals["alice"] = replace(self.runtime.principals["alice"], active=False)
        with self.assertRaises(Forbidden):
            self.runtime.execute("bob", run_id)
        self.assertEqual(self.gateway.calls, 0)

    def test_invalid_recipient_or_business_condition_is_rejected(self):
        with self.assertRaises(Forbidden):
            self.runtime.request("alice", self.issue.id,
                                 replace(self.params, recipient_id="outside-project"), "invalid")
        self.replace_issue(status="done", revision=2)
        with self.assertRaises(Conflict):
            self.request()

    def test_response_lost_does_not_trigger_blind_resend(self):
        self.gateway.lose_response = True
        run_id = self.approved()
        self.assertEqual(self.runtime.execute("bob", run_id)["state"], "UNKNOWN")
        with self.assertRaises(Conflict):
            self.runtime.execute("bob", run_id)
        self.assertEqual(self.runtime.reconcile("bob", run_id)["state"], "SUCCEEDED")
        self.assertEqual(self.gateway.calls, 1)
        self.assertEqual(len(self.gateway.tasks), 1)

    def test_no_lookup_result_stays_unknown(self):
        class NoResponseGateway(FakeTaskGateway):
            def create(self, execution_id, payload):
                raise TimeoutError("no confirmed result")
        self.runtime.gateway = NoResponseGateway()
        run_id = self.approved()
        self.runtime.execute("bob", run_id)
        self.assertEqual(self.runtime.reconcile("bob", run_id)["state"], "UNKNOWN")

    def test_ledger_survives_runtime_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "demo.db")
            runtime = demo_runtime(path, self.gateway)
            runtime.seed_issue(self.issue)
            run_id = runtime.request("alice", self.issue.id, self.params, "persisted")
            runtime.approve("bob", run_id)
            runtime.execute("bob", run_id)
            runtime.db.close()
            restarted = demo_runtime(path, self.gateway)
            try:
                self.assertEqual(restarted.execute("bob", run_id)["state"], "SUCCEEDED")
                self.assertEqual(self.gateway.calls, 1)
            finally:
                restarted.db.close()

    def test_concurrent_workers_only_one_dispatches(self):
        started, release = threading.Event(), threading.Event()

        class BlockingGateway(FakeTaskGateway):
            def create(self, execution_id, payload):
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test worker was not released")
                return super().create(execution_id, payload)

        gateway = BlockingGateway()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "concurrency.db")
            runtime = demo_runtime(path, gateway)
            runtime.seed_issue(self.issue)
            run_id = runtime.request("alice", self.issue.id, self.params, "concurrent")
            runtime.approve("bob", run_id)
            runtime.db.close()

            def execute():
                worker = demo_runtime(path, gateway)
                try:
                    return worker.execute("bob", run_id)
                finally:
                    worker.db.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(execute)
                try:
                    self.assertTrue(started.wait(5))
                    with self.assertRaises(Conflict):
                        pool.submit(execute).result(timeout=5)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5)["state"], "SUCCEEDED")
            self.assertEqual(gateway.calls, 1)


if __name__ == "__main__":
    unittest.main()
