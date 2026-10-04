import unittest

from src import rules
from src.domain import ConflictError, ValidationError


class RulesTest(unittest.TestCase):
    def test_effective_capacity(self):
        self.assertEqual(rules.effective_gate_capacity(100.0, 'available', None), 100.0)
        self.assertEqual(rules.effective_gate_capacity(100.0, 'available', 60.0), 60.0)
        self.assertEqual(rules.effective_gate_capacity(100.0, 'unavailable', 90.0), 0.0)

    def test_allocation_equal_split_and_validation(self):
        plan = rules.normalize_allocation(['G1', 'G2'], 100.0)
        self.assertAlmostEqual(plan['G1'] + plan['G2'], 100.0)
        explicit = rules.normalize_allocation(['G1', 'G2'], 100.0, {'G1': 30, 'G2': 70})
        self.assertEqual(explicit['G2'], 70.0)
        with self.assertRaises(ValidationError):
            rules.normalize_allocation(['G1', 'G2'], 100.0, {'G1': 40, 'G2': 40})
        with self.assertRaises(ValidationError):
            rules.normalize_allocation(['G1'], 100.0, {'G2': 100})
        with self.assertRaises(ValidationError):
            rules.normalize_allocation(['G1', 'G1'], 10.0)

    def test_evaluate_batch_first_come_first_served(self):
        caps = {'G1': 100.0}
        first = rules.evaluate_batch(1, ['G1'], {'G1': 80.0}, {}, caps)
        self.assertTrue(first['filled'])
        second = rules.evaluate_batch(2, ['G1'], {'G1': 40.0}, {'G1': 80.0}, caps)
        self.assertFalse(second['filled'])
        self.assertEqual(second['shortfalls'][0]['gate_code'], 'G1')
        self.assertAlmostEqual(second['shortfalls'][0]['gap'], 20.0)
        self.assertAlmostEqual(second['shortfalls'][0]['remaining'], 20.0)

    def test_transition_guards(self):
        self.assertTrue(rules.can_transition('reserved', 'authorized'))
        self.assertFalse(rules.can_transition('queued', 'authorized'))
        with self.assertRaises(ConflictError):
            rules.validate_transition('executed', 'authorized')

    def test_plan_window_freezes_executed_and_reorders_fifo(self):
        orders = [
            {'id': 1, 'status': 'executed', 'lines': [{'gate_code': 'G1', 'allocated': 80.0}]},
            {'id': 2, 'status': 'reserved', 'lines': [{'gate_code': 'G1', 'allocated': 10.0}]},
            {'id': 3, 'status': 'queued', 'lines': [{'gate_code': 'G1', 'allocated': 20.0}]},
        ]
        results = rules.plan_window(orders, ['G1'], {'G1': 100.0})
        self.assertTrue(results[0]['frozen'])
        self.assertTrue(results[1]['filled'])
        self.assertFalse(results[2]['filled'])
        self.assertAlmostEqual(results[2]['shortfalls'][0]['gap'], 10.0)


if __name__ == "__main__":
    unittest.main()
