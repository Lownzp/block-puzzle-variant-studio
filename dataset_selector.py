from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


DEFAULT_SOURCE = Path(r"\\10.80.1.20\ivy_assets\三消+puzzle\puzzle\2677\核心玩法")
DEFAULT_PROJECT = Path(r"G:\爆款素材变体生成器")
SEED = "variant-action-dataset-v1-20260714"

KNOWN_DEVELOPMENT = [
    "2677_@李心怡_#编辑器自制_箭头大块全消_粉沙块_带片尾.mp4",
    "2677_@杨金月_#编辑器自制_2d游戏玩法天蓝纯色对称布局.mp4",
    "2677_ssq_20251020_彩块游戏玩法快节奏1080-1920.mp4",
    "2677_ssq_20251020_粉色大全消游戏玩法.mp4",
    "2677_ssq_20251020_连胜录屏1.mp4",
    "2677_yjy_20251119_编辑器自制_2d游戏玩法纯色蓝块全消结尾.mp4",
]

LABEL_PATTERNS = {
    "editor": r"编辑器自制|编辑器",
    "fixed_board": r"固定|固定图版",
    "random_board": r"随机|随机图版",
    "replica": r"竞品|复刻|仿竞品",
    "recording": r"录屏|实机|实拍|试玩",
    "chain_clear": r"连消|连续消除|多次数消",
    "full_clear": r"全消|清屏|全屏消",
    "delayed_clear": r"最后操作|开头全消后|延迟",
    "fast": r"快节奏|快速|连胜",
    "low_resolution_hint": r"360[-_x]640|低清",
    "two_d": r"(?:^|[_#])2d|2D",
    "three_d": r"(?:^|[_#仿])3d|3D",
    "colorful": r"彩块|彩色|多色|彩虹",
    "solid": r"纯色|单色",
    "color_change": r"变色",
    "sand": r"粉沙|沙块",
    "special_material": r"冰块|木制|木质|宝石|珍珠|毛线|糖果|宠物|猫块|狗块",
    "russian": r"俄罗斯",
    "holiday": r"圣诞|情人节|母亲节|世界杯|春节",
    "no_background": r"无背景",
}

# V1 dataset is intentionally limited to gameplay the current editor can replay.
# These are hard exclusions, not low-priority samples.
EXCLUSION_PATTERNS = {
    "live_person_capture": r"实拍|真人|手指动作与画面互动",
    "falling_block_gameplay": r"俄罗斯|破碎|整体变色|@杨锦(?:_|-)",
    "unsupported_event_level": r"新关卡|实机操作|手机录屏|活动关卡|宠物关卡|宠物块|动物|企鹅|兔子|猫窝|地鼠|小熊|指针|水管|木头|木桩|木箱|面包|礼盒|冰箱|三元素",
    "non_action_or_marketing": r"未操作|卖场视频",
    "out_of_v1_scope": r"30[×x]30|特殊图版|拼豆|非核心|ai切|斜视角|透视",
}

SUPPORTED_NEW_SAMPLE_PATTERN = r"编辑器自制|试玩录屏"

TARGETS = {
    "development": {
        "editor": 18, "fixed_board": 6, "random_board": 6,
        "chain_clear": 8, "full_clear": 5, "recording": 5,
        "replica": 5, "fast": 2, "three_d": 2, "color_change": 2,
        "solid": 4, "colorful": 4, "sand": 2,
    },
    "tuning": {
        "editor": 7, "fixed_board": 3, "random_board": 3,
        "chain_clear": 4, "full_clear": 2, "recording": 3,
        "replica": 2, "fast": 1, "three_d": 1, "color_change": 1,
        "solid": 2, "colorful": 2, "sand": 1,
    },
    "sealed_test": {
        "editor": 7, "fixed_board": 3, "random_board": 3,
        "chain_clear": 4, "full_clear": 2, "recording": 3,
        "replica": 2, "fast": 1, "three_d": 1, "color_change": 1,
        "solid": 2, "colorful": 2, "sand": 1,
    },
}

SPLIT_SIZES = {"development": 36, "tuning": 12, "sealed_test": 12}


@dataclass
class Video:
    dataset_id: str
    split: str
    path: str
    filename: str
    family_key: str
    labels: list[str]
    size_bytes: int
    modified: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    visual_fingerprint: str = ""
    source_role: str = "new_sample"


def labels_for(name: str) -> list[str]:
    return sorted(label for label, pattern in LABEL_PATTERNS.items() if re.search(pattern, name, re.I))


def exclusion_reason(name: str) -> str | None:
    for reason, pattern in EXCLUSION_PATTERNS.items():
        if re.search(pattern, name, re.I):
            return reason
    return None


