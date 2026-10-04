"""容量批次完整流程测试。

覆盖需求：
- 指令提交绑定时段、闸门组合、下泄量，按剩余过流能力预占
- 容量不够待排队并写明缺口
- 水情观测或闸门状态变化后未执行指令失效重算，已执行保留快照
- 先到者占用，后到者看到余量和冲突
- 写入失败凭原请求号恢复，重试不重复预占、不重复追加记录
- 总工只能授权已占足容量的指令，历史指令升级待补核
"""
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import CapacityError, ConflictError
from src.repository import Repository
from src.service import Service
from src.rules import (CAP_EXECUTED, CAP_PENDING_REVIEW, CAP_QUEUED,
                        CAP_RESERVED, STATES)


class CapacityBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.gate = self.service.create_gate(
            {"code": "G1", "name": "1号闸门", "capacity": 100},
            "creator", 'duty_officer')
        self.service.record_observation(
            {"observed_at": "2026-10-04T00:00:00", "reservoir_level": 100,
             "inflow": 50, "downstream_alert": 90},
            "observer", 'duty_officer')

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _make_item(self, title, discharge, gate_ids=None,
                   period_start="2026-10-04T00:00:00",
                   period_end="2026-10-05T00:00:00", **extra):
        payload = {
            "title": title, "description": "test", "severity": "urgent",
            "quantity": 5, "threshold": 10,
            "period_start": period_start, "period_end": period_end,
            "gate_ids": gate_ids or [self.gate["id"]],
            "discharge": discharge,
        }
        payload.update(extra)
        return self.service.create_item(payload, "creator", 'duty_officer')

    # ------------------------------------------------------------------
    # 预占与排队
    # ------------------------------------------------------------------

    def test_sufficient_capacity_reserved(self):
        item = self._make_item("充足容量", 60)
        self.assertEqual(item["capacity_status"], CAP_RESERVED)
        cap = self.service.get_capacity(item["id"], "viewer")
        self.assertEqual(cap["reservations"][0]["status"], "reserved")
        self.assertEqual(cap["reservations"][0]["allocated_discharge"], 60)
        self.assertEqual(cap["reservations"][0]["gap"], 0)

    def test_insufficient_capacity_queued_with_gap(self):
        # 先占60，剩余40
        self._make_item("先到", 60)
        # 再要50，剩余40，缺口10
        item = self._make_item("后到", 50)
        self.assertEqual(item["capacity_status"], CAP_QUEUED)
        cap = self.service.get_capacity(item["id"], "viewer")
        res = cap["reservations"][0]
        self.assertEqual(res["status"], "queued")
        self.assertEqual(res["allocated_discharge"], 40)
        self.assertEqual(res["gap"], 10)

    def test_gate_remaining_capacity_view(self):
        self._make_item("先到", 60)
        info = self.service.get_gate_capacity(
            self.gate["id"], "2026-10-04T00:00:00",
            "2026-10-05T00:00:00", "viewer")
        self.assertEqual(info["reserved_discharge"], 60)
        self.assertEqual(info["remaining_capacity"], 40)

    # ------------------------------------------------------------------
    # 授权
    # ------------------------------------------------------------------

    def test_chief_engineer_authorizes_only_reserved(self):
        item = self._make_item("可授权", 60)
        self.assertEqual(item["capacity_status"], CAP_RESERVED)
        # 先提交到 checked
        item = self.service.transition(
            item["id"], STATES[1], item["version"],
            "reviewer", 'duty_officer')
        # 总工授权 reserved 指令
        authorized = self.service.transition(
            item["id"], STATES[2], item["version"],
            "engineer", 'chief_engineer')
        self.assertEqual(authorized["status"], STATES[2])

    def test_chief_engineer_cannot_authorize_queued(self):
        self._make_item("先到", 60)
        item = self._make_item("排队", 50)
        self.assertEqual(item["capacity_status"], CAP_QUEUED)
        item = self.service.transition(
            item["id"], STATES[1], item["version"],
            "reviewer", 'duty_officer')
        with self.assertRaises(CapacityError):
            self.service.transition(
                item["id"], STATES[2], item["version"],
                "engineer", 'chief_engineer')

    def test_historical_item_pending_review_cannot_authorize(self):
        # 无容量字段的历史指令
        item = self.service.create_item({
            "title": "历史指令", "description": "无容量依据",
            "severity": "routine", "quantity": 1, "threshold": 1,
        }, "creator", 'duty_officer')
        self.assertEqual(item["capacity_status"], CAP_PENDING_REVIEW)
        item = self.service.transition(
            item["id"], STATES[1], item["version"],
            "reviewer", 'duty_officer')
        with self.assertRaises(CapacityError):
            self.service.transition(
                item["id"], STATES[2], item["version"],
                "engineer", 'chief_engineer')

    def test_supplement_capacity_then_authorize(self):
        item = self.service.create_item({
            "title": "待补核", "description": "历史指令",
            "severity": "routine", "quantity": 1, "threshold": 1,
        }, "creator", 'duty_officer')
        self.assertEqual(item["capacity_status"], CAP_PENDING_REVIEW)
        # 补核
        item = self.service.supplement_capacity(item["id"], {
            "period_start": "2026-10-04T00:00:00",
            "period_end": "2026-10-05T00:00:00",
            "gate_ids": [self.gate["id"]], "discharge": 50,
        }, "creator", 'duty_officer')
        self.assertEqual(item["capacity_status"], CAP_RESERVED)
        # 现在可以授权
        item = self.service.transition(
            item["id"], STATES[1], item["version"],
            "reviewer", 'duty_officer')
        authorized = self.service.transition(
            item["id"], STATES[2], item["version"],
            "engineer", 'chief_engineer')
        self.assertEqual(authorized["status"], STATES[2])

    # ------------------------------------------------------------------
    # 快照与重算
    # ------------------------------------------------------------------

    def test_executed_item_keeps_snapshot_on_new_observation(self):
        item = self._make_item("已执行", 60)
        old_basis = item["basis_observation_id"]
        # 走到 executed
        for target in STATES[1:4]:
            item = self.service.transition(
                item["id"], target, item["version"],
                "reviewer",
                'duty_officer' if target == STATES[1]
                else 'chief_engineer' if target == STATES[2]
                else 'dispatcher')
        self.assertEqual(item["status"], STATES[3])
        self.assertEqual(item["capacity_status"], CAP_EXECUTED)
        # 记录新水情观测
        self.service.record_observation(
            {"observed_at": "2026-10-06T00:00:00", "reservoir_level": 110,
             "inflow": 60, "downstream_alert": 90},
            "observer", 'duty_officer')
        # 已执行指令保留当时快照
        refreshed = self.service.get_item(item["id"], "viewer")
        self.assertEqual(refreshed["basis_observation_id"], old_basis)
        self.assertEqual(refreshed["capacity_status"], CAP_EXECUTED)
        self.assertIsNotNone(refreshed["capacity_snapshot"])

    def test_unexecuted_item_recalculated_on_gate_change(self):
        item = self._make_item("未执行", 60)
        self.assertEqual(item["capacity_status"], CAP_RESERVED)
        # 闸门进入维护状态（能力降为0）
        self.service.update_gate(
            self.gate["id"], {"status": "maintenance"},
            "engineer", 'duty_officer')
        # 未执行指令按新依据失效重算 → 排队
        refreshed = self.service.get_item(item["id"], "viewer")
        self.assertEqual(refreshed["capacity_status"], CAP_QUEUED)

    def test_executed_item_not_recalculated_on_gate_change(self):
        item = self._make_item("已执行", 60)
        for target in STATES[1:4]:
            item = self.service.transition(
                item["id"], target, item["version"],
                "reviewer",
                'duty_officer' if target == STATES[1]
                else 'chief_engineer' if target == STATES[2]
                else 'dispatcher')
        old_snapshot = item["capacity_snapshot"]
        self.service.update_gate(
            self.gate["id"], {"status": "maintenance"},
            "engineer", 'duty_officer')
        refreshed = self.service.get_item(item["id"], "viewer")
        self.assertEqual(refreshed["capacity_status"], CAP_EXECUTED)
        self.assertEqual(refreshed["capacity_snapshot"], old_snapshot)

    # ------------------------------------------------------------------
    # 并发争用：先到者占用
    # ------------------------------------------------------------------

    def test_concurrent_contention_first_occupies(self):
        results = []
        errors = []

        def create(title, discharge):
            try:
                item = self._make_item(title, discharge)
                results.append((title, item["capacity_status"]))
            except Exception as exc:
                errors.append((title, str(exc)))

        threads = [
            threading.Thread(target=create, args=("先到", 60)),
            threading.Thread(target=create, args=("后到", 50)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        statuses = dict(results)
        # 先到者占足60，后到者看到剩余40与缺口10
        self.assertEqual(statuses["先到"], CAP_RESERVED)
        self.assertEqual(statuses["后到"], CAP_QUEUED)

    # ------------------------------------------------------------------
    # 幂等：凭原请求号恢复
    # ------------------------------------------------------------------

    def test_idempotent_create_no_double_reservation(self):
        item1 = self.service.create_item({
            "title": "幂等", "description": "test", "severity": "urgent",
            "quantity": 5, "threshold": 10,
            "period_start": "2026-10-04T00:00:00",
            "period_end": "2026-10-05T00:00:00",
            "gate_ids": [self.gate["id"]], "discharge": 60,
            "request_no": "REQ-CREATE-1",
        }, "creator", 'duty_officer')
        # 重试同一请求号
        item2 = self.service.create_item({
            "title": "幂等", "description": "test", "severity": "urgent",
            "quantity": 5, "threshold": 10,
            "period_start": "2026-10-04T00:00:00",
            "period_end": "2026-10-05T00:00:00",
            "gate_ids": [self.gate["id"]], "discharge": 60,
            "request_no": "REQ-CREATE-1",
        }, "creator", 'duty_officer')
        self.assertEqual(item1["id"], item2["id"])
        # 只有一条指令、一条预占
        items = self.service.list_items("viewer")
        self.assertEqual(len(items), 1)
        cap = self.service.get_capacity(item1["id"], "viewer")
        self.assertEqual(len(cap["reservations"]), 1)

    def test_idempotent_record_no_duplicate(self):
        item = self._make_item("记录幂等", 60)
        rec1 = self.service.add_record(item["id"], {
            "kind": "action", "detail": "第一次", "status": "open",
            "request_no": "REQ-REC-1",
        }, "recorder", 'duty_officer')
        rec2 = self.service.add_record(item["id"], {
            "kind": "action", "detail": "第一次", "status": "open",
            "request_no": "REQ-REC-1",
        }, "recorder", 'duty_officer')
        self.assertEqual(rec1["id"], rec2["id"])
        records = self.service.list_records(item["id"], "viewer")
        self.assertEqual(len(records), 1)

    # ------------------------------------------------------------------
    # 审计链
    # ------------------------------------------------------------------

    def test_audit_chain_includes_capacity_events(self):
        item = self._make_item("审计", 60)
        events = self.service.audit("viewer", item["id"])
        actions = [e["action"] for e in events]
        self.assertIn("create", actions)
        self.assertIn("capacity_evaluated", actions)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
