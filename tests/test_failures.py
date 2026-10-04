import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.audit import utc_now
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service

WINDOW = {"start_at": "2026-08-01T00:00:00+00:00",
          "end_at": "2026-08-01T04:00:00+00:00"}


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        with self.repo.transaction() as conn:
            self.repo.create_gate(conn, "G1", "一号闸", 100.0, "officer-a",
                                  utc_now())

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def submit(self, request_id, discharge=40.0, gates=("G1",), actor="officer-a"):
        return self.service.submit_order(
            {"request_id": request_id, "purpose": "泄洪", "discharge": discharge,
             "gate_codes": list(gates), "window": WINDOW}, actor, "duty_officer")

    def authorize_execute(self, order):
        order = self.service.authorize(order["id"], {}, "chief", "chief_engineer")
        return self.service.execute(order["id"], {"request_id": "EXEC-" + order["request_id"],
                                                  "feedback": "执行完成"},
                                   "dispatcher", "dispatcher")

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_gate(
                {"code": "GX", "name": "x", "design_capacity": 10}, "a", "viewer")
        order = self.submit("REQ-P")
        with self.assertRaises(PermissionDenied):
            self.service.authorize(order["id"], {}, "duty-officer", "duty_officer")
        with self.assertRaises(PermissionDenied):
            self.service.execute(order["id"], {"request_id": "EX-P", "feedback": "x"},
                                 "chief", "chief_engineer")

    def test_cannot_authorize_unfilled_order(self):
        self.submit("REQ-A", 80.0)
        queued = self.submit("REQ-B", 80.0)
        self.assertEqual(queued["status"], "queued")
        with self.assertRaises(ConflictError):
            self.service.authorize(queued["id"], {}, "chief", "chief_engineer")

    def test_retry_same_request_id_does_not_double_reserve_or_record(self):
        payload = {"request_id": "REQ-IDEM", "purpose": "泄洪", "discharge": 50.0,
                   "gate_codes": ["G1"], "window": WINDOW}
        first = self.service.submit_order(payload, "officer-a", "duty_officer")
        second = self.service.submit_order(dict(payload), "officer-a", "duty_officer")
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second.get("replayed"))
        orders = self.service.list_orders("viewer")
        self.assertEqual(len(orders), 1)
        submit_events = [e for e in self.service.audit("viewer") if e["action"] == "submit"]
        self.assertEqual(len(submit_events), 1)

        first = self.service.authorize(first["id"], {}, "chief", "chief_engineer")
        exec_payload = {"request_id": "EXEC-IDEM", "feedback": "执行完成"}
        r1 = self.service.execute(first["id"], exec_payload, "dispatcher", "dispatcher")
        r2 = self.service.execute(first["id"], dict(exec_payload), "dispatcher", "dispatcher")
        self.assertEqual(r2["record"]["id"], r1["record"]["id"])
        self.assertTrue(r2.get("replayed"))
        records = self.service.list_records(first["id"], "viewer")
        self.assertEqual(len(records), 1)

    def test_concurrent_contention_first_come_reserved_second_queued(self):
        results = {}

        def worker(tag, discharge):
            results[tag] = self.submit("REQ-" + tag, discharge)

        t1 = threading.Thread(target=worker, args=("T1", 70.0))
        t2 = threading.Thread(target=worker, args=("T2", 60.0))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        statuses = {tag: order["status"] for tag, order in results.items()}
        self.assertEqual(sorted(statuses.values()), ["queued", "reserved"])
        winner = next(order for order in results.values() if order["status"] == "reserved")
        loser = next(order for order in results.values() if order["status"] == "queued")
        self.assertEqual(loser["evaluation"]["competitors"][0]["order_id"], winner["id"])
        self.assertGreaterEqual(loser["evaluation"]["shortfalls"][0]["gap"], 30.0 - 1e-6)

    def test_legacy_order_without_basis_escalates_to_recheck(self):
        now = utc_now()
        with self.repo.transaction() as conn:
            window = self.repo.get_or_create_window(conn, WINDOW["start_at"],
                                                    WINDOW["end_at"], now)
            gate = self.repo.get_gate(conn, code="G1")
            legacy = self.repo.create_order(conn, "REQ-LEGACY", "历史指令", window["id"],
                                            10.0, "recheck_pending", None, "old", now)
            self.repo.add_order_gate(conn, legacy["id"], gate["id"], "G1", 10.0)
            legacy_id = legacy["id"]
        with self.assertRaises(ConflictError):
            self.service.authorize(legacy_id, {}, "chief", "chief_engineer")
        order = self.service.get_order(legacy_id, "viewer")
        self.assertEqual(order["status"], "recheck_pending")
        self.assertIsNone(order["basis_version"])
        audit = self.service.audit("viewer")
        self.assertTrue(any(e["action"] == "escalate_recheck"
                            and e["entity_id"] == legacy_id for e in audit))

    def test_executed_snapshot_survives_gate_failure_recompute(self):
        order = self.submit("REQ-SNAP", 60.0)
        self.authorize_execute(order)
        self.service.update_gate_status(
            {"gate_code": "G1", "status": "unavailable", "reason": "故障"},
            "officer-a", "duty_officer")
        order = self.service.get_order(order["id"], "viewer")
        self.assertEqual(order["status"], "executed")
        snapshot = self.service.list_records(order["id"], "viewer")[0]["snapshot"]
        self.assertEqual(snapshot["basis_version"], 1)
        self.assertEqual(snapshot["gates"][0]["gate_status"], "available")

    def test_version_conflict_on_stale_authorize(self):
        order = self.submit("REQ-VER", 10.0)
        stale_version = order["version"]
        # 依据变化引发重算，指令版本推进，旧expected_version失效
        self.service.submit_observation(
            {"gate_code": "G1", "observed_at": "2026-07-31T00:00:00+00:00",
             "capacity_limit": 50.0, "source": "观测"},
            "observer", "dispatcher")
        current = self.service.get_order(order["id"], "viewer")
        self.assertGreater(current["version"], stale_version)
        with self.assertRaises(ConflictError):
            self.service.authorize(order["id"], {"expected_version": stale_version},
                                   "chief", "chief_engineer")


if __name__ == "__main__":
    unittest.main()