def family_key(name: str) -> str:
    value = Path(name).stem.lower()
    # Prefer gameplay/layout identity over visual skin. These explicit signatures
    # deliberately over-group variants so near-duplicate skins cannot leak.
    group = re.search(r"0601分组测试_(\d+组[a-z])_固定.*?(拼凑连消|鱼骨连消|全屏连消|开屏连消|大箭头连消|大箭头消除)", value)
    if group:
        return f"0601_{group.group(1)}_{group.group(2)}"
    signatures = [
        r"连胜录屏_换皮", r"卖场视频音效加强_.*?消除特效", r"对称无限消除",
        r"箭头大块全消", r"四箭头对称全消", r"双l型连消", r"倒l型开屏连消",
        r"阶梯型多次数连消", r"交叉型连消", r"九宫格填消", r"横列填空消除",
        r"全屏连消", r"镂空鱼骨连消", r"鱼骨连消", r"镂空连消", r"快速连消",
        r"拼凑连消", r"开屏连消", r"最后操作全消", r"开头全消后接连消",
        r"粉色大全消", r"彩色大全消", r"仿3d竞品复刻宝石立方块",
        r"仿3d竞品复刻立方块", r"仿3d啫喱", r"仿3d珍珠块",
    ]
    for signature in signatures:
        hit = re.search(signature, value)
        if hit:
            prefix = "replica_" if "竞品" in value else ""
            return prefix + hit.group(0)
    value = re.sub(r"^2677[_-]?", "", value)
    value = re.sub(r"@[\w\u4e00-\u9fff-]+", "", value)
    value = re.sub(r"(?:19|20)\d{6}|(?:19|20)\d{4}|\d{3,4}[-x_]\d{3,4}", "", value)
    value = re.sub(r"(?:核心玩法|带片尾|无片尾|去除片尾|有片尾|横版|竖版|9[:：]16|\d+s)", "", value)
    value = re.sub(r"(?:纯色|马卡龙色|彩色|多色|单色|变色|白色|黑色|红色|绿色|蓝色|黄色|紫色|粉色|青色|橙色|天蓝|深粉红|浅粉)", "", value)
    value = re.sub(r"(?:背景|主题|方块|块)(?:\d+)?", "", value)
    value = re.sub(r"(?:版本|改版|改|片段|视频|成品)(?:\d+)?", "", value)
    value = re.sub(r"[_#\-+\s（）()]+", "_", value).strip("_")
    return value or hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def stable_rank(path: str) -> str:
    return hashlib.sha256(f"{SEED}|{path}".encode("utf-8")).hexdigest()


def ffprobe(path: str) -> dict:
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
        "-of", "json", path,
    ]
    try:
        raw = subprocess.run(command, capture_output=True, text=True, timeout=45, check=True)
        data = json.loads(raw.stdout)
        stream = data["streams"][0]
        num, den = (stream.get("avg_frame_rate") or "0/1").split("/")
        return {
            "duration_seconds": round(float(data["format"]["duration"]), 3),
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "fps": round(float(num) / float(den), 3) if float(den) else None,
        }
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError):
        return {}


def visual_fingerprint(path: str) -> str:
    """Create a compact, deterministic three-frame aHash without AI models."""
    try:
        import cv2
        import numpy as np

        capture = cv2.VideoCapture(path)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        hashes = []
        for ratio in (0.2, 0.5, 0.8):
            capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_count * ratio)))
            ok, frame = capture.read()
            if not ok:
                hashes.append("0" * 64)
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA)
            bits = (small >= float(np.median(small))).reshape(-1)
            hashes.append(f"{int(''.join('1' if bit else '0' for bit in bits), 2):064x}")
        capture.release()
        return "".join(hashes)
    except (ImportError, ValueError, OSError):
        return ""


def fingerprint_distance(left: str, right: str) -> float:
    if not left or not right or len(left) != len(right):
        return 1.0
    differing = (int(left, 16) ^ int(right, 16)).bit_count()
    return differing / (len(left) * 4)


def score_candidate(labels: set[str], counts: Counter, targets: dict[str, int], rank: str) -> tuple:
    unmet = sum(1 for label in labels if counts[label] < targets.get(label, 0))
    weighted_gap = sum(max(0, targets.get(label, 0) - counts[label]) for label in labels)
    rare_unmet = sum(
        label in {"recording", "fast", "three_d", "color_change", "sand"}
        and counts[label] < targets.get(label, 0)
        for label in labels
    )
    overflow = sum(counts[label] >= targets.get(label, 0) for label in labels if label in targets)
    return (unmet, weighted_gap, rare_unmet, -overflow, rank)


def select_for_split(candidates: list[Video], split: str, count: int, used_families: set[str], initial: list[Video] | None = None) -> list[Video]:
    chosen = list(initial or [])
    counts = Counter(label for video in chosen for label in video.labels)
    while len(chosen) < count:
        available = [video for video in candidates if video.family_key not in used_families]
        if not available:
            raise RuntimeError(f"No candidates left for {split}")
        best = max(
            available,
            key=lambda video: score_candidate(set(video.labels), counts, TARGETS[split], stable_rank(video.path)),
        )
        best.split = split
        chosen.append(best)
        used_families.add(best.family_key)
        counts.update(best.labels)
    return chosen


