from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import unittest

from timeline_analyzer import (
    _bottom_groups,
    _grid_split_quality,
    _largest_connected_component,
)

try:
    import cv2
    import numpy as np

    _HAS_CV2 = True
except Exception:  # pragma: no cover - environment without cv2/numpy
    _HAS_CV2 = False


class LargestConnectedComponentTests(unittest.TestCase):
    def test_drops_stray_disconnected_cell(self):
        # A same-color mis-split can leave a stray cell across a gap; real tray
        # pieces are one connected shape, so the stray must be dropped.
        self.assertEqual(
            _largest_connected_component({(0, 0), (0, 1), (5, 5)}),
            {(0, 0), (0, 1)},
        )

    def test_keeps_fully_connected_shape(self):
        cells = {(0, 0), (0, 1), (1, 1)}
        self.assertEqual(_largest_connected_component(cells), cells)

    def test_empty_returns_empty(self):
        self.assertEqual(_largest_connected_component(set()), set())


class GridSplitQualityTests(unittest.TestCase):
    def test_clean_separation_scores_high(self):
        self.assertGreaterEqual(_grid_split_quality([0.9, 0.85], [0.02]), 0.8)

    def test_overlapping_coverage_scores_low(self):
        self.assertLessEqual(_grid_split_quality([0.3], [0.28]), 0.1)

    def test_no_occupied_cells_is_zero(self):
        self.assertEqual(_grid_split_quality([], [0.1]), 0.0)

    def test_score_is_clamped_to_unit_interval(self):
        self.assertEqual(_grid_split_quality([1.0], []), 1.0)


@unittest.skipUnless(_HAS_CV2, "requires cv2/numpy")
class BottomGroupsRefineIntegrationTests(unittest.TestCase):
    def _frame_with_tray_piece(self):
        # Board occupies the top; rows=cols=4 gives a 50px cell and ~21px tray
        # pitch. A solid saturated block sits under the middle slot.
        frame = np.zeros((400, 300, 3), dtype=np.uint8)
        board = (50, 20, 200, 200)
        cv2.rectangle(frame, (120, 250), (180, 275), (200, 60, 60), -1)
        return frame, board

    def test_refine_adds_score_and_keeps_shape_connected(self):
        frame, board = self._frame_with_tray_piece()

        groups = _bottom_groups(frame, board, 4, 4, refine_grid=True)

        filled = [group for group in groups if group]
        self.assertTrue(filled, "expected the tray block to be detected")
        for group in filled:
            self.assertIn("score", group)
            self.assertIn("connected", group)
            self.assertIsInstance(group["score"], float)
            self.assertTrue(group["connected"])
            cells = {(cell["row"], cell["col"]) for cell in group["shape"]}
            self.assertEqual(_largest_connected_component(cells), cells)

    def test_default_path_omits_refine_fields(self):
        frame, board = self._frame_with_tray_piece()

        groups = _bottom_groups(frame, board, 4, 4)

        for group in groups:
            if group:
                self.assertNotIn("score", group)
                self.assertNotIn("connected", group)


if __name__ == "__main__":
    unittest.main()
