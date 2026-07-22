"""Evaluate recognition drafts against confirmed deterministic replay truths."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
TASK_ROOT = ROOT / "视频重建任务"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def recognition_actions(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the solved recognizer output, falling back to raw events for old runs."""
    review_actions = document.get("reviewActions") or []
    raw_actions = document.get("actions") or []
    if not review_actions:
        return list(raw_actions)
    result = []
    for action in review_actions:
        merged = dict(action)
        source_index = action.get("sourceEventIndex")
        if isinstance(source_index, int) and 0 <= source_index < len(raw_actions):
            raw = raw_actions[source_index]
            merged.setdefault("time", raw.get("time"))
            merged.setdefault("frameIndex", raw.get("frameIndex"))
        result.append(merged)
    return result


def shape_signature(action: dict[str, Any], truth: bool = False) -> tuple[tuple[int, int], ...]:
    cells = action.get("shape") if truth else (action.get("shape") or action.get("group", {}).get("shape"))
    return tuple(sorted((int(cell["row"]), int(cell["col"])) for cell in (cells or [])))


def action_time(action: dict[str, Any], truth: bool = False) -> float:
    if truth:
        return float(action.get("executeAt") or action.get("timeRanges", {}).get("drag", {}).get("end") or 0.0)
    ranges = action.get("timeRanges") or {}
    return float(ranges.get("drag", {}).get("end") or action.get("time") or 0.0)


def clear_enabled(action: dict[str, Any], truth: bool = False) -> bool:
    if truth:
        return bool(action.get("clearDetection"))
    if action.get("clearState") is not None:
        return action.get("clearState") == "on"
    return action.get("clearMode") == "immediate"


def initial_draft(task: Path) -> dict[str, Any]:
    versions = sorted((task / "动作脚本版本").glob("*识别初稿.json"))
    if versions:
        version = load_json(versions[0])
        return version.get("payload", {}).get("analysis") or version.get("payload") or version
    return load_json(task / "识别草稿.json")


def latest_truth_tasks() -> list[Path]:
    latest: dict[str, Path] = {}
    for task in TASK_ROOT.iterdir():
        if not task.is_dir() or not (task / "确定性回放真值.json").exists():
            continue
        marker = next((part for part in task.name.split("_") if part.startswith("DEV-")), None)
        if marker and (marker not in latest or task.name > latest[marker].name):
            latest[marker] = task
    return [latest[key] for key in sorted(latest)]


def pair_cost(pred: dict[str, Any], truth: dict[str, Any]) -> float:
    dt = abs(action_time(pred) - action_time(truth, truth=True))
    target_a, target_b = pred.get("target") or {}, truth.get("target") or {}
    target_distance = abs(int(target_a.get("row", -20)) - int(target_b.get("row", 20))) + abs(
        int(target_a.get("col", -20)) - int(target_b.get("col", 20))
    )
    shape_penalty = 0.0 if shape_signature(pred) == shape_signature(truth, truth=True) else 0.8
    return dt + min(1.2, target_distance * 0.18) + shape_penalty


def align(predicted: list[dict[str, Any]], truth: list[dict[str, Any]]) -> list[tuple[int | None, int | None]]:
    """Monotonic sequence alignment; false candidates and missed actions are explicit."""
    n, m = len(predicted), len(truth)
    gap_pred, gap_truth = 0.72, 1.1
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    back: list[list[str | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0], back[i][0] = i * gap_pred, "pred"
    for j in range(1, m + 1):
        dp[0][j], back[0][j] = j * gap_truth, "truth"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            options = (
                (dp[i - 1][j - 1] + pair_cost(predicted[i - 1], truth[j - 1]), "pair"),
                (dp[i - 1][j] + gap_pred, "pred"),
                (dp[i][j - 1] + gap_truth, "truth"),
            )
            dp[i][j], back[i][j] = min(options, key=lambda item: item[0])
    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i or j:
        operation = back[i][j]
        if operation == "pair":
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1
        elif operation == "pred":
            pairs.append((i - 1, None)); i -= 1
        else:
            pairs.append((None, j - 1)); j -= 1
    return list(reversed(pairs))


def evaluate_task(task: Path, predicted_override: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    truth_doc = load_json(task / "确定性回放真值.json")
    draft_doc = initial_draft(task)
    truth = list(truth_doc.get("steps") or [])
    predicted = list(predicted_override if predicted_override is not None else (draft_doc.get("reviewActions") or []))
    pairs = align(predicted, truth)
    matched = [(predicted[i], truth[j]) for i, j in pairs if i is not None and j is not None]
    false_positive = sum(j is None for _, j in pairs)
    missed = sum(i is None for i, _ in pairs)
    def accuracy(check):
        return round(sum(check(a, b) for a, b in matched) / max(1, len(matched)), 4)
    time_errors = [abs(action_time(a) - action_time(b, truth=True)) for a, b in matched]
    return {
        "id": next(part for part in task.name.split("_") if part.startswith("DEV-")),
        "task": task.name,
        "predicted": len(predicted),
        "truth": len(truth),
        "matched": len(matched),
        "falsePositive": false_positive,
        "missed": missed,
        "precision": round(len(matched) / max(1, len(predicted)), 4),
        "recall": round(len(matched) / max(1, len(truth)), 4),
        "slotAccuracy": accuracy(lambda a, b: int(a.get("sourceSlot", -1)) == int(b.get("sourceSlot", -2))),
        "shapeAccuracy": accuracy(lambda a, b: shape_signature(a) == shape_signature(b, truth=True)),
        "targetAccuracy": accuracy(lambda a, b: a.get("target") == b.get("target")),
        "clearAccuracy": accuracy(lambda a, b: clear_enabled(a) == clear_enabled(b, truth=True)),
        "timeMae": round(sum(time_errors) / max(1, len(time_errors)), 4),
        "withinTwoFrames": round(sum(error <= 2 / float(truth_doc.get("sourceFrameRate") or 30) for error in time_errors) / max(1, len(time_errors)), 4),
    }


def build_report(prediction_dir: Path | None = None) -> dict[str, Any]:
    tasks = []
    for task in latest_truth_tasks():
        dataset_id = next(part for part in task.name.split("_") if part.startswith("DEV-"))
        override = None
        if prediction_dir and (prediction_dir / f"{dataset_id}.json").exists():
            override_doc = load_json(prediction_dir / f"{dataset_id}.json")
            # Raw timeline events are diagnostics; review actions are what the
            # annotator actually sees and confirms.
            override = recognition_actions(override_doc)
        tasks.append(evaluate_task(task, override))
    totals = {key: sum(item[key] for item in tasks) for key in ("predicted", "truth", "matched", "falsePositive", "missed")}
    totals["precision"] = round(totals["matched"] / max(1, totals["predicted"]), 4)
    totals["recall"] = round(totals["matched"] / max(1, totals["truth"]), 4)
    for key in ("slotAccuracy", "shapeAccuracy", "targetAccuracy", "clearAccuracy", "timeMae", "withinTwoFrames"):
        totals[key] = round(sum(item[key] * item["matched"] for item in tasks) / max(1, totals["matched"]), 4)
    return {"truthCount": len(tasks), "totals": totals, "tasks": tasks}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prediction-dir", type=Path)
    args = parser.parse_args()
    report = build_report(args.prediction_dir)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
