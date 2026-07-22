from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from variant_bridge import analyse_video, build_action_review


ROOT = Path(__file__).resolve().parent
FIXTURE_ROOT = ROOT / "测试素材"
REPORT_ROOT = ROOT / "测试报告" / "全量回归"
MIN_ACTIONS = {"T01": 20, "T02": 20, "T03": 40, "T04": 60, "T05": 30, "T06": 20}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def evidence_coverage(events: list[dict]) -> dict:
    kinds = ("before", "action", "placed", "cleared")
    result = {kind: 0 for kind in kinds}
    clear_steps = 0
    for event in events:
        evidence = event.get("evidenceFrames") or {}
        for kind in kinds:
            if evidence.get(kind):
                result[kind] += 1
        if event.get("clearMode") not in (None, "none"):
            clear_steps += 1
    result["clearSteps"] = clear_steps
    return result


def assess(analysis: dict, case_id: str) -> tuple[list[str], list[str]]:
    events = analysis["timeline"]["events"]
    validation = analysis["timeline"]["validation"]
    board = analysis["board"]
    failures: list[str] = []
    warnings: list[str] = []
    if board.get("rows", 0) < 3 or board.get("cols", 0) < 3:
        failures.append("invalid_grid")
    if board.get("gridConfidence", 0) <= 0:
        failures.append("board_not_confident")
    if not events:
        failures.append("zero_actions")
    if len(events) < MIN_ACTIONS.get(case_id, 1):
        failures.append(f"action_count_below_regression_floor:{len(events)}<{MIN_ACTIONS[case_id]}")
    if validation.get("unresolvedStableTransitions", 0):
        failures.append("unresolved_stable_transitions")
    coverage = evidence_coverage(events)
    if coverage["before"] != len(events) or coverage["placed"] != len(events):
        failures.append("missing_required_evidence")
    unknown_slots = sum(event.get("sourceSlot", -1) not in (0, 1, 2) for event in events)
    unknown_clear = sum(event.get("clearMode") == "unknown" for event in events)
    if unknown_slots:
        warnings.append(f"source_slot_pending:{unknown_slots}")
    if unknown_clear:
        warnings.append(f"clear_mode_pending:{unknown_clear}")
    if validation.get("candidateMoveCount", 0):
        warnings.append(f"manual_confirmation_required:{validation['candidateMoveCount']}")
    return failures, warnings


def run_one(video: Path, run_root: Path, index: int) -> dict:
    case_id = f"T{index:02d}"
    case_root = run_root / case_id
    if case_root.exists():
        shutil.rmtree(case_root)
    case_root.mkdir(parents=True)
    started = time.perf_counter()
    result: dict = {"id": case_id, "video": video.name}
    try:
        analysis = analyse_video(video, case_root)
        timeline_path = Path(analysis["timeline"]["path"])
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        analysis["reviewActions"] = build_action_review(timeline)
        write_json(case_root / "识别草稿.json", analysis)
        failures, warnings = assess(analysis, case_id)
        events = analysis["timeline"]["events"]
        validation = analysis["timeline"]["validation"]
        result.update({
            "board": {key: analysis["board"].get(key) for key in ("x", "y", "width", "height", "rows", "cols", "gridConfidence", "source")},
            "eventCount": len(events),
            "verifiedCount": validation.get("verifiedMoveCount", 0),
            "candidateCount": validation.get("candidateMoveCount", 0),
            "unknownSourceSlots": sum(event.get("sourceSlot", -1) not in (0, 1, 2) for event in events),
            "unknownClearModes": sum(event.get("clearMode") == "unknown" for event in events),
            "unresolvedTransitions": validation.get("unresolvedStableTransitions", 0),
            "gameplayEndTime": validation.get("gameplayEndTime"),
            "multiSolutionSteps": sum(bool(event.get("candidateSolutions")) for event in events),
            "evidence": evidence_coverage(events),
            "recognitionPassed": not failures,
            "humanTruthConfirmed": False,
            "failures": failures,
            "warnings": warnings,
        })
    except Exception as exc:
        result.update({
            "recognitionPassed": False,
            "humanTruthConfirmed": False,
            "failures": [f"exception:{type(exc).__name__}:{exc}"],
            "warnings": [],
        })
    result["elapsedSeconds"] = round(time.perf_counter() - started, 2)
    write_json(case_root / "回归结果.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic recognition regression for every mandatory video fixture.")
    parser.add_argument("--output", type=Path, help="Optional output directory")
    parser.add_argument("--case", type=int, action="append", help="Run only a 1-based fixture index; repeat for multiple cases")
    args = parser.parse_args()
    videos = sorted(FIXTURE_ROOT.glob("*.mp4"))
    if not videos:
        raise SystemExit(f"No MP4 fixtures found under {FIXTURE_ROOT}")
    indexed_videos = list(enumerate(videos, 1))
    if args.case:
        selected = set(args.case)
        indexed_videos = [(index, video) for index, video in indexed_videos if index in selected]
        if not indexed_videos:
            raise SystemExit("No fixture matched --case")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = args.output or (REPORT_ROOT / stamp)
    run_root.mkdir(parents=True, exist_ok=True)
    results = []
    for position, (index, video) in enumerate(indexed_videos, 1):
        print(f"[{position}/{len(indexed_videos)}] T{index:02d} {video.name}", flush=True)
        result = run_one(video, run_root, index)
        results.append(result)
        print(
            f"  actions={result.get('eventCount', 0)} "
            f"verified={result.get('verifiedCount', 0)} "
            f"pending={result.get('candidateCount', 0)} "
            f"recognition={'PASS' if result['recognitionPassed'] else 'FAIL'}",
            flush=True,
        )
    summary = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "fixtureRoot": str(FIXTURE_ROOT),
        "caseCount": len(results),
        "recognitionPassed": sum(item["recognitionPassed"] for item in results),
        "humanTruthConfirmed": sum(item["humanTruthConfirmed"] for item in results),
        "overallPassed": all(item["recognitionPassed"] and item["humanTruthConfirmed"] for item in results),
        "results": results,
    }
    write_json(run_root / "汇总.json", summary)
    (REPORT_ROOT / "latest.txt").write_text(str(run_root), encoding="utf-8")
    print(f"Report: {run_root}")
    print(f"Recognition: {summary['recognitionPassed']}/{summary['caseCount']}")
    print(f"Human truth: {summary['humanTruthConfirmed']}/{summary['caseCount']}")
    return 0 if summary["recognitionPassed"] == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
