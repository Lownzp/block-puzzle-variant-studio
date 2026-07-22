"""Deterministic CV reconstruction for 10x10 block-puzzle videos.

The analyzer does not use an AI vision model. It converts stable video frames
to discrete board/group states, then accepts a move only when a legal puzzle
simulation reproduces the next stable board exactly.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from cell_occupancy_model import extract_cell_features, load_model, predict_occupied


IMAGE_MATERIAL_KEYWORDS = (
    "甜品", "冰淇淋", "木制", "立方", "仿3d", "猫块", "泳池", "排球", "黄鸭", "篮球",
)
COLOR_MATERIAL_KEYWORDS = (
    "纯色", "彩块", "粉色", "蓝块", "粉沙", "彩虹", "橙块",
)


def infer_material_profile(video_path):
    """Infer the visual material family from task/video naming conventions."""
    text = " ".join(str(part) for part in Path(video_path).parts).lower()
    if any(keyword.lower() in text for keyword in IMAGE_MATERIAL_KEYWORDS):
        return "image_block"
    if any(keyword.lower() in text for keyword in COLOR_MATERIAL_KEYWORDS):
        return "color_block"
    return "unknown"


def _cell_hue(hsv_cell):
    import numpy as np

    if hsv_cell.size == 0:
        return None
    pixels = hsv_cell.reshape(-1, 3)
    bright = (pixels[:, 1] >= 65) & (pixels[:, 2] >= 125)
    # A real block fills most of the cell interior. Short flashes and grid
    # highlights generally do not survive this coverage threshold.
    if float(np.mean(bright)) < 0.28:
        return None
    hue = float(np.median(pixels[bright, 0]))
    return int(round(hue / 5.0) * 5) % 180


def _cell_soft_hue(hsv_cell):
    import numpy as np

    if hsv_cell.size == 0:
        return None
    pixels = hsv_cell.reshape(-1, 3)
    colored = (pixels[:, 1] >= 8) & (pixels[:, 2] >= 120)
    if not np.any(colored):
        return None
    hue = float(np.median(pixels[colored, 0]))
    return int(round(hue / 5.0) * 5) % 180


def _cell_shadow_like(hsv_cell, gray_cell):
    """Return true for weak block shadows that should not occupy a grid cell."""
    import numpy as np

    if hsv_cell.size == 0 or gray_cell.size == 0:
        return False
    height, width = gray_cell.shape[:2]
    pixels = hsv_cell.reshape(-1, 3)
    saturation = pixels[:, 1].astype(np.float32)
    value = pixels[:, 2].astype(np.float32)
    body = (saturation >= 65) & (value >= 125)
    strong_body = (saturation >= 82) & (value >= 148)
    body_coverage = float(np.mean(body))
    strong_coverage = float(np.mean(strong_body))
    center_pad = max(1, round(min(height, width) * 0.32))
    center_hsv = hsv_cell[
        center_pad:max(center_pad + 1, height - center_pad),
        center_pad:max(center_pad + 1, width - center_pad),
    ]
    if center_hsv.size:
        center_pixels = center_hsv.reshape(-1, 3)
        center_saturation = center_pixels[:, 1].astype(np.float32)
        center_value = center_pixels[:, 2].astype(np.float32)
        center_body_coverage = float(np.mean(
            (center_saturation >= 65) & (center_value >= 125)
        ))
        center_strong_coverage = float(np.mean(
            (center_saturation >= 82) & (center_value >= 148)
        ))
    else:
        center_body_coverage = body_coverage
        center_strong_coverage = strong_coverage
    gray_std = float(np.std(gray_cell))
    value_std = float(np.std(value))
    mean_saturation = float(np.mean(saturation))
    mean_value = float(np.mean(value))
    # Real 3D blocks fill a meaningful part of the cell with saturated,
    # bright faces and bevels. Shadows are weaker, flatter and usually cover
    # only a small strip near the block edge.
    weak_body = body_coverage < 0.22 and strong_coverage < 0.10
    edge_strip = (
        body_coverage < 0.48
        and center_body_coverage < 0.18
        and center_strong_coverage < 0.14
    )
    flat_patch = gray_std < 18.0 and value_std < 22.0
    faded_color = mean_saturation < 78.0 or mean_value < 132.0
    return edge_strip or (weak_body and (flat_patch or faded_color))


def _cell_appearance(hsv_cell, gray_cell):
    """Return material-independent appearance features for one grid cell."""
    import cv2
    import numpy as np

    if hsv_cell.size == 0 or gray_cell.size == 0:
        return None
    edges = cv2.Canny(gray_cell, 35, 90)
    return np.asarray([
        float(np.mean(hsv_cell[:, :, 2])) / 255.0,
        float(np.std(gray_cell)) / 64.0,
        float(np.mean(edges > 0)),
        float(np.std(hsv_cell[:, :, 2])) / 64.0,
        float(np.std(hsv_cell[:, :, 1])) / 64.0,
    ], dtype=np.float32)


def _board_cell_crops(frame, board, rows, cols):
    import cv2

    x, y, w, h = board
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    cells = []
    for row in range(rows):
        line = []
        for col in range(cols):
            left = int(x + (col + 0.18) * w / cols)
            right = int(x + (col + 0.82) * w / cols)
            top = int(y + (row + 0.18) * h / rows)
            bottom = int(y + (row + 0.82) * h / rows)
            line.append((hsv[top:bottom, left:right], gray[top:bottom, left:right]))
        cells.append(line)
    return cells


def _build_board_appearance_profile(frame, board, rows, cols):
    """Learn cell appearances when a themed board defeats saturation rules.

    This fallback is deliberately conservative. It handles both extremes:
    colorful backgrounds called almost entirely occupied, and bright pastel
    blocks called almost entirely empty. Both require two reliable appearance
    populations before the established HSV recognizer is bypassed.
    """
    import cv2
    import numpy as np

    cells = _board_cell_crops(frame, board, rows, cols)
    legacy = []
    features = []
    for line in cells:
        for hsv_cell, gray_cell in line:
            legacy.append(_cell_hue(hsv_cell) is not None)
            appearance = _cell_appearance(hsv_cell, gray_cell)
            if appearance is None:
                return None
            features.append(appearance)
    legacy_rate = float(np.mean(legacy))
    legacy_overfilled = legacy_rate >= 0.90
    legacy_underfilled = legacy_rate <= 0.15
    if not (legacy_overfilled or legacy_underfilled):
        return None

    matrix = np.asarray(features, dtype=np.float32)
    values = np.clip(np.rint(matrix[:, 0] * 255.0), 0, 255).astype(np.uint8)
    if int(values.max()) - int(values.min()) < 12:
        return None
    threshold, labels = cv2.threshold(
        values.reshape(-1, 1), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    occupied = labels.reshape(-1) > 0
    occupied_count = int(np.sum(occupied))
    empty_count = len(occupied) - occupied_count
    if min(occupied_count, empty_count) < max(3, round(len(occupied) * 0.04)):
        return None

    empty_center = np.median(matrix[~occupied], axis=0)
    occupied_center = np.median(matrix[occupied], axis=0)
    value_separation = float(occupied_center[0] - empty_center[0])
    if legacy_overfilled:
        # The empty material itself is colorful. Accept only a moderate value
        # split; a very large split is the normal dark-board case and should
        # remain on the established HSV recognizer.
        if value_separation < 0.03 or value_separation > 0.16:
            return None
    elif value_separation < 0.25:
        # White and pastel blocks can be almost saturation-free. They are safe
        # to recover only when they are dramatically brighter than the board.
        return None
    scale = np.maximum(np.abs(occupied_center - empty_center), np.asarray(
        [0.035, 0.08, 0.02, 0.08, 0.08], dtype=np.float32
    ))
    texture_separation = float(np.sum(np.abs(
        occupied_center[1:] - empty_center[1:]
    )))
    if legacy_overfilled and texture_separation < 0.30:
        return None
    if legacy_underfilled and texture_separation < 0.30:
        return None
    if legacy_overfilled:
        profile_mode = (
            "adaptive_overfilled_appearance"
            if texture_separation >= 0.50
            else "adaptive_overfilled_brightness"
        )
    else:
        profile_mode = "adaptive_underfilled_bright_blocks"
    return {
        "mode": profile_mode,
        "classifier": "appearance_prototype",
        "emptyCenter": empty_center.tolist(),
        "occupiedCenter": occupied_center.tolist(),
        "scale": scale.tolist(),
        "valueThreshold": round(float(threshold) / 255.0, 4),
        "textureSeparation": round(texture_separation, 4),
        "initialOccupiedCount": occupied_count,
    }


def _build_image_material_appearance_profile(frame, board, rows, cols):
    """Learn appearance prototypes for image/themed blocks.

    Unlike the adaptive fallback above, this is allowed to run when the normal
    HSV classifier is only partially successful. It uses the legacy occupied
    cells as seeds, then classifies later frames by material appearance.
    """
    import numpy as np

    legacy = []
    features = []
    for line in _board_cell_crops(frame, board, rows, cols):
        for hsv_cell, gray_cell in line:
            legacy.append(_cell_hue(hsv_cell) is not None)
            appearance = _cell_appearance(hsv_cell, gray_cell)
            if appearance is None:
                return None
            features.append(appearance)
    occupied = np.asarray(legacy, dtype=bool)
    occupied_count = int(np.sum(occupied))
    empty_count = len(occupied) - occupied_count
    if min(occupied_count, empty_count) < max(4, round(len(occupied) * 0.06)):
        return None

    matrix = np.asarray(features, dtype=np.float32)
    empty_center = np.median(matrix[~occupied], axis=0)
    occupied_center = np.median(matrix[occupied], axis=0)
    separation = np.abs(occupied_center - empty_center)
    value_separation = float(separation[0])
    texture_separation = float(np.sum(separation[1:]))
    if value_separation < 0.035 and texture_separation < 0.34:
        return None
    scale = np.maximum(
        separation,
        np.asarray([0.035, 0.08, 0.02, 0.08, 0.08], dtype=np.float32),
    )
    return {
        "mode": "image_material_appearance",
        "classifier": "appearance_prototype",
        "emptyCenter": empty_center.tolist(),
        "occupiedCenter": occupied_center.tolist(),
        "scale": scale.tolist(),
        "valueSeparation": round(value_separation, 4),
        "textureSeparation": round(texture_separation, 4),
        "initialOccupiedCount": occupied_count,
    }


def _build_occupied_cell_validator(frame, board, rows, cols):
    """Learn the normal occupied-cell texture to reject score/text overlays."""
    import numpy as np

    occupied = []
    empty = []
    for line in _board_cell_crops(frame, board, rows, cols):
        for hsv_cell, gray_cell in line:
            appearance = _cell_appearance(hsv_cell, gray_cell)
            if appearance is None:
                continue
            target = occupied if _cell_hue(hsv_cell) is not None else empty
            target.append(appearance)
    if len(occupied) < 4 or len(empty) < 4:
        return None
    occupied_matrix = np.asarray(occupied, dtype=np.float32)
    empty_matrix = np.asarray(empty, dtype=np.float32)
    occupied_center = np.median(occupied_matrix, axis=0)
    empty_center = np.median(empty_matrix, axis=0)
    scale = np.maximum(
        np.abs(occupied_center - empty_center),
        np.asarray([0.035, 0.08, 0.02, 0.08, 0.08], dtype=np.float32),
    )
    distances = np.mean(((occupied_matrix - occupied_center) / scale) ** 2, axis=1)
    maximum_distance = max(1.5, float(np.quantile(distances, 0.95)) * 4.0)
    return {
        "occupiedCenter": occupied_center.tolist(),
        "scale": scale.tolist(),
        "maximumDistance": round(maximum_distance, 4),
        "sampleCount": len(occupied),
    }


def _board_state(
    frame, board, rows, cols, appearance_profile=None,
    occupied_cell_validator=None,
    occupancy_model=None,
):
    import cv2
    import numpy as np

    cells = _board_cell_crops(frame, board, rows, cols)
    empty_center = occupied_center = scale = None
    if appearance_profile:
        empty_center = np.asarray(appearance_profile["emptyCenter"], dtype=np.float32)
        occupied_center = np.asarray(appearance_profile["occupiedCenter"], dtype=np.float32)
        scale = np.asarray(appearance_profile["scale"], dtype=np.float32)
    values = []
    for row in range(rows):
        line = []
        for col in range(cols):
            hsv_cell, gray_cell = cells[row][col]
            hue = _cell_hue(hsv_cell)
            model_decided = False
            if occupancy_model:
                features = extract_cell_features(hsv_cell, gray_cell)
                model_occupied, margin = predict_occupied(occupancy_model, features)
                if abs(margin) >= 0.15:
                    model_decided = True
                    if model_occupied:
                        pixels = hsv_cell.reshape(-1, 3)
                        colorful = pixels[:, 1] >= 20
                        selected = pixels[colorful] if np.any(colorful) else pixels
                        if _cell_shadow_like(hsv_cell, gray_cell):
                            hue = None
                        else:
                            hue = int(round(float(np.median(selected[:, 0])) / 5.0) * 5) % 180
                    else:
                        hue = None
            if hue is not None and occupied_cell_validator and not appearance_profile and not model_decided:
                appearance = _cell_appearance(hsv_cell, gray_cell)
                center = np.asarray(
                    occupied_cell_validator["occupiedCenter"], dtype=np.float32
                )
                validator_scale = np.asarray(
                    occupied_cell_validator["scale"], dtype=np.float32
                )
                distance = float(np.mean(
                    ((appearance - center) / validator_scale) ** 2
                ))
                if distance > occupied_cell_validator["maximumDistance"]:
                    hue = None
            elif hue is None and occupied_cell_validator and not appearance_profile and not model_decided:
                appearance = _cell_appearance(hsv_cell, gray_cell)
                center = np.asarray(
                    occupied_cell_validator["occupiedCenter"], dtype=np.float32
                )
                validator_scale = np.asarray(
                    occupied_cell_validator["scale"], dtype=np.float32
                )
                distance = float(np.mean(
                    ((appearance - center) / validator_scale) ** 2
                ))
                if distance <= occupied_cell_validator["maximumDistance"]:
                    mean_saturation = float(np.mean(hsv_cell[:, :, 1]))
                    gray_contrast = float(np.std(gray_cell))
                    if mean_saturation >= 8.0 or gray_contrast >= 8.0:
                        hue = _cell_soft_hue(hsv_cell)
                        if hue is None:
                            hue = 0
            if appearance_profile and not model_decided:
                appearance = _cell_appearance(hsv_cell, gray_cell)
                if appearance_profile.get("mode") in {
                    "adaptive_overfilled_brightness",
                    "adaptive_underfilled_bright_blocks",
                }:
                    is_occupied = appearance[0] > (
                        float(appearance_profile["valueThreshold"]) + 1.0 / 255.0
                    )
                else:
                    empty_distance = float(np.mean(((appearance - empty_center) / scale) ** 2))
                    occupied_distance = float(np.mean(((appearance - occupied_center) / scale) ** 2))
                    is_occupied = occupied_distance < empty_distance
                if is_occupied:
                    pixels = hsv_cell.reshape(-1, 3)
                    colorful = pixels[:, 1] >= 20
                    selected = pixels[colorful] if np.any(colorful) else pixels
                    hue = int(round(float(np.median(selected[:, 0])) / 5.0) * 5) % 180
                else:
                    hue = None
            line.append(hue)
        values.append(line)
    return values


def _grid_alignment_score(frame, board, rows, cols):
    """Measure whether strong edges still follow the calibrated board grid."""
    import cv2
    import numpy as np

    x, y, width, height = board
    crop = frame[y:y + height, x:x + width]
    if crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 45, 130).astype(np.float32) / 255.0
    cell_width = width / cols
    cell_height = height / rows
    line_samples = []
    interior_samples = []
    band = max(1, round(min(cell_width, cell_height) * 0.035))
    for col in range(1, cols):
        px = min(width - 1, max(0, round(col * cell_width)))
        line_samples.append(float(edges[:, max(0, px - band):min(width, px + band + 1)].mean()))
        interior_x = min(width - 1, max(0, round((col - 0.5) * cell_width)))
        interior_samples.append(float(edges[:, max(0, interior_x - band):min(width, interior_x + band + 1)].mean()))
    for row in range(1, rows):
        py = min(height - 1, max(0, round(row * cell_height)))
        line_samples.append(float(edges[max(0, py - band):min(height, py + band + 1), :].mean()))
        interior_y = min(height - 1, max(0, round((row - 0.5) * cell_height)))
        interior_samples.append(float(edges[max(0, interior_y - band):min(height, interior_y + band + 1), :].mean()))
    line_mean = float(np.mean(line_samples)) if line_samples else 0.0
    interior_mean = float(np.mean(interior_samples)) if interior_samples else 0.0
    return round(line_mean / max(0.01, interior_mean), 3)


def _board_motion(previous_frame, frame, board, rows, cols):
    """Return per-cell motion coverage between two sampled source frames."""
    import cv2
    import numpy as np

    x, y, width, height = board
    previous = cv2.cvtColor(
        previous_frame[y:y + height, x:x + width], cv2.COLOR_BGR2GRAY
    )
    current = cv2.cvtColor(
        frame[y:y + height, x:x + width], cv2.COLOR_BGR2GRAY
    )
    if previous.size == 0 or current.size == 0 or previous.shape != current.shape:
        return [[0.0 for _ in range(cols)] for _ in range(rows)]
    difference = cv2.absdiff(previous, current)
    result = []
    for row in range(rows):
        line = []
        for col in range(cols):
            left = int((col + 0.06) * width / cols)
            right = int((col + 0.94) * width / cols)
            top = int((row + 0.06) * height / rows)
            bottom = int((row + 0.94) * height / rows)
            cell = difference[top:bottom, left:right]
            line.append(round(float(np.mean(cell > 18)) if cell.size else 0.0, 4))
        result.append(line)
    return result


def _clear_effect_evidence(
    samples,
    start_time,
    end_time,
    rows,
    cols,
    expected_rows=None,
    expected_cols=None,
    min_support_frames=2,
):
    """Find repeated full-line effects, optionally limited to rule-completed lines."""
    import numpy as np

    observations = []
    for sample in samples:
        if not (start_time <= sample["time"] <= end_time):
            continue
        motion = sample.get("boardMotion")
        if not motion:
            continue
        matrix = np.asarray(motion, dtype=np.float32)
        if matrix.shape != (rows, cols):
            continue
        row_scores = np.quantile(matrix, 0.25, axis=1)
        col_scores = np.quantile(matrix, 0.25, axis=0)
        if expected_rows is None:
            effect_rows = tuple(int(row) for row, score in enumerate(row_scores) if score >= 0.42)
        else:
            effect_rows = tuple(
                int(row) for row in expected_rows if row_scores[int(row)] >= 0.42
            )
        if expected_cols is None:
            effect_cols = tuple(int(col) for col, score in enumerate(col_scores) if score >= 0.42)
        else:
            effect_cols = tuple(
                int(col) for col in expected_cols if col_scores[int(col)] >= 0.42
            )
        if expected_rows is not None and effect_rows != tuple(sorted(expected_rows)):
            continue
        if expected_cols is not None and effect_cols != tuple(sorted(expected_cols)):
            continue
        if not effect_rows and not effect_cols:
            continue
        line_scores = [float(row_scores[row]) for row in effect_rows]
        line_scores.extend(float(col_scores[col]) for col in effect_cols)
        observations.append({
            "signature": (effect_rows, effect_cols),
            "time": sample["time"],
            "frameIndex": sample["frameIndex"],
            "score": round(float(np.mean(line_scores)), 3),
        })

    counts = Counter(item["signature"] for item in observations)
    repeated = [
        item
        for item in observations
        if counts[item["signature"]] >= min_support_frames
    ]
    if not repeated:
        return None
    best = max(
        repeated,
        key=lambda item: (counts[item["signature"]], item["score"]),
    )
    effect_rows, effect_cols = best["signature"]
    supporting = [item for item in repeated if item["signature"] == best["signature"]]
    return {
        "rows": list(effect_rows),
        "cols": list(effect_cols),
        "time": best["time"],
        "startTime": min(item["time"] for item in supporting),
        "endTime": max(item["time"] for item in supporting),
        "frameIndex": best["frameIndex"],
        "score": best["score"],
        "supportFrames": counts[best["signature"]],
        "verification": "full_line_synchronous_visual_effect",
    }


def _rule_completed_lines(before_board, shape, target, rows, cols):
    """Return only full rows/columns completed by cells from this placement."""
    if not before_board or not shape or not target:
        return None
    simulation = _simulate(
        _occupancy(before_board),
        shape,
        int(target["row"]),
        int(target["col"]),
        rows,
        cols,
    )
    if simulation is None:
        return None
    _, final_signature, cleared_rows, cleared_cols, placed = simulation
    touched_rows = {row for row, _ in placed}
    touched_cols = {col for _, col in placed}
    cleared_rows = [row for row in cleared_rows if row in touched_rows]
    cleared_cols = [col for col in cleared_cols if col in touched_cols]
    return {
        "rows": cleared_rows,
        "cols": cleared_cols,
        "placed": placed,
        "finalSignature": final_signature,
    }


def _clear_resolution_evidence(
    samples,
    start_time,
    end_time,
    cleared_rows,
    cleared_cols,
    rows,
    cols,
    min_frames=2,
):
    """Require every cell in the completed lines to disappear after the effect."""
    cleared_cells = {
        (row, col)
        for row in range(rows)
        for col in range(cols)
        if row in cleared_rows or col in cleared_cols
    }
    if not cleared_cells:
        return None
    matches = []
    for sample in samples:
        if start_time <= sample["time"] <= end_time:
            board = sample["board"]
            if all(board[row][col] is None for row, col in cleared_cells):
                matches.append(sample)
            elif matches:
                matches = []
            if len(matches) >= min_frames:
                return {
                    "time": matches[0]["time"],
                    "endTime": matches[-1]["time"],
                    "frameIndex": matches[0]["frameIndex"],
                    "supportFrames": len(matches),
                    "verification": "rule_simulated_board_observed_after_effect",
                }
    return None


def _stable_clear_resolution_evidence(
    stable_states,
    start_state_index,
    end_state_index,
    cleared_rows,
    cleared_cols,
    rows,
    cols,
):
    """Accept a stable board where every cell from the cleared lines is absent."""
    cleared_cells = {
        (row, col)
        for row in range(rows)
        for col in range(cols)
        if row in cleared_rows or col in cleared_cols
    }
    for state in stable_states:
        if not (start_state_index <= state["stateIndex"] <= end_state_index):
            continue
        board = state["board"]
        if all(board[row][col] is None for row, col in cleared_cells):
            return {
                "time": state["startTime"],
                "endTime": state["endTime"],
                "frameIndex": state["frameIndex"],
                "supportFrames": state["frameCount"],
                "verification": "stable_board_cleared_lines_absent",
            }
    return None


def _apply_rule_verified_clear_detection(
    events,
    samples,
    stable_states,
    rows,
    cols,
    timeline_end_time,
    conservative_unverified_clear=False,
):
    """Reconcile every clear from placement rules, effect frames, and disappearance."""
    states = {state["stateIndex"]: state for state in stable_states}
    for index, event in enumerate(events):
        expected = _rule_completed_lines(
            event.get("beforeBoard"),
            (event.get("group") or {}).get("shape"),
            event.get("target"),
            rows,
            cols,
        )
        event.pop("clearEffectEvidence", None)
        event.pop("clearResolutionEvidence", None)
        event.pop("effectFrameTime", None)
        if not expected or (not expected["rows"] and not expected["cols"]):
            event.update({
                "clearedRows": [],
                "clearedCols": [],
                "clearMode": "none",
                "clearVerification": "placement_does_not_complete_a_line",
            })
            continue

        before_state = states.get(event.get("sourceStateIndex"))
        window_start = (
            before_state.get("endTime", event["time"] - 0.3)
            if before_state
            else max(0.0, event["time"] - 0.3)
        )
        next_time = (
            events[index + 1]["time"] - 0.03
            if index + 1 < len(events)
            else timeline_end_time
        )
        window_end = min(next_time, event["time"] + 1.35)
        effect = _clear_effect_evidence(
            samples,
            window_start,
            window_end,
            rows,
            cols,
            expected_rows=expected["rows"],
            expected_cols=expected["cols"],
            min_support_frames=1,
        )
        resolution = None
        if effect:
            resolution = _clear_resolution_evidence(
                samples,
                effect["endTime"],
                window_end,
                expected["rows"],
                expected["cols"],
                rows,
                cols,
            )
            if resolution is None:
                end_state_index = (
                    events[index + 1].get("sourceStateIndex", len(stable_states) - 1)
                    if index + 1 < len(events)
                    else len(stable_states) - 1
                )
                resolution = _stable_clear_resolution_evidence(
                    stable_states,
                    event.get("targetStateIndex", 0),
                    end_state_index,
                    expected["rows"],
                    expected["cols"],
                    rows,
                    cols,
                )

        event["clearedRows"] = expected["rows"]
        event["clearedCols"] = expected["cols"]
        event["ruleCompletedRows"] = expected["rows"]
        event["ruleCompletedCols"] = expected["cols"]
        if effect:
            event["clearEffectEvidence"] = effect
            event["effectFrameTime"] = effect["time"]
        if effect and resolution:
            event["clearMode"] = "immediate"
            event["clearResolutionEvidence"] = resolution
            event["clearVerification"] = "rule_effect_and_disappearance_verified"
        else:
            event["clearMode"] = (
                "none"
                if conservative_unverified_clear and not effect
                else "unknown"
            )
            event["clearVerification"] = (
                "effect_seen_but_disappearance_not_verified"
                if effect
                else "rule_completed_line_but_effect_not_verified"
            )
    return events


def _occupancy(board_state):
    return tuple(cell is not None for row in board_state for cell in row)


def _difference(previous, current):
    added, removed = [], []
    for row in range(len(previous)):
        for col in range(len(previous[row])):
            a, b = previous[row][col], current[row][col]
            if a is None and b is not None:
                added.append({"row": row, "col": col, "hue": b})
            elif a is not None and b is None:
                removed.append({"row": row, "col": col, "hue": a})
    return added, removed


def _nearest_cell_value(samples, sample_index, row, col):
    for offset in range(0, max(sample_index + 1, len(samples) - sample_index)):
        for candidate_index in (sample_index - offset, sample_index + offset):
            if 0 <= candidate_index < len(samples):
                value = samples[candidate_index]["board"][row][col]
                if value is not None:
                    return value
    return 0


def _apply_cell_temporal_voting(samples, rows, cols, radius=2, minimum_votes=3):
    """Suppress short-lived per-cell flicker before stable-state extraction."""
    if len(samples) < minimum_votes:
        return {"changedCells": 0, "changedFrames": 0, "windowRadius": radius}
    original = [[list(row) for row in sample["board"]] for sample in samples]
    changed_cells = 0
    changed_frames = set()
    for index, sample in enumerate(samples):
        start = max(0, index - radius)
        end = min(len(samples), index + radius + 1)
        window = original[start:end]
        updated = [list(row) for row in sample["board"]]
        frame_changed = False
        for row in range(rows):
            for col in range(cols):
                occupied_votes = sum(board[row][col] is not None for board in window)
                empty_votes = len(window) - occupied_votes
                current_occupied = original[index][row][col] is not None
                if current_occupied and empty_votes >= minimum_votes and empty_votes > occupied_votes:
                    updated[row][col] = None
                elif (
                    not current_occupied
                    and occupied_votes >= minimum_votes
                    and occupied_votes > empty_votes
                ):
                    updated[row][col] = _nearest_cell_value(samples, index, row, col)
                else:
                    continue
                changed_cells += 1
                frame_changed = True
        if frame_changed:
            sample["board"] = updated
            changed_frames.add(index)
    return {
        "changedCells": changed_cells,
        "changedFrames": len(changed_frames),
        "windowRadius": radius,
        "minimumVotes": minimum_votes,
    }


def _sample_cache_summary(samples, rows, cols):
    summary = []
    previous = None
    for sample in samples:
        occupied = sum(sample["board"][row][col] is not None for row in range(rows) for col in range(cols))
        motion = sample.get("boardMotion") or []
        motion_score = (
            sum(sum(float(cell) for cell in row) for row in motion) / max(1, rows * cols)
            if motion
            else 0.0
        )
        signature = _occupancy(sample["board"])
        changed = sum(a != b for a, b in zip(previous, signature)) if previous is not None else 0
        previous = signature
        summary.append({
            "frameIndex": sample["frameIndex"],
            "time": sample["time"],
            "occupied": occupied,
            "changedCells": changed,
            "gridAlignment": round(float(sample.get("gridAlignment", 0.0)), 4),
            "motion": round(motion_score, 4),
        })
    return summary


def _group_layout_signature(groups):
    return tuple(
        None
        if group is None
        else tuple(sorted((cell["row"], cell["col"]) for cell in group.get("shape", [])))
        for group in (groups or [None, None, None])[:3]
    )


def _source_piece_restored(before, candidate, event):
    """Return whether the piece picked up for this event is back in its slot."""
    source_slot = int(event.get("sourceSlot", -1))
    before_groups = (before.get("groups") or [None, None, None])[:3]
    candidate_groups = (candidate.get("groups") or [None, None, None])[:3]
    if 0 <= source_slot < min(len(before_groups), len(candidate_groups)):
        original = before_groups[source_slot]
        returned = candidate_groups[source_slot]
        # Tray segmentation can read the same moving piece with a different
        # cell count (DEV-008 is seen as 2 cells before and 6 after return).
        # Board restoration and absence of clear evidence are the stronger
        # signals here; the source slot only needs to become occupied again.
        return original is not None and returned is not None
    return _group_layout_signature(candidate_groups) == _group_layout_signature(before_groups)


def _filter_cancelled_drags(events, stable_states, max_window=3.0):
    """Drop drag attempts whose board resets and picked piece returns home."""
    states = {state["stateIndex"]: state for state in stable_states}
    ordered_states = sorted(stable_states, key=lambda state: state["stateIndex"])
    cancelled_ranges = []

    for event in events:
        before = states.get(event["sourceStateIndex"])
        after = states.get(event["targetStateIndex"])
        if before is None or after is None:
            continue
        before_board = _occupancy(before["board"])
        if before_board == _occupancy(after["board"]):
            continue
        for candidate in ordered_states:
            if candidate["stateIndex"] <= event["targetStateIndex"]:
                continue
            if candidate["startTime"] - after["endTime"] > max_window:
                break
            if _occupancy(candidate["board"]) != before_board:
                continue
            if not _source_piece_restored(before, candidate, event):
                continue
            span_events = [
                item
                for item in events
                if item["sourceStateIndex"] >= event["sourceStateIndex"]
                and item["targetStateIndex"] <= candidate["stateIndex"]
            ]
            if any(item.get("clearEffectEvidence") for item in span_events):
                continue
            source_event = next((item for item in span_events if item.get("sourceSlot", -1) >= 0), event)
            source_group = source_event.get("group") or {}

            def is_board_hover(item, target):
                shape = source_group.get("shape") or (item.get("group") or {}).get("shape") or []
                if not shape:
                    return True
                board_rows = len(before["board"])
                board_cols = len(before["board"][0]) if before["board"] else 0
                return all(
                    0 <= int(target["row"]) + int(cell["row"]) < board_rows
                    and 0 <= int(target["col"]) + int(cell["col"]) < board_cols
                    for cell in shape
                )

            candidate_targets = []
            for item in span_events:
                target = item.get("target") or {}
                if "row" not in target or "col" not in target:
                    continue
                if not is_board_hover(item, target):
                    continue
                normalized = {"row": int(target["row"]), "col": int(target["col"])}
                if normalized not in candidate_targets:
                    candidate_targets.append(normalized)
            preview_rows = sorted({row for item in span_events for row in item.get("clearedRows", [])})
            preview_cols = sorted({col for item in span_events for col in item.get("clearedCols", [])})
            hover_passes = []
            for item in span_events:
                target = item.get("target") or {}
                if "row" not in target or "col" not in target:
                    continue
                if not is_board_hover(item, target):
                    continue
                source_state = states.get(item.get("sourceStateIndex")) or before
                target_state = states.get(item.get("targetStateIndex")) or after
                hover_passes.append(
                    {
                        "passIndex": len(hover_passes) + 1,
                        "startTime": round(float(source_state.get("endTime", item.get("time", 0))), 3),
                        "endTime": round(float(target_state.get("endTime", item.get("time", 0))), 3),
                        "target": {"row": int(target["row"]), "col": int(target["col"])},
                        "previewClearedRows": sorted(set(item.get("clearedRows", []))),
                        "previewClearedCols": sorted(set(item.get("clearedCols", []))),
                    }
                )
            return_complete_time = candidate.get(
                "time",
                round((candidate["startTime"] + candidate["endTime"]) / 2, 3),
            )
            cancelled_ranges.append(
                {
                    "type": "cancelled_drag",
                    "sourceStateIndex": event["sourceStateIndex"],
                    "returnStateIndex": candidate["stateIndex"],
                    "startTime": before["endTime"],
                    # Use the representative frame of the restored stable state.
                    # candidate.startTime can still contain the tail of the return animation.
                    "endTime": return_complete_time,
                    "returnCompleteTime": return_complete_time,
                    "returnStableRange": {
                        "startTime": candidate["startTime"],
                        "endTime": candidate["endTime"],
                    },
                    "duration": round(return_complete_time - before["endTime"], 3),
                    "sourceSlot": int(source_event.get("sourceSlot", -1)),
                    "shape": [dict(cell) for cell in source_group.get("shape", [])],
                    "hoverTarget": candidate_targets[0] if candidate_targets else None,
                    "candidateTargets": candidate_targets,
                    "hoverPasses": hover_passes,
                    "boardBefore": before["board"],
                    "previewClearedRows": preview_rows,
                    "previewClearedCols": preview_cols,
                    "boardMutation": False,
                    "clearExecuted": False,
                    "discardedSourceSteps": [item["stepIndex"] for item in span_events],
                    "reason": "board_restored_and_source_piece_returned",
                }
            )
            break

    unique_ranges = []
    for item in sorted(cancelled_ranges, key=lambda value: (value["sourceStateIndex"], value["returnStateIndex"])):
        if unique_ranges and item["sourceStateIndex"] < unique_ranges[-1]["returnStateIndex"]:
            if item["returnStateIndex"] > unique_ranges[-1]["returnStateIndex"]:
                unique_ranges[-1]["returnStateIndex"] = item["returnStateIndex"]
                unique_ranges[-1]["endTime"] = item["endTime"]
                unique_ranges[-1]["returnCompleteTime"] = item.get("returnCompleteTime", item["endTime"])
                unique_ranges[-1]["returnStableRange"] = item.get("returnStableRange")
                unique_ranges[-1]["discardedSourceSteps"] = sorted(set(
                    unique_ranges[-1]["discardedSourceSteps"] + item["discardedSourceSteps"]
                ))
                for target in item.get("candidateTargets", []):
                    if target not in unique_ranges[-1]["candidateTargets"]:
                        unique_ranges[-1]["candidateTargets"].append(target)
                unique_ranges[-1].setdefault("hoverPasses", []).extend(item.get("hoverPasses", []))
                unique_ranges[-1]["duration"] = round(
                    unique_ranges[-1]["endTime"] - unique_ranges[-1]["startTime"], 3
                )
            continue
        unique_ranges.append(item)

    # A hand can move a piece away from the board and immediately hover it
    # again without returning it to the tray. A very short baseline gap with
    # the same piece/target is one cancelled drag with multiple hover passes.
    merged_ranges = []
    for item in unique_ranges:
        previous = merged_ranges[-1] if merged_ranges else None
        gap = item["startTime"] - previous["endTime"] if previous else None
        previous_shape = {
            (cell.get("row"), cell.get("col")) for cell in (previous or {}).get("shape", [])
        }
        item_shape = {(cell.get("row"), cell.get("col")) for cell in item.get("shape", [])}
        compatible_slot = previous and (
            previous.get("sourceSlot", -1) < 0
            or item.get("sourceSlot", -1) < 0
            or previous.get("sourceSlot") == item.get("sourceSlot")
        )
        shared_target = previous and any(
            target in previous.get("candidateTargets", []) for target in item.get("candidateTargets", [])
        )
        if previous and gap is not None and 0 <= gap <= 0.25 and compatible_slot and previous_shape == item_shape and shared_target:
            previous["returnStateIndex"] = item["returnStateIndex"]
            previous["endTime"] = item["endTime"]
            previous["returnCompleteTime"] = item.get("returnCompleteTime", item["endTime"])
            previous["returnStableRange"] = item.get("returnStableRange")
            previous["duration"] = round(previous["endTime"] - previous["startTime"], 3)
            if previous.get("sourceSlot", -1) < 0 <= item.get("sourceSlot", -1):
                previous["sourceSlot"] = item["sourceSlot"]
            previous["discardedSourceSteps"] = sorted(set(
                previous.get("discardedSourceSteps", []) + item.get("discardedSourceSteps", [])
            ))
            for target in item.get("candidateTargets", []):
                if target not in previous["candidateTargets"]:
                    previous["candidateTargets"].append(target)
            previous["previewClearedRows"] = sorted(set(
                previous.get("previewClearedRows", []) + item.get("previewClearedRows", [])
            ))
            previous["previewClearedCols"] = sorted(set(
                previous.get("previewClearedCols", []) + item.get("previewClearedCols", [])
            ))
            previous.setdefault("hoverPasses", []).extend(item.get("hoverPasses", []))
            previous["reason"] = "multiple_hover_passes_then_returned_to_source"
            continue
        merged_ranges.append(item)
    unique_ranges = merged_ranges

    for interaction in unique_ranges:
        for pass_index, hover_pass in enumerate(interaction.get("hoverPasses", []), 1):
            hover_pass["passIndex"] = pass_index

    kept = [
        event
        for event in events
        if not any(
            event["sourceStateIndex"] >= item["sourceStateIndex"]
            and event["targetStateIndex"] <= item["returnStateIndex"]
            for item in unique_ranges
        )
    ]
    for step_index, event in enumerate(kept, 1):
        event["stepIndex"] = step_index
    return kept, unique_ranges


def _filter_low_quality_tail_candidates(events, stable_states, timeline_end_time):
    """Remove only unverified candidates produced after the board disappears."""
    states = {state["stateIndex"]: state for state in stable_states}
    kept, discarded = [], []
    tail_start = timeline_end_time * 0.8
    for event in events:
        before = states.get(event["sourceStateIndex"], {})
        after = states.get(event["targetStateIndex"], {})
        low_quality_tail = (
            event.get("confidence") != "verified"
            and not event.get("clearEffectEvidence")
            and after.get("startTime", 0.0) >= tail_start
            and max(before.get("gridAlignment", 0.0), after.get("gridAlignment", 0.0)) < 1.15
        )
        if low_quality_tail:
            discarded.append({
                "sourceStep": event["stepIndex"],
                "time": event["time"],
                "sourceStateIndex": event["sourceStateIndex"],
                "targetStateIndex": event["targetStateIndex"],
                "reason": "unverified_candidate_after_grid_loss",
            })
        else:
            kept.append(event)
    for step_index, event in enumerate(kept, 1):
        event["stepIndex"] = step_index
    return kept, discarded


def _classify_stable_transition(
    previous,
    current,
    placement_step=None,
    follows_clear=False,
):
    """Classify a stable board change without turning presets into moves."""
    added, removed = _difference(previous, current)
    if placement_step is not None:
        kind = "placement_path"
    elif removed and not added:
        kind = "clear_effect"
    elif added and (
        follows_clear
        or (not removed and not any(_occupancy(previous)))
    ):
        # Some recordings switch directly from the clearing board to the next
        # preset, so an empty stable frame is not guaranteed to exist.
        kind = "preset_load"
    elif len(added) + len(removed) <= 2:
        kind = "animation_noise"
    else:
        kind = "unresolved"
    return kind, added, removed


def _bottom_groups(frame, board, rows, cols, material_profile="unknown"):
    """Read the three fixed bottom slots as normalized cell shapes.

    Tray pieces may mix saturated blocks with pale or nearly-white materials.
    Detect occupancy independently from material, then retain the per-cell hue
    so mixed pieces remain one shape without losing their visual composition.
    """
    import cv2
    import numpy as np

    x, y, w, h = board
    frame_h, frame_w = frame.shape[:2]
    start_y = max(0, min(frame_h - 1, int(y + h + h * 0.035)))
    end_y = max(start_y + 1, frame_h - max(8, int(frame_h * 0.015)))
    crop = frame[start_y:end_y]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    pale_material = (saturation >= 10) & (value >= 170)
    saturated_material = (saturation >= 50) & (value >= 80)
    mask = np.where(pale_material | saturated_material, 255, 0).astype(np.uint8)
    if material_profile == "image_block":
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 35, 90)
        textured = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1) > 0
        visible = (value >= 55) & (value <= 248)
        mask = np.where((mask > 0) | (textured & visible), 255, 0).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask)

    # Bottom pieces occupy about 42% of a board cell. Same-color neighboring
    # cells can merge into one component, so components represent whole piece
    # regions here; they are split back into cells by local grid fitting below.
    expected_pitch_x = (w / cols) * 0.42
    expected_pitch_y = (h / rows) * 0.42
    components_by_slot = [[], [], []]
    slot_centers = [x + w / 6.0, x + w / 2.0, x + 5.0 * w / 6.0]
    for index in range(1, count):
        bx, by, bw, bh, area = map(int, stats[index])
        if area < expected_pitch_x * expected_pitch_y * 0.10:
            continue
        if bw > expected_pitch_x * 8.5 or bh > expected_pitch_y * 8.5:
            continue
        cx, cy = map(float, centroids[index])
        slot = min(range(3), key=lambda item: abs(cx - slot_centers[item]))
        if abs(cx - slot_centers[slot]) > w * 0.28:
            continue
        components_by_slot[slot].append((bx, by, bw, bh, area))

    def fit_count(length, expected_pitch):
        candidates = []
        for cell_count in range(1, 9):
            pitch = length / cell_count
            if expected_pitch * 0.62 <= pitch <= expected_pitch * 1.38:
                candidates.append((abs(pitch - expected_pitch) / expected_pitch, cell_count, pitch))
        return min(candidates)[1:] if candidates else (1, float(length))

    result = []
    for slot, components in enumerate(components_by_slot):
        if not components:
            result.append(None)
            continue
        left = min(item[0] for item in components)
        top = min(item[1] for item in components)
        right = max(item[0] + item[2] for item in components)
        bottom = max(item[1] + item[3] for item in components)
        local_mask = mask[top:bottom, left:right]
        local_hsv = hsv[top:bottom, left:right]
        grid_cols, pitch_x = fit_count(right - left, expected_pitch_x)
        grid_rows, pitch_y = fit_count(bottom - top, expected_pitch_y)
        normalized = {}
        for row in range(grid_rows):
            for col in range(grid_cols):
                x0 = int(round(col * (right - left) / grid_cols))
                x1 = int(round((col + 1) * (right - left) / grid_cols))
                y0 = int(round(row * (bottom - top) / grid_rows))
                y1 = int(round((row + 1) * (bottom - top) / grid_rows))
                cell_mask = local_mask[y0:y1, x0:x1]
                if cell_mask.size == 0 or float(np.mean(cell_mask > 0)) < 0.16:
                    continue
                selected = local_hsv[y0:y1, x0:x1][cell_mask > 0]
                hue = int(round(float(np.median(selected[:, 0])) / 5.0) * 5) % 180 if selected.size else 0
                normalized[(row, col)] = hue
        if not normalized:
            result.append(None)
            continue
        shape = [
            {"row": row, "col": col, "hue": hue}
            for (row, col), hue in sorted(normalized.items())
        ]
        shape_rows = max(cell["row"] for cell in shape) + 1
        shape_cols = max(cell["col"] for cell in shape) + 1
        result.append({
            "slot": slot,
            "shape": shape,
            "rows": shape_rows,
            "cols": shape_cols,
            "cellCount": len(shape),
            "center": {
                "x": round((left + right) / 2.0, 1),
                "y": round(start_y + (top + bottom) / 2.0, 1),
            },
            "recognition": "material_agnostic_component_grid_fit",
            "pitch": {"x": round(pitch_x, 2), "y": round(pitch_y, 2)},
        })
    return result


def _bottom_slot_activity(frame, board):
    """Measure material-independent edge activity in the three source slots."""
    import cv2
    import numpy as np

    x, y, w, h = board
    frame_h, frame_w = frame.shape[:2]
    start_y = max(0, int(y + h + h * 0.02))
    end_y = min(frame_h, int(y + h + h * 0.42))
    activity = []
    for slot in range(3):
        padding = w * 0.035
        start_x = max(0, int(x + w * slot / 3 + padding))
        end_x = min(frame_w, int(x + w * (slot + 1) / 3 - padding))
        roi = frame[start_y:end_y, start_x:end_x]
        if roi.size == 0:
            activity.append(0.0)
            continue
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 35, 90)
        activity.append(round(float(np.mean(edges > 0)), 6))
    return activity


def _detect_source_slot_pickups(samples, sample_fps):
    """Detect a piece leaving a slot from a sustained local edge decrease."""
    import numpy as np

    if len(samples) < 13:
        return []
    raw = np.asarray([sample.get("slotActivity", [0.0, 0.0, 0.0]) for sample in samples], dtype=float)
    smooth = np.asarray([
        [float(np.median(raw[max(0, index - 2):min(len(raw), index + 3), slot])) for slot in range(3)]
        for index in range(len(raw))
    ])
    candidates = []
    for index in range(6, len(smooth) - 6):
        for slot in range(3):
            before = float(np.median(smooth[index - 5:index, slot]))
            after = float(np.median(smooth[index + 1:index + 6, slot]))
            score = before - after
            if before >= 0.001 and after <= before * 0.45 and score >= 0.0008:
                candidates.append({
                    "time": samples[index]["time"],
                    "frameIndex": samples[index]["frameIndex"],
                    "sourceSlot": slot,
                    "score": round(score, 6),
                })
    # Collapse the several adjacent frames that describe one physical pickup.
    kept = []
    for candidate in sorted(candidates, key=lambda item: -item["score"]):
        if any(
            item["sourceSlot"] == candidate["sourceSlot"]
            and abs(item["time"] - candidate["time"]) < 0.35
            for item in kept
        ):
            continue
        kept.append(candidate)
    kept = [
        candidate for candidate in kept
        if len({
            item["sourceSlot"] for item in kept
            if abs(item["time"] - candidate["time"]) < 0.14
        }) < 3
    ]
    result = []
    for candidate in sorted(kept, key=lambda item: item["time"]):
        if result and candidate["time"] - result[-1]["time"] < 0.14:
            if candidate["score"] > result[-1]["score"]:
                result[-1] = candidate
        else:
            result.append(candidate)
    return result


def _has_pickup_coverage_gap(event_count, pickup_count):
    """Enable pickup backfill only for videos with a systemic action deficit."""
    return pickup_count >= max(12, round(event_count * 1.55))


def _post_large_clear_predecessor(events, pickup_time, rows, cols, max_gap=3.5):
    """Return the large-clear action whose animation can hide a next move."""
    threshold = max(4, min(rows, cols))
    candidates = [
        event for event in events
        if event.get("time", 0.0) < pickup_time
        and pickup_time - event.get("time", 0.0) <= max_gap
        and len(event.get("clearedRows", [])) + len(event.get("clearedCols", []))
        >= threshold
    ]
    return max(candidates, key=lambda item: item.get("time", 0.0), default=None)


def _early_clear_transition(stable_states, pickup_time, first_event_time, max_delay=1.1):
    """Find a clear-only board change before the first normal placement event."""
    best = None
    for before, after in zip(stable_states, stable_states[1:]):
        clear_time = float(after.get("startTime", after.get("time", 0.0)) or 0.0)
        if clear_time < pickup_time or clear_time - pickup_time > max_delay:
            continue
        if first_event_time is not None and clear_time >= first_event_time:
            continue
        added, removed = _difference(before["board"], after["board"])
        if removed and not added:
            candidate = {
                "before": before,
                "after": after,
                "time": clear_time,
                "removed": removed,
            }
            if best is None or candidate["time"] < best["time"]:
                best = candidate
    return best


def _pickup_remains_consumed(samples, pickup, minimum_duration=0.75):
    """Verify that a lifted source piece does not promptly return to its slot."""
    import numpy as np

    slot = pickup.get("sourceSlot")
    if slot not in (0, 1, 2) or not samples:
        return None
    index = min(
        range(len(samples)),
        key=lambda item: abs(samples[item]["frameIndex"] - pickup["frameIndex"]),
    )
    before = [
        sample.get("slotActivity", [0.0, 0.0, 0.0])[slot]
        for sample in samples[max(0, index - 6):index]
    ]
    after = [
        sample.get("slotActivity", [0.0, 0.0, 0.0])[slot]
        for sample in samples[index + 2:]
        if sample["time"] <= pickup["time"] + minimum_duration
    ]
    if len(before) < 4 or len(after) < 8:
        return None
    baseline = float(np.median(before))
    remaining = float(np.median(after))
    low_fraction = float(np.mean(np.asarray(after) <= baseline * 0.55))
    consumed = baseline >= 0.001 and remaining <= baseline * 0.42 and low_fraction >= 0.80
    return {
        "consumed": bool(consumed),
        "baselineActivity": round(baseline, 6),
        "remainingActivity": round(remaining, 6),
        "lowActivityFraction": round(low_fraction, 3),
        "duration": minimum_duration,
    }


def _grid_shape_from_mask(mask, max_cells=25):
    """Fit a compact square lattice to a tray difference mask."""
    import numpy as np

    ys, xs = np.where(mask > 0)
    if not len(xs):
        return None
    left, right = int(xs.min()), int(xs.max()) + 1
    top, bottom = int(ys.min()), int(ys.max()) + 1
    width, height = right - left, bottom - top
    best = None
    for grid_rows in range(1, 6):
        for grid_cols in range(1, 6):
            cell_aspect = (width / grid_cols) / max(1e-6, height / grid_rows)
            aspect_penalty = abs(float(np.log(cell_aspect)))
            densities = np.asarray([
                [
                    float(np.mean(mask[
                        round(top + row * height / grid_rows):round(top + (row + 1) * height / grid_rows),
                        round(left + col * width / grid_cols):round(left + (col + 1) * width / grid_cols),
                    ] > 0))
                    for col in range(grid_cols)
                ]
                for row in range(grid_rows)
            ])
            ordered = np.sort(densities.reshape(-1))
            if len(ordered) < 2:
                occupied = densities >= 0.18
            else:
                split = int(np.argmax(np.diff(ordered)))
                threshold = max(0.12, float(ordered[split] + ordered[split + 1]) / 2.0)
                occupied = densities >= threshold
            positions = {
                (row, col)
                for row in range(grid_rows)
                for col in range(grid_cols)
                if occupied[row, col]
            }
            if not (1 <= len(positions) <= max_cells) or not _connected_cells(positions):
                continue
            if not (
                any(row == 0 for row, _ in positions)
                and any(row == grid_rows - 1 for row, _ in positions)
                and any(col == 0 for _, col in positions)
                and any(col == grid_cols - 1 for _, col in positions)
            ):
                continue
            occupied_values = densities[occupied]
            empty_values = densities[~occupied]
            if not len(empty_values):
                continue
            separation = float(np.mean(occupied_values) - np.mean(empty_values))
            within = float(np.std(occupied_values) + np.std(empty_values))
            score = separation - 0.30 * within - 0.30 * aspect_penalty
            candidate = (score, positions, grid_rows, grid_cols)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None or best[0] < 0.38:
        return None
    score, positions, _, _ = best
    return {
        "shape": [
            {"row": row, "col": col, "hue": 0}
            for row, col in sorted(positions)
        ],
        "score": round(float(score), 3),
        "recognition": "source_slot_temporal_grid_fit",
    }


def _cluster_repeated_element_axis(values, tolerance):
    """Cluster noisy element centers into ordered lattice coordinates."""
    groups = []
    for value in sorted(float(item) for item in values):
        if not groups or value - groups[-1][-1] > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _repeated_round_element_shape(
    frame, board, rows, cols, source_slot, after_frame=None
):
    """Recover sparse tray shapes from repeated round rendered elements.

    This deliberately works from individual element centers instead of a
    merged foreground region. It handles themed pieces such as basketballs,
    whose transparent gaps are part of the intended shape.
    """
    import cv2
    import numpy as np

    if source_slot not in (0, 1, 2):
        return None
    x, y, width, height = board
    frame_height, frame_width = frame.shape[:2]
    top = max(0, int(y + height + height * 0.02))
    bottom = min(frame_height, int(y + height + height * 0.42))
    left = max(0, int(x + source_slot * width / 3.0 - width * 0.03))
    right = min(frame_width, int(x + (source_slot + 1) * width / 3.0 + width * 0.03))
    crop = frame[top:bottom, left:right]
    if crop.size == 0:
        return None

    board_pitch = (width / cols + height / rows) / 2.0
    minimum_radius = max(6, round(board_pitch * 0.12))
    maximum_radius = max(minimum_radius + 2, round(board_pitch * 0.36))
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 1.2)
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.1,
        minDist=max(12, round(board_pitch * 0.27)),
        param1=100,
        param2=30,
        minRadius=minimum_radius,
        maxRadius=maximum_radius,
    )
    if circles is None:
        return None
    detected = np.asarray(circles[0], dtype=float)
    if not 4 <= len(detected) <= 25:
        return None

    radii = detected[:, 2]
    median_radius = float(np.median(radii))
    if median_radius <= 0 or float(np.std(radii) / median_radius) > 0.16:
        return None

    temporal_score = 1.0
    if after_frame is not None and after_frame.shape == frame.shape:
        after_crop = after_frame[top:bottom, left:right]
        difference = cv2.cvtColor(cv2.absdiff(crop, after_crop), cv2.COLOR_BGR2GRAY)
        yy, xx = np.ogrid[:difference.shape[0], :difference.shape[1]]
        element_changes = []
        for center_x, center_y, radius in detected:
            disk = (
                (xx - center_x) ** 2 + (yy - center_y) ** 2
                <= (radius * 0.70) ** 2
            )
            element_changes.append(float(np.mean(difference[disk])))
        changed_fraction = float(np.mean(np.asarray(element_changes) >= 12.0))
        median_change = float(np.median(element_changes))
        if changed_fraction < 0.75 or median_change < 18.0:
            return None
        temporal_score = min(1.0, median_change / 45.0)
    axis_tolerance = max(3.0, median_radius * 0.70)
    x_levels = _cluster_repeated_element_axis(detected[:, 0], axis_tolerance)
    y_levels = _cluster_repeated_element_axis(detected[:, 1], axis_tolerance)
    if not (2 <= len(x_levels) <= 5 and 2 <= len(y_levels) <= 5):
        return None

    def regular_spacing(levels):
        if len(levels) <= 2:
            return 1.0
        gaps = np.diff(levels)
        mean_gap = float(np.mean(gaps))
        return 0.0 if mean_gap <= 0 else max(0.0, 1.0 - float(np.std(gaps) / mean_gap))

    x_regularity = regular_spacing(x_levels)
    y_regularity = regular_spacing(y_levels)
    if min(x_regularity, y_regularity) < 0.78:
        return None

    positions = set()
    for center_x, center_y, _ in detected:
        col = min(range(len(x_levels)), key=lambda index: abs(x_levels[index] - center_x))
        row = min(range(len(y_levels)), key=lambda index: abs(y_levels[index] - center_y))
        if abs(x_levels[col] - center_x) > axis_tolerance:
            return None
        if abs(y_levels[row] - center_y) > axis_tolerance:
            return None
        positions.add((row, col))
    if len(positions) != len(detected):
        return None

    # Sparse themed pieces may connect diagonally, so use 8-neighbor
    # connectivity here instead of the legacy polyomino-only 4-neighbor rule.
    pending = {next(iter(positions))}
    visited = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        row, col = current
        pending.update({
            (other_row, other_col)
            for other_row in range(row - 1, row + 2)
            for other_col in range(col - 1, col + 2)
            if (other_row, other_col) in positions
            and (other_row, other_col) not in visited
        })
    if visited != positions:
        return None

    radius_consistency = max(0.0, 1.0 - float(np.std(radii) / median_radius))
    score = (
        radius_consistency + x_regularity + y_regularity + temporal_score
    ) / 4.0
    return {
        "shape": [
            {"row": row, "col": col, "hue": 0}
            for row, col in sorted(positions)
        ],
        "score": round(float(score), 3),
        "recognition": "source_repeated_round_elements",
        "elementCount": len(positions),
        "lattice": {"rows": len(y_levels), "cols": len(x_levels)},
    }


def _pickup_piece_shape(
    before_frame, after_frame, board, rows, cols, source_slot=None,
    adaptive_grid=False,
):
    """Recover the lifted piece from the pixels that leave the source tray."""
    import cv2
    import numpy as np

    repeated = _repeated_round_element_shape(
        before_frame, board, rows, cols, source_slot, after_frame=after_frame
    )
    if repeated is not None and repeated["score"] >= 0.82:
        return repeated

    # Preserve the previous unscoped temporal-difference behavior for normal
    # pieces. The source slot is used above only by the repeated-element path.
    if not adaptive_grid:
        source_slot = None
    x, y, width, height = board
    frame_height, frame_width = before_frame.shape[:2]
    top = max(0, int(y + height + height * 0.02))
    bottom = min(frame_height, int(y + height + height * 0.42))
    if source_slot is None:
        left = max(0, int(x - width * 0.15))
        right = min(frame_width, int(x + width * 1.15))
    else:
        left = max(0, int(x + source_slot * width / 3.0 - width * 0.03))
        right = min(frame_width, int(x + (source_slot + 1) * width / 3.0 + width * 0.03))
    before = before_frame[top:bottom, left:right]
    after = after_frame[top:bottom, left:right]
    if before.size == 0 or before.shape != after.shape:
        return None
    difference = cv2.cvtColor(cv2.absdiff(before, after), cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(difference, 20, 255, cv2.THRESH_BINARY)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    kept = np.zeros_like(mask)
    board_pitch = (width / cols + height / rows) / 2.0
    for component in range(1, count):
        if stats[component, cv2.CC_STAT_AREA] >= max(30, board_pitch * board_pitch * 0.04):
            kept[labels == component] = 255
    if adaptive_grid:
        fitted = _grid_shape_from_mask(kept)
        if fitted is not None:
            return fitted
    ys, xs = np.where(kept > 0)
    if not len(xs):
        return None
    box_left, box_right = int(xs.min()), int(xs.max()) + 1
    box_top, box_bottom = int(ys.min()), int(ys.max()) + 1
    box_width, box_height = box_right - box_left, box_bottom - box_top
    aspect = max(box_width, box_height) / max(1, min(box_width, box_height))
    if aspect >= 2.4:
        if box_width >= box_height:
            grid_rows, grid_cols = 1, max(1, min(5, round(box_width / box_height)))
        else:
            grid_rows, grid_cols = max(1, min(5, round(box_height / box_width))), 1
    else:
        grid_cols = max(1, min(5, round(box_width / (width / cols))))
        grid_rows = max(1, min(5, round(box_height / (height / rows))))
    cells = []
    densities = []
    for row in range(grid_rows):
        for col in range(grid_cols):
            x0 = round(box_left + col * box_width / grid_cols)
            x1 = round(box_left + (col + 1) * box_width / grid_cols)
            y0 = round(box_top + row * box_height / grid_rows)
            y1 = round(box_top + (row + 1) * box_height / grid_rows)
            density = float(np.mean(kept[y0:y1, x0:x1] > 0))
            if density >= 0.18:
                cells.append({"row": row, "col": col, "hue": 0})
                densities.append(density)
    if not cells or len(cells) > 9:
        return None
    min_row = min(cell["row"] for cell in cells)
    min_col = min(cell["col"] for cell in cells)
    for cell in cells:
        cell["row"] -= min_row
        cell["col"] -= min_col
    return {
        "shape": cells,
        "score": round(float(np.mean(densities)), 3),
        "recognition": "source_tray_temporal_difference",
    }


def _pickup_target(before_frame, candidate_frames, board, rows, cols, shape):
    """Estimate the board anchor from the strongest shape-aligned motion."""
    import cv2
    import numpy as np

    x, y, width, height = board
    before = cv2.cvtColor(before_frame[y:y + height, x:x + width], cv2.COLOR_BGR2GRAY)
    if before.size == 0:
        return None
    best = None
    for frame_index, frame in candidate_frames:
        current = cv2.cvtColor(frame[y:y + height, x:x + width], cv2.COLOR_BGR2GRAY)
        if current.shape != before.shape:
            continue
        difference = cv2.absdiff(before, current)
        motion = np.zeros((rows, cols), dtype=float)
        for row in range(rows):
            for col in range(cols):
                top = int((row + 0.15) * height / rows)
                bottom = int((row + 0.85) * height / rows)
                left = int((col + 0.15) * width / cols)
                right = int((col + 0.85) * width / cols)
                motion[row, col] = float(np.mean(difference[top:bottom, left:right] > 20))
        for target_row in range(rows):
            for target_col in range(cols):
                placed = [
                    (target_row + int(cell["row"]), target_col + int(cell["col"]))
                    for cell in shape
                ]
                if any(row >= rows or col >= cols for row, col in placed):
                    continue
                inside = float(np.mean([motion[row, col] for row, col in placed]))
                outside = float(np.mean([
                    motion[row, col]
                    for row in range(rows)
                    for col in range(cols)
                    if (row, col) not in placed
                ]))
                score = inside - 0.2 * outside
                if best is None or score > best[0]:
                    best = (score, target_row, target_col, frame_index)
    if best is None:
        return None
    return {
        "target": {"row": best[1], "col": best[2]},
        "score": round(float(best[0]), 3),
        "frameIndex": best[3],
    }


def _repeated_round_element_target(candidate_frames, board, rows, cols, shape):
    """Locate a repeated-element piece directly on the board lattice.

    Full-board clear effects make motion-difference targets unreliable.  For
    themed round pieces, match the observed element centers against every
    legal anchor instead; this also identifies the cleanest hover frame.
    """
    import cv2
    import numpy as np

    if not shape:
        return None
    x, y, width, height = board
    pitch_x = width / cols
    pitch_y = height / rows
    pitch = (pitch_x + pitch_y) / 2.0
    shape_rows = max(int(cell["row"]) for cell in shape) + 1
    shape_cols = max(int(cell["col"]) for cell in shape) + 1
    best = None
    for frame_index, frame in candidate_frames:
        crop = frame[y:y + height, x:x + width]
        if crop.size == 0:
            continue
        gray = cv2.GaussianBlur(
            cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (5, 5), 1.2
        )
        circles = cv2.HoughCircles(
            gray,
            cv2.HOUGH_GRADIENT,
            dp=1.1,
            minDist=max(12, round(pitch * 0.27)),
            param1=100,
            param2=30,
            minRadius=max(6, round(pitch * 0.12)),
            maxRadius=max(8, round(pitch * 0.36)),
        )
        if circles is None:
            continue
        detected = [
            (float(center_x), float(center_y), float(radius))
            for center_x, center_y, radius in circles[0]
        ]
        maximum_distance = pitch * 0.42
        for target_row in range(rows - shape_rows + 1):
            for target_col in range(cols - shape_cols + 1):
                expected = [
                    (
                        (target_col + int(cell["col"]) + 0.5) * pitch_x,
                        (target_row + int(cell["row"]) + 0.5) * pitch_y,
                    )
                    for cell in shape
                ]
                pairs = sorted(
                    (
                        float(np.hypot(expected_x - center_x, expected_y - center_y)),
                        expected_index,
                        detected_index,
                    )
                    for expected_index, (expected_x, expected_y) in enumerate(expected)
                    for detected_index, (center_x, center_y, _) in enumerate(detected)
                )
                used_expected = set()
                used_detected = set()
                distances = []
                for distance, expected_index, detected_index in pairs:
                    if distance > maximum_distance:
                        break
                    if expected_index in used_expected or detected_index in used_detected:
                        continue
                    used_expected.add(expected_index)
                    used_detected.add(detected_index)
                    distances.append(distance)
                if len(distances) != len(expected):
                    continue
                raw_score = 1.0 - (
                    0.70 * float(np.mean(distances))
                    + 0.30 * float(np.max(distances))
                ) / maximum_distance
                # Matching every expected center uniquely is strong evidence,
                # especially for sparse rings with many elements.
                score = 0.65 + 0.35 * max(0.0, raw_score)
                candidate = (
                    score,
                    -float(np.mean(distances)),
                    frame_index,
                    target_row,
                    target_col,
                )
                if best is None or candidate > best:
                    best = candidate
    if best is None or best[0] < 0.82:
        return None
    return {
        "target": {"row": best[3], "col": best[4]},
        "score": round(float(best[0]), 3),
        "frameIndex": int(best[2]),
        "recognition": "repeated_elements_aligned_to_board_grid",
    }


def _repeated_round_board_evidence(frame, board, rows, cols):
    """Map repeated round elements to occupied board cells.

    The detector intentionally ignores full-board results: those are usually
    celebration overlays rather than a playable board state. Stable temporal
    consensus is applied separately before this evidence can replace the
    legacy color/brightness classifier.
    """
    import cv2
    import numpy as np

    x, y, width, height = board
    crop = frame[y:y + height, x:x + width]
    if crop.size == 0:
        return None
    pitch_x = width / cols
    pitch_y = height / rows
    pitch = (pitch_x + pitch_y) / 2.0
    gray = cv2.GaussianBlur(
        cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (5, 5), 1.2
    )
    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=1.1,
        minDist=max(12, round(pitch * 0.27)),
        param1=100,
        param2=30,
        minRadius=max(6, round(pitch * 0.12)),
        maxRadius=max(8, round(pitch * 0.46)),
    )
    if circles is None:
        return None
    aligned = {}
    for center_x, center_y, radius in circles[0]:
        if not pitch * 0.24 <= radius <= pitch * 0.46:
            continue
        col = round(float(center_x) / pitch_x - 0.5)
        row = round(float(center_y) / pitch_y - 0.5)
        if not (0 <= row < rows and 0 <= col < cols):
            continue
        distance = float(np.hypot(
            center_x - (col + 0.5) * pitch_x,
            center_y - (row + 0.5) * pitch_y,
        )) / pitch
        if distance > 0.28:
            continue
        previous = aligned.get((row, col))
        if previous is None or distance < previous[0]:
            aligned[(row, col)] = (distance, float(radius))
    occupied = set(aligned)
    # Decorative text and clear effects can contribute isolated circles. A
    # repeated board pattern has grid-adjacent peers, so discard isolated
    # detections when a meaningful connected pattern remains.
    connected = {
        cell
        for cell in occupied
        if any(
            (cell[0] + delta_row, cell[1] + delta_col) in occupied
            for delta_row, delta_col in ((-1, 0), (1, 0), (0, -1), (0, 1))
        )
    }
    if len(connected) >= 4:
        occupied = connected
        aligned = {cell: aligned[cell] for cell in occupied}
    if not 4 <= len(occupied) <= max(4, round(rows * cols * 0.50)):
        return None
    distances = np.asarray([value[0] for value in aligned.values()], dtype=float)
    radii = np.asarray([value[1] for value in aligned.values()], dtype=float)
    radius_variation = float(np.std(radii) / max(1e-6, np.median(radii)))
    if radius_variation > 0.20:
        return None
    score = (
        max(0.0, 1.0 - float(np.mean(distances)) / 0.28)
        + max(0.0, 1.0 - radius_variation / 0.20)
    ) / 2.0
    if score < 0.58:
        return None
    return {
        "occupied": occupied,
        "score": round(score, 3),
        "elementCount": len(occupied),
        "recognition": "repeated_round_elements_on_board_grid",
    }


def _apply_repeated_board_consensus(samples, minimum_frames, maximum_gap=1):
    """Fuse persistent repeated-element states into sampled board matrices."""
    corrected_frames = 0
    corrected_runs = 0
    rejected_runs = 0
    index = 0
    while index < len(samples):
        evidence = samples[index].get("repeatedBoardEvidence")
        if not evidence:
            index += 1
            continue
        signature = tuple(sorted(evidence["occupied"]))
        evidence_indices = [index]
        cursor = index + 1
        while cursor < len(samples):
            following = samples[cursor].get("repeatedBoardEvidence")
            if following:
                if tuple(sorted(following["occupied"])) != signature:
                    break
                evidence_indices.append(cursor)
            elif cursor - evidence_indices[-1] > maximum_gap:
                break
            cursor += 1
        run = [samples[evidence_index] for evidence_index in evidence_indices]
        mean_score = sum(
            sample["repeatedBoardEvidence"]["score"] for sample in run
        ) / len(run)
        if len(run) >= minimum_frames and mean_score >= 0.68:
            occupied = set(signature)
            corrected = samples[evidence_indices[0]:evidence_indices[-1] + 1]
            legacy_signatures = []
            for sample in corrected:
                legacy_signatures.append(tuple(
                    (row, col)
                    for row, values in enumerate(sample["board"])
                    for col, value in enumerate(values)
                    if value is not None
                ))
            legacy_mode = max(
                set(legacy_signatures), key=legacy_signatures.count
            )
            # Repeated-element evidence may also appear while a piece is moving
            # or a clear animation is active. It may smooth a stable state, but
            # must not invent a new state boundary or placement event.
            if legacy_mode == signature:
                for sample in corrected:
                    legacy = sample["board"]
                    sample["board"] = [
                        [
                            legacy[row][col]
                            if (row, col) in occupied and legacy[row][col] is not None
                            else (0 if (row, col) in occupied else None)
                            for col in range(len(legacy[row]))
                        ]
                        for row in range(len(legacy))
                    ]
                    sample["boardRecognition"] = (
                        "repeated_round_elements_temporal_consensus"
                    )
                corrected_frames += len(corrected)
                corrected_runs += 1
            else:
                rejected_runs += 1
        index = max(index + 1, cursor)
    return {
        "correctedFrames": corrected_frames,
        "correctedRuns": corrected_runs,
        "rejectedRuns": rejected_runs,
    }


def _source_slot_material_consensus(candidates, after_group=None):
    """Choose a stable tray shape without assuming every cell has one material."""
    usable = [item for item in candidates if item[0] and item[0].get("shape")]
    if len(usable) < 3:
        return None
    signatures = [
        tuple((cell["row"], cell["col"]) for cell in item[0]["shape"])
        for item in usable
    ]
    winning_signature, support = Counter(signatures).most_common(1)[0]
    if support < 3 or support / len(usable) < 0.55:
        return None
    if after_group and tuple(
        (cell["row"], cell["col"]) for cell in after_group.get("shape", [])
    ) == winning_signature:
        return None

    matches = [
        item for item, signature in zip(usable, signatures)
        if signature == winning_signature
    ]
    representative, before, before_frame_index = matches[-1]
    hue_by_cell = {}
    for group, _, _ in matches:
        for cell in group["shape"]:
            hue_by_cell.setdefault((cell["row"], cell["col"]), []).append(cell["hue"])
    shape = []
    for row, col in winning_signature:
        hues = hue_by_cell[(row, col)]
        hue = Counter(hues).most_common(1)[0][0]
        shape.append({"row": row, "col": col, "hue": hue})
    evidence = dict(representative)
    evidence.update({
        "shape": shape,
        "rows": max(cell["row"] for cell in shape) + 1,
        "cols": max(cell["col"] for cell in shape) + 1,
        "cellCount": len(shape),
        "recognition": "source_slot_material_consensus",
        "score": round(support / len(usable), 3),
        "consensusSupport": support,
        "consensusSampleCount": len(usable),
        "consensusFrameIndex": before_frame_index,
    })
    return evidence, before, before_frame_index


def _pickup_visual_evidence(
    video_path, board, rows, cols, pickups, adaptive_grid=False,
    shape_after_offset=5,
):
    """Read compact visual evidence only for independently detected pickups."""
    import cv2

    capture = cv2.VideoCapture(str(video_path))
    results = [None] * len(pickups)
    due_pickups = {}
    repeated_offsets = (-30, -24, -21, -18, -15, -12, -9, -6, -5)
    required_frames = set()
    for pickup_index, pickup in enumerate(pickups):
        source_frame = int(pickup["frameIndex"])
        due_frame = source_frame + 15
        due_pickups.setdefault(due_frame, []).append(pickup_index)
        required_frames.update(
            source_frame + offset
            for offset in repeated_offsets
            if source_frame + offset >= 0
        )
        required_frames.update(source_frame + offset for offset in range(3, 16))

    # Decode once in frame order. A short rolling window is enough for every
    # pickup and avoids retaining hundreds of full-resolution video frames.
    frame_window = {}
    frame_index = 0
    final_frame = max(due_pickups, default=-1)
    while frame_index <= final_frame:
        ok, frame = capture.read()
        if not ok:
            break
        if frame_index in required_frames:
            frame_window[frame_index] = frame
        for pickup_index in due_pickups.get(frame_index, []):
            pickup = pickups[pickup_index]
            source_frame = int(pickup["frameIndex"])
            after = frame_window.get(source_frame + shape_after_offset)
            if after is None:
                continue
            repeated_candidates = []
            material_candidates = []
            source_slot = pickup.get("sourceSlot")
            for offset in repeated_offsets:
                before_candidate = frame_window.get(source_frame + offset)
                if before_candidate is None:
                    continue
                if isinstance(source_slot, int) and 0 <= source_slot < 3:
                    slot_group = _bottom_groups(
                        before_candidate, board, rows, cols
                    )[source_slot]
                    if slot_group:
                        material_candidates.append((
                            slot_group,
                            before_candidate,
                            source_frame + offset,
                        ))
                repeated = _repeated_round_element_shape(
                    before_candidate,
                    board,
                    rows,
                    cols,
                    pickup.get("sourceSlot"),
                    after_frame=after,
                )
                if repeated is not None:
                    signature = tuple(
                        (cell["row"], cell["col"])
                        for cell in repeated["shape"]
                    )
                    repeated_candidates.append((
                        signature,
                        repeated,
                        before_candidate,
                        source_frame + offset,
                    ))
            shape_evidence = None
            before = frame_window.get(max(0, source_frame - 5))
            after_group = None
            if isinstance(source_slot, int) and 0 <= source_slot < 3:
                after_group = _bottom_groups(after, board, rows, cols)[source_slot]
            material_consensus = _source_slot_material_consensus(
                material_candidates, after_group
            )
            if material_consensus:
                shape_evidence, before, _ = material_consensus
            if repeated_candidates:
                signature_counts = Counter(
                    item[0] for item in repeated_candidates
                )
                winning_signature, support = signature_counts.most_common(1)[0]
                if support >= 3:
                    matches = [
                        item for item in repeated_candidates
                        if item[0] == winning_signature
                    ]
                    _, repeated_evidence, repeated_before, before_frame_index = max(
                        matches, key=lambda item: item[1].get("score", 0.0)
                    )
                    repeated_evidence = dict(repeated_evidence)
                    repeated_evidence["consensusSupport"] = support
                    repeated_evidence["consensusFrameIndex"] = before_frame_index
                    if (
                        repeated_evidence.get("score", 0.0) >= 0.82
                        or shape_evidence is None
                    ):
                        shape_evidence = repeated_evidence
                        before = repeated_before
            if shape_evidence is None and before is not None:
                shape_evidence = _pickup_piece_shape(
                    before, after, board, rows, cols,
                    source_slot=pickup.get("sourceSlot"),
                    adaptive_grid=adaptive_grid,
                )
            if not shape_evidence:
                continue
            target_frames = [
                (source_frame + offset, frame_window[source_frame + offset])
                for offset in range(3, 16)
                if source_frame + offset in frame_window
            ]
            motion_target_evidence = _pickup_target(
                before, target_frames, board, rows, cols,
                shape_evidence["shape"],
            )
            target_evidence = None
            if (
                shape_evidence.get("recognition")
                == "source_repeated_round_elements"
            ):
                target_evidence = _repeated_round_element_target(
                    target_frames, board, rows, cols, shape_evidence["shape"]
                )
            if target_evidence is None:
                target_evidence = motion_target_evidence
            results[pickup_index] = {
                "shape": shape_evidence,
                "target": target_evidence,
                "stableTarget": motion_target_evidence,
            }

        oldest_needed = frame_index - 48
        for old_index in [key for key in frame_window if key < oldest_needed]:
            del frame_window[old_index]
        frame_index += 1
    capture.release()
    return results


def _group_signature(groups):
    signature = []
    for group in groups:
        if group is None:
            signature.append(None)
        else:
            signature.append(tuple((cell["row"], cell["col"]) for cell in group["shape"]))
    return tuple(signature)


def _representative_groups(samples):
    signatures = [_group_signature(sample["groups"]) for sample in samples]
    signature, _ = Counter(signatures).most_common(1)[0]
    return next(sample["groups"] for sample in samples if _group_signature(sample["groups"]) == signature)


def _group_candidates(samples):
    """Return group sets that persist, excluding one-frame drag fragments."""
    counts = Counter(_group_signature(sample["groups"]) for sample in samples)
    representatives = {}
    for sample in samples:
        representatives.setdefault(_group_signature(sample["groups"]), sample["groups"])
    candidates = [
        representatives[signature]
        for signature, count in counts.most_common()
        if count >= 3
    ]
    return candidates or [_representative_groups(samples)]


def _simulate(board_signature, shape, target_row, target_col, rows, cols):
    occupied = {
        (row, col)
        for row in range(rows)
        for col in range(cols)
        if board_signature[row * cols + col]
    }
    placed = set()
    for cell in shape:
        row, col = target_row + cell["row"], target_col + cell["col"]
        if not (0 <= row < rows and 0 <= col < cols) or (row, col) in occupied:
            return None
        placed.add((row, col))
    combined = occupied | placed
    cleared_rows = [row for row in range(rows) if all((row, col) in combined for col in range(cols))]
    cleared_cols = [col for col in range(cols) if all((row, col) in combined for row in range(rows))]
    cleared = {
        (row, col)
        for row, col in combined
        if row in cleared_rows or col in cleared_cols
    }
    final = combined - cleared
    placed_signature = tuple((row, col) in combined for row in range(rows) for col in range(cols))
    final_signature = tuple((row, col) in final for row in range(rows) for col in range(cols))
    return placed_signature, final_signature, cleared_rows, cleared_cols, placed


def _connected_cells(cells):
    cells = set(cells)
    if not cells:
        return False
    seen = {next(iter(cells))}
    stack = list(seen)
    while stack:
        row, col = stack.pop()
        for neighbor in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
            if neighbor in cells and neighbor not in seen:
                seen.add(neighbor)
                stack.append(neighbor)
    return seen == cells


def _reverse_clear_moves(
    before_signature,
    after_signature,
    rows,
    cols,
    max_piece_cells=25,
    effect_rows=None,
    effect_cols=None,
):
    """Recover a placed piece from boards observed before and after line clears.

    A candidate clear line must contain at least one previously occupied cell
    that disappeared. For each row/column cover of those removed cells, the
    missing cells needed to complete the lines plus surviving additions define
    exactly one possible piece. Exact forward simulation is the final filter.
    """
    before = {
        (row, col)
        for row in range(rows)
        for col in range(cols)
        if before_signature[row * cols + col]
    }
    after = {
        (row, col)
        for row in range(rows)
        for col in range(cols)
        if after_signature[row * cols + col]
    }
    removed = before - after
    if not removed:
        return []

    row_options = sorted({row for row, _ in removed})
    col_options = sorted({col for _, col in removed})
    candidates = {}
    for row_mask in range(1 << len(row_options)):
        cleared_rows = {
            row_options[index]
            for index in range(len(row_options))
            if row_mask & (1 << index)
        }
        for col_mask in range(1 << len(col_options)):
            cleared_cols = {
                col_options[index]
                for index in range(len(col_options))
                if col_mask & (1 << index)
            }
            if not cleared_rows and not cleared_cols:
                continue
            if any(row not in cleared_rows and col not in cleared_cols for row, col in removed):
                continue
            if any(row in cleared_rows or col in cleared_cols for row, col in after):
                continue
            if any(
                (row, col) not in after
                for row, col in before
                if row not in cleared_rows and col not in cleared_cols
            ):
                continue

            cleared_area = {
                (row, col)
                for row in range(rows)
                for col in range(cols)
                if row in cleared_rows or col in cleared_cols
            }
            placed = (after - before) | (cleared_area - before)
            if not (1 <= len(placed) <= max_piece_cells) or not _connected_cells(placed):
                continue
            # A placement may only claim lines it actually completes. Without
            # this guard, a pending clear from the previous move can be joined
            # to an unrelated next piece and still satisfy forward simulation.
            if any(not any(cell_row == row for cell_row, _ in placed) for row in cleared_rows):
                continue
            if any(not any(cell_col == col for _, cell_col in placed) for col in cleared_cols):
                continue

            min_row = min(row for row, _ in placed)
            min_col = min(col for _, col in placed)
            shape = [
                {"row": row - min_row, "col": col - min_col, "hue": 0}
                for row, col in sorted(placed)
            ]
            simulation = _simulate(before_signature, shape, min_row, min_col, rows, cols)
            if simulation is None:
                continue
            _, final_signature, simulated_rows, simulated_cols, simulated_placed = simulation
            if final_signature != after_signature:
                continue
            if set(simulated_rows) != cleared_rows or set(simulated_cols) != cleared_cols:
                continue
            if effect_rows is not None and set(simulated_rows) != set(effect_rows):
                continue
            if effect_cols is not None and set(simulated_cols) != set(effect_cols):
                continue

            key = (
                tuple((cell["row"], cell["col"]) for cell in shape),
                min_row,
                min_col,
                tuple(simulated_rows),
                tuple(simulated_cols),
            )
            candidates[key] = {
                "shape": shape,
                "target": {"row": min_row, "col": min_col},
                "placedCells": [
                    {"row": row, "col": col}
                    for row, col in sorted(simulated_placed)
                ],
                "clearedRows": simulated_rows,
                "clearedCols": simulated_cols,
                "clearMode": "immediate",
            }
    return list(candidates.values())


def _slot_matches_for_shape(before_state, shape):
    signature = tuple(sorted((cell["row"], cell["col"]) for cell in shape))
    matches = {}
    for group_set in before_state.get("groupCandidates", []):
        for slot, group in enumerate(group_set):
            if group is None:
                continue
            group_signature = tuple(sorted((cell["row"], cell["col"]) for cell in group["shape"]))
            if group_signature == signature:
                matches[slot] = group
    return matches


def _group_shape_signature(group):
    if group is None:
        return None
    return tuple(sorted((cell["row"], cell["col"]) for cell in group["shape"]))


def _same_visible_groups(first_state, second_state):
    first = first_state.get("groups") or [None, None, None]
    second = second_state.get("groups") or [None, None, None]
    return all(
        _group_shape_signature(first[slot] if slot < len(first) else None)
        == _group_shape_signature(second[slot] if slot < len(second) else None)
        for slot in range(3)
    )


def _is_visual_cleanup_state(before_state, after_state, rows, cols):
    """Identify a cleaner observation of the same pre-placement board.

    Score text and clear particles can briefly occupy board cells. They may
    disappear without a player move while the three source pieces stay fixed.
    Only the experimental recognizer is allowed to skip such a state.
    """
    if after_state["startTime"] - before_state["endTime"] > 0.5:
        return False
    added, removed = _difference(before_state["board"], after_state["board"])
    if added or not (1 <= len(removed) <= max(2, round(rows * cols * 0.04))):
        return False
    if not _same_visible_groups(before_state, after_state):
        return False
    before_quality = before_state.get("gridAlignment", 0.0)
    after_quality = after_state.get("gridAlignment", 0.0)
    return after_quality >= max(1.2, before_quality * 1.2)


def _slot_for_inferred_shape(before_state, after_state, shape):
    matches = _slot_matches_for_shape(before_state, shape)
    if len(matches) == 1:
        slot, group = next(iter(matches.items()))
        return slot, group

    changed_slots = []
    before_groups = before_state.get("groups") or [None, None, None]
    after_groups = after_state.get("groups") or [None, None, None]
    for slot in range(min(3, len(before_groups), len(after_groups))):
        before_group = before_groups[slot]
        after_group = after_groups[slot]
        before_signature = None if before_group is None else tuple(
            sorted((cell["row"], cell["col"]) for cell in before_group["shape"])
        )
        after_signature = None if after_group is None else tuple(
            sorted((cell["row"], cell["col"]) for cell in after_group["shape"])
        )
        if before_group is not None and before_signature != after_signature:
            changed_slots.append(slot)
    if len(changed_slots) == 1:
        slot = changed_slots[0]
        return slot, before_groups[slot]
    return -1, None


def _find_move(before_signature, after_signature, groups, rows, cols):
    matches = []
    for slot, group in enumerate(groups):
        if group is None or not group["shape"]:
            continue
        for target_row in range(rows - group["rows"] + 1):
            for target_col in range(cols - group["cols"] + 1):
                simulation = _simulate(before_signature, group["shape"], target_row, target_col, rows, cols)
                if simulation is None:
                    continue
                placed_signature, final_signature, cleared_rows, cleared_cols, placed = simulation
                if after_signature == placed_signature:
                    clear_mode = "deferred" if cleared_rows or cleared_cols else "none"
                elif after_signature == final_signature:
                    clear_mode = "immediate"
                else:
                    continue
                matches.append({
                    "slot": slot,
                    "group": group,
                    "target": {"row": target_row, "col": target_col},
                    "clearedRows": cleared_rows,
                    "clearedCols": cleared_cols,
                    "clearMode": clear_mode,
                    "postClearSignature": final_signature,
                    "placedCells": [
                        {"row": row, "col": col}
                        for row, col in sorted(placed)
                    ],
                })
    return matches


def _collapse_drag_windows(events, stable_states, rows, cols):
    """Replace short drag-over-board candidates with one settled placement.

    This is intentionally conservative: the window must remain contiguous,
    end in a long-lived state, and produce one connected final shape. It may
    either consume one bottom slot or contain the add/remove signature made
    when a dragged piece briefly crosses board cells before settling.
    """
    states = {state["stateIndex"]: state for state in stable_states}

    def connected(cells):
        positions = {(cell["row"], cell["col"]) for cell in cells}
        if not positions:
            return False
        pending = [next(iter(positions))]
        visited = set()
        while pending:
            row, col = pending.pop()
            if (row, col) in visited:
                continue
            visited.add((row, col))
            for neighbor in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
                if neighbor in positions and neighbor not in visited:
                    pending.append(neighbor)
        return visited == positions

    collapsed = []
    index = 0
    while index < len(events):
        first = events[index]
        if first.get("confidence") == "verified":
            collapsed.append(first)
            index += 1
            continue
        end = index
        while end + 1 < len(events):
            current, following = events[end], events[end + 1]
            if following.get("confidence") == "verified":
                break
            if following["time"] - first["time"] > 0.65:
                break
            if following["sourceStateIndex"] != current["targetStateIndex"]:
                break
            end += 1
        if end == index:
            collapsed.append(first)
            index += 1
            continue

        last = events[end]
        before = states[first["sourceStateIndex"]]
        after = states[last["targetStateIndex"]]
        added, removed = _difference(before["board"], after["board"])
        before_groups = before.get("groups") or [None, None, None]
        after_groups = after.get("groups") or [None, None, None]
        disappeared_slots = [
            slot
            for slot in range(min(3, len(before_groups), len(after_groups)))
            if before_groups[slot] is not None and after_groups[slot] is None
        ]
        window = events[index:end + 1]
        tiny_slot_consumption = (
            first.get("group", {}).get("cellCount", 0) <= 2
            and len(disappeared_slots) == 1
        )
        drag_overlay_signature = (
            not first.get("removedCells")
            and any(event.get("removedCells") for event in window[1:])
        )
        can_collapse = (
            len(added) >= 3
            and len(added) <= 25
            and not removed
            and connected(added)
            and after.get("frameCount", 0) >= 6
            and (tiny_slot_consumption or drag_overlay_signature)
        )
        if not can_collapse:
            # Keep overlapping windows available for reconsideration. A clear
            # event immediately before a drag must not hide the drag window.
            collapsed.append(first)
            index += 1
            continue

        min_row = min(cell["row"] for cell in added)
        min_col = min(cell["col"] for cell in added)
        shape = [
            {"row": cell["row"] - min_row, "col": cell["col"] - min_col, "hue": cell["hue"]}
            for cell in added
        ]
        combined = list(_occupancy(before["board"]))
        for cell in added:
            combined[cell["row"] * cols + cell["col"]] = True
        completed_rows = [row for row in range(rows) if all(combined[row * cols + col] for col in range(cols))]
        completed_cols = [col for col in range(cols) if all(combined[row * cols + col] for row in range(rows))]
        source_slot = disappeared_slots[0] if len(disappeared_slots) == 1 else -1
        merged = dict(last)
        merged.update({
            "sourceStateIndex": first["sourceStateIndex"],
            "targetStateIndex": last["targetStateIndex"],
            "time": last["time"],
            "frameIndex": last["frameIndex"],
            "type": "placement_candidate",
            "sourceSlot": source_slot,
            "group": {
                "slot": source_slot,
                "shape": shape,
                "rows": max(cell["row"] for cell in shape) + 1,
                "cols": max(cell["col"] for cell in shape) + 1,
                "cellCount": len(shape),
                "center": None,
            },
            "target": {"row": min_row, "col": min_col},
            "placedCells": [{"row": cell["row"], "col": cell["col"]} for cell in added],
            "clearedRows": completed_rows,
            "clearedCols": completed_cols,
            "clearMode": "deferred" if completed_rows or completed_cols else "none",
            "addedCells": added,
            "removedCells": [],
            "beforeBoard": before["board"],
            "afterBoard": after["board"],
            "confidence": "candidate",
            "verification": "drag_window_collapsed_by_terminal_state",
            "candidateReasons": [
                "drag_overlay_add_remove" if drag_overlay_signature else "short_intermediate_deltas",
                "single_slot_consumed" if source_slot >= 0 else "source_slot_requires_confirmation",
                "terminal_state_stable",
            ],
            "collapsedSourceSteps": [event["stepIndex"] for event in events[index:end + 1]],
            "candidateSolutions": [],
        })
        collapsed.append(merged)
        index = end + 1

    for step_index, event in enumerate(collapsed, 1):
        event["stepIndex"] = step_index
    return collapsed


def _coalesce_action_fragments(events, stable_states, rows, cols, max_window=0.70):
    """Merge board deltas emitted while one dragged piece is settling.

    Stable-state detection can briefly treat partially covered cells as several
    placements. Real actions in the confirmed development set are separated by
    more than 0.70 seconds, while these fragments are contiguous and occur
    inside that window. The merged shape is reconstructed from the union of
    newly occupied cells relative to the board before the first fragment.
    """
    states = {state["stateIndex"]: state for state in stable_states}

    def connected(positions):
        if not positions:
            return False
        pending = [next(iter(positions))]
        visited = set()
        while pending:
            position = pending.pop()
            if position in visited:
                continue
            visited.add(position)
            row, col = position
            pending.extend(
                neighbor
                for neighbor in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1))
                if neighbor in positions and neighbor not in visited
            )
        return visited == positions

    merged_events = []
    index = 0
    while index < len(events):
        first = events[index]
        end = index
        recognized_slots = {first.get("sourceSlot")} if first.get("sourceSlot", -1) >= 0 else set()
        while end + 1 < len(events):
            current, following = events[end], events[end + 1]
            state_gap = following["sourceStateIndex"] - current["targetStateIndex"]
            if state_gap < 0 or state_gap > 2:
                break
            # A skipped stable state plus a visible half-second interval is a
            # completed action boundary, not a settling fragment. DEV-010 has
            # distinct placements on both sides of this boundary; merging
            # them silently removes real moves.
            if state_gap > 0 and following["time"] - current["time"] >= 0.50:
                break
            if following["time"] - first["time"] > max_window:
                break
            following_slot = following.get("sourceSlot", -1)
            if following_slot >= 0 and recognized_slots and following_slot not in recognized_slots:
                break
            if following_slot >= 0:
                recognized_slots.add(following_slot)
            end += 1
        if end == index:
            merged_events.append(first)
            index += 1
            continue

        window = events[index:end + 1]
        before = states[first["sourceStateIndex"]]
        after = states[window[-1]["targetStateIndex"]]
        before_occupied = {
            (row, col)
            for row in range(rows)
            for col in range(cols)
            if before["board"][row][col] is not None
        }
        added_positions = {
            (cell["row"], cell["col"])
            for event in window
            for cell in event.get("addedCells", [])
            if (cell["row"], cell["col"]) not in before_occupied
        }
        usable_union = 1 <= len(added_positions) <= 25 and connected(added_positions)
        anchor = next((event for event in window if event.get("confidence") == "verified"), first)
        merged = dict(anchor)
        merged.update({
            "sourceStateIndex": first["sourceStateIndex"],
            "targetStateIndex": window[-1]["targetStateIndex"],
            "time": first["time"],
            "frameIndex": first["frameIndex"],
            "beforeBoard": before["board"],
            "afterBoard": after["board"],
            "collapsedSourceSteps": [
                step
                for event in window
                for step in (event.get("collapsedSourceSteps") or [event["stepIndex"]])
            ],
        })
        if usable_union:
            min_row = min(row for row, _ in added_positions)
            min_col = min(col for _, col in added_positions)
            shape = [
                {
                    "row": row - min_row,
                    "col": col - min_col,
                    "hue": after["board"][row][col] if after["board"][row][col] is not None else 0,
                }
                for row, col in sorted(added_positions)
            ]
            slot_matches = _slot_matches_for_shape(before, shape)
            recognized_slots = {event.get("sourceSlot") for event in window if event.get("sourceSlot", -1) >= 0}
            source_slot = next(iter(slot_matches)) if len(slot_matches) == 1 else (
                next(iter(recognized_slots)) if len(recognized_slots) == 1 else -1
            )
            merged.update({
                "sourceSlot": source_slot,
                "group": {
                    "slot": source_slot,
                    "shape": shape,
                    "rows": max(cell["row"] for cell in shape) + 1,
                    "cols": max(cell["col"] for cell in shape) + 1,
                    "cellCount": len(shape),
                    "center": None,
                },
                "target": {"row": min_row, "col": min_col},
                "placedCells": [{"row": row, "col": col} for row, col in sorted(added_positions)],
                "addedCells": [
                    {"row": row, "col": col, "hue": after["board"][row][col]}
                    for row, col in sorted(added_positions)
                ],
                "verification": "action_fragments_coalesced",
                "confidence": "verified" if len(slot_matches) == 1 else "candidate",
            })
        reasons = list(merged.get("candidateReasons") or [])
        if "short_action_fragments_merged" not in reasons:
            reasons.append("short_action_fragments_merged")
        merged["candidateReasons"] = reasons
        merged_events.append(merged)
        index = end + 1

    for step_index, event in enumerate(merged_events, 1):
        event["stepIndex"] = step_index
    return merged_events


def _filter_color_clear_cooldown_fragments(events, cooldown=0.42):
    """Drop tiny color-block fragments that appear inside a clear cooldown.

    Color blocks are already easy to segment, so the common failure is not
    missed material but clear animation being promoted to another placement.
    Keep strong pickup-backed actions and only remove weak, tiny fragments.
    """
    filtered = []
    discarded = []
    last_clear_time = None
    for event in events:
        is_clear = (
            event.get("clearMode") in {"immediate", "deferred", "unknown"}
            and (event.get("clearedRows") or event.get("clearedCols"))
        )
        cell_count = int((event.get("group") or {}).get("cellCount") or 0)
        has_pickup = bool(event.get("sourcePickupEvidence"))
        has_source_slot = int(event.get("sourceSlot", -1)) >= 0
        weak_fragment = (
            last_clear_time is not None
            and 0.0 < float(event.get("time", 0.0)) - last_clear_time <= cooldown
            and event.get("confidence") != "verified"
            and not has_pickup
            and (not has_source_slot or cell_count <= 2)
            and cell_count <= 3
        )
        if weak_fragment:
            discarded.append({
                "stepIndex": event.get("stepIndex"),
                "time": event.get("time"),
                "sourceSlot": event.get("sourceSlot", -1),
                "cellCount": cell_count,
                "reason": "color_block_clear_cooldown_fragment",
            })
            continue
        filtered.append(event)
        if is_clear:
            last_clear_time = float(event.get("effectFrameTime") or event.get("time") or 0.0)
    for step_index, event in enumerate(filtered, 1):
        event["stepIndex"] = step_index
    return filtered, discarded


def _apply_fsm_event_constraints(events):
    """Reject low-evidence fragments that cannot close a source-slot lifecycle."""
    filtered = []
    discarded = []
    for event in events:
        reasons = set(event.get("candidateReasons") or [])
        source_slot = int(event.get("sourceSlot", -1))
        shape_size = int((event.get("group") or {}).get("cellCount") or len(event.get("placedCells") or []))
        low_evidence_unknown_slot = (
            source_slot < 0
            and event.get("confidence") != "verified"
            and shape_size <= 4
            and (
                "short_action_fragments_merged" in reasons
                or "pure_addition" in reasons
                or "bottom_slot_not_unique" in reasons
            )
        )
        if low_evidence_unknown_slot:
            discarded.append({
                "stepIndex": event.get("stepIndex"),
                "time": event.get("time"),
                "reason": "fsm_unknown_slot_fragment",
                "shapeSize": shape_size,
            })
            continue
        filtered.append(event)
    for step_index, event in enumerate(filtered, 1):
        event["stepIndex"] = step_index
    return filtered, discarded


def build_timeline(
    video_path: Path,
    board: tuple[int, int, int, int],
    rows: int = 10,
    cols: int = 10,
    sample_fps: float = 30.0,
    recognition_strategy: str = "legacy",
    experiment_flags=None,
):
    import cv2
    from recognition_experiments import parse_flags

    experiment_flags = parse_flags(experiment_flags)
    material_profile = infer_material_profile(video_path)
    material_profile_enabled = recognition_strategy == "material_profile_v1"
    color_profile_enabled = recognition_strategy == "color_block_v1" and material_profile == "color_block"
    repeated_board_evidence_enabled = material_profile == "image_block" or material_profile_enabled
    capture = cv2.VideoCapture(str(video_path))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    requested_fps = min(float(sample_fps), float(fps))
    frame_step = max(1, round(fps / requested_fps))
    actual_sample_fps = fps / frame_step
    repeated_detection_stride = max(1, round(actual_sample_fps / 10.0))
    samples = []
    frame_index = 0
    previous_sample_frame = None
    appearance_profile = None
    occupied_cell_validator = None
    occupancy_model = load_model() if color_profile_enabled else None
    validator_occupancy_model = None
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if frame_index % frame_step == 0:
            if not samples:
                appearance_profile = None
                appearance_profile = _build_board_appearance_profile(
                    frame, board, rows, cols
                )
                occupied_cell_validator = _build_occupied_cell_validator(
                    frame, board, rows, cols
                )
                if (
                    color_profile_enabled
                    and occupied_cell_validator
                    and not validator_occupancy_model
                ):
                    validator_occupancy_model = load_model()
            sample = {
                "frameIndex": frame_index,
                "time": round(frame_index / fps, 3),
                "board": _board_state(
                    frame, board, rows, cols, appearance_profile,
                    occupancy_model=occupancy_model,
                ),
                "groups": _bottom_groups(
                    frame,
                    board,
                    rows,
                    cols,
                    material_profile if material_profile_enabled else "unknown",
                ),
                "slotActivity": _bottom_slot_activity(frame, board),
                "gridAlignment": _grid_alignment_score(frame, board, rows, cols),
            }
            if occupied_cell_validator and not appearance_profile:
                sample["validatedBoard"] = _board_state(
                    frame, board, rows, cols,
                    occupied_cell_validator=occupied_cell_validator,
                    occupancy_model=validator_occupancy_model,
                )
            if repeated_board_evidence_enabled and len(samples) % repeated_detection_stride == 0:
                sample["repeatedBoardEvidence"] = _repeated_round_board_evidence(
                    frame, board, rows, cols
                )
            sample["boardMotion"] = (
                _board_motion(previous_sample_frame, frame, board, rows, cols)
                if previous_sample_frame is not None
                else [[0.0 for _ in range(cols)] for _ in range(rows)]
            )
            previous_sample_frame = frame.copy()
            samples.append(sample)
        frame_index += 1
    capture.release()

    repeated_board_diagnostics = _apply_repeated_board_consensus(
        samples,
        minimum_frames=3,
        maximum_gap=repeated_detection_stride,
    )
    # Detector evidence is temporary. Remove sets and other internals before
    # sampled frames can be copied into the serializable timeline payload.
    for sample in samples:
        sample.pop("repeatedBoardEvidence", None)
    cell_temporal_voting_diagnostics = None
    if color_profile_enabled and experiment_flags.is_enabled("cell_temporal_voting"):
        cell_temporal_voting_diagnostics = _apply_cell_temporal_voting(
            samples,
            rows,
            cols,
            radius=2,
            minimum_votes=3,
        )
    temporal_candidate_cache = (
        _sample_cache_summary(samples, rows, cols)
        if experiment_flags.is_enabled("temporal_candidate_cache")
        else None
    )

    # Collapse equal occupancy frames into runs and retain states visible for
    # at least 0.13 seconds. Shorter runs are placement/clear animation noise.
    runs, active = [], []
    for sample in samples:
        if not active or _occupancy(sample["board"]) == _occupancy(active[-1]["board"]):
            active.append(sample)
        else:
            runs.append(active)
            active = [sample]
    if active:
        runs.append(active)
    min_stable_frames = max(3, round(actual_sample_fps * 0.13))
    stable_states = []
    for run in runs:
        if len(run) < min_stable_frames:
            continue
        representative = run[len(run) // 2]
        stable_states.append({
            "stateIndex": len(stable_states),
            "startTime": run[0]["time"],
            "endTime": run[-1]["time"],
            "time": representative["time"],
            "frameIndex": representative["frameIndex"],
            "board": representative["board"],
            "validatedBoard": representative.get(
                "validatedBoard", representative["board"]
            ),
            "groups": _representative_groups(run),
            "groupCandidates": _group_candidates(run),
            "frameCount": len(run),
            "gridAlignment": representative.get("gridAlignment", 0.0),
        })

    gameplay_end_state = None
    timeline_end_time = samples[-1]["time"] if samples else 0.0
    for state_index, (before_state, after_state) in enumerate(zip(stable_states, stable_states[1:]), 1):
        added, removed = _difference(before_state["board"], after_state["board"])
        changed_ratio = (len(added) + len(removed)) / max(1, rows * cols)
        before_quality = before_state.get("gridAlignment", 0.0)
        after_quality = after_state.get("gridAlignment", 0.0)
        quality_collapsed = after_quality < 1.15 and after_quality < before_quality * 0.78
        future_qualities = [state.get("gridAlignment", 0.0) for state in stable_states[state_index:state_index + 4]]
        quality_stays_low = len(future_qualities) >= 2 and max(future_qualities) < 1.2
        in_video_tail = after_state["startTime"] >= timeline_end_time * 0.8
        if changed_ratio >= 0.55 and quality_collapsed and quality_stays_low and in_video_tail:
            gameplay_end_state = state_index
            break
    analysis_states = stable_states if gameplay_end_state is None else stable_states[:gameplay_end_state]

    events = []
    index = 0
    while index < len(analysis_states) - 1:
        if recognition_strategy == "reverse_clear_v1":
            while (
                index < len(analysis_states) - 1
                and _is_visual_cleanup_state(
                    analysis_states[index], analysis_states[index + 1], rows, cols
                )
            ):
                index += 1
        before = analysis_states[index]
        before_signature = _occupancy(before["board"])
        accepted = None
        reverse_accepted = None
        ambiguous_matches = []
        # Animation can insert several transient-but-stable states. Search a
        # short future window for the first state closed by one legal move.
        for next_index in range(index + 1, min(index + 6, len(analysis_states))):
            after = analysis_states[next_index]
            if after["startTime"] - before["endTime"] > 2.2:
                break
            matches = []
            for groups in before["groupCandidates"]:
                matches.extend(_find_move(before_signature, _occupancy(after["board"]), groups, rows, cols))
            unique = {}
            for match in matches:
                key = (
                    match["slot"],
                    tuple((cell["row"], cell["col"]) for cell in match["group"]["shape"]),
                    match["target"]["row"],
                    match["target"]["col"],
                    match["clearMode"],
                )
                unique[key] = match
            matches = list(unique.values())
            if len(matches) == 1:
                accepted = (next_index, after, matches[0])
                break
            if len(matches) > len(ambiguous_matches):
                ambiguous_matches = matches
            reverse_moves = []
            if recognition_strategy == "reverse_clear_v1":
                clear_effect = _clear_effect_evidence(
                    samples,
                    before["endTime"],
                    min(timeline_end_time, after["startTime"] + 0.2),
                    rows,
                    cols,
                )
                if clear_effect is not None:
                    reverse_moves = _reverse_clear_moves(
                        before_signature,
                        _occupancy(after["board"]),
                        rows,
                        cols,
                        effect_rows=clear_effect["rows"],
                        effect_cols=clear_effect["cols"],
                    )
            slot_matched_moves = []
            for reverse_move in reverse_moves:
                slot_matches = _slot_matches_for_shape(before, reverse_move["shape"])
                if slot_matches:
                    reverse_move = dict(reverse_move)
                    reverse_move["slotMatches"] = slot_matches
                    reverse_move["clearEffectEvidence"] = clear_effect
                    slot_matched_moves.append(reverse_move)
            # Board inversion alone can have several mathematically valid but
            # visually false answers. Automatic recovery therefore requires
            # exactly one candidate corroborated by a visible source piece.
            if len(slot_matched_moves) == 1:
                reverse_accepted = (next_index, after, slot_matched_moves[0])
                break
        if accepted:
            next_index, after, move = accepted
            added, removed = _difference(before["board"], after["board"])
            events.append({
                "stepIndex": len(events) + 1,
                "sourceStateIndex": before["stateIndex"],
                "targetStateIndex": after["stateIndex"],
                "time": after["startTime"],
                "frameIndex": after["frameIndex"],
                "type": "placement",
                "sourceSlot": move["slot"],
                "group": move["group"],
                "target": move["target"],
                "placedCells": move["placedCells"],
                "clearedRows": move["clearedRows"],
                "clearedCols": move["clearedCols"],
                "clearMode": move["clearMode"],
                "addedCells": added,
                "removedCells": removed,
                "beforeBoard": before["board"],
                "afterBoard": after["board"],
                "confidence": "verified",
                "verification": "exact_rule_simulation",
            })
            index = next_index
        elif reverse_accepted:
            next_index, after, move = reverse_accepted
            slot_matches = move.get("slotMatches") or {}
            if len(slot_matches) == 1:
                source_slot, recognized_group = next(iter(slot_matches.items()))
            else:
                source_slot, recognized_group = _slot_for_inferred_shape(before, after, move["shape"])
            inferred_signature = tuple((cell["row"], cell["col"]) for cell in move["shape"])
            recognized_signature = () if recognized_group is None else tuple(
                sorted((cell["row"], cell["col"]) for cell in recognized_group["shape"])
            )
            shape_matched = recognized_group is not None and recognized_signature == inferred_signature
            if shape_matched:
                shape = [dict(cell) for cell in recognized_group["shape"]]
            else:
                survivor_hues = [
                    after["board"][cell["row"]][cell["col"]]
                    for cell in move["placedCells"]
                    if after["board"][cell["row"]][cell["col"]] is not None
                ]
                hue = Counter(survivor_hues).most_common(1)[0][0] if survivor_hues else 0
                shape = [{**cell, "hue": hue} for cell in move["shape"]]
            group = {
                "slot": source_slot,
                "shape": shape,
                "rows": max(cell["row"] for cell in shape) + 1,
                "cols": max(cell["col"] for cell in shape) + 1,
                "cellCount": len(shape),
                "center": None,
            }
            added, removed = _difference(before["board"], after["board"])
            events.append({
                "stepIndex": len(events) + 1,
                "sourceStateIndex": before["stateIndex"],
                "targetStateIndex": after["stateIndex"],
                "time": after["startTime"],
                "frameIndex": after["frameIndex"],
                "type": "placement" if source_slot >= 0 else "placement_candidate",
                "sourceSlot": source_slot,
                "group": group,
                "target": move["target"],
                "placedCells": move["placedCells"],
                "clearedRows": move["clearedRows"],
                "clearedCols": move["clearedCols"],
                "clearMode": "immediate",
                "addedCells": added,
                "removedCells": removed,
                "beforeBoard": before["board"],
                "afterBoard": after["board"],
                "confidence": "verified" if shape_matched else "candidate",
                "verification": "reverse_clear_exact_simulation",
                "clearEffectEvidence": move.get("clearEffectEvidence"),
                "effectFrameTime": (move.get("clearEffectEvidence") or {}).get("time"),
                "candidateReasons": [
                    "shape_and_target_recovered_from_cleared_lines",
                    "cleared_lines_observed_in_effect_frame",
                    "slot_shape_matched" if shape_matched else "source_slot_requires_confirmation",
                ],
                "candidateSolutions": [],
            })
            index = next_index
        else:
            # Preserve a board-delta action candidate even when bottom-slot
            # recognition cannot provide a unique legal solution. This keeps
            # monochrome, occluded and fast moves available for manual review
            # instead of silently collapsing the whole video to zero steps.
            after = analysis_states[index + 1]
            added, removed = _difference(before["board"], after["board"])
            if added and len(added) <= 25 and not (
                not any(before_signature) and len(added) > rows * cols * 0.35
            ):
                min_row = min(cell["row"] for cell in added)
                min_col = min(cell["col"] for cell in added)
                shape = [
                    {
                        "row": cell["row"] - min_row,
                        "col": cell["col"] - min_col,
                        "hue": cell["hue"],
                    }
                    for cell in added
                ]
                shape_signature = tuple(sorted((cell["row"], cell["col"]) for cell in shape))
                slot_matches = set()
                for group_set in before.get("groupCandidates", []):
                    for slot, group in enumerate(group_set):
                        if group is None:
                            continue
                        candidate_signature = tuple(sorted((cell["row"], cell["col"]) for cell in group["shape"]))
                        if candidate_signature == shape_signature:
                            slot_matches.add(slot)
                source_slot = next(iter(slot_matches)) if len(slot_matches) == 1 else -1
                if source_slot < 0:
                    changed_slots = []
                    before_groups = before.get("groups") or [None, None, None]
                    after_groups = after.get("groups") or [None, None, None]
                    for slot in range(min(3, len(before_groups), len(after_groups))):
                        before_group = before_groups[slot]
                        after_group = after_groups[slot]
                        before_group_signature = None if before_group is None else tuple(
                            sorted((cell["row"], cell["col"]) for cell in before_group["shape"])
                        )
                        after_group_signature = None if after_group is None else tuple(
                            sorted((cell["row"], cell["col"]) for cell in after_group["shape"])
                        )
                        if before_group is not None and before_group_signature != after_group_signature:
                            changed_slots.append(slot)
                    if len(changed_slots) == 1:
                        source_slot = changed_slots[0]
                combined_signature = list(before_signature)
                for cell in added:
                    combined_signature[cell["row"] * cols + cell["col"]] = True
                completed_rows = [
                    row for row in range(rows)
                    if all(combined_signature[row * cols + col] for col in range(cols))
                ]
                completed_cols = [
                    col for col in range(cols)
                    if all(combined_signature[row * cols + col] for row in range(rows))
                ]
                event = {
                    "stepIndex": len(events) + 1,
                    "sourceStateIndex": before["stateIndex"],
                    "targetStateIndex": after["stateIndex"],
                    "time": after["startTime"],
                    "frameIndex": after["frameIndex"],
                    "type": "placement_candidate",
                    "sourceSlot": source_slot,
                    "group": {
                        "slot": source_slot,
                        "shape": shape,
                        "rows": max(cell["row"] for cell in shape) + 1,
                        "cols": max(cell["col"] for cell in shape) + 1,
                        "cellCount": len(shape),
                        "center": None,
                    },
                    "target": {"row": min_row, "col": min_col},
                    "placedCells": [{"row": cell["row"], "col": cell["col"]} for cell in added],
                    "clearedRows": completed_rows,
                    "clearedCols": completed_cols,
                    "clearMode": "immediate" if removed and (completed_rows or completed_cols) else ("unknown" if removed else ("deferred" if completed_rows or completed_cols else "none")),
                    "addedCells": added,
                    "removedCells": removed,
                    "beforeBoard": before["board"],
                    "afterBoard": after["board"],
                    "confidence": "candidate",
                    "verification": "board_delta_requires_confirmation",
                    "candidateReasons": [
                        "bottom_slot_not_unique" if source_slot < 0 else "slot_shape_matched",
                        "simultaneous_removal" if removed else "pure_addition",
                    ],
                    "candidateSolutions": [
                        {
                            "sourceSlot": match["slot"],
                            "shape": match["group"]["shape"],
                            "target": match["target"],
                            "clearMode": match["clearMode"],
                            "clearedRows": match["clearedRows"],
                            "clearedCols": match["clearedCols"],
                        }
                        for match in ambiguous_matches
                    ],
                }
                events.append(event)
            index += 1

    slot_pickups = _detect_source_slot_pickups(samples, actual_sample_fps)
    pickup_visual_evidence = _pickup_visual_evidence(
        video_path,
        board,
        rows,
        cols,
        slot_pickups,
        adaptive_grid=bool(
            appearance_profile
            and appearance_profile.get("mode")
            == "adaptive_underfilled_bright_blocks"
        ),
        shape_after_offset=(
            10
            if appearance_profile
            and appearance_profile.get("mode")
            == "adaptive_underfilled_bright_blocks"
            else 5
        ),
    ) if slot_pickups else []
    raw_event_count = len(events)
    events, cancelled_drags = _filter_cancelled_drags(events, stable_states)
    after_cancelled_drag_count = len(events)
    events, discarded_tail_candidates = _filter_low_quality_tail_candidates(
        events, stable_states, timeline_end_time
    )
    after_tail_filter_count = len(events)
    before_drag_collapse_steps = [event["stepIndex"] for event in events]
    events = _collapse_drag_windows(events, stable_states, rows, cols)
    after_drag_collapse_count = len(events)
    before_fragment_coalesce_steps = [event["stepIndex"] for event in events]
    before_fragment_coalesce_events = [
        {
            "stepIndex": event["stepIndex"],
            "time": event["time"],
            "sourceStateIndex": event["sourceStateIndex"],
            "targetStateIndex": event["targetStateIndex"],
            "sourceSlot": event.get("sourceSlot", -1),
            "cellCount": (event.get("group") or {}).get("cellCount", 0),
            "target": event.get("target"),
            "confidence": event.get("confidence"),
            "verification": event.get("verification"),
        }
        for event in events
    ]
    if recognition_strategy in {"legacy", "color_block_v1"}:
        events = _coalesce_action_fragments(events, stable_states, rows, cols)
    after_fragment_coalesce_count = len(events)
    fsm_discarded_events = []
    if color_profile_enabled and experiment_flags.is_enabled("fsm_event_constraints"):
        events, fsm_discarded_events = _apply_fsm_event_constraints(events)

    states_by_index = {state["stateIndex"]: state for state in stable_states}
    if recognition_strategy == "color_block_v1":
        for event in events:
            if event.get("sourceSlot", -1) >= 0:
                continue
            before_state = states_by_index.get(event.get("sourceStateIndex"))
            after_state = states_by_index.get(event.get("targetStateIndex"))
            if not before_state or not after_state:
                continue
            slot, group = _slot_for_inferred_shape(
                before_state,
                after_state,
                (event.get("group") or {}).get("shape") or [],
            )
            if slot < 0 or not group:
                continue
            event["sourceSlot"] = slot
            event["group"]["slot"] = slot
            event.setdefault("candidateReasons", []).append(
                "source_slot_from_pre_action_shape"
            )

    # Source-slot evidence must be attached after fragment coalescing. Adding
    # it earlier makes two fragments of one drag look like different actions.
    used_pickups = set()
    def shape_signature(shape):
        return tuple(sorted((cell["row"], cell["col"]) for cell in (shape or [])))

    def board_delta_shape(event):
        before_board = event.get("beforeBoard") or []
        after_board = event.get("afterBoard") or []
        if not before_board or not after_board:
            return None
        added, removed = _difference(before_board, after_board)
        if removed or not added or not _connected_cells({(cell["row"], cell["col"]) for cell in added}):
            return None
        min_row = min(cell["row"] for cell in added)
        min_col = min(cell["col"] for cell in added)
        shape = [
            {
                "row": cell["row"] - min_row,
                "col": cell["col"] - min_col,
                "hue": cell.get("hue", 0),
            }
            for cell in added
        ]
        return {
            "shape": shape,
            "target": {"row": min_row, "col": min_col},
            "cellCount": len(shape),
        }

    def score_pickup_for_event(event, pickup, visual):
        event_time = float(event.get("time", 0.0) or 0.0)
        pickup_time = float(pickup.get("time", 0.0) or 0.0)
        dt = abs(event_time - pickup_time)
        if dt > 1.25:
            return -1.0
        score = max(0.0, 0.75 - dt / 1.25)
        score += min(float(pickup.get("score", 0.0) or 0.0) * 18.0, 0.45)
        event_shape = shape_signature((event.get("group") or {}).get("shape"))
        source_shape = ((visual or {}).get("shape") or {}).get("shape")
        source_signature = shape_signature(source_shape)
        if event_shape and source_signature:
            if event_shape == source_signature:
                score += 3.2
            elif len(event_shape) == len(source_signature):
                score += 0.9
            else:
                score -= 0.75
        target = event.get("target")
        visual_target = ((visual or {}).get("stableTarget") or (visual or {}).get("target") or {}).get("target")
        if target and visual_target:
            if target == visual_target:
                score += 0.8
            else:
                score -= 0.25
        if pickup.get("sourceSlot") == event.get("sourceSlot"):
            score += 0.35
        consumption = _pickup_remains_consumed(samples, pickup, minimum_duration=0.45)
        if consumption and consumption.get("consumed"):
            score += 0.65
        elif consumption:
            score -= 0.3
        return score

    for event in events:
        pickup_window = 1.25 if recognition_strategy == "color_block_v1" else (0.95 if appearance_profile else 0.55)
        nearby = [
            (pickup_index, pickup)
            for pickup_index, pickup in enumerate(slot_pickups)
            if recognition_strategy == "color_block_v1" or pickup_index not in used_pickups
            if -0.20 <= event["time"] - pickup["time"] <= pickup_window
        ]
        if not nearby:
            continue
        if recognition_strategy == "color_block_v1":
            raw_scored = [
                (
                    float(pickup.get("score", 0.0) or 0.0)
                    + (
                        0.004
                        if 0.0 <= event["time"] - pickup["time"] <= 1.25
                        else -0.004
                    ),
                    pickup_index,
                    pickup,
                )
                for pickup_index, pickup in nearby
            ]
            raw_score, raw_pickup_index, raw_pickup = max(
                raw_scored, key=lambda item: item[0]
            )
            visual_scored = [
                (
                    score_pickup_for_event(
                        event,
                        pickup,
                        pickup_visual_evidence[pickup_index]
                        if pickup_index < len(pickup_visual_evidence)
                        else None,
                    ),
                    pickup_index,
                    pickup,
                )
                for pickup_index, pickup in nearby
            ]
            score, pickup_index, pickup = max(visual_scored, key=lambda item: item[0])
            if raw_score >= 0.014 and (
                event.get("sourceSlot", -1) < 0
                or raw_score >= float(pickup.get("score", 0.0) or 0.0) + 0.004
            ):
                score, pickup_index, pickup = raw_score, raw_pickup_index, raw_pickup
            if event.get("sourceSlot", -1) < 0 and score < 0.014:
                continue
            if event.get("sourceSlot", -1) >= 0 and score < 1.1:
                continue
        else:
            pickup_index, pickup = min(nearby, key=lambda item: abs(event["time"] - item[1]["time"]))
        if event.get("sourceSlot", -1) < 0 or (
            recognition_strategy == "color_block_v1"
            and event.get("sourceSlot") != pickup["sourceSlot"]
            and score_pickup_for_event(
                event,
                pickup,
                pickup_visual_evidence[pickup_index]
                if pickup_index < len(pickup_visual_evidence)
                else None,
            ) >= 2.4
        ):
            event["sourceSlot"] = pickup["sourceSlot"]
            event["group"]["slot"] = pickup["sourceSlot"]
            event.setdefault("candidateReasons", []).append("source_slot_from_pickup_activity")
        event["sourcePickupEvidence"] = pickup
        visual = pickup_visual_evidence[pickup_index]
        source_shape = (visual or {}).get("shape") or {}
        source_recognition = source_shape.get("recognition")
        delta_shape = board_delta_shape(event)
        source_cells = source_shape.get("shape") or []
        source_has_extra_edge_cells = (
            delta_shape
            and source_cells
            and event.get("clearMode") in {None, "none", "off"}
            and len(source_cells) > delta_shape["cellCount"]
            and shape_signature(source_cells) != shape_signature(delta_shape["shape"])
        )
        if source_has_extra_edge_cells:
            shape = delta_shape["shape"]
            event["target"] = delta_shape["target"]
            event["group"].update({
                "shape": shape,
                "rows": max(cell["row"] for cell in shape) + 1,
                "cols": max(cell["col"] for cell in shape) + 1,
                "cellCount": len(shape),
                "canonicalShapeSource": "board_delta_added_cells",
            })
            event["shapeConsistency"] = {
                "canonicalSource": "board_delta_added_cells",
                "recognition": source_recognition,
                "sourceCellCount": len(source_cells),
                "cellCount": len(shape),
                "appliesTo": ["placement", "replay"],
            }
            event.setdefault("candidateReasons", []).append(
                "source_slot_edge_cells_rejected_by_board_delta"
            )
        strong_repeated_shape = (
            source_recognition == "source_repeated_round_elements"
            and source_shape.get("score", 0.0) >= 0.82
        )
        strong_material_consensus = (
            source_recognition == "source_slot_material_consensus"
            and source_shape.get("score", 0.0) >= 0.55
            and source_shape.get("consensusSupport", 0) >= 3
        )
        if not source_has_extra_edge_cells and (strong_repeated_shape or strong_material_consensus):
            shape = source_shape["shape"]
            old_signature = tuple(
                (cell["row"], cell["col"])
                for cell in event["group"].get("shape", [])
            )
            new_signature = tuple(
                (cell["row"], cell["col"])
                for cell in shape
            )
            event["group"].update({
                "shape": shape,
                "rows": max(cell["row"] for cell in shape) + 1,
                "cols": max(cell["col"] for cell in shape) + 1,
                "cellCount": len(shape),
                "recognition": source_recognition,
                "recognitionScore": source_shape["score"],
                "canonicalShapeSource": "source_slot_before_pickup",
            })
            event["shapeConsistency"] = {
                "canonicalSource": "source_slot_before_pickup",
                "recognition": source_recognition,
                "score": source_shape["score"],
                "cellCount": len(shape),
                "appliesTo": ["drag", "placement", "clearSimulation", "replay"],
            }
            event["pickupVisualEvidence"] = visual
            reasons = event.setdefault("candidateReasons", [])
            if strong_repeated_shape:
                stable_target = visual.get("stableTarget") or visual.get("target")
                if stable_target:
                    event["target"] = stable_target["target"]
                if "shape_from_repeated_source_elements" not in reasons:
                    reasons.append("shape_from_repeated_source_elements")
                if visual.get("target") and "target_from_shape_aligned_motion" not in reasons:
                    reasons.append("target_from_shape_aligned_motion")
            else:
                if "shape_from_material_agnostic_source_consensus" not in reasons:
                    reasons.append("shape_from_material_agnostic_source_consensus")
                if old_signature != new_signature:
                    event["confidence"] = "candidate"
                    event["verification"] = (
                        "source_slot_material_consensus_requires_confirmation"
                    )
        if appearance_profile:
            event["time"] = round(float(pickup["time"]) + 0.25, 3)
            event["frameIndex"] = int(pickup["frameIndex"] + round(actual_sample_fps * 0.25))
            event.setdefault("candidateReasons", []).append(
                "time_aligned_to_source_pickup"
            )
        used_pickups.add(pickup_index)

    pickup_backed_candidates = []
    pickup_unbacked_events = []
    first_event_time = min((event.get("time", 0.0) for event in events), default=None)
    for pickup_index, (pickup, evidence) in enumerate(zip(slot_pickups, pickup_visual_evidence)):
        if pickup_index in used_pickups or not evidence:
            continue
        if pickup.get("score", 0.0) < 0.02:
            continue
        if first_event_time is not None and pickup.get("time", 0.0) >= first_event_time:
            continue
        shape_evidence = evidence.get("shape") or {}
        target_evidence = evidence.get("target") or {}
        shape = shape_evidence.get("shape")
        target = target_evidence.get("target")
        if not shape or not target:
            continue
        if shape_evidence.get("score", 0.0) < 0.55 or target_evidence.get("score", 0.0) < 0.55:
            continue
        clear_transition = _early_clear_transition(
            stable_states,
            float(pickup.get("time", 0.0)),
            first_event_time,
        )
        if clear_transition is None:
            continue
        event_time = round(float(pickup["time"]) + 0.25, 3)
        group = {
            "slot": pickup["sourceSlot"],
            "shape": shape,
            "rows": max(cell["row"] for cell in shape) + 1,
            "cols": max(cell["col"] for cell in shape) + 1,
            "cellCount": len(shape),
            "recognition": shape_evidence.get("recognition"),
            "recognitionScore": shape_evidence.get("score"),
        }
        event = {
            "stepIndex": 0,
            "time": event_time,
            "frameIndex": int(target_evidence.get("frameIndex", pickup["frameIndex"])),
            "sourceStateIndex": clear_transition["before"]["stateIndex"],
            "targetStateIndex": clear_transition["after"]["stateIndex"],
            "sourceSlot": pickup["sourceSlot"],
            "group": group,
            "target": target,
            "clearedRows": [],
            "clearedCols": [],
            "clearMode": "immediate",
            "addedCells": [],
            "removedCells": clear_transition["removed"],
            "beforeBoard": clear_transition["before"]["board"],
            "afterBoard": clear_transition["after"]["board"],
            "confidence": "candidate",
            "verification": "opening_clear_pickup_requires_confirmation",
            "candidateReasons": [
                "opening_source_slot_pickup",
                "opening_clear_effect_transition",
                "source_tray_temporal_shape",
                "shape_aligned_board_motion_target",
            ],
            "candidateSolutions": [],
            "sourcePickupEvidence": pickup,
            "pickupVisualEvidence": evidence,
            "placedEvidenceTime": round(
                int(target_evidence.get("frameIndex", pickup["frameIndex"])) / fps, 3
            ),
            "clearEffectEvidence": {
                "time": clear_transition["time"],
                "frameIndex": int(round(clear_transition["time"] * fps)),
                "rows": [],
                "cols": [],
                "removedCellCount": len(clear_transition["removed"]),
                "verification": "opening_clear_effect_transition",
            },
            "effectFrameTime": clear_transition["time"],
        }
        events.append(event)
        pickup_backed_candidates.append(event)
        used_pickups.add(pickup_index)
    if pickup_backed_candidates:
        events.sort(key=lambda event: (event["time"], event.get("frameIndex", 0)))
        for step_index, event in enumerate(events, 1):
            event["stepIndex"] = step_index

    coverage_gap = (
        (
            recognition_strategy != "color_block_v1"
            and _has_pickup_coverage_gap(len(events), len(slot_pickups))
        )
        or bool(
            appearance_profile
            and appearance_profile.get("mode")
            == "adaptive_underfilled_bright_blocks"
        )
    )
    if coverage_gap:
        pickup_unbacked_events = [
            event for event in events if not event.get("sourcePickupEvidence")
        ]
        events = [event for event in events if event.get("sourcePickupEvidence")]
        for pickup_index, (pickup, evidence) in enumerate(zip(slot_pickups, pickup_visual_evidence)):
            if pickup_index in used_pickups or not evidence or not evidence.get("target"):
                continue
            event_time = round(float(pickup["time"]) + 0.25, 3)
            before_state = max(
                (state for state in stable_states if state["startTime"] <= event_time),
                key=lambda state: state["startTime"],
                default=stable_states[0],
            )
            after_state = min(
                (state for state in stable_states if state["startTime"] >= event_time),
                key=lambda state: state["startTime"],
                default=before_state,
            )
            next_pickup_time = (
                slot_pickups[pickup_index + 1]["time"]
                if pickup_index + 1 < len(slot_pickups)
                else min(timeline_end_time, event_time + 1.0)
            )
            clear_effect = _clear_effect_evidence(
                samples,
                event_time,
                min(event_time + 0.9, next_pickup_time + 0.05),
                rows,
                cols,
            )
            shape = evidence["shape"]["shape"]
            target_evidence = evidence["target"]
            target = target_evidence["target"]
            group = {
                "slot": pickup["sourceSlot"],
                "shape": shape,
                "rows": max(cell["row"] for cell in shape) + 1,
                "cols": max(cell["col"] for cell in shape) + 1,
                "cellCount": len(shape),
                "recognition": evidence["shape"]["recognition"],
            }
            event = {
                "stepIndex": 0,
                "time": event_time,
                "frameIndex": target_evidence["frameIndex"],
                "sourceStateIndex": before_state["stateIndex"],
                "targetStateIndex": after_state["stateIndex"],
                "sourceSlot": pickup["sourceSlot"],
                "group": group,
                "target": target,
                "clearedRows": clear_effect["rows"] if clear_effect else [],
                "clearedCols": clear_effect["cols"] if clear_effect else [],
                "clearMode": "immediate" if clear_effect else "none",
                "addedCells": [],
                "removedCells": [],
                "beforeBoard": before_state["board"],
                "afterBoard": after_state["board"],
                "confidence": "candidate",
                "verification": "pickup_without_stable_transition_requires_confirmation",
                "candidateReasons": [
                    "independent_source_slot_pickup",
                    "source_tray_temporal_shape",
                    "shape_aligned_board_motion_target",
                ],
                "candidateSolutions": [],
                "sourcePickupEvidence": pickup,
                "pickupVisualEvidence": evidence,
                "placedEvidenceTime": round(
                    target_evidence["frameIndex"] / fps, 3
                ),
            }
            if clear_effect:
                event["clearEffectEvidence"] = clear_effect
                event["effectFrameTime"] = clear_effect["time"]
            events.append(event)
            pickup_backed_candidates.append(event)
            used_pickups.add(pickup_index)
        events.sort(key=lambda event: (event["time"], event.get("frameIndex", 0)))
        for step_index, event in enumerate(events, 1):
            event["stepIndex"] = step_index

    if recognition_strategy == "reverse_clear_v1":
        for event_index, event in enumerate(events):
            if not event.get("clearedRows") and not event.get("clearedCols"):
                continue
            next_time = (
                events[event_index + 1]["time"] - 0.05
                if event_index + 1 < len(events)
                else timeline_end_time
            )
            effect = _clear_effect_evidence(
                samples,
                max(0.0, event["time"] - 0.2),
                min(next_time, event["time"] + 1.2),
                rows,
                cols,
            )
            if effect is None:
                continue
            if set(effect["rows"]) != set(event.get("clearedRows", [])):
                continue
            if set(effect["cols"]) != set(event.get("clearedCols", [])):
                continue
            event["clearEffectEvidence"] = effect
            event["effectFrameTime"] = effect["time"]
            reasons = event.setdefault("candidateReasons", [])
            if "cleared_lines_observed_in_effect_frame" not in reasons:
                reasons.append("cleared_lines_observed_in_effect_frame")

    events = _apply_rule_verified_clear_detection(
        events,
        samples,
        stable_states,
        rows,
        cols,
        timeline_end_time,
        conservative_unverified_clear=recognition_strategy == "color_block_v1",
    )

    if not coverage_gap:
        # Clear verification must run first: the celebration can obscure every
        # stable board delta while the next tray is already playable.
        for pickup_index, (pickup, evidence) in enumerate(zip(
            slot_pickups, pickup_visual_evidence
        )):
            if pickup_index in used_pickups or not evidence:
                continue
            shape_evidence = evidence.get("shape") or {}
            target_evidence = evidence.get("target") or {}
            large_clear = _post_large_clear_predecessor(
                events, pickup["time"], rows, cols
            )
            if large_clear is None:
                continue
            if (
                shape_evidence.get("recognition")
                != "source_repeated_round_elements"
                or shape_evidence.get("score", 0.0) < 0.90
                or target_evidence.get("score", 0.0) < 0.72
            ):
                continue
            event_time = round(float(pickup["time"]) + 0.25, 3)
            before_state = max(
                (state for state in stable_states if state["startTime"] <= event_time),
                key=lambda state: state["startTime"],
                default=stable_states[0],
            )
            after_state = min(
                (state for state in stable_states if state["startTime"] >= event_time),
                key=lambda state: state["startTime"],
                default=before_state,
            )
            shape = shape_evidence["shape"]
            consumption = _pickup_remains_consumed(samples, pickup)
            if not consumption or not consumption["consumed"]:
                continue
            event = {
                "stepIndex": 0,
                "time": event_time,
                "frameIndex": target_evidence["frameIndex"],
                "sourceStateIndex": before_state["stateIndex"],
                "targetStateIndex": after_state["stateIndex"],
                "sourceSlot": pickup["sourceSlot"],
                "group": {
                    "slot": pickup["sourceSlot"],
                    "shape": shape,
                    "rows": max(cell["row"] for cell in shape) + 1,
                    "cols": max(cell["col"] for cell in shape) + 1,
                    "cellCount": len(shape),
                    "recognition": shape_evidence["recognition"],
                    "recognitionScore": shape_evidence["score"],
                },
                "target": target_evidence["target"],
                "clearedRows": [],
                "clearedCols": [],
                "clearMode": "unknown",
                "addedCells": [],
                "removedCells": [],
                "beforeBoard": before_state["board"],
                "afterBoard": after_state["board"],
                "confidence": "candidate",
                "verification": "post_clear_animation_pickup_requires_confirmation",
                "candidateReasons": [
                    "pickup_during_large_clear_animation",
                    "shape_from_repeated_source_elements",
                    "target_from_shape_aligned_motion",
                    f"follows_large_clear_step_{large_clear['stepIndex']}",
                ],
                "candidateSolutions": [],
                "sourcePickupEvidence": pickup,
                "sourceConsumptionEvidence": consumption,
                "pickupVisualEvidence": evidence,
                "placedEvidenceTime": round(
                    target_evidence["frameIndex"] / fps, 3
                ),
            }
            events.append(event)
            pickup_backed_candidates.append(event)
            used_pickups.add(pickup_index)
        events.sort(key=lambda event: (event["time"], event.get("frameIndex", 0)))
        for step_index, event in enumerate(events, 1):
            event["stepIndex"] = step_index

    for event in events:
        if event.get("verification") != "post_clear_animation_pickup_requires_confirmation":
            continue
        large_clear = _post_large_clear_predecessor(
            events, event["time"], rows, cols
        )
        if (
            large_clear
            and large_clear.get("clearVerification")
            in {
                "rule_effect_and_disappearance_verified",
                "stable_board_cleared_lines_absent",
            }
            and (event.get("sourceConsumptionEvidence") or {}).get("consumed")
        ):
            event["confidence"] = "verified"
            event["verification"] = "post_clear_animation_pickup_verified"
            event["clearMode"] = "none"
            event.setdefault("candidateReasons", []).append(
                "source_slot_remained_consumed"
            )

    color_cooldown_fragments = []
    if color_profile_enabled:
        events, color_cooldown_fragments = _filter_color_clear_cooldown_fragments(events)

    # Keep action segmentation on the established board recognizer, then use
    # the learned per-video cell appearance only to remove transient score or
    # praise text from boards exposed to review and replay.
    output_states = {state["stateIndex"]: state for state in stable_states}
    for state in stable_states:
        state["board"] = state.pop("validatedBoard", state["board"])
    for event in events:
        source_state = output_states.get(event.get("sourceStateIndex"))
        target_state = output_states.get(event.get("targetStateIndex"))
        if source_state:
            event["beforeBoard"] = source_state["board"]
        if target_state:
            event["afterBoard"] = target_state["board"]
    for sample in samples:
        sample.pop("validatedBoard", None)

    replay_steps = [
        {
            "stepIndex": event["stepIndex"],
            "sourceStateIndex": event["sourceStateIndex"],
            "targetStateIndex": event["targetStateIndex"],
            "time": event["time"],
            "sourceSlot": event["sourceSlot"],
            "target": event["target"],
            "shape": event["group"]["shape"],
            "cellCount": event["group"]["cellCount"],
            "clearedRows": event["clearedRows"],
            "clearedCols": event["clearedCols"],
            "clearMode": event["clearMode"],
            "confidence": event["confidence"],
        }
        for event in events
    ]
    covered_edges = {}
    for event in events:
        for edge in range(event["sourceStateIndex"], event["targetStateIndex"]):
            covered_edges[edge] = event["stepIndex"]
    clear_target_states = {
        event["targetStateIndex"]
        for event in events
        if event.get("clearedRows") or event.get("clearedCols")
    }
    transitions = []
    unresolved = 0
    for edge, (before, after) in enumerate(zip(analysis_states, analysis_states[1:])):
        placement_step = covered_edges.get(edge)
        kind, added, removed = _classify_stable_transition(
            before["board"],
            after["board"],
            placement_step,
            before["stateIndex"] in clear_target_states,
        )
        if kind == "unresolved":
            unresolved += 1
        transitions.append({
            "fromState": before["stateIndex"],
            "toState": after["stateIndex"],
            "time": after["startTime"],
            "type": kind,
            "placementStep": placement_step,
            "addedCells": added,
            "removedCells": removed,
        })
    validation = {
        "allMovesRuleVerified": bool(events) and all(event["confidence"] == "verified" for event in events),
        "verifiedMoveCount": sum(event["confidence"] == "verified" for event in events),
        "candidateMoveCount": sum(event["confidence"] != "verified" for event in events),
        "unresolvedStableTransitions": unresolved,
        "readyForAutomaticReplay": False,
        "reason": "manual_ground_truth_comparison_required",
        "gameplayEndState": gameplay_end_state,
        "gameplayEndTime": stable_states[gameplay_end_state]["startTime"] if gameplay_end_state is not None else None,
    }
    return {
        "recognitionStrategy": recognition_strategy,
        "recognitionVersion": "legacy_soft_occupied_recovery_v21" if recognition_strategy == "legacy" else recognition_strategy,
        "experimentFlags": experiment_flags.as_metadata(),
        "sampleRate": round(actual_sample_fps, 3),
        "sourceFrameRate": round(fps, 3),
        "grid": {"rows": rows, "cols": cols},
        "sampleCount": len(samples),
        "stableStates": stable_states,
        "transitions": transitions,
        "events": events,
        "cancelledDrags": cancelled_drags,
        "processingDiagnostics": {
            "materialProfile": material_profile,
            "materialProfileEnabled": material_profile_enabled,
            "colorProfileEnabled": color_profile_enabled,
            "boardAppearanceProfile": appearance_profile,
            "occupiedCellValidator": occupied_cell_validator,
            "repeatedBoardConsensus": repeated_board_diagnostics,
            "rawEventCount": raw_event_count,
            "afterCancelledDragCount": after_cancelled_drag_count,
            "afterTailFilterCount": after_tail_filter_count,
            "afterDragCollapseCount": after_drag_collapse_count,
            "afterFragmentCoalesceCount": after_fragment_coalesce_count,
            "fsmEventConstraintDiscardCount": len(fsm_discarded_events),
            "fsmEventConstraints": fsm_discarded_events,
            "colorCooldownFragmentCount": len(color_cooldown_fragments),
            "cellTemporalVoting": cell_temporal_voting_diagnostics,
            "temporalCandidateCache": temporal_candidate_cache,
            "cancelledSourceSteps": sorted({
                step
                for interaction in cancelled_drags
                for step in interaction.get("discardedSourceSteps", [])
            }),
            "discardedTailSourceSteps": [
                item.get("sourceStep") for item in discarded_tail_candidates
            ],
            "beforeDragCollapseSteps": before_drag_collapse_steps,
            "beforeFragmentCoalesceSteps": before_fragment_coalesce_steps,
            "beforeFragmentCoalesceEvents": before_fragment_coalesce_events,
            "sourceSlotPickupCount": len(slot_pickups),
            "sourceSlotPickups": slot_pickups,
            "matchedSourceSlotPickups": len(used_pickups),
            "pickupBackedCandidateCount": len(pickup_backed_candidates),
            "discardedPickupUnbackedEventCount": len(pickup_unbacked_events),
        },
        "referenceInteractions": cancelled_drags,
        "discardedTailCandidates": discarded_tail_candidates,
        "colorCooldownFragments": color_cooldown_fragments,
        "replayCandidates": replay_steps,
        "validation": validation,
        "samples": samples,
    }
