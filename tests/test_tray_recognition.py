from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import unittest

from evaluate_tray_recognition import (
    aggregate_outcomes,
    compare_slot,
    normalize_cells,
)


class NormalizeCellsTests(unittest.TestCase):
    def test_translation_invariant(self):
        self.assertEqual(normalize_cells([(5, 5), (5, 6)]), normalize_cells([(0, 0), (0, 1)]))

    def test_order_independent(self):
        self.assertEqual(normalize_cells([(1, 0), (0, 0)]), normalize_cells([(0, 0), (1, 0)]))

    def test_accepts_dict_cells_and_ignores_hue(self):
        self.assertEqual(normalize_cells([{"row": 2, "col": 3, "hue": 40}]), frozenset({(0, 0)}))

    def test_empty(self):
        self.assertEqual(normalize_cells([]), frozenset())


class CompareSlotTests(unittest.TestCase):
    def test_detected_exact_is_translation_invariant(self):
        truth = {"occupied": True, "shape": [[0, 0], [0, 1]]}
        pred = {"shape": [{"row": 5, "col": 5}, {"row": 5, "col": 6}]}
        self.assertEqual(compare_slot(truth, pred), "detected_exact")

    def test_detected_wrong_when_shapes_differ(self):
        truth = {"occupied": True, "shape": [[0, 0], [0, 1]]}
        pred = {"shape": [{"row": 0, "col": 0}]}
        self.assertEqual(compare_slot(truth, pred), "detected_wrong")

    def test_missed_when_occupied_but_no_prediction(self):
        self.assertEqual(compare_slot({"occupied": True, "shape": [[0, 0]]}, None), "missed")

    def test_false_when_empty_but_predicted(self):
        self.assertEqual(compare_slot({"occupied": False, "shape": []}, {"shape": [{"row": 0, "col": 0}]}), "false")

    def test_correct_empty(self):
        self.assertEqual(compare_slot({"occupied": False, "shape": []}, None), "correct_empty")


class AggregateOutcomesTests(unittest.TestCase):
    def test_rates_over_mixed_outcomes(self):
        outcomes = ["detected_exact", "detected_wrong", "missed", "false", "correct_empty"]

        agg = aggregate_outcomes(outcomes)

        self.assertEqual(agg["occupiedSlots"], 3)
        self.assertEqual(agg["detected"], 2)
        self.assertEqual(agg["slotDetectionRate"], round(2 / 3, 4))
        self.assertEqual(agg["slotShapeExactRate"], round(1 / 2, 4))
        self.assertEqual(agg["falseSlotRate"], round(1 / 2, 4))

    def test_empty_input_is_zeroed(self):
        agg = aggregate_outcomes([])
        self.assertEqual(agg["slotDetectionRate"], 0.0)
        self.assertEqual(agg["slotShapeExactRate"], 0.0)
        self.assertEqual(agg["falseSlotRate"], 0.0)


if __name__ == "__main__":
    unittest.main()
