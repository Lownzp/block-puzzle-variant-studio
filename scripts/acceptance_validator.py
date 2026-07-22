"""Compare a reconstructed timeline with a manually confirmed baseline."""


from __future__ import annotations


from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json
import sys
from datetime import datetime
from pathlib import Path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def normalized_step(step: dict) -> dict:
    target = step["target"]
    if isinstance(target, dict):
        target = [target["row"], target["col"]]
    group = step.get("group", {})
    shape = step.get("shape") or group.get("shape") or []
    normalized_shape = sorted([
        [cell["row"], cell["col"]] if isinstance(cell, dict) else list(cell)
        for cell in shape
    ])
    return {
        "stepIndex": int(step["stepIndex"]),
        "sourceSlot": int(step["sourceSlot"]),
        "target": list(target),
        "shape": normalized_shape,
    }


def validate(timeline: dict, baseline: dict) -> dict:
    failures = []
    actual_grid = timeline.get("grid", {})
    if actual_grid != baseline["grid"]:
        failures.append({"field": "grid", "expected": baseline["grid"], "actual": actual_grid})

    actual_steps = [normalized_step(step) for step in timeline.get("events", [])]
    expected_steps = [normalized_step(step) for step in baseline.get("steps", [])]
    if len(actual_steps) != baseline["expectedStepCount"]:
        failures.append({"field": "stepCount", "expected": baseline["expectedStepCount"], "actual": len(actual_steps)})
    for index in range(max(len(expected_steps), len(actual_steps))):
        expected = expected_steps[index] if index < len(expected_steps) else None
        actual = actual_steps[index] if index < len(actual_steps) else None
        if expected != actual:
            failures.append({"field": f"steps[{index}]", "expected": expected, "actual": actual})

    validation = timeline.get("validation", {})
    if not validation.get("allMovesRuleVerified"):
        failures.append({"field": "allMovesRuleVerified", "expected": True, "actual": validation.get("allMovesRuleVerified")})
    if validation.get("unresolvedStableTransitions") != 0:
        failures.append({"field": "unresolvedStableTransitions", "expected": 0, "actual": validation.get("unresolvedStableTransitions")})

    return {
        "checkedAt": datetime.now().isoformat(timespec="seconds"),
        "passed": not failures,
        "grid": actual_grid,
        "expectedStepCount": baseline["expectedStepCount"],
        "actualStepCount": len(actual_steps),
        "unresolvedStableTransitions": validation.get("unresolvedStableTransitions"),
        "failures": failures,
    }


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print("usage: acceptance_validator.py TIMELINE BASELINE [REPORT]")
        return 2
    timeline_path, baseline_path = map(Path, sys.argv[1:3])
    report = validate(load_json(timeline_path), load_json(baseline_path))
    if len(sys.argv) == 4:
        Path(sys.argv[3]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