def scan(source: Path) -> list[Video]:
    videos = []
    for path in source.rglob("*.mp4"):
        stat = path.stat()
        if stat.st_size < 300_000:
            continue
        videos.append(Video(
            dataset_id="",
            split="",
            path=str(path),
            filename=path.name,
            family_key=family_key(path.name),
            labels=labels_for(path.name),
            size_bytes=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        ))
    return videos


def build(source: Path, project: Path) -> list[Video]:
    network = scan(source)
    local_dir = project / "测试素材"
    known = []
    for name in KNOWN_DEVELOPMENT:
        path = local_dir / name
        stat = path.stat()
        known.append(Video(
            dataset_id="", split="development", path=str(path), filename=name,
            family_key=family_key(name), labels=labels_for(name), size_bytes=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            source_role="known_development_regression",
        ))

    used_families = {video.family_key for video in known}
    # Also exclude network files whose normalized family matches any known development video.
    eligible = [
        video for video in network
        if video.family_key not in used_families
        and exclusion_reason(video.filename) is None
        and re.search(SUPPORTED_NEW_SAMPLE_PATTERN, video.filename, re.I)
    ]
    development = select_for_split(eligible, "development", SPLIT_SIZES["development"], used_families, known)
    tuning = select_for_split(eligible, "tuning", SPLIT_SIZES["tuning"], used_families)
    sealed = select_for_split(eligible, "sealed_test", SPLIT_SIZES["sealed_test"], used_families)
    selected = development + tuning + sealed

    prefixes = {"development": "DEV", "tuning": "TUNE", "sealed_test": "TEST"}
    counters = Counter()
    for video in selected:
        counters[video.split] += 1
        video.dataset_id = f"{prefixes[video.split]}-{counters[video.split]:03d}"
        metadata = ffprobe(video.path)
        for key, value in metadata.items():
            setattr(video, key, value)
        video.visual_fingerprint = visual_fingerprint(video.path)
    return selected


def validate(selected: list[Video]) -> None:
    assert len(selected) == 60
    assert len({video.path for video in selected}) == 60
    assert len({video.family_key for video in selected}) == 60
    assert Counter(video.split for video in selected) == Counter(SPLIT_SIZES)
    assert sum(video.source_role == "known_development_regression" for video in selected) == 6
    assert all(Path(video.path).exists() for video in selected)


def write_outputs(selected: list[Video], output: Path, source: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(selected[0]).keys())
    with (output / "视频数据集清单.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for video in selected:
            row = asdict(video)
            row["labels"] = "|".join(video.labels)
            writer.writerow(row)
    payload = {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seed": SEED,
        "source": str(source),
        "selection_unit": "content_family",
        "videos": [asdict(video) for video in selected],
    }
    (output / "视频数据集清单.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 视频动作识别数据集选样报告", "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 源目录：`{source}`", f"- 固定种子：`{SEED}`",
        "- 选择单位：内容家族；每个规范化家族仅选 1 条。", "- 视频仅保存路径，没有复制源文件。", "",
        "- 当前能力边界：仅选择经典三槽拼图玩法；排除实拍真人、俄罗斯下落玩法及带特殊障碍的活动新关卡。", "",
    ]
    for split in ("development", "tuning", "sealed_test"):
        rows = [video for video in selected if video.split == split]
        label_counts = Counter(label for video in rows for label in video.labels)
        lines += [f"## {split}（{len(rows)} 条）", "", "标签覆盖：" + "、".join(f"{k}={v}" for k, v in label_counts.most_common()), ""]
        lines += [f"- `{video.dataset_id}` `{video.filename}`" for video in rows]
        lines.append("")
    suspects = []
    for index, left in enumerate(selected):
        for right in selected[index + 1:]:
            if left.split == right.split or left.duration_seconds is None or right.duration_seconds is None:
                continue
            distance = fingerprint_distance(left.visual_fingerprint, right.visual_fingerprint)
            if abs(left.duration_seconds - right.duration_seconds) <= 1.0 and distance <= 0.12:
                suspects.append((left, right, distance))
    lines += ["## 跨集合视觉近重复检查", ""]
    if suspects:
        lines += [
            f"- 待人工复核：`{left.dataset_id}` 与 `{right.dataset_id}`，指纹距离 `{distance:.3f}`"
            for left, right, distance in suspects
        ]
    else:
        lines.append("- 未发现“时长差 <= 1 秒且三帧感知指纹距离 <= 0.12”的跨集合近重复。")
    lines.append("")
    lines += [
        "## 人工复核清单", "",
        "1. 检查同母版的换色、换背景、节日和导出版本是否跨集合。",
        "2. 检查调优集、封存测试集是否含已人工调试过的视频。",
        "3. 检查封存测试集是否覆盖录屏、连消、随机/固定棋盘和特殊视觉。",
        "4. 对近似文件补做抽样帧指纹；如发现近重复，整家族移动，不能只替换单文件。",
    ]
    (output / "数据集选样报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    args = parser.parse_args()
    selected = build(args.source, args.project)
    validate(selected)
    write_outputs(selected, args.project / "数据集", args.source)
    print(json.dumps(Counter(video.split for video in selected), ensure_ascii=False))


if __name__ == "__main__":
    main()
