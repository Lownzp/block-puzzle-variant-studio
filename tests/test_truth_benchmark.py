from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import unittest

from truth_benchmark import clear_enabled, recognition_actions


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


if __name__ == "__main__":
    unittest.main()
