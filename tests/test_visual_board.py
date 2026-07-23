from __future__ import annotations


from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import copy
import json
import tempfile
import unittest
from pathlib import Path

from timeline_analyzer import (
    _board_state,
    _bottom_groups,
    _build_board_appearance_profile,
    _build_occupied_cell_validator,
    _detect_source_slot_pickups,
    _has_pickup_coverage_gap,
    _pickup_piece_shape,
    _pickup_remains_consumed,
    _post_large_clear_predecessor,
    _classify_stable_transition,
    _apply_rule_verified_clear_detection,
    _apply_repeated_board_consensus,
    _clear_effect_evidence,
    _coalesce_action_fragments,
    _collapse_drag_windows,
    _filter_cancelled_drags,
    _filter_color_clear_cooldown_fragments,
    _filter_low_quality_tail_candidates,
    _grid_shape_from_mask,
    _is_visual_cleanup_state,
    _repeated_round_element_target,
    _repeated_round_board_evidence,
    _repeated_round_element_shape,
    _reverse_clear_moves,
    _slot_matches_for_shape,
    _source_slot_material_consensus,
    infer_material_profile,
)
from variant_bridge import (
    apply_confirmed_actions,
    apply_block_style,
    backfill_annotation_timing,
    build_action_review,
    build_replay_truth,
    build_truth_progress,
    canvas_size_for_aspect,
    parse_byte_range,
    preset_break_steps,
)


class TruthProgressTests(unittest.TestCase):
    def test_progress_is_deduplicated_by_dataset_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "manifest.json"
            tasks = root / "tasks"
            tasks.mkdir()
            manifest.write_text(json.dumps({"videos": [
                {"dataset_id": "DEV-001", "split": "development", "filename": "one.mp4"},
                {"dataset_id": "DEV-002", "split": "development", "filename": "two.mp4"},
                {"dataset_id": "TEST-001", "split": "test", "filename": "test.mp4"},
            ]}), encoding="utf-8")
            for name, confirmed_at, steps in [
                ("20260714_DEV-001_first", "2026-07-14T10:00:00", 4),
                ("20260715_DEV-001_latest", "2026-07-15T10:00:00", 6),
            ]:
                task = tasks / name
                task.mkdir()
                (task / "动作确认状态.json").write_text(json.dumps({"status": "confirmed", "confirmedAt": confirmed_at, "stepCount": steps}), encoding="utf-8")
                (task / "确定性回放真值.json").write_text("{}", encoding="utf-8")
            pending = tasks / "20260715_DEV-002_pending"
            pending.mkdir()
            (pending / "动作确认状态.json").write_text(json.dumps({"status": "pending"}), encoding="utf-8")

            result = build_truth_progress(manifest, tasks)

            self.assertEqual(result["summary"], {"completed": 1, "total": 3})
            dev_one = next(item for item in result["items"] if item["datasetId"] == "DEV-001")
            self.assertEqual(dev_one["jobId"], "20260715_DEV-001_latest")
            self.assertEqual(dev_one["stepCount"], 6)
            self.assertEqual(next(item for item in result["items"] if item["datasetId"] == "DEV-002")["status"], "pending")


class VariantBridgeAssetTests(unittest.TestCase):
    def test_canvas_size_for_batch_aspect_ratios(self):
        self.assertEqual(canvas_size_for_aspect("1:1", 720, 1280), (1080, 1080))
        self.assertEqual(canvas_size_for_aspect("16:9", 720, 1280), (1920, 1080))
        self.assertEqual(canvas_size_for_aspect("9:16", 720, 1280), (1080, 1920))
        self.assertEqual(canvas_size_for_aspect("", 720, 1280), (720, 1280))

    def test_custom_block_resource_overrides_board_and_round_blocks(self):
        config = {
            "board": {"blocks": [{"colorIndex": 0, "resourceId": "old_board"}]},
            "rounds": [{"groups": [{"blocks": [{"colorIndex": 2, "resourceId": "old_round"}]}]}],
        }

        apply_block_style(config, "custom", "custom_block_uploaded")

        all_blocks = [
            config["board"]["blocks"][0],
            config["rounds"][0]["groups"][0]["blocks"][0],
        ]
        self.assertTrue(all(block["resourceId"] == "custom_block_uploaded" for block in all_blocks))
        self.assertTrue(all(block["kind"] == "Normal" for block in all_blocks))


class MaterialProfileTests(unittest.TestCase):
    def test_infers_image_material_from_task_name(self):
        self.assertEqual(
            infer_material_profile(Path("任务") / "DEV-010_仿3d木制立方块" / "source.mp4"),
            "image_block",
        )

    def test_infers_color_material_from_task_name(self):
        self.assertEqual(
            infer_material_profile(Path("任务") / "DEV-006_纯色蓝块全消" / "source.mp4"),
            "color_block",
        )


def board(*filled: tuple[int, int]) -> list[list[int | None]]:
    result = [[None for _ in range(3)] for _ in range(3)]
    for row, col in filled:
        result[row][col] = 60
    return result


def event(step: int, source: int, target: int, before, after, row=0, col=0) -> dict:
    return {
        "stepIndex": step,
        "sourceStateIndex": source,
        "targetStateIndex": target,
        "time": float(step),
        "frameIndex": step * 10,
        "sourceSlot": (step - 1) % 3,
        "target": {"row": row, "col": col},
        "group": {"slot": (step - 1) % 3, "shape": [{"row": 0, "col": 0, "hue": 60}], "rows": 1, "cols": 1, "cellCount": 1},
        "placedCells": [{"row": row, "col": col}],
        "clearedRows": [],
        "clearedCols": [],
        "clearMode": "none",
        "beforeBoard": copy.deepcopy(before),
        "afterBoard": copy.deepcopy(after),
        "confidence": "verified",
    }


def timeline_with_transition(transition_type: str) -> dict:
    state0 = board()
    state1 = board((0, 0))
    state2 = board((0, 0))
    state3 = board((0, 0), (1, 1))
    events = [
        event(1, 0, 1, state0, state1, 0, 0),
        event(2, 2, 3, state2, state3, 1, 1),
    ]
    return {
        "grid": {"rows": 3, "cols": 3},
        "sourceFrameRate": 30,
        "events": events,
        "replayCandidates": [
            {"stepIndex": item["stepIndex"], "sourceSlot": item["sourceSlot"], "target": item["target"], "shape": item["group"]["shape"], "cellCount": 1, "clearedRows": [], "clearedCols": [], "clearMode": "none", "confidence": "verified"}
            for item in events
        ],
        "stableStates": [
            {"stateIndex": 0, "board": state0},
            {"stateIndex": 1, "board": state1},
            {"stateIndex": 2, "board": state2},
            {"stateIndex": 3, "board": state3},
        ],
        "transitions": [{"fromState": 1, "toState": 2, "type": transition_type}],
        "validation": {},
    }


