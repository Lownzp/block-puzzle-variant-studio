from __future__ import annotations


from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json
from pathlib import Path

import cv2

from cell_occupancy_model import extract_cell_features, save_model, train_gaussian_model
from timeline_analyzer import _board_cell_crops, infer_material_profile


ROOT = Path(__file__).resolve().parent
TASK_ROOT = ROOT / "视频重建任务"
MAX_TASKS = 16


def iter_confirmed_tasks():
    for task_dir in TASK_ROOT.iterdir():
        if not task_dir.is_dir():
            continue
        truth_path = task_dir / "确定性回放真值.json"
        draft_path = task_dir / "识别草稿.json"
        if truth_path.is_file() and draft_path.is_file():
            yield task_dir, truth_path, draft_path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def board_from_draft(draft: dict, truth: dict):
    board = draft.get("board") or {}
    grid = truth.get("grid") or {}
    required = ["x", "y", "width", "height"]
    if not all(key in board for key in required):
        return None
    rows = int(board.get("rows") or grid.get("rows") or 0)
    cols = int(board.get("cols") or grid.get("cols") or 0)
    if rows <= 0 or cols <= 0:
        return None
    return (
        (int(board["x"]), int(board["y"]), int(board["width"]), int(board["height"])),
        rows,
        cols,
    )


def capture_frame(capture, fps: float, second: float):
    frame_index = max(0, round(second * fps))
    capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    ok, frame = capture.read()
    return frame if ok else None


def add_frame_samples(features, labels, frame, board, rows, cols, state):
    if frame is None or not state:
        return 0
    cells = _board_cell_crops(frame, board, rows, cols)
    added = 0
    for row in range(min(rows, len(state))):
        for col in range(min(cols, len(state[row]))):
            hsv_cell, gray_cell = cells[row][col]
            features.append(extract_cell_features(hsv_cell, gray_cell))
            labels.append(1 if state[row][col] is not None else 0)
            added += 1
    return added


def sample_times_for_step(step: dict):
    ranges = step.get("timeRanges") or {}
    before = ranges.get("before") or {}
    placed = ranges.get("placed") or {}
    clear = ranges.get("clear") or {}
    result = []
    if step.get("expectedBoardBefore") is not None:
        end = float(before.get("end") or before.get("start") or max(0.0, float(step.get("executeAt", 0.0)) - 0.5))
        result.append((max(0.0, end), step["expectedBoardBefore"]))
    if step.get("expectedBoardAfterPlacement") is not None:
        start = placed.get("start")
        end = placed.get("end")
        if start is not None and end is not None:
            result.append(((float(start) + float(end)) / 2.0, step["expectedBoardAfterPlacement"]))
    if step.get("expectedBoardAfterResolution") is not None:
        start = clear.get("start")
        end = clear.get("end")
        if start is not None and end is not None:
            result.append(((float(start) + float(end)) / 2.0, step["expectedBoardAfterResolution"]))
    return result


def main():
    features = []
    labels = []
    task_summaries = []
    for task_dir, truth_path, draft_path in iter_confirmed_tasks():
        if len(task_summaries) >= MAX_TASKS:
            break
        truth = read_json(truth_path)
        draft = read_json(draft_path)
        video_path = Path(truth.get("sourceVideo") or draft.get("video") or task_dir / "source.mp4")
        if not video_path.is_file():
            continue
        material_profile = infer_material_profile(video_path)
        if material_profile == "image_block":
            continue
        board_info = board_from_draft(draft, truth)
        if board_info is None:
            continue
        board, rows, cols = board_info
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            continue
        fps = float(capture.get(cv2.CAP_PROP_FPS) or truth.get("sourceFrameRate") or 30.0)
        before_count = len(features)
        initial = truth.get("initialBoard")
        if initial:
            add_frame_samples(features, labels, capture_frame(capture, fps, 0.5), board, rows, cols, initial)
        for step in truth.get("steps", []):
            for second, state in sample_times_for_step(step)[:2]:
                add_frame_samples(features, labels, capture_frame(capture, fps, second), board, rows, cols, state)
        capture.release()
        added = len(features) - before_count
        if added:
            task_summaries.append({
                "task": task_dir.name,
                "materialProfile": material_profile,
                "samples": added,
            })
    model = train_gaussian_model(features, labels)
    model["training"] = {
        "taskCount": len(task_summaries),
        "sampleCount": len(labels),
        "occupiedCount": int(sum(labels)),
        "emptyCount": int(len(labels) - sum(labels)),
        "tasks": task_summaries,
    }
    save_model(model)
    print(json.dumps(model["training"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
