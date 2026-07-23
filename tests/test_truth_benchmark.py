from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import unittest

from truth_benchmark import (
    clear_enabled,
    legal_placement,
    recognition_actions,
    semantic_action_correct,
    truth_action_window,
)


class RecognitionActionsTests(unittest.TestCase):
    def test_unknown_clear_state_is_not_counted_as_enabled(self):
        self.assertFalse(clear_enabled({"clearState": "unknown", "clearMode": "unknown", "clearedRows": [2]}))
        self.assertTrue(clear_enabled({"clearState": "on", "clearMode": "immediate"}))

    def test_prefers_solved_review_actions(self):
        raw = [{"id": "raw", "time": 1.25, "frameIndex": 30}]
        solved = [{"id": "solved", "sourceEventIndex": 0}]

        result = recognition_actions({"actions": raw, "reviewActions": solved})

        self.assertEqual(result[0]["id"], "solved")
        self.assertEqual(result[0]["time"], 1.25)
        self.assertEqual(result[0]["frameIndex"], 30)

    def test_supports_legacy_raw_only_result(self):
        raw = [{"id": "raw"}]

        self.assertEqual(recognition_actions({"actions": raw}), raw)

    def test_semantic_action_ignores_frame_offset(self):
        truth = {
            "sourceSlot": 1,
            "target": {"row": 2, "col": 3},
            "shape": [{"row": 0, "col": 0}, {"row": 1, "col": 0}],
            "clearDetection": False,
            "executeAt": 10.0,
        }
        predicted = {
            "sourceSlot": 1,
            "target": {"row": 2, "col": 3},
            "shape": [{"row": 1, "col": 0}, {"row": 0, "col": 0}],
            "clearState": "off",
            "time": 10.4,
        }

        self.assertTrue(semantic_action_correct(predicted, truth))

    def test_truth_action_window_allows_nearby_stable_frame(self):
        truth = {"executeAt": 10.0}

        start, end = truth_action_window(truth, 30.0)

        self.assertLessEqual(start, 9.65)
        self.assertGreaterEqual(end, 10.35)

    def test_legal_placement_rejects_out_of_board_shape(self):
        action = {
            "target": {"row": 9, "col": 9},
            "shape": [{"row": 0, "col": 0}, {"row": 0, "col": 1}],
        }

        self.assertFalse(legal_placement(action, 10, 10))


if __name__ == "__main__":
    unittest.main()