class AdaptiveBoardAppearanceTests(unittest.TestCase):
    def test_rejects_textured_overlay_that_does_not_match_real_cells(self):
        import cv2
        import numpy as np

        initial = np.full((240, 240, 3), (248, 248, 248), dtype=np.uint8)
        for row, col in ((0, 0), (0, 1), (1, 0), (1, 1)):
            left, top = col * 60, row * 60
            cv2.rectangle(initial, (left + 4, top + 4), (left + 55, top + 55), (205, 105, 235), -1)
            cv2.rectangle(initial, (left + 10, top + 10), (left + 49, top + 49), (220, 145, 240), 2)
        validator = _build_occupied_cell_validator(
            initial, (0, 0, 240, 240), 4, 4
        )

        overlay = np.full((240, 240, 3), (248, 248, 248), dtype=np.uint8)
        cv2.putText(
            overlay, "WOW", (2, 95), cv2.FONT_HERSHEY_SIMPLEX,
            1.35, (20, 20, 240), 8, cv2.LINE_AA,
        )
        unfiltered = _board_state(
            overlay, (0, 0, 240, 240), 4, 4
        )
        state = _board_state(
            overlay, (0, 0, 240, 240), 4, 4,
            occupied_cell_validator=validator,
        )

        self.assertIsNotNone(validator)
        self.assertGreater(
            sum(cell is not None for line in unfiltered for cell in line),
            0,
        )
        self.assertEqual(
            sum(cell is not None for line in state for cell in line),
            0,
        )

    def _themed_board(self):
        import cv2
        import numpy as np

        frame = np.full((240, 240, 3), (70, 150, 205), dtype=np.uint8)
        occupied = {(0, 1), (1, 0), (1, 1), (2, 2)}
        pitch = 60
        for row in range(4):
            for col in range(4):
                left, top = col * pitch, row * pitch
                cv2.rectangle(frame, (left, top), (left + 59, top + 59), (70, 150, 205), -1)
                if (row, col) in occupied:
                    cv2.rectangle(frame, (left + 8, top + 8), (left + 51, top + 51), (90, 190, 240), -1)
                    cv2.circle(frame, (left + 30, top + 30), 12, (40, 80, 150), 3)
        return frame, occupied

    def test_themed_board_uses_learned_empty_and_block_appearances(self):
        frame, occupied = self._themed_board()

        profile = _build_board_appearance_profile(frame, (0, 0, 240, 240), 4, 4)
        state = _board_state(frame, (0, 0, 240, 240), 4, 4, profile)

        self.assertIsNotNone(profile)
        self.assertEqual(profile["mode"], "adaptive_overfilled_appearance")
        self.assertEqual(profile["classifier"], "appearance_prototype")
        actual = {
            (row, col)
            for row in range(4)
            for col in range(4)
            if state[row][col] is not None
        }
        self.assertEqual(actual, occupied)

    def test_low_saturation_bright_blocks_recover_from_dark_board(self):
        import cv2
        import numpy as np

        frame = np.full((240, 240, 3), (48, 42, 38), dtype=np.uint8)
        occupied = {(0, 0), (0, 2), (1, 1), (2, 0), (2, 3), (3, 2)}
        for row, col in occupied:
            left, top = col * 60, row * 60
            cv2.rectangle(
                frame, (left + 6, top + 6), (left + 53, top + 53),
                (235, 235, 245), -1,
            )
            cv2.rectangle(
                frame, (left + 11, top + 11), (left + 48, top + 48),
                (205, 205, 220), 3,
            )

        profile = _build_board_appearance_profile(frame, (0, 0, 240, 240), 4, 4)
        state = _board_state(frame, (0, 0, 240, 240), 4, 4, profile)

        self.assertIsNotNone(profile)
        self.assertEqual(profile["mode"], "adaptive_underfilled_bright_blocks")
        actual = {
            (row, col)
            for row in range(4)
            for col in range(4)
            if state[row][col] is not None
        }
        self.assertEqual(actual, occupied)

    def test_uniform_board_does_not_enable_adaptive_profile(self):
        import numpy as np

        frame = np.full((240, 240, 3), (70, 150, 205), dtype=np.uint8)
        profile = _build_board_appearance_profile(frame, (0, 0, 240, 240), 4, 4)

        self.assertIsNone(profile)

    def test_sparse_flat_highlights_do_not_enable_bright_block_profile(self):
        import cv2
        import numpy as np

        frame = np.full((240, 240, 3), (48, 42, 38), dtype=np.uint8)
        for row, col in {(0, 0), (1, 2), (2, 1), (3, 3)}:
            left, top = col * 60, row * 60
            cv2.rectangle(
                frame, (left + 6, top + 6), (left + 53, top + 53),
                (235, 235, 245), -1,
            )

        profile = _build_board_appearance_profile(frame, (0, 0, 240, 240), 4, 4)

        self.assertIsNone(profile)

    def test_temporal_grid_fit_recovers_large_l_shape(self):
        import cv2
        import numpy as np

        mask = np.zeros((260, 260), dtype=np.uint8)
        expected = {(0, 0), (0, 1), (0, 2), (0, 3), (1, 3), (2, 3), (3, 3)}
        for row, col in expected:
            cv2.rectangle(
                mask,
                (col * 62 + 5, row * 62 + 5),
                (col * 62 + 60, row * 62 + 60),
                255,
                -1,
            )

        result = _grid_shape_from_mask(mask)

        self.assertIsNotNone(result)
        self.assertEqual(
            {(cell["row"], cell["col"]) for cell in result["shape"]},
            expected,
        )


