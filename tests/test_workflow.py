import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        # 预先建立闸门与水情观测依据
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

    def test_complete_workflow_and_audit(self):
        item = self.service.create_item({
            "title": "workflow item", "description": "complete business flow",
            "severity": 'urgent', "quantity": 12, "threshold": 6,
            "external_ref": "WF-1",
            "period_start": "2026-10-04T00:00:00",
            "period_end": "2026-10-05T00:00:00",
            "gate_ids": [self.gate["id"]], "discharge": 50,
        }, "creator", 'duty_officer')
        self.assertEqual(item["status"], STATES[0])
        self.assertEqual(item["capacity_status"], "reserved")
        self.service.add_record(item["id"], {
            "kind": "evidence", "detail": "evidence registered",
            "status": "closed", "external_ref": "EV-1",
        }, "recorder", 'duty_officer')
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"],
                "reviewer", TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], STATES[-1])
        self.assertEqual(len(self.service.list_records(current["id"], "viewer")), 1)
        events = self.service.audit("viewer", current["id"])
        self.assertGreaterEqual(len(events), len(STATES) + 1)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
