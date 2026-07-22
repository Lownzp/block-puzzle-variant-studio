"""Summarize benchmark bottlenecks from a truth benchmark report."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ACCURACY_KEYS = ("slotAccuracy", "shapeAccuracy", "targetAccuracy", "clearAccuracy", "withinTwoFrames")

IMAGE_MATERIAL_KEYWORDS = (
    "甜品", "冰淇淋", "木制", "立方", "仿3d", "猫块", "泳池", "排球", "黄鸭", "篮球",
)
COLOR_MATERIAL_KEYWORDS = (
    "纯色", "彩块", "粉色", "蓝块", "粉沙", "彩虹", "橙块",
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def weighted_accuracy(tasks: list[dict[str, Any]], key: str) -> float:
    matched = sum(int(task.get("matched") or 0) for task in tasks)
    if not matched:
        return 0.0
    return round(sum(float(task.get(key) or 0.0) * int(task.get("matched") or 0) for task in tasks) / matched, 4)


def aggregate(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "caseCount": len(tasks),
        "predicted": sum(int(task.get("predicted") or 0) for task in tasks),
        "truth": sum(int(task.get("truth") or 0) for task in tasks),
        "matched": sum(int(task.get("matched") or 0) for task in tasks),
        "falsePositive": sum(int(task.get("falsePositive") or 0) for task in tasks),
        "missed": sum(int(task.get("missed") or 0) for task in tasks),
    }
    totals["precision"] = round(totals["matched"] / max(1, totals["predicted"]), 4)
    totals["recall"] = round(totals["matched"] / max(1, totals["truth"]), 4)
    for key in ACCURACY_KEYS:
        totals[key] = weighted_accuracy(tasks, key)
    totals["timeMae"] = weighted_accuracy(tasks, "timeMae")
    return totals


def weighted_error(task: dict[str, Any], key: str) -> float:
    return int(task.get("matched") or 0) * max(0.0, 1.0 - float(task.get(key) or 0.0))


def error_family(task: dict[str, Any]) -> str:
    if int(task.get("missed") or 0) >= max(3, int(task.get("truth") or 0) * 0.18):
        return "漏检/动作窗口缺失"
    if int(task.get("falsePositive") or 0) >= max(3, int(task.get("predicted") or 0) * 0.18):
        return "误检/拖拽碎片未合并"
    errors = {key: weighted_error(task, key) for key in ACCURACY_KEYS}
    dominant = max(errors, key=errors.get)
    return {
        "shapeAccuracy": "形状还原错误",
        "clearAccuracy": "清除状态错误",
        "slotAccuracy": "槽位生命周期错误",
        "targetAccuracy": "落点/规则约束错误",
        "withinTwoFrames": "时间边界漂移",
    }.get(dominant, "综合错误")


def material_type(task_name: str) -> str:
    lowered = task_name.lower()
    if any(keyword.lower() in lowered for keyword in IMAGE_MATERIAL_KEYWORDS):
        return "图片/主题素材替换"
    if any(keyword.lower() in lowered for keyword in COLOR_MATERIAL_KEYWORDS):
        return "常规彩色方块"
    return "未明确分类"


def classify_tasks(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for task in report.get("tasks") or []:
        row = {
            "id": task.get("id"),
            "task": task.get("task"),
            "materialType": material_type(str(task.get("task") or "")),
            "predicted": task.get("predicted"),
            "truth": task.get("truth"),
            "matched": task.get("matched"),
            "falsePositive": task.get("falsePositive"),
            "missed": task.get("missed"),
            "precision": task.get("precision"),
            "recall": task.get("recall"),
            "slotAccuracy": task.get("slotAccuracy"),
            "shapeAccuracy": task.get("shapeAccuracy"),
            "targetAccuracy": task.get("targetAccuracy"),
            "clearAccuracy": task.get("clearAccuracy"),
            "timeMae": task.get("timeMae"),
            "withinTwoFrames": task.get("withinTwoFrames"),
            "dominantFamily": error_family(task),
            "weightedShapeErrors": round(weighted_error(task, "shapeAccuracy"), 2),
            "weightedClearErrors": round(weighted_error(task, "clearAccuracy"), 2),
            "weightedSlotErrors": round(weighted_error(task, "slotAccuracy"), 2),
            "weightedTargetErrors": round(weighted_error(task, "targetAccuracy"), 2),
        }
        row["totalWeightedCoreErrors"] = round(
            row["weightedShapeErrors"]
            + row["weightedClearErrors"]
            + row["weightedSlotErrors"]
            + row["weightedTargetErrors"]
            + float(row["falsePositive"] or 0)
            + float(row["missed"] or 0),
            2,
        )
        rows.append(row)
    return sorted(rows, key=lambda item: item["totalWeightedCoreErrors"], reverse=True)


def material_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row["materialType"]), []).append(row)
    return {name: aggregate(tasks) for name, tasks in sorted(groups.items())}


def build_summary(report_path: Path, report: dict[str, Any]) -> dict[str, Any]:
    rows = classify_tasks(report)
    totals = report.get("totals") or {}
    metric_gaps = [
        {"metric": key, "value": round(float(totals.get(key) or 0.0), 4), "gapToPerfect": round(1.0 - float(totals.get(key) or 0.0), 4)}
        for key in ACCURACY_KEYS
    ]
    metric_gaps.sort(key=lambda item: item["gapToPerfect"], reverse=True)
    families: dict[str, int] = {}
    for row in rows:
        families[row["dominantFamily"]] = families.get(row["dominantFamily"], 0) + 1
    return {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "sourceReport": str(report_path),
        "truthCount": report.get("truthCount"),
        "totals": totals,
        "metricGaps": metric_gaps,
        "dominantFamilies": families,
        "materialSummary": material_summary(rows),
        "topTasks": rows[:8],
        "allTasks": rows,
    }


def markdown_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| 样本 | 类型 | 主因 | FP | Miss | 槽位 | 形状 | 落点 | 清除 | 时间<=2帧 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {id} | {materialType} | {dominantFamily} | {falsePositive} | {missed} | {slotAccuracy:.2%} | "
            "{shapeAccuracy:.2%} | {targetAccuracy:.2%} | {clearAccuracy:.2%} | {withinTwoFrames:.2%} |".format(**row)
        )
    return "\n".join(lines)


def material_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "| 类型 | 样本数 | Precision | Recall | 槽位 | 形状 | 落点 | 清除 | 时间<=2帧 | FP | Miss |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in summary["materialSummary"].items():
        lines.append(
            f"| {name} | {item['caseCount']} | {item['precision']:.2%} | {item['recall']:.2%} | "
            f"{item['slotAccuracy']:.2%} | {item['shapeAccuracy']:.2%} | {item['targetAccuracy']:.2%} | "
            f"{item['clearAccuracy']:.2%} | {item['withinTwoFrames']:.2%} | {item['falsePositive']} | {item['missed']} |"
        )
    return "\n".join(lines)


def write_markdown(path: Path, summary: dict[str, Any]) -> None:
    totals = summary["totals"]
    metric_lines = "\n".join(
        f"- `{item['metric']}`: {item['value']:.2%}, 缺口 {item['gapToPerfect']:.2%}"
        for item in summary["metricGaps"]
    )
    family_lines = "\n".join(f"- {name}: {count} 条" for name, count in summary["dominantFamilies"].items())
    content = f"""# 当前项目瓶颈分析