class DragWindowTests(unittest.TestCase):
    def test_only_backfills_pickups_immediately_after_a_large_clear(self):
        events = [{
            "stepIndex": 4,
            "time": 10.0,
            "clearedRows": [0, 1, 2, 3, 4],
            "clearedCols": [0, 1, 2, 3, 4],
        }]

        self.assertEqual(
            _post_large_clear_predecessor(events, 12.5, 8, 8)["stepIndex"],
            4,
        )
        self.assertIsNone(_post_large_clear_predecessor(events, 14.0, 8, 8))
        events[0]["clearedRows"] = [0, 1]
        events[0]["clearedCols"] = [0, 1]
        self.assertIsNone(_post_large_clear_predecessor(events, 12.0, 8, 8))

    def test_pickup_must_remain_absent_before_it_is_auto_verified(self):
        samples = []
        for frame in range(40):
            activity = 0.08 if frame < 10 else 0.012
            samples.append({
                "time": frame / 30,
                "frameIndex": frame,
                "slotActivity": [activity, 0.04, 0.04],
            })
        pickup = {"time": 10 / 30, "frameIndex": 10, "sourceSlot": 0}

        evidence = _pickup_remains_consumed(samples, pickup)

        self.assertTrue(evidence["consumed"])
        for frame in range(25, 40):
            samples[frame]["slotActivity"][0] = 0.08
        self.assertFalse(_pickup_remains_consumed(samples, pickup)["consumed"])

    def test_unverified_candidate_after_grid_loss_is_discarded(self):
        original = board((2, 2))
        changed = board((1, 1), (2, 2))
        candidate = event(1, 0, 1, original, changed, 1, 1)
        candidate["confidence"] = "candidate"
        states = [
            {"stateIndex": 0, "startTime": 8.2, "gridAlignment": 0.8},
            {"stateIndex": 1, "startTime": 8.4, "gridAlignment": 0.7},
        ]

        kept, discarded = _filter_low_quality_tail_candidates([candidate], states, 10.0)

        self.assertEqual(kept, [])
        self.assertEqual(discarded[0]["reason"], "unverified_candidate_after_grid_loss")

    def test_verified_action_is_kept_after_grid_loss(self):
        original = board((2, 2))
        changed = board((1, 1), (2, 2))
        verified = event(1, 0, 1, original, changed, 1, 1)
        states = [
            {"stateIndex": 0, "startTime": 8.2, "gridAlignment": 0.8},
            {"stateIndex": 1, "startTime": 8.4, "gridAlignment": 0.7},
        ]

        kept, discarded = _filter_low_quality_tail_candidates([verified], states, 10.0)

        self.assertEqual(len(kept), 1)
        self.assertEqual(discarded, [])

    def test_candidate_with_clear_evidence_is_kept_after_grid_loss(self):
        original = board((2, 2))
        changed = board((1, 1), (2, 2))
        candidate = event(1, 0, 1, original, changed, 1, 1)
        candidate["confidence"] = "candidate"
        candidate["clearEffectEvidence"] = {"rows": [1], "cols": []}
        states = [
            {"stateIndex": 0, "startTime": 8.2, "gridAlignment": 0.8},
            {"stateIndex": 1, "startTime": 8.4, "gridAlignment": 0.7},
        ]

        kept, discarded = _filter_low_quality_tail_candidates([candidate], states, 10.0)

        self.assertEqual(len(kept), 1)
        self.assertEqual(discarded, [])

    def test_cancelled_drag_is_not_emitted_as_an_action(self):
        original = board((2, 2))
        overlay = board((1, 0), (1, 1), (2, 2))
        source_group = {"shape": [{"row": 0, "col": 0}, {"row": 0, "col": 1}]}
        states = [
            {"stateIndex": 0, "startTime": 0.0, "endTime": 0.3, "board": original, "groups": [source_group, None, None]},
            {"stateIndex": 1, "startTime": 0.4, "endTime": 0.7, "board": overlay, "groups": [None, None, None]},
            {"stateIndex": 2, "startTime": 0.8, "endTime": 1.1, "board": original, "groups": [source_group, None, None]},
        ]
        candidate = event(1, 0, 1, original, overlay, 1, 0)

        kept, cancelled = _filter_cancelled_drags([candidate], states)

        self.assertEqual(kept, [])
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["boardBefore"], original)
        self.assertEqual(cancelled[0]["discardedSourceSteps"], [1])
        self.assertEqual(cancelled[0]["type"], "cancelled_drag")
        self.assertEqual(cancelled[0]["sourceSlot"], 0)
        self.assertEqual(cancelled[0]["hoverTarget"], {"row": 1, "col": 0})
        self.assertFalse(cancelled[0]["boardMutation"])
        self.assertFalse(cancelled[0]["clearExecuted"])

    def test_repeated_preview_hover_is_one_cancelled_drag_with_two_passes(self):
        original = board((2, 2))
        overlay = board((1, 0), (1, 1), (2, 2))
        source_group = {"shape": [{"row": 0, "col": 0}, {"row": 0, "col": 1}]}
        states = [
            {"stateIndex": 0, "startTime": 0.0, "endTime": 0.3, "board": original, "groups": [source_group, None, None]},
            {"stateIndex": 1, "startTime": 0.4, "endTime": 1.0, "board": overlay, "groups": [None, None, None]},
            {"stateIndex": 2, "startTime": 1.05, "endTime": 1.15, "board": original, "groups": [source_group, None, None]},
            {"stateIndex": 3, "startTime": 1.2, "endTime": 1.9, "board": overlay, "groups": [None, None, None]},
            {"stateIndex": 4, "startTime": 1.95, "endTime": 2.2, "board": original, "groups": [source_group, None, None]},
        ]
        first = event(1, 0, 1, original, overlay, 1, 0)
        second = event(2, 2, 3, original, overlay, 1, 0)
        for item in (first, second):
            item["sourceSlot"] = 0
            item["group"] = {"slot": 0, "shape": [
                {"row": 0, "col": 0, "hue": 60},
                {"row": 0, "col": 1, "hue": 60},
            ], "rows": 1, "cols": 2, "cellCount": 2}
            item["clearedCols"] = [1]

        kept, cancelled = _filter_cancelled_drags([first, second], states)

        self.assertEqual(kept, [])
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["reason"], "multiple_hover_passes_then_returned_to_source")
        self.assertEqual(len(cancelled[0]["hoverPasses"]), 2)
        self.assertEqual([item["passIndex"] for item in cancelled[0]["hoverPasses"]], [1, 2])
        self.assertEqual(cancelled[0]["discardedSourceSteps"], [1, 2])

    def test_off_board_return_path_is_not_counted_as_hover_pass(self):
        original = board((0, 2))
        overlay = board((0, 2), (1, 0), (2, 0))
        clipped = board((0, 2), (2, 0))
        source_group = {"shape": [{"row": 0, "col": 0}, {"row": 1, "col": 0}]}
        states = [
            {"stateIndex": 0, "startTime": 0.0, "endTime": 0.3, "board": original, "groups": [source_group, None, None]},
            {"stateIndex": 1, "startTime": 0.4, "endTime": 0.8, "board": overlay, "groups": [None, None, None]},
            {"stateIndex": 2, "startTime": 0.9, "endTime": 1.0, "board": clipped, "groups": [None, None, None]},
            {"stateIndex": 3, "startTime": 1.1, "endTime": 1.3, "board": original, "groups": [source_group, None, None]},
        ]
        hover = event(1, 0, 1, original, overlay, 1, 0)
        return_path = event(2, 1, 2, overlay, clipped, 2, 0)
        for item in (hover, return_path):
            item["sourceSlot"] = 0
            item["group"] = {"slot": 0, "shape": [
                {"row": 0, "col": 0, "hue": 60},
                {"row": 1, "col": 0, "hue": 60},
            ], "rows": 2, "cols": 1, "cellCount": 2}

        kept, cancelled = _filter_cancelled_drags([hover, return_path], states)

        self.assertEqual(kept, [])
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(len(cancelled[0]["hoverPasses"]), 1)
        self.assertEqual(cancelled[0]["hoverPasses"][0]["target"], {"row": 1, "col": 0})

    def test_board_restoration_without_source_tray_restoration_is_kept(self):
        original = board((2, 2))
        overlay = board((1, 0), (1, 1), (2, 2))
        source_group = {"shape": [{"row": 0, "col": 0}, {"row": 0, "col": 1}]}
        states = [
            {"stateIndex": 0, "startTime": 0.0, "endTime": 0.3, "board": original, "groups": [source_group, None, None]},
            {"stateIndex": 1, "startTime": 0.4, "endTime": 0.7, "board": overlay, "groups": [None, None, None]},
            {"stateIndex": 2, "startTime": 0.8, "endTime": 1.1, "board": original, "groups": [None, None, None]},
        ]
        candidate = event(1, 0, 1, original, overlay, 1, 0)

        kept, cancelled = _filter_cancelled_drags([candidate], states)

        self.assertEqual(len(kept), 1)
        self.assertEqual(cancelled, [])

    def test_returned_piece_can_have_a_noisy_recognized_shape(self):
        original = board((2, 2))
        overlay = board((1, 0), (1, 1), (2, 2))
        source_group = {"shape": [{"row": 0, "col": 0}, {"row": 1, "col": 0}]}
        noisy_return = {"shape": [
            {"row": 0, "col": 0}, {"row": 0, "col": 1},
            {"row": 1, "col": 0}, {"row": 1, "col": 1},
            {"row": 2, "col": 0}, {"row": 2, "col": 1},
        ]}
        states = [
            {"stateIndex": 0, "startTime": 0.0, "endTime": 0.3, "board": original, "groups": [source_group, None, None]},
            {"stateIndex": 1, "startTime": 0.4, "endTime": 0.7, "board": overlay, "groups": [None, None, None]},
            {"stateIndex": 2, "startTime": 0.8, "endTime": 1.1, "board": original, "groups": [noisy_return, None, None]},
        ]
        candidate = event(1, 0, 1, original, overlay, 1, 0)

        kept, cancelled = _filter_cancelled_drags([candidate], states)

        self.assertEqual(kept, [])
        self.assertEqual(len(cancelled), 1)

    def test_cancelled_drag_ignores_noise_in_unrelated_slots(self):
        original = board((2, 2))
        overlay = board((1, 0), (1, 1), (2, 2))
        source_group = {"shape": [{"row": 0, "col": 0}, {"row": 0, "col": 1}]}
        unrelated_before = {"shape": [{"row": 0, "col": 0}]}
        unrelated_noisy = {"shape": [{"row": 0, "col": 0}, {"row": 1, "col": 0}]}
        states = [
            {"stateIndex": 0, "startTime": 0.0, "endTime": 0.3, "board": original, "groups": [source_group, unrelated_before, None]},
            {"stateIndex": 1, "startTime": 0.4, "endTime": 0.7, "board": overlay, "groups": [None, unrelated_before, None]},
            {"stateIndex": 2, "startTime": 0.8, "endTime": 1.1, "board": original, "groups": [source_group, unrelated_noisy, None]},
        ]
        candidate = event(1, 0, 1, original, overlay, 1, 0)

        kept, cancelled = _filter_cancelled_drags([candidate], states)

        self.assertEqual(kept, [])
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["reason"], "board_restored_and_source_piece_returned")

    def test_clear_evidence_prevents_cancelled_drag_filtering(self):
        original = board((2, 2))
        overlay = board((1, 0), (1, 1), (2, 2))
        source_group = {"shape": [{"row": 0, "col": 0}, {"row": 0, "col": 1}]}
        states = [
            {"stateIndex": 0, "startTime": 0.0, "endTime": 0.3, "board": original, "groups": [source_group, None, None]},
            {"stateIndex": 1, "startTime": 0.4, "endTime": 0.7, "board": overlay, "groups": [None, None, None]},
            {"stateIndex": 2, "startTime": 0.8, "endTime": 1.1, "board": original, "groups": [source_group, None, None]},
        ]
        candidate = event(1, 0, 1, original, overlay, 1, 0)
        candidate["clearEffectEvidence"] = {"rows": [1], "cols": []}

        kept, cancelled = _filter_cancelled_drags([candidate], states)

        self.assertEqual(len(kept), 1)
        self.assertEqual(cancelled, [])

    def test_color_clear_cooldown_drops_weak_tiny_fragment(self):
        events = [
            {
                "stepIndex": 1,
                "time": 1.0,
                "clearMode": "immediate",
                "clearedRows": [2],
                "clearedCols": [],
                "group": {"cellCount": 4},
                "sourceSlot": 1,
                "confidence": "verified",
            },
            {
                "stepIndex": 2,
                "time": 1.22,
                "clearMode": "none",
                "clearedRows": [],
                "clearedCols": [],
                "group": {"cellCount": 1},
                "sourceSlot": -1,
                "confidence": "candidate",
            },
            {
                "stepIndex": 3,
                "time": 1.5,
                "clearMode": "none",
                "clearedRows": [],
                "clearedCols": [],
                "group": {"cellCount": 2},
                "sourceSlot": 0,
                "confidence": "candidate",
                "sourcePickupEvidence": {"sourceSlot": 0},
            },
        ]

        filtered, discarded = _filter_color_clear_cooldown_fragments(events)

        self.assertEqual([event["stepIndex"] for event in filtered], [1, 2])
        self.assertEqual(discarded[0]["reason"], "color_block_clear_cooldown_fragment")

    def test_drag_overlay_is_collapsed_to_terminal_shape(self):
        empty = board()
        overlay = board((1, 0), (1, 1), (2, 0), (2, 1))
        settled = board((0, 0), (1, 0), (2, 0))
        first = event(1, 0, 1, empty, overlay)
        second = event(2, 1, 2, overlay, settled)
        for item in (first, second):
            item["confidence"] = "candidate"
        first.update({"time": 1.0, "addedCells": [{"row": 1, "col": 0}], "removedCells": []})
        second.update({"time": 1.4, "addedCells": [{"row": 0, "col": 0}], "removedCells": [{"row": 1, "col": 1}]})
        states = [
            {"stateIndex": 0, "board": empty, "groups": [None, None, None], "frameCount": 8},
            {"stateIndex": 1, "board": overlay, "groups": [None, None, None], "frameCount": 3},
            {"stateIndex": 2, "board": settled, "groups": [None, None, None], "frameCount": 8},
        ]

        result = _collapse_drag_windows([first, second], states, 3, 3)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["verification"], "drag_window_collapsed_by_terminal_state")
        self.assertEqual(result[0]["target"], {"row": 0, "col": 0})
        self.assertEqual(result[0]["group"]["cellCount"], 3)

    def test_two_pure_additions_are_not_collapsed(self):
        empty = board()
        first_board = board((2, 0))
        second_board = board((2, 0), (2, 1))
        first = event(1, 0, 1, empty, first_board, 2, 0)
        second = event(2, 1, 2, first_board, second_board, 2, 1)
        for item, at in ((first, (2, 0)), (second, (2, 1))):
            item["confidence"] = "candidate"
            item["addedCells"] = [{"row": at[0], "col": at[1]}]
            item["removedCells"] = []
        first["time"], second["time"] = 1.0, 1.4
        states = [
            {"stateIndex": 0, "board": empty, "groups": [None, None, None], "frameCount": 8},
            {"stateIndex": 1, "board": first_board, "groups": [None, None, None], "frameCount": 8},
            {"stateIndex": 2, "board": second_board, "groups": [None, None, None], "frameCount": 8},
        ]

        result = _collapse_drag_windows([first, second], states, 3, 3)

        self.assertEqual(len(result), 2)

    def test_short_contiguous_fragments_are_merged_into_complete_shape(self):
        empty = board()
        partial = board((1, 0), (2, 0))
        complete = board((1, 0), (2, 0), (2, 1))
        first = event(1, 0, 1, empty, partial, 1, 0)
        second = event(2, 1, 2, partial, complete, 2, 1)
        first.update({"time": 1.0, "sourceSlot": -1, "confidence": "candidate", "addedCells": [{"row": 1, "col": 0}, {"row": 2, "col": 0}]})
        second.update({"time": 1.3, "sourceSlot": -1, "confidence": "candidate", "addedCells": [{"row": 2, "col": 1}]})
        states = [
            {"stateIndex": 0, "board": empty, "groups": [None] * 3, "groupCandidates": []},
            {"stateIndex": 1, "board": partial, "groups": [None] * 3, "groupCandidates": []},
            {"stateIndex": 2, "board": complete, "groups": [None] * 3, "groupCandidates": []},
        ]

        result = _coalesce_action_fragments([first, second], states, 3, 3)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["target"], {"row": 1, "col": 0})
        self.assertEqual(result[0]["group"]["cellCount"], 3)
        self.assertEqual(result[0]["collapsedSourceSteps"], [1, 2])

    def test_actions_over_window_boundary_remain_separate(self):
        empty = board()
        first_board = board((2, 0))
        second_board = board((2, 0), (2, 1))
        first = event(1, 0, 1, empty, first_board, 2, 0)
        second = event(2, 1, 2, first_board, second_board, 2, 1)
        first.update({"time": 1.0, "addedCells": [{"row": 2, "col": 0}]})
        second.update({"time": 1.71, "addedCells": [{"row": 2, "col": 1}]})
        states = [
            {"stateIndex": 0, "board": empty, "groups": [None] * 3, "groupCandidates": []},
            {"stateIndex": 1, "board": first_board, "groups": [None] * 3, "groupCandidates": []},
            {"stateIndex": 2, "board": second_board, "groups": [None] * 3, "groupCandidates": []},
        ]

        result = _coalesce_action_fragments([first, second], states, 3, 3)

        self.assertEqual(len(result), 2)

    def test_recognized_slot_change_starts_a_new_action(self):
        empty = board()
        first_board = board((2, 0))
        second_board = board((2, 0), (2, 1))
        first = event(1, 0, 1, empty, first_board, 2, 0)
        second = event(2, 1, 2, first_board, second_board, 2, 1)
        first.update({"time": 1.0, "sourceSlot": 0, "addedCells": [{"row": 2, "col": 0}]})
        second.update({"time": 1.3, "sourceSlot": 1, "addedCells": [{"row": 2, "col": 1}]})
        states = [
            {"stateIndex": 0, "board": empty, "groups": [None] * 3, "groupCandidates": []},
            {"stateIndex": 1, "board": first_board, "groups": [None] * 3, "groupCandidates": []},
            {"stateIndex": 2, "board": second_board, "groups": [None] * 3, "groupCandidates": []},
        ]

        result = _coalesce_action_fragments([first, second], states, 3, 3)

        self.assertEqual(len(result), 2)

    def test_skipped_stable_state_marks_distinct_actions(self):
        empty = board()
        first_board = board((2, 0), (2, 1), (2, 2))
        second_board = board((1, 0), (1, 1), (2, 0), (2, 1), (2, 2))
        first = event(1, 0, 1, empty, first_board, 2, 0)
        second = event(2, 2, 3, first_board, second_board, 1, 0)
        first.update({"time": 1.0, "sourceSlot": -1, "confidence": "candidate"})
        second.update({"time": 1.6, "sourceSlot": -1, "confidence": "candidate"})
        states = [
            {"stateIndex": 0, "board": empty, "groups": [None] * 3, "groupCandidates": []},
            {"stateIndex": 1, "board": first_board, "groups": [None] * 3, "groupCandidates": []},
            {"stateIndex": 2, "board": first_board, "groups": [None] * 3, "groupCandidates": []},
            {"stateIndex": 3, "board": second_board, "groups": [None] * 3, "groupCandidates": []},
        ]

        result = _coalesce_action_fragments([first, second], states, 3, 3)

        self.assertEqual(len(result), 2)


