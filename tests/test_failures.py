import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class FailureTest(unittest.TestCase):
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
        self.item = self.service.create_item({
            "title": "failure item", "description": "failure scenarios",
            "severity": 'urgent', "quantity": 5, "threshold": 10,
            "external_ref": "FAIL-1",
            "period_start": "2026-10-04T00:00:00",
            "period_end": "2026-10-05T00:00:00",
            "gate_ids": [self.gate["id"]], "discharge": 50,
        }, "creator", 'duty_officer')

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_permission_version_duplicate_and_invariant(self):
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.item["id"], STATES[1], 1, "attacker", "viewer")
        with self.assertRaises(ConflictError):
            self.service.transition(self.item["id"], STATES[1], 99, "reviewer",
                                    TRANSITION_ROLES[STATES[1]][0])
        payload = {"kind": "action", "detail": "same reference",
                   "status": "open", "external_ref": "DUP-1"}
        self.service.add_record(self.item["id"], payload, "recorder", 'duty_officer')
        with self.assertRaises(ConflictError):
            self.service.add_record(self.item["id"], payload, "recorder", 'duty_officer')
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"],
                "reviewer", TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[-1]][0])


if __name__ == "__main__":
    unittest.main()
