import json
import tempfile
import unittest
from pathlib import Path

from src.repository import Repository
from src.rules import GATE_ENTITY, ORDER_ENTITY
from src.service import Service

WINDOW = {"start_at": "2026-07-10T00:00:00+00:00",
          "end_at": "2026-07-10T06:00:00+00:00"}
OBS_AT = "2026-07-09T12:00:00+00:00"


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.repo.create_gate  # touch
        with self.repo.transaction() as conn:
            self.g1 = self.repo.create_gate(conn, "G1", "一号闸", 200.0, "officer-a", "2026-07-01T00:00:00+00:00")
            self.g2 = self.repo.create_gate(conn, "G2", "二号闸", 50.0, "officer-a", "2026-07-01T00:00:00+00:00")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def submit(self, request_id, discharge, gates, actor="officer-a", allocations=None,
               purpose="泄洪"):
        payload = {"request_id": request_id, "purpose": purpose, "discharge": discharge,
                   "gate_codes": gates, "window": WINDOW}
        if allocations:
            payload["allocations"] = allocations
        return self.service.submit_order(payload, actor, "duty_officer")

    def test_reserve_queue_and_gap_visibility(self):
        first = self.submit("REQ-1", 80.0, ["G1"])
        self.assertEqual(first["status"], "reserved")
        self.assertAlmostEqual(
            first["evaluation"]["reservations"][0]["remaining_after"], 120.0)
        second = self.submit("REQ-2", 140.0, ["G1"])
        self.assertEqual(second["status"], "queued")
        gap = second["evaluation"]["shortfalls"][0]
        self.assertAlmostEqual(gap["remaining"], 120.0)
        self.assertAlmostEqual(gap["gap"], 20.0)
        self.assertEqual(second["evaluation"]["queue_position"], 1)
        competitors = second["evaluation"]["competitors"]
        self.assertEqual([c["order_id"] for c in competitors], [first["id"]])

        view = self.service.capacity_view(WINDOW, "viewer")
        g1 = next(g for g in view["gates"] if g["gate_code"] == "G1")
        self.assertAlmostEqual(g1["remaining"], 120.0)
        self.assertAlmostEqual(g1["queued_demand"], 140.0)
        self.assertEqual(view["window"]["basis_version"], 1)

    def test_authorize_execute_and_snapshot_audit_chain(self):
        order = self.submit("REQ-W", 30.0, ["G1", "G2"])
        self.assertEqual(order["status"], "reserved")
        authorized = self.service.authorize(order["id"], {}, "chief", "chief_engineer")
        self.assertEqual(authorized["status"], "authorized")
        result = self.service.execute(order["id"], {"request_id": "EXEC-1",
                                                     "feedback": "开度到位"},
                                      "dispatcher", "dispatcher")
        self.assertEqual(result["order"]["status"], "executed")
        snapshot = result["record"]["snapshot"]
        self.assertEqual(snapshot["basis_version"], 1)
        self.assertAlmostEqual(sum(g["allocated"] for g in snapshot["gates"]), 30.0)
        records = self.service.list_records(order["id"], "viewer")
        self.assertEqual(len(records), 1)
        events = self.service.audit("viewer", entity_type=ORDER_ENTITY)
        actions = [e["action"] for e in events]
        self.assertEqual(actions, ["submit", "authorize", "execute"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_observation_invalidates_unexecuted_but_keeps_executed(self):
        keep = self.submit("REQ-KEEP", 80.0, ["G1"])
        self.service.authorize(keep["id"], {}, "chief", "chief_engineer")
        self.service.execute(keep["id"], {"request_id": "EXEC-K", "feedback": "已开闸"},
                             "dispatcher", "dispatcher")
        drop = self.submit("REQ-DROP", 30.0, ["G1"])
        self.assertEqual(drop["status"], "reserved")
        wait = self.submit("REQ-WAIT", 20.0, ["G1"])
        self.assertEqual(wait["status"], "reserved")
        # 再提交一条把余量吃满，使WAIT在队列中有后续参照
        tail = self.submit("REQ-TAIL", 80.0, ["G1"])
        self.assertEqual(tail["status"], "queued")

        # 水情依据收紧到10：已执行快照不动；未执行指令失效重算
        self.service.submit_observation(
            {"gate_code": "G1", "observed_at": OBS_AT, "capacity_limit": 10.0,
             "source": "上游来水"},
            "observer", "dispatcher")
        refreshed_keep = self.service.get_order(keep["id"], "viewer")
        self.assertEqual(refreshed_keep["status"], "executed")
        self.assertEqual(refreshed_keep["basis_version"], 1)
        record = self.service.list_records(keep["id"], "viewer")[0]
        self.assertEqual(record["snapshot"]["basis_version"], 1)
        self.assertEqual(self.service.get_order(drop["id"], "viewer")["status"], "queued")
        self.assertEqual(self.service.get_order(wait["id"], "viewer")["status"], "queued")
        drop_eval = self.service.get_order(drop["id"], "viewer")["evaluation"]
        self.assertEqual(drop_eval["basis_version"], 2)
        self.assertTrue(drop_eval["shortfalls"])
        self.assertAlmostEqual(drop_eval["shortfalls"][0]["gap"], 30.0)

        # 依据恢复后，FIFO重算：DROP占足(80+30=110)，WAIT仍缺20
        self.service.submit_observation(
            {"gate_code": "G1", "observed_at": OBS_AT, "capacity_limit": 110.0,
             "source": "上游来水"},
            "observer", "dispatcher")
        self.assertEqual(self.service.get_order(drop["id"], "viewer")["status"], "reserved")
        self.assertEqual(self.service.get_order(wait["id"], "viewer")["status"], "queued")

        # 依据恢复后总工只能授权已占足容量的指令
        authorized = self.service.authorize(drop["id"], {}, "chief", "chief_engineer")
        self.assertEqual(authorized["status"], "authorized")
        with self.assertRaises(Exception):
            self.service.authorize(wait["id"], {}, "chief", "chief_engineer")

    def test_gate_unavailable_then_recompute_and_recover(self):
        order = self.submit("REQ-G", 20.0, ["G1"])
        self.assertEqual(order["status"], "reserved")
        self.service.update_gate_status(
            {"gate_code": "G1", "status": "unavailable", "reason": "闸门检修"},
            "officer-a", "duty_officer")
        self.assertEqual(self.service.get_order(order["id"], "viewer")["status"], "queued")
        self.service.update_gate_status(
            {"gate_code": "G1", "status": "available", "reason": "检修完成"},
            "officer-a", "duty_officer")
        self.assertEqual(self.service.get_order(order["id"], "viewer")["status"], "reserved")


if __name__ == "__main__":
    unittest.main()