class PresetBoundaryTests(unittest.TestCase):
    def test_direct_post_clear_board_replacement_is_a_preset(self):
        clearing_board = board((0, 0), (0, 1), (0, 2), (1, 0))
        preset_board = board((1, 1), (2, 0), (2, 1))

        kind, added, removed = _classify_stable_transition(
            clearing_board,
            preset_board,
            follows_clear=True,
        )

        self.assertEqual(kind, "preset_load")
        self.assertTrue(added)
        self.assertTrue(removed)

    def test_unexplained_board_replacement_is_not_assumed_to_be_a_preset(self):
        before = board((0, 0), (0, 1), (1, 0))
        after = board((1, 1), (2, 0), (2, 1))

        kind, _, _ = _classify_stable_transition(before, after)

        self.assertEqual(kind, "unresolved")

    def test_clear_effect_gap_does_not_reset_board(self):
        timeline = timeline_with_transition("clear_effect")
        self.assertEqual(preset_break_steps(timeline), set())
        self.assertFalse(build_action_review(timeline)[1]["resetBefore"])
        self.assertEqual(build_replay_truth(timeline, "test.mp4")["observedPresetTransitions"], [])

    def test_explicit_preset_load_resets_board(self):
        timeline = timeline_with_transition("preset_load")
        self.assertEqual(preset_break_steps(timeline), {2})
        self.assertTrue(build_action_review(timeline)[1]["resetBefore"])
        transitions = build_replay_truth(timeline, "test.mp4")["observedPresetTransitions"]
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["afterStepIndex"], 1)

    def test_preset_board_is_not_used_as_clear_evidence(self):
        timeline = timeline_with_transition("preset_load")
        timeline["events"][0]["clearedRows"] = [0]
        timeline["events"][0]["clearMode"] = "deferred"
        review = build_action_review(timeline)
        self.assertEqual(review[0]["clearState"], "unknown")
        self.assertIn("棋盘切换", review[0]["clearEvidence"])


