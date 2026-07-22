from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json
from pathlib import Path


ROOT = Path(r"G:\爆款素材变体生成器")
TASK = ROOT / "视频重建任务" / "20260710_235749_4月29日(1)"
TIMELINE_PATH = TASK / "步骤时间线.json"
OUTPUT_PATH = TASK / "确定性回放真值.json"


def filled_count(board):
    return sum(cell is not None for row in board for cell in row)


def main():
    timeline = json.loads(TIMELINE_PATH.read_text(encoding="utf-8"))
    events = timeline["events"]
    states = {state["stateIndex"]: state for state in timeline["stableStates"]}

    steps = []
    for index, event in enumerate(events):
        next_source_index = (
            events[index + 1]["sourceStateIndex"]
            if index + 1 < len(events)
            else event["targetStateIndex"]
        )
        target_state = states[event["targetStateIndex"]]
        next_source_state = states[next_source_index]
        after_placement = event["afterBoard"]

        completed_lines = bool(event.get("clearedRows") or event.get("clearedCols"))
        board_reduced_before_next_step = (
            filled_count(next_source_state["board"]) < filled_count(after_placement)
        )
        # The editor only clears when the placed group's isCheckDie flag is on.
        # A completed line that remains visible therefore proves the flag was off.
        clear_detection = completed_lines and board_reduced_before_next_step

        steps.append(
            {
                "stepIndex": event["stepIndex"],
                "roundIndex": (event["stepIndex"] - 1) // 3 + 1,
                "sourceSlot": event["sourceSlot"],
                "target": event["target"],
                "shape": event["group"]["shape"],
                "executeAt": event["time"],
                "clearDetection": clear_detection,
                "completedRowsAfterPlacement": event.get("clearedRows", []),
                "completedColsAfterPlacement": event.get("clearedCols", []),
                "observedBoardReductionBeforeNextStep": board_reduced_before_next_step,
                "expectedBoardBefore": event["beforeBoard"],
                "expectedBoardAfterPlacement": after_placement,
                "expectedBoardAtNextAction": next_source_state["board"],
                "sourceStateIndex": event["sourceStateIndex"],
                "targetStateIndex": event["targetStateIndex"],
            }
        )

    output = {
        "schemaVersion": 1,
        "sourceVideo": r"F:\4月29日(1).mp4",
        "grid": timeline["grid"],
        "sourceFrameRate": timeline["sourceFrameRate"],
        "initialBoard": states[events[0]["sourceStateIndex"]]["board"],
        "stepCount": len(steps),
        "steps": steps,
        "observedPresetTransitions": [
            {
                "afterStepIndex": 9,
                "emptyStateIndex": 12,
                "presetStateIndex": 13,
                "switchMode": "CustomBlock",
                "expectedBoard": states[13]["board"],
            }
        ],
        "notes": [
            "clearDetection is true only when completed lines visibly reduce the board before the next action.",
            "Steps without completed lines are encoded false because the source pixels cannot distinguish the switch state and false preserves deterministic deferred-clear behavior.",
            "Step 9 clears the full board, then CustomBlock switches to the second preset before step 10.",
        ],
    }
    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(OUTPUT_PATH)
    for step in steps:
        print(
            step["stepIndex"],
            step["clearDetection"],
            step["completedRowsAfterPlacement"],
            step["completedColsAfterPlacement"],
            step["observedBoardReductionBeforeNextStep"],
        )


if __name__ == "__main__":
    main()
