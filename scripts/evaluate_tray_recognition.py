"""Per-slot (component-level) evaluation for tray shape recognition.

Compares `_bottom_groups` output against hand-annotated `trayBefore` truth on
the exact frame each action was picked, so we can tell whether a shortfall is
"tray not detected" versus "tray split wrong" — something action-level metrics
cannot isolate. Baseline vs the tray_shape_grid_refine_v1 refinement, side by
side.

Truth source: `识别草稿.json` `reviewActions[i].trayBefore` (written verbatim by
the confirm-actions endpoint). Board/grid reused from the confirmed truth task,
matching scripts/reanalyze_truth_set.py.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import argparse
import json
import statistics

from truth_benchmark import latest_truth_tasks, initial_draft, load_json


# --- Pure helpers (unit tested) --------------------------------------------

def normalize_cells(cells):
    """Normalize a cell list to a frozenset anchored at the top-left origin.

    Accepts (row, col) pairs or {"row", "col", ...} dicts (hue ignored).
    Order- and translation-independent, so two shapes compare equal iff they
    describe the same piece regardless of where it sits.
    """
    points = []
    for cell in cells or []:
        if isinstance(cell, dict):
            points.append((int(cell["row"]), int(cell["col"])))
        else:
            points.append((int(cell[0]), int(cell[1])))
    if not points:
        return frozenset()
    min_row = min(row for row, _ in points)
    min_col = min(col for _, col in points)
    return frozenset((row - min_row, col - min_col) for row, col in points)


def compare_slot(truth_slot, predicted_group):
    """Classify one slot's outcome.

    Returns one of: detected_exact, detected_wrong, missed, false, correct_empty.
    """
    occupied = bool(truth_slot.get("occupied"))
    predicted = predicted_group is not None
    if occupied and predicted:
        same = normalize_cells(truth_slot.get("shape") or []) == normalize_cells(
            (predicted_group or {}).get("shape") or []
        )
        return "detected_exact" if same else "detected_wrong"
    if occupied and not predicted:
        return "missed"
    if not occupied and predicted:
        return "false"
    return "correct_empty"


def aggregate_outcomes(outcomes):
    """Roll per-slot outcomes into detection / shape / false rates."""
    occupied = [o for o in outcomes if o in ("detected_exact", "detected_wrong", "missed")]
    detected = [o for o in occupied if o.startswith("detected")]
    empty = [o for o in outcomes if o in ("false", "correct_empty")]
    return {
        "occupiedSlots": len(occupied),
        "detected": len(detected),
        "emptySlots": len(empty),
        "slotDetectionRate": round(len(detected) / max(1, len(occupied)), 4),
        "slotShapeExactRate": round(
            sum(o == "detected_exact" for o in detected) / max(1, len(detected)), 4
        ),
        "falseSlotRate": round(sum(o == "false" for o in empty) / max(1, len(empty)), 4),
    }


# --- Frame / task evaluation (needs cv2 + video) ---------------------------

def _board_rect(board):
    if isinstance(board, dict):
        return (board["x"], board["y"], board["w"], board["h"])
    return tuple(board)


def _grid_dims(truth_doc):
    grid = truth_doc.get("grid") or {}
    rows = int(grid.get("rows") or len(truth_doc.get("initialBoard") or []) or 10)
    cols = int(grid.get("cols") or len((truth_doc.get("initialBoard") or [[]])[0]) or 10)
    return rows, cols


def evaluate_task_tray(task: Path) -> dict | None:
    """Evaluate one confirmed truth task; None if it has no trayBefore truth."""
    import cv2  # local: keep pure helpers importable without cv2

    from timeline_analyzer import _bottom_groups

    dataset_id = next(part for part in task.name.split("_") if part.startswith("DEV-"))
    draft = load_json(task / "识别草稿.json")
    board = _board_rect(draft.get("board") or initial_draft(task)["board"])
    truth_doc = load_json(task / "确定性回放真值.json")
    rows, cols = _grid_dims(truth_doc)

    review_actions = draft.get("reviewActions") or []
    samples = [a for a in review_actions if a.get("trayBefore")]
    if not samples:
        return None

    video = task / "source.mp4"
    capture = cv2.VideoCapture(str(video))
    video_fps = capture.get(cv2.CAP_PROP_FPS) or 30.0

    baseline_outcomes: list[str] = []
    refine_outcomes: list[str] = []
    refine_scores: list[float] = []
    changed_slots = 0
    per_action = []

    for action in samples:
        tray = action["trayBefore"]
        time_s = tray.get("time")
        frame_index = (
            int(round(float(time_s) * video_fps))
            if time_s is not None
            else int(tray.get("frameIndex", 0))
        )
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = capture.read()
        if not ok:
            continue
        base_groups = _bottom_groups(frame, board, rows, cols, refine_grid=False)
        ref_groups = _bottom_groups(frame, board, rows, cols, refine_grid=True)
        truth_slots = {int(s["slot"]): s for s in tray.get("slots") or []}

        source_slot = int(action.get("sourceSlot", -1))
        action_base = action_ref = None
        for slot in range(3):
            truth_slot = truth_slots.get(slot, {"occupied": False, "shape": []})
            base_out = compare_slot(truth_slot, base_groups[slot] if slot < len(base_groups) else None)
            ref_out = compare_slot(truth_slot, ref_groups[slot] if slot < len(ref_groups) else None)
            baseline_outcomes.append(base_out)
            refine_outcomes.append(ref_out)
            if base_out != ref_out:
                changed_slots += 1
            ref_group = ref_groups[slot] if slot < len(ref_groups) else None
            if ref_group is not None and isinstance(ref_group.get("score"), (int, float)):
                refine_scores.append(float(ref_group["score"]))
            if slot == source_slot:
                action_base, action_ref = base_out, ref_out

        per_action.append({
            "time": action.get("time"),
            "frameIndex": frame_index,
            "sourceSlot": source_slot,
            "sourceSlotBaseline": action_base,
            "sourceSlotRefine": action_ref,
        })

    capture.release()

    return {
        "id": dataset_id,
        "task": task.name,
        "sampleCount": len(per_action),
        "baseline": aggregate_outcomes(baseline_outcomes),
        "refine": aggregate_outcomes(refine_outcomes),
        "refineChangedSlots": changed_slots,
        "refineScoreStats": _score_stats(refine_scores),
        "perAction": per_action,
    }


def _score_stats(scores):
    if not scores:
        return {"count": 0}
    return {
        "count": len(scores),
        "min": round(min(scores), 4),
        "median": round(statistics.median(scores), 4),
        "max": round(max(scores), 4),
    }


def build_tray_report(only: set[str]) -> dict:
    tasks = []
    skipped = []
    for task in latest_truth_tasks():
        dataset_id = next(part for part in task.name.split("_") if part.startswith("DEV-"))
        if only and dataset_id not in only:
            continue
        result = evaluate_task_tray(task)
        if result is None:
            skipped.append(dataset_id)
        else:
            tasks.append(result)
    return {"tasks": tasks, "skippedNoTrayTruth": skipped}


def markdown_report(report: dict) -> str:
    lines = [
        "# Tray Recognition (per-slot) Report",
        "",
        "| DEV | Samples | Detect (base→refine) | ShapeExact (base→refine) | False (base→refine) | RefineChangedSlots | Score min/med/max |",
        "|---|---:|---|---|---|---:|---|",
    ]
    for item in report["tasks"]:
        base, ref = item["baseline"], item["refine"]
        s = item["refineScoreStats"]
        score = f"{s['min']}/{s['median']}/{s['max']}" if s.get("count") else "—"
        lines.append(
            "| {id} | {n} | {bd:.2%}→{rd:.2%} | {bs:.2%}→{rs:.2%} | {bf:.2%}→{rf:.2%} | {chg} | {score} |".format(
                id=item["id"], n=item["sampleCount"],
                bd=base["slotDetectionRate"], rd=ref["slotDetectionRate"],
                bs=base["slotShapeExactRate"], rs=ref["slotShapeExactRate"],
                bf=base["falseSlotRate"], rf=ref["falseSlotRate"],
                chg=item["refineChangedSlots"], score=score,
            )
        )
    if report["skippedNoTrayTruth"]:
        lines += ["", "未标注 trayBefore（跳过）: " + ", ".join(report["skippedNoTrayTruth"])]
    lines += [
        "",
        "> 判定：slotDetectionRate 低 = 托盘没检出；detect 高但 slotShapeExactRate 低 = 检出但拆错。",
        "> 与动作级 missed 对照：看 perAction 里每个动作 sourceSlot 槽的 outcome（JSON 报告）。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path, help="输出目录")
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    report = build_tray_report(set(args.only))
    (args.output / "tray_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output / "tray_report.md").write_text(markdown_report(report), encoding="utf-8")
    print(f"tasks={len(report['tasks'])} skipped={report['skippedNoTrayTruth']}")
    print(args.output / "tray_report.md")


if __name__ == "__main__":
    main()