class EffectDrivenReviewTests(unittest.TestCase):
    def test_does_not_infer_clear_from_a_later_board_count(self):
        timeline = timeline_with_transition("clear_effect")
        timeline["recognitionStrategy"] = "reverse_clear_v1"
        timeline["events"][0]["clearedRows"] = [0]
        timeline["events"][0]["clearMode"] = "deferred"
        completed_row = board((0, 0), (0, 1), (0, 2))
        timeline["events"][0]["afterBoard"] = completed_row
        timeline["stableStates"][2]["board"] = completed_row

        review = build_action_review(timeline)

        self.assertEqual(review[0]["clearState"], "off")

    def test_observed_effect_is_marked_as_on(self):
        timeline = timeline_with_transition("clear_effect")
        timeline["recognitionStrategy"] = "reverse_clear_v1"
        timeline["events"][0]["clearedRows"] = [0]
        timeline["events"][0]["clearEffectEvidence"] = {
            "rows": [0], "cols": [], "startTime": 0.2, "endTime": 0.3
        }

        review = build_action_review(timeline)

        self.assertEqual(review[0]["clearState"], "on")
        self.assertIsNotNone(review[0]["clearEffectEvidence"])


class ConfirmationTests(unittest.TestCase):
    def test_manual_action_can_be_inserted_between_recognized_steps(self):
        timeline = timeline_with_transition("clear_effect")
        actions = [
            {"stepIndex": 1, "originalStepIndex": 1, "sourceSlot": 0, "target": {"row": 0, "col": 0}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
            {
                "stepIndex": 2, "manualAdded": True, "sourceSlot": 2,
                "target": {"row": 0, "col": 1}, "shape": [{"row": 0, "col": 0}],
                "clearState": "off", "timeRanges": {"placed": {"start": 1.5, "end": 1.6}},
            },
            {"stepIndex": 3, "originalStepIndex": 2, "sourceSlot": 1, "target": {"row": 1, "col": 1}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
        ]

        truth = apply_confirmed_actions(timeline, actions, "test.mp4", [])

        self.assertEqual(truth["stepCount"], 3)
        self.assertTrue(truth["steps"][1]["manualAdded"])
        self.assertEqual(truth["steps"][1]["executeAt"], 1.5)
        self.assertEqual(truth["steps"][1]["target"], {"row": 0, "col": 1})

        saved_again = apply_confirmed_actions(timeline, actions, "test.mp4", [])
        self.assertEqual([step["executeAt"] for step in saved_again["steps"]], [1.0, 1.5, 2.0])
        self.assertEqual(saved_again["steps"][2]["target"], {"row": 1, "col": 1})

    def test_confirmed_cancelled_drag_is_saved_as_reference_interaction(self):
        timeline = timeline_with_transition("clear_effect")
        actions = [
            {"stepIndex": 1, "sourceSlot": 0, "target": {"row": 0, "col": 0}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
            {"stepIndex": 2, "sourceSlot": 1, "target": {"row": 1, "col": 1}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
        ]
        reference = [{
            "type": "cancelled_drag",
            "sourceSlot": 2,
            "shape": [{"row": 0, "col": 0}, {"row": 0, "col": 1}],
            "hoverTarget": {"row": 2, "col": 0},
            "startTime": 0.2,
            "endTime": 0.8,
            "manuallyVerified": True,
            "previewClearedRows": [],
            "previewClearedCols": [1],
            "hoverPasses": [
                {"startTime": 0.25, "endTime": 0.4, "target": {"row": 2, "col": 0}, "previewClearedRows": [], "previewClearedCols": [1]},
                {"startTime": 0.5, "endTime": 0.7, "target": {"row": 2, "col": 0}, "previewClearedRows": [], "previewClearedCols": [1]},
            ],
        }]

        truth = apply_confirmed_actions(timeline, actions, "test.mp4", reference)

        self.assertEqual(truth["stepCount"], 2)
        self.assertEqual(len(truth["referenceInteractions"]), 1)
        self.assertTrue(truth["referenceInteractions"][0]["returnedToSource"])
        self.assertFalse(truth["referenceInteractions"][0]["clearExecuted"])
        self.assertEqual(len(truth["referenceInteractions"][0]["hoverPasses"]), 2)
        self.assertEqual(truth["referenceInteractions"][0]["returnCompleteTime"], 0.8)
        self.assertEqual(truth["referenceInteractions"][0]["afterStepIndex"], 0)
        self.assertEqual(truth["referenceInteractions"][0]["beforeStepIndex"], 1)

    def test_cancelled_drag_return_must_follow_last_hover(self):
        timeline = timeline_with_transition("clear_effect")
        actions = [
            {"stepIndex": 1, "sourceSlot": 0, "target": {"row": 0, "col": 0}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
            {"stepIndex": 2, "sourceSlot": 1, "target": {"row": 1, "col": 1}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
        ]
        reference = [{
            "type": "cancelled_drag", "sourceSlot": 2,
            "shape": [{"row": 0, "col": 0}], "hoverTarget": {"row": 1, "col": 1},
            "startTime": 0.2, "endTime": 0.6, "manuallyVerified": True,
            "hoverPasses": [{"startTime": 0.3, "endTime": 0.7, "target": {"row": 1, "col": 1}}],
        }]

        with self.assertRaisesRegex(ValueError, "完全归位时间早于最后一次悬停结束"):
            apply_confirmed_actions(timeline, actions, "test.mp4", reference)

    def test_unconfirmed_cancelled_drag_is_rejected(self):
        timeline = timeline_with_transition("clear_effect")
        actions = [
            {"stepIndex": 1, "sourceSlot": 0, "target": {"row": 0, "col": 0}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
            {"stepIndex": 2, "sourceSlot": 1, "target": {"row": 1, "col": 1}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
        ]
        reference = [{"type": "cancelled_drag", "sourceSlot": 2, "shape": [{"row": 0, "col": 0}], "hoverTarget": {"row": 1, "col": 1}, "startTime": 0.2, "endTime": 0.8}]

        with self.assertRaisesRegex(ValueError, "尚未人工确认"):
            apply_confirmed_actions(timeline, actions, "test.mp4", reference)

    def test_manual_initial_board_correction_becomes_truth_initial_board(self):
        timeline = timeline_with_transition("clear_effect")
        corrected = board((0, 2))
        actions = [
            {"stepIndex": 1, "sourceSlot": 0, "target": {"row": 0, "col": 0}, "shape": [{"row": 0, "col": 0}], "clearState": "off", "manualBeforeBoard": corrected},
            {"stepIndex": 2, "sourceSlot": 1, "target": {"row": 1, "col": 1}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
        ]

        truth = apply_confirmed_actions(timeline, actions, "test.mp4")

        self.assertEqual(truth["initialBoard"], corrected)
        self.assertEqual(truth["steps"][0]["expectedBoardBefore"], corrected)
        self.assertEqual(truth["steps"][0]["beforeBoardSource"], "manual_correction")

    def test_later_manual_board_correction_creates_replay_checkpoint(self):
        timeline = timeline_with_transition("clear_effect")
        corrected = board((0, 0), (0, 2))
        actions = [
            {"stepIndex": 1, "sourceSlot": 0, "target": {"row": 0, "col": 0}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
            {"stepIndex": 2, "sourceSlot": 1, "target": {"row": 1, "col": 1}, "shape": [{"row": 0, "col": 0}], "clearState": "off", "manualBeforeBoard": corrected},
        ]

        truth = apply_confirmed_actions(timeline, actions, "test.mp4")

        checkpoint = next(item for item in truth["observedPresetTransitions"] if item["afterStepIndex"] == 1)
        self.assertTrue(checkpoint["manualBoardCorrection"])
        self.assertEqual(checkpoint["expectedBoard"], corrected)
        self.assertEqual(truth["steps"][1]["expectedBoardBefore"], corrected)

    def test_review_carries_annotation_evidence_and_ranges(self):
        timeline = timeline_with_transition("clear_effect")
        timeline["events"][0]["evidenceTimes"] = {"before": 0.1, "action": 0.2, "placed": 0.3, "cleared": None}
        timeline["events"][0]["timeRanges"] = {"before": {"start": 0.0, "end": 0.1}}
        review = build_action_review(timeline)
        self.assertEqual(review[0]["evidenceTimes"]["placed"], 0.3)
        self.assertEqual(review[0]["timeRanges"]["before"]["end"], 0.1)

    def test_review_scores_action_components_independently(self):
        timeline = timeline_with_transition("clear_effect")

        action = build_action_review(timeline)[0]

        confidence = action["componentConfidence"]
        self.assertGreaterEqual(confidence["shape"], 0.9)
        self.assertGreaterEqual(confidence["target"], 0.9)
        self.assertGreaterEqual(confidence["slot"], 0.7)
        self.assertGreaterEqual(confidence["clear"], 0.9)
        self.assertFalse(action["requiresComponentReview"])

    def test_review_suggests_clear_repair_when_rule_simulation_disagrees(self):
        timeline = timeline_with_transition("clear_effect")
        before = board((0, 0), (0, 1))
        after = board((0, 0), (0, 1), (0, 2))
        timeline["stableStates"][0]["board"] = before
        timeline["stableStates"][1]["board"] = after
        timeline["events"][0] = event(1, 0, 1, before, after, 0, 2)
        timeline["events"][0]["clearMode"] = "unknown"

        action = build_action_review(timeline)[0]

        self.assertLess(action["componentConfidence"]["clear"], 0.55)
        self.assertIn("clear", action["suspiciousComponents"])
        self.assertIn(
            {"component": "clear", "reason": "clear_state_unknown", "suggestedValue": "on"},
            action["repairHints"],
        )

    def test_sequence_repair_clear_requires_experiment_flag(self):
        timeline = timeline_with_transition("clear_effect")
        before = board((0, 0), (0, 1))
        after = board((0, 0), (0, 1), (0, 2))
        resolved = board()
        timeline["stableStates"][0]["board"] = before
        timeline["stableStates"][1]["board"] = after
        timeline["stableStates"][2]["board"] = resolved
        timeline["events"][0] = event(1, 0, 1, before, after, 0, 2)
        timeline["events"][0]["clearMode"] = "unknown"

        action = build_action_review(timeline)[0]

        self.assertEqual(action["clearState"], "unknown")
        self.assertNotIn("autoRepair", action)

    def test_sequence_repair_clear_applies_when_replay_improves(self):
        timeline = timeline_with_transition("clear_effect")
        timeline["experimentFlags"] = {"enabled": ["sequence_repair_clear_v1"]}
        before = board((0, 0), (0, 1))
        after = board((0, 0), (0, 1), (0, 2))
        resolved = board()
        timeline["stableStates"][0]["board"] = before
        timeline["stableStates"][1]["board"] = after
        timeline["stableStates"][2]["board"] = resolved
        timeline["events"][0] = event(1, 0, 1, before, after, 0, 2)
        timeline["events"][0]["clearMode"] = "unknown"

        action = build_action_review(timeline)[0]

        self.assertEqual(action["clearState"], "on")
        self.assertEqual(action["autoRepair"]["component"], "clear")
        self.assertGreater(action["autoRepair"]["improvement"], 0)

    def test_review_marks_out_of_bounds_target_as_low_confidence(self):
        timeline = timeline_with_transition("clear_effect")
        timeline["events"][0]["target"] = {"row": 3, "col": 0}

        action = build_action_review(timeline)[0]

        self.assertLess(action["componentConfidence"]["target"], 0.65)
        self.assertTrue(any(hint["reason"] == "target_realigned_to_observed_board_delta" for hint in action["repairHints"]))

    def test_sequence_repair_target_applies_when_replay_improves(self):
        timeline = timeline_with_transition("clear_effect")
        timeline["recognitionStrategy"] = "color_block_v1"
        timeline["experimentFlags"] = {"enabled": ["sequence_repair_shape_target_v1"]}
        before = board()
        after = board((0, 1))
        timeline["stableStates"][0]["board"] = before
        timeline["stableStates"][1]["board"] = after
        timeline["stableStates"][2]["board"] = after
        timeline["events"][0] = event(1, 0, 1, before, after, 0, 0)
        timeline["events"][0]["target"] = {"row": 0, "col": 0}

        action = build_action_review(timeline)[0]

        self.assertEqual(action["target"], {"row": 0, "col": 1})
        self.assertEqual(action["autoRepair"]["component"], "target")

    def test_sequence_repair_shape_applies_when_replay_improves(self):
        timeline = timeline_with_transition("clear_effect")
        timeline["recognitionStrategy"] = "color_block_v1"
        timeline["experimentFlags"] = {"enabled": ["sequence_repair_shape_target_v1"]}
        before = board()
        after = board((0, 0), (0, 1))
        timeline["stableStates"][0]["board"] = before
        timeline["stableStates"][1]["board"] = after
        timeline["stableStates"][2]["board"] = after
        timeline["events"][0] = event(1, 0, 1, before, after, 0, 0)

        action = build_action_review(timeline)[0]

        self.assertEqual(action["shape"], [{"row": 0, "col": 0}, {"row": 0, "col": 1}])
        self.assertEqual(action["autoRepair"]["component"], "shape")

    def test_old_draft_timing_is_recovered_from_evidence_filename(self):
        analysis = {
            "duration": 3.0,
            "reviewActions": [{
                "evidenceFrames": {
                    "before": "动作帧/step_01_before_0.233s.jpg",
                    "action": "动作帧/step_01_action_0.416s.jpg",
                    "placed": "动作帧/step_01_placed_0.733s.jpg",
                    "cleared": "动作帧/step_01_cleared_1.300s.jpg",
                }
            }],
        }
        backfill_annotation_timing(analysis)
        action = analysis["reviewActions"][0]
        self.assertEqual(action["evidenceTimes"]["placed"], 0.733)
        self.assertEqual(action["timeRanges"]["drag"], {"start": 0.416, "end": 0.733})
        self.assertEqual(action["timeRanges"]["clear"], {"start": 1.3, "end": 1.7})

    def test_deleted_step_is_removed_and_remaining_steps_are_renumbered(self):
        timeline = timeline_with_transition("clear_effect")
        actions = [
            {"stepIndex": 1, "sourceSlot": 0, "target": {"row": 0, "col": 0}, "shape": [{"row": 0, "col": 0}], "clearState": "off", "deleted": True},
            {"stepIndex": 1, "sourceSlot": 1, "target": {"row": 1, "col": 1}, "shape": [{"row": 0, "col": 0}], "clearState": "off"},
        ]
        truth = apply_confirmed_actions(timeline, actions, "test.mp4")
        self.assertEqual(truth["stepCount"], 1)
        self.assertEqual(len(timeline["events"]), 1)
        self.assertEqual(timeline["events"][0]["stepIndex"], 1)

    def test_overlap_is_rejected(self):
        initial = board((0, 0))
        timeline = {
            "grid": {"rows": 3, "cols": 3},
            "sourceFrameRate": 30,
            "events": [event(1, 0, 1, initial, initial, 0, 0)],
            "replayCandidates": [{"stepIndex": 1, "sourceSlot": 0, "target": {"row": 0, "col": 0}, "shape": [{"row": 0, "col": 0, "hue": 60},], "cellCount": 1, "clearedRows": [], "clearedCols": [], "clearMode": "none", "confidence": "candidate"}],
            "stableStates": [{"stateIndex": 0, "board": initial}, {"stateIndex": 1, "board": initial}],
            "transitions": [],
            "validation": {},
        }
        action = {"stepIndex": 1, "sourceSlot": 0, "target": {"row": 0, "col": 0}, "shape": [{"row": 0, "col": 0}], "clearState": "off"}
        with self.assertRaisesRegex(ValueError, "重叠"):
            apply_confirmed_actions(timeline, [action], "test.mp4")

    def test_out_of_bounds_is_rejected(self):
        initial = board()
        after = board((2, 2))
        timeline = {
            "grid": {"rows": 3, "cols": 3},
            "sourceFrameRate": 30,
            "events": [event(1, 0, 1, initial, after, 2, 2)],
            "replayCandidates": [{"stepIndex": 1, "sourceSlot": 0, "target": {"row": 2, "col": 2}, "shape": [{"row": 0, "col": 0, "hue": 60}], "cellCount": 1, "clearedRows": [], "clearedCols": [], "clearMode": "none", "confidence": "candidate"}],
            "stableStates": [{"stateIndex": 0, "board": initial}, {"stateIndex": 1, "board": after}],
            "transitions": [],
            "validation": {},
        }
        action = {"stepIndex": 1, "sourceSlot": 0, "target": {"row": 2, "col": 2}, "shape": [{"row": 0, "col": 0}, {"row": 1, "col": 0}], "clearState": "off"}
        with self.assertRaisesRegex(ValueError, "超出棋盘"):
            apply_confirmed_actions(timeline, [action], "test.mp4")


class ByteRangeTests(unittest.TestCase):
    def test_explicit_range_is_clamped_to_file(self):
        self.assertEqual(parse_byte_range("bytes=100-999", 500), (100, 499))

    def test_open_and_suffix_ranges(self):
        self.assertEqual(parse_byte_range("bytes=400-", 500), (400, 499))
        self.assertEqual(parse_byte_range("bytes=-50", 500), (450, 499))

    def test_unsatisfiable_range_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_byte_range("bytes=500-600", 500)


class SourceSlotPickupTests(unittest.TestCase):
    def test_material_consensus_ignores_a_noisy_partial_shape(self):
        stable_shape = [
            {"row": 0, "col": 0, "hue": 50},
            {"row": 0, "col": 1, "hue": 50},
            {"row": 1, "col": 0, "hue": 55},
            {"row": 1, "col": 1, "hue": 165},
        ]
        candidates = []
        for frame_index in (31, 35, 38, 41):
            candidates.append(({
                "slot": 0,
                "shape": copy.deepcopy(stable_shape),
                "rows": 2,
                "cols": 2,
                "cellCount": 4,
            }, f"frame-{frame_index}", frame_index))
        candidates.append(({
            "slot": 0,
            "shape": [{"row": 1, "col": 1, "hue": 165}],
            "rows": 2,
            "cols": 2,
            "cellCount": 1,
        }, "noisy-frame", 44))

        evidence, before, frame_index = _source_slot_material_consensus(
            candidates, after_group=None
        )

        self.assertEqual(evidence["cellCount"], 4)
        self.assertEqual(evidence["recognition"], "source_slot_material_consensus")
        self.assertEqual(evidence["consensusSupport"], 4)
        self.assertEqual(frame_index, 41)
        self.assertEqual(before, "frame-41")
        self.assertEqual({cell["hue"] for cell in evidence["shape"]}, {50, 55, 165})

    def test_bottom_group_keeps_pale_and_saturated_cells_in_one_mixed_piece(self):
        import cv2
        import numpy as np

        frame = np.full((940, 528, 3), 250, dtype=np.uint8)
        board = (8, 216, 512, 512)
        cell = 27
        left = 66
        top = 780
        colors = {
            (0, 0): (205, 229, 205),
            (0, 1): (205, 229, 205),
            (1, 0): (205, 229, 205),
            (1, 1): (205, 105, 235),
        }
        for (row, col), color in colors.items():
            x0 = left + col * cell
            y0 = top + row * cell
            cv2.rectangle(frame, (x0, y0), (x0 + 23, y0 + 23), color, -1)
            cv2.line(frame, (x0 + 7, y0), (x0 + 7, y0 + 23), (235, 242, 235), 2)
            cv2.line(frame, (x0, y0 + 7), (x0 + 23, y0 + 7), (235, 242, 235), 2)

        groups = _bottom_groups(frame, board, 8, 8)

        self.assertIsNotNone(groups[0])
        self.assertEqual(groups[0]["cellCount"], 4)
        self.assertEqual(
            [(cell["row"], cell["col"]) for cell in groups[0]["shape"]],
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )
        self.assertGreater(
            len({cell["hue"] for cell in groups[0]["shape"]}),
            1,
        )

    def test_backfill_only_runs_for_a_systemic_coverage_gap(self):
        self.assertFalse(_has_pickup_coverage_gap(30, 40))
        self.assertFalse(_has_pickup_coverage_gap(5, 10))
        self.assertTrue(_has_pickup_coverage_gap(29, 50))

    def test_detects_sustained_activity_drop_in_one_slot(self):
        samples = []
        for frame_index in range(30):
            middle_activity = 0.012 if frame_index <= 11 else 0.0002
            samples.append({
                "time": frame_index / 30,
                "frameIndex": frame_index,
                "slotActivity": [0.01, middle_activity, 0.009],
            })

        pickups = _detect_source_slot_pickups(samples, 30)

        self.assertEqual(len(pickups), 1)
        self.assertEqual(pickups[0]["sourceSlot"], 1)
        self.assertGreaterEqual(pickups[0]["frameIndex"], 8)
        self.assertLessEqual(pickups[0]["frameIndex"], 14)

    def test_ignores_global_activity_drop(self):
        samples = []
        for frame_index in range(30):
            activity = 0.012 if frame_index <= 11 else 0.0002
            samples.append({
                "time": frame_index / 30,
                "frameIndex": frame_index,
                "slotActivity": [activity, activity, activity],
            })

        pickups = _detect_source_slot_pickups(samples, 30)

        self.assertEqual(pickups, [])

    def test_recovers_full_scale_piece_from_tray_difference(self):
        import cv2
        import numpy as np

        before = np.zeros((1300, 1100, 3), dtype=np.uint8)
        before[:] = (130, 70, 150)
        after = before.copy()
        board = (100, 100, 800, 800)
        cells = ((0, 0), (1, 0), (1, 1), (2, 0))
        for row, col in cells:
            left = 440 + col * 100
            top = 910 + row * 100
            cv2.rectangle(before, (left, top), (left + 94, top + 94), (30, 80, 245), -1)

        evidence = _pickup_piece_shape(before, after, board, 8, 8)

        self.assertIsNotNone(evidence)
        self.assertEqual(
            [(cell["row"], cell["col"]) for cell in evidence["shape"]],
            list(cells),
        )


class ReverseClearTests(unittest.TestCase):
    @staticmethod
    def _clear_event(before):
        return {
            "stepIndex": 1,
            "sourceStateIndex": 0,
            "targetStateIndex": 1,
            "time": 0.55,
            "group": {"shape": [{"row": 0, "col": 0}]},
            "target": {"row": 2, "col": 2},
            "beforeBoard": before,
            "clearedRows": [],
            "clearedCols": [],
            "clearMode": "none",
        }

    def test_clear_requires_rule_effect_and_disappearance(self):
        before = board((2, 0), (2, 1))
        empty = board()
        quiet = [[0.0 for _ in range(3)] for _ in range(3)]
        row_effect = [[0.0] * 3, [0.0] * 3, [0.85] * 3]
        samples = [
            {"time": 0.5, "frameIndex": 15, "boardMotion": quiet, "board": before},
            {"time": 0.6, "frameIndex": 18, "boardMotion": row_effect, "board": before},
            {"time": 0.7, "frameIndex": 21, "boardMotion": quiet, "board": empty},
            {"time": 0.8, "frameIndex": 24, "boardMotion": quiet, "board": empty},
        ]
        events = [self._clear_event(before)]
        states = [{"stateIndex": 0, "endTime": 0.45}]

        _apply_rule_verified_clear_detection(events, samples, states, 3, 3, 1.0)

        self.assertEqual(events[0]["clearMode"], "immediate")
        self.assertEqual(events[0]["clearedRows"], [2])
        self.assertEqual(events[0]["clearedCols"], [])
        self.assertEqual(events[0]["clearVerification"], "rule_effect_and_disappearance_verified")

    def test_completed_line_without_effect_stays_unknown(self):
        before = board((2, 0), (2, 1))
        empty = board()
        quiet = [[0.0 for _ in range(3)] for _ in range(3)]
        samples = [
            {"time": time, "frameIndex": index, "boardMotion": quiet, "board": empty}
            for index, time in enumerate((0.6, 0.7, 0.8), 18)
        ]
        events = [self._clear_event(before)]

        _apply_rule_verified_clear_detection(
            events, samples, [{"stateIndex": 0, "endTime": 0.45}], 3, 3, 1.0
        )

        self.assertEqual(events[0]["clearMode"], "unknown")
        self.assertEqual(events[0]["clearVerification"], "rule_completed_line_but_effect_not_verified")

    def test_stable_board_can_confirm_line_disappearance_after_effect(self):
        before = board((2, 0), (2, 1))
        quiet = [[0.0 for _ in range(3)] for _ in range(3)]
        row_effect = [[0.0] * 3, [0.0] * 3, [0.85] * 3]
        samples = [
            {"time": 0.6, "frameIndex": 18, "boardMotion": row_effect, "board": before},
            {"time": 0.7, "frameIndex": 21, "boardMotion": quiet, "board": before},
        ]
        events = [self._clear_event(before)]
        states = [
            {"stateIndex": 0, "endTime": 0.45, "startTime": 0.0, "frameIndex": 0, "frameCount": 10, "board": before},
            {"stateIndex": 1, "endTime": 0.9, "startTime": 0.75, "frameIndex": 23, "frameCount": 5, "board": board()},
        ]

        _apply_rule_verified_clear_detection(events, samples, states, 3, 3, 1.0)

        self.assertEqual(events[0]["clearMode"], "immediate")
        self.assertEqual(
            events[0]["clearResolutionEvidence"]["verification"],
            "stable_board_cleared_lines_absent",
        )

    def test_visual_effect_cannot_create_a_clear_without_a_completed_line(self):
        before = board((0, 0))
        row_effect = [[0.85] * 3, [0.0] * 3, [0.0] * 3]
        samples = [
            {"time": 0.6, "frameIndex": 18, "boardMotion": row_effect, "board": before},
            {"time": 0.7, "frameIndex": 21, "boardMotion": row_effect, "board": before},
        ]
        event_item = self._clear_event(before)

        _apply_rule_verified_clear_detection(
            [event_item], samples, [{"stateIndex": 0, "endTime": 0.45}], 3, 3, 1.0
        )

        self.assertEqual(event_item["clearMode"], "none")
        self.assertEqual(event_item["clearedRows"], [])
        self.assertEqual(event_item["clearedCols"], [])

    def test_detects_repeated_full_row_effect_without_text_signal(self):
        quiet = [[0.0 for _ in range(4)] for _ in range(4)]
        two_rows = [[0.82 for _ in range(4)] if row in (1, 2) else [0.0] * 4 for row in range(4)]
        samples = [
            {"time": 1.0, "frameIndex": 30, "boardMotion": quiet},
            {"time": 1.1, "frameIndex": 33, "boardMotion": two_rows},
            {"time": 1.2, "frameIndex": 36, "boardMotion": two_rows},
        ]

        evidence = _clear_effect_evidence(samples, 0.9, 1.3, 4, 4)

        self.assertEqual(evidence["rows"], [1, 2])
        self.assertEqual(evidence["cols"], [])
        self.assertEqual(evidence["supportFrames"], 2)

    def test_does_not_attach_pending_clear_to_unrelated_next_piece(self):
        rows = cols = 4
        before_cells = {(1, col) for col in range(cols)}
        after_cells = {(3, 0)}
        before = tuple((row, col) in before_cells for row in range(rows) for col in range(cols))
        after = tuple((row, col) in after_cells for row in range(rows) for col in range(cols))

        moves = _reverse_clear_moves(before, after, rows, cols)

        self.assertEqual(moves, [])

    def test_visual_cleanup_requires_unchanged_source_pieces_and_better_grid(self):
        noisy = board((2, 0), (2, 1), (0, 0))
        clean = board((2, 0), (2, 1))
        group = {"shape": [{"row": 0, "col": 0}, {"row": 0, "col": 1}]}
        before = {
            "board": noisy,
            "groups": [None, group, None],
            "endTime": 1.0,
            "gridAlignment": 1.5,
        }
        after = {
            "board": clean,
            "groups": [None, group, None],
            "startTime": 1.1,
            "gridAlignment": 2.5,
        }

        self.assertTrue(_is_visual_cleanup_state(before, after, 3, 3))

        after_with_changed_piece = copy.deepcopy(after)
        after_with_changed_piece["groups"][1] = {"shape": [{"row": 0, "col": 0}]}
        self.assertFalse(_is_visual_cleanup_state(before, after_with_changed_piece, 3, 3))

    def test_visual_cleanup_never_skips_added_board_cells(self):
        group = {"shape": [{"row": 0, "col": 0}]}
        before = {
            "board": board((2, 0)),
            "groups": [group, None, None],
            "endTime": 1.0,
            "gridAlignment": 1.0,
        }
        after = {
            "board": board((2, 0), (2, 1)),
            "groups": [group, None, None],
            "startTime": 1.1,
            "gridAlignment": 3.0,
        }

        self.assertFalse(_is_visual_cleanup_state(before, after, 3, 3))

    def test_recovers_piece_cells_erased_by_row_clear(self):
        rows = cols = 4
        before_cells = {(3, 0), (3, 1), (3, 3)}
        after_cells = {(2, 2)}
        before = tuple((row, col) in before_cells for row in range(rows) for col in range(cols))
        after = tuple((row, col) in after_cells for row in range(rows) for col in range(cols))

        moves = _reverse_clear_moves(before, after, rows, cols)

        expected = next(move for move in moves if move["target"] == {"row": 2, "col": 2})
        self.assertEqual(expected["target"], {"row": 2, "col": 2})
        self.assertEqual(
            [(cell["row"], cell["col"]) for cell in expected["shape"]],
            [(0, 0), (1, 0)],
        )
        self.assertEqual(expected["clearedRows"], [3])
        self.assertEqual(expected["clearedCols"], [])

        vertical_group = {
            "shape": [{"row": 0, "col": 0}, {"row": 1, "col": 0}],
            "rows": 2,
            "cols": 1,
            "cellCount": 2,
        }
        before_state = {"groupCandidates": [[None, vertical_group, None]]}
        slot_filtered = [
            move for move in moves
            if _slot_matches_for_shape(before_state, move["shape"])
        ]
        self.assertEqual(slot_filtered, [expected])

        effect_filtered = _reverse_clear_moves(
            before,
            after,
            rows,
            cols,
            effect_rows=[3],
            effect_cols=[],
        )
        self.assertIn(expected, effect_filtered)
        self.assertEqual(
            _reverse_clear_moves(before, after, rows, cols, effect_rows=[2], effect_cols=[]),
            [],
        )

    def test_recovers_sparse_shape_from_repeated_round_elements(self):
        import cv2
        import numpy as np

        frame = np.zeros((900, 600, 3), dtype=np.uint8)
        expected = {
            (0, 0), (0, 3),
            (1, 1), (1, 2),
            (2, 1), (2, 2),
            (3, 0), (3, 3),
        }
        for row, col in expected:
            center = (220 + col * 52, 647 + row * 52)
            cv2.circle(frame, center, 25, (0, 130, 240), -1)
            cv2.circle(frame, center, 25, (255, 255, 255), 2)
            cv2.line(
                frame,
                (center[0] - 18, center[1]),
                (center[0] + 18, center[1]),
                (15, 15, 15),
                3,
            )

        result = _repeated_round_element_shape(
            frame, (0, 0, 600, 600), 6, 6, source_slot=1
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["recognition"], "source_repeated_round_elements")
        self.assertEqual(
            {(cell["row"], cell["col"]) for cell in result["shape"]},
            expected,
        )
        self.assertIsNone(_repeated_round_element_shape(
            frame,
            (0, 0, 600, 600),
            6,
            6,
            source_slot=1,
            after_frame=frame.copy(),
        ))

    def test_aligns_repeated_round_elements_to_board_grid(self):
        import cv2
        import numpy as np

        frame = np.zeros((800, 800, 3), dtype=np.uint8)
        shape = [
            {"row": row, "col": col, "hue": 0}
            for row in range(2)
            for col in range(2)
        ]
        for row in range(2):
            for col in range(2):
                center = (350 + col * 100, 350 + row * 100)
                cv2.circle(frame, center, 30, (0, 130, 240), -1)
                cv2.circle(frame, center, 30, (255, 255, 255), 2)
                cv2.line(
                    frame,
                    (center[0] - 22, center[1]),
                    (center[0] + 22, center[1]),
                    (15, 15, 15),
                    3,
                )

        result = _repeated_round_element_target(
            [(87, frame)], (0, 0, 800, 800), 8, 8, shape
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["target"], {"row": 3, "col": 3})
        self.assertEqual(result["frameIndex"], 87)
        self.assertEqual(
            result["recognition"],
            "repeated_elements_aligned_to_board_grid",
        )

    def test_recovers_persistent_repeated_elements_as_board_state(self):
        import cv2
        import numpy as np

        frame = np.zeros((800, 800, 3), dtype=np.uint8)
        expected = {
            (row, col)
            for row in range(2, 6)
            for col in range(2, 6)
        }
        for row, col in expected:
            center = (50 + col * 100, 50 + row * 100)
            cv2.circle(frame, center, 42, (0, 130, 240), -1)
            cv2.circle(frame, center, 42, (255, 255, 255), 2)
            cv2.line(
                frame,
                (center[0] - 22, center[1]),
                (center[0] + 22, center[1]),
                (15, 15, 15),
                3,
            )
        cv2.circle(frame, (50, 50), 30, (0, 130, 240), -1)
        cv2.circle(frame, (50, 50), 30, (255, 255, 255), 2)

        evidence = _repeated_round_board_evidence(
            frame, (0, 0, 800, 800), 8, 8
        )

        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["occupied"], expected)

        expected_board = [
            [0 if (row, col) in expected else None for col in range(8)]
            for row in range(8)
        ]
        noisy_board = copy.deepcopy(expected_board)
        noisy_board[2][2] = None
        samples = [
            {
                "board": copy.deepcopy(
                    noisy_board if index in {1, 4} else expected_board
                ),
                "repeatedBoardEvidence": evidence if index in {0, 3, 6} else None,
            }
            for index in range(7)
        ]
        diagnostics = _apply_repeated_board_consensus(
            samples, minimum_frames=3, maximum_gap=3
        )

        self.assertEqual(diagnostics["correctedRuns"], 1)
        self.assertEqual(diagnostics["correctedFrames"], 7)
        for sample in samples:
            occupied = {
                (row, col)
                for row in range(8)
                for col in range(8)
                if sample["board"][row][col] is not None
            }
            self.assertEqual(occupied, expected)

            sample.pop("repeatedBoardEvidence", None)
        json.dumps(samples)


if __name__ == "__main__":
    unittest.main()
