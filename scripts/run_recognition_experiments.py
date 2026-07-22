"""Run recognition A/B experiments and summarize benchmark deltas."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import argparse
import json
import subprocess
from datetime import datetime

from recognition_experiments import KNOWN_FLAGS, parse_flags
from truth_benchmark import build_report


DEFAULT_COLOR_IDS = [
    "DEV-001",
    "DEV-002",
    "DEV-003",
    "DEV-004",
    "DEV-006",
    "DEV-008",
    "DEV-011",
    "DEV-012",
]

METRIC_KEYS = [
    "predicted",
    "truth",
    "matched",
    "falsePositive",
    "missed",
    "precision",
    "recall",
    "slotAccuracy",
    "shapeAccuracy",
    "targetAccuracy",
    "clearAccuracy",
    "timeMae",
    "withinTwoFrames",
]


def run_command(command: list[str]) -> None:
    print(" ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def report_for(prediction_dir: Path, ids: set[str]) -> dict:
    full = build_report(prediction_dir)
    tasks = [item for item in full["tasks"] if item["id"] in ids]
    totals = {key: sum(item[key] for item in tasks) for key in ("predicted", "truth", "matched", "falsePositive", "missed")}
    totals["precision"] = round(totals["matched"] / max(1, totals["predicted"]), 4)
    totals["recall"] = round(totals["matched"] / max(1, totals["truth"]), 4)
    for key in ("slotAccuracy", "shapeAccuracy", "targetAccuracy", "clearAccuracy", "timeMae", "withinTwoFrames"):
        totals[key] = round(sum(item[key] * item["matched"] for item in tasks) / max(1, totals["matched"]), 4)
    return {"truthCount": len(tasks), "totals": totals, "tasks": tasks}


def percent(value: float) -> str:
    return f"{value * 100:.2f}%" if isinstance(value, float) and value <= 1.5 else str(value)


def markdown_summary(summary: dict) -> str:
    baseline = summary["experiments"][0]["report"]["totals"]
    lines = [
        f"# Recognition Experiment Summary",
        "",
        f"- Date: {summary['createdAt']}",
        f"- Dataset: {', '.join(summary['datasetIds'])}",
        "",
        "| Experiment | Flags | Precision | Recall | Target | Shape | FP | Missed | Decision |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in summary["experiments"]:
        totals = item["report"]["totals"]
        flags = ", ".join(item["flags"]) or "none"
        decision = "baseline" if not item["flags"] else "review"
        lines.append(
            "| {name} | {flags} | {precision} ({pdelta:+.2f}pp) | {recall} ({rdelta:+.2f}pp) | "
            "{target} | {shape} | {fp} | {missed} | {decision} |".format(
                name=item["name"],
                flags=flags,
                precision=percent(totals["precision"]),
                pdelta=(totals["precision"] - baseline["precision"]) * 100,
                recall=percent(totals["recall"]),
                rdelta=(totals["recall"] - baseline["recall"]) * 100,
                target=percent(totals["targetAccuracy"]),
                shape=percent(totals["shapeAccuracy"]),
                fp=totals["falsePositive"],
                missed=totals["missed"],
                decision=decision,
            )
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--only", nargs="*", default=DEFAULT_COLOR_IDS)
    parser.add_argument("--single-flags", nargs="*", default=sorted(KNOWN_FLAGS))
    parser.add_argument("--combined", nargs="*", default=[])
    parser.add_argument("--strategy", default="legacy")
    args = parser.parse_args()

    ids = set(args.only)
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    experiments: list[tuple[str, list[str]]] = [("baseline", [])]
    for flag in args.single_flags:
        parse_flags([flag])
        experiments.append((flag, [flag]))
    if args.combined:
        combined_flags = parse_flags(args.combined).as_list()
        experiments.append(("combined", combined_flags))

    results = []
    for name, flags in experiments:
        prediction_dir = output_root / name
        command = [
            sys.executable,
            "scripts/reanalyze_truth_set.py",
            str(prediction_dir),
            "--strategy",
            args.strategy,
            "--label",
            name,
            "--only",
            *args.only,
        ]
        if flags:
            command.extend(["--flags", *flags])
        run_command(command)
        report = report_for(prediction_dir, ids)
        (output_root / f"{name}_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        results.append({"name": name, "flags": flags, "predictionDir": str(prediction_dir), "report": report})

    summary = {
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "datasetIds": args.only,
        "strategy": args.strategy,
        "experiments": results,
    }
    (output_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_root / "summary.md").write_text(markdown_summary(summary), encoding="utf-8")
    print(output_root / "summary.md")


if __name__ == "__main__":
    main()