生成时间：{summary['generatedAt']}

来源报告：`{summary['sourceReport']}`

## 结论摘要

当前识别链路在 {summary['truthCount']} 条真值样本上匹配 {totals.get('matched')}/{totals.get('truth')} 个动作，整体 precision 为 {totals.get('precision'):.2%}，recall 为 {totals.get('recall'):.2%}。时间边界不是第一瓶颈，`withinTwoFrames` 为 {totals.get('withinTwoFrames'):.2%}；主要瓶颈集中在形状还原、清除状态、槽位生命周期和落点约束。

## 指标缺口

{metric_lines}

## 素材类型对比

分组规则：命中“甜品、冰淇淋、木制、立方、仿3d、猫块、泳池、排球、黄鸭、篮球”的样本归为图片/主题素材替换；命中“纯色、彩块、粉色、蓝块、粉沙、彩虹、橙块”的样本归为常规彩色方块。这个分类来自文件名语义，需要人工复核。

{material_markdown(summary)}

## 样本主因分布

{family_lines}

## 优先排查样本

{markdown_table(summary['topTasks'])}

## 优化方向

1. 优先把常规彩色方块和图片/主题素材替换拆成两套视觉判别 profile，但共用后续规则求解与回归接口。
2. 常规彩色方块继续以 HSV/饱和度/亮度为主，保留现有路径并减少过度拟合。
3. 图片/主题素材替换应使用纹理、边缘、局部模板一致性和多帧稳定性作为主要信号，降低对单一 hue 的依赖。
4. 落点和清除错误多数受形状输入影响，应在素材类型分流后再集中优化规则约束。
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=Path("benchmark_v34_merged_report.json"))
    parser.add_argument("--json-output", type=Path, default=Path("测试报告/current_bottleneck_analysis.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("测试报告/current_bottleneck_analysis.md"))
    args = parser.parse_args()
    report = load_json(args.report)
    summary = build_summary(args.report, report)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(args.markdown_output, summary)
    print(f"Wrote {args.json_output}")
    print(f"Wrote {args.markdown_output}")


if __name__ == "__main__":
    main()
