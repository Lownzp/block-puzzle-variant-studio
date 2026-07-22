"""Validate a rendered replay by exact ordered 8x8 occupancy masks."""


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


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def board_bits(board: list[list[int | None]]) -> str:
    return "".join(
        "1" if cell is not None else "0"
        for row in board
        for cell in row
    )


def validate(timeline: dict, truth: dict) -> dict:
    failures = []
    matches = []
    if timeline.get("grid") != truth.get("grid"):
        failures.append(
            {
                "field": "grid",
                "expected": truth.get("grid"),
                "actual": timeline.get("grid"),
            }
        )

    states = [
        {
            "stateIndex": state["stateIndex"],
            "startTime": state["startTime"],
            "endTime": state["endTime"],
            "bits": board_bits(state["board"]),
        }
        for state in timeline.get("stableStates", [])
    ]
    cursor = -1
    for step in truth.get("steps", []):
        expected = board_bits(step["expectedBoardAfterPlacement"])
        match = next(
            (
                state
                for state in states
                if state["stateIndex"] > cursor and state["bits"] == expected
            ),
            None,
        )
        if match is None:
            failures.append(
                {
                    "field": f"steps[{step['stepIndex']}].boardAfterPlacement",
                    "expectedFilled": expected.count("1"),
                    "expectedBits": expected,
                }
            )
            continue
        cursor = match["stateIndex"]
        matches.append(
            {
                "stepIndex": step["stepIndex"],
                "sourceSlot": step["sourceSlot"],
                "target": step["target"],
                "clearDetection": step["clearDetection"],
                "filled": expected.count("1"),
                "stateIndex": match["stateIndex"],
                "startTime": match["startTime"],
                "endTime": match["endTime"],
            }
        )

    return {
        "checkedAt": datetime.now().isoformat(timespec="seconds"),
        "passed": not failures and len(matches) == truth.get("stepCount"),
        "grid": timeline.get("grid"),
        "expectedStepCount": truth.get("stepCount"),
        "matchedStepCount": len(matches),
        "matchingRule": "exact ordered full-board occupancy mask",
        "matches": matches,
        "failures": failures,
    }


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print("usage: video_replay_validator.py TIMELINE TRUTH [REPORT]")
        return 2
    timeline_path = Path(sys.argv[1])
    truth_path = Path(sys.argv[2])
    report = validate(load(timeline_path), load(truth_path))
    if len(sys.argv) == 4:
        Path(sys.argv[3]).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
