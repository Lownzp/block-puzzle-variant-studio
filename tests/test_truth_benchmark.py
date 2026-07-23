from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import unittest

from truth_benchmark import (
    action_count_exact,
    aggregate_totals,
    clear_enabled,
    detection_aligned,
    group_by_scene,
    legal_placement,
    recognition_actions,
    scene_label,
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


class DetectionRecognitionSplitTests(unittest.TestCase):
    def test_action_count_exact_requires_no_missed_or_false_positive(self):
        self.assertEqual(action_count_exact(0, 0), 1.0)
        self.assertEqual(action_count_exact(1, 0), 0.0)
        self.assertEqual(action_count_exact(0, 2), 0.0)

    def test_detection_aligned_drops_pairs_outside_truth_window(self):
        truth = {"executeAt": 10.0}
        aligned_pred = {"time": 10.2}
        drifted_pred = {"time": 14.0}
        matched = [(aligned_pred, truth), (drifted_pred, truth)]

        aligned = detection_aligned(matched, 30.0)

        self.assertEqual(aligned, [(aligned_pred, truth)])

    def test_scene_label_reads_first_available_key(self):
        self.assertEqual(scene_label({"scenario": "录屏连消"}), "录屏连消")
        self.assertEqual(scene_label({"scene": "  快节奏  "}), "快节奏")

    def test_scene_label_defaults_to_unlabeled(self):
        self.assertEqual(scene_label({"steps": []}), "unlabeled")


class ReportAggregationTests(unittest.TestCase):
    def test_aligned_recognition_excludes_misaligned_pair_noise(self):
        # A misaligned second video drags matched-weighted shape down to 0.75,
        # but the detection-aligned view isolates true recognition at 1.0.
        tasks = [
            {
                "predicted": 2, "truth": 2, "matched": 2, "falsePositive": 0, "missed": 0,
                "matchedAligned": 2, "shapeAccuracy": 1.0, "shapeAccuracyAligned": 1.0,
                "actionCountExact": 1.0,
            },
            {
                "predicted": 2, "truth": 3, "matched": 2, "falsePositive": 0, "missed": 1,
                "matchedAligned": 1, "shapeAccuracy": 0.5, "shapeAccuracyAligned": 1.0,
                "actionCountExact": 0.0,
            },
        ]

        totals = aggregate_totals(tasks)

        self.assertEqual(totals["matched"], 4)
        self.assertEqual(totals["recall"], round(4 / 5, 4))
        self.assertEqual(totals["shapeAccuracy"], 0.75)
        self.assertEqual(totals["shapeAccuracyAligned"], 1.0)
        self.assertEqual(totals["actionCountExactRate"], 0.5)

    def test_group_by_scene_separates_and_aggregates(self):
        tasks = [
            {"scene": "录屏", "predicted": 1, "truth": 1, "matched": 1, "falsePositive": 0, "missed": 0, "matchedAligned": 1},
            {"scene": "快节奏", "predicted": 1, "truth": 2, "matched": 1, "falsePositive": 0, "missed": 1, "matchedAligned": 1},
        ]

        grouped = group_by_scene(tasks)

        self.assertEqual(set(grouped), {"录屏", "快节奏"})
        self.assertEqual(grouped["录屏"]["recall"], 1.0)
        self.assertEqual(grouped["快节奏"]["recall"], 0.5)


if __name__ == "__main__":
    unittest.main()
