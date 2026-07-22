"""Run the current recognizer against all confirmed truth videos."""



from __future__ import annotations


from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import argparse
import json
from pathlib import Path

from recognition_experiments import parse_flags
from truth_benchmark import initial_draft, latest_truth_tasks
from variant_bridge import analyse_video, build_action_review


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--strategy", default="legacy")
    parser.add_argument("--flags", nargs="*", default=[])
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    experiment_flags = parse_flags(args.flags)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    for task in latest_truth_tasks():
        dataset_id = next(part for part in task.name.split("_") if part.startswith("DEV-"))
        if args.only and dataset_id not in args.only:
            continue
        draft = initial_draft(task)
        board = draft["board"]
        task_output = args.output / dataset_id
        task_output.mkdir(parents=True, exist_ok=True)
        analysis = analyse_video(
            task / "source.mp4",
            task_output,
            board_override=board,
            recognition_strategy=args.strategy,
            experiment_flags=experiment_flags,
        )
        timeline = json.loads((task_output / "步骤时间线.json").read_text(encoding="utf-8"))
        payload = {
            "datasetId": dataset_id,
            "eventCount": len(timeline["events"]),
            "actions": timeline["events"],
            "reviewActions": build_action_review(timeline),
            "recognitionStrategy": analysis.get("recognitionStrategy"),
            "experiment": {
                "label": args.label,
                "flags": experiment_flags.as_list(),
            },
            "processingDiagnostics": timeline.get("processingDiagnostics", {}),
            "cancelledDrags": timeline.get("cancelledDrags", []),
            "discardedTailCandidates": timeline.get("discardedTailCandidates", []),
        }
        (args.output / f"{dataset_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"{dataset_id}: {payload['eventCount']} actions", flush=True)


if __name__ == "__main__":
    main()
