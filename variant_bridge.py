"""Local bridge between the variant browser tool and BlockPuzzleEditor.

The editor is a Unity desktop application rather than an HTTP service. This
bridge deliberately does not modify its installation: it creates a per-variant
preview config and task brief under G:, then opens the editor and the task file.
"""

from __future__ import annotations

import copy
import cgi
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cell_occupancy_model import load_model
from timeline_analyzer import _board_state, build_timeline, infer_material_profile

ROOT = Path(__file__).resolve().parent
TASK_ROOT = ROOT / "编辑器任务"
DEFAULT_VIDEO_ROOT = ROOT / "变体视频"
UPLOAD_ROOT = ROOT / "视频重建任务"
ANALYSIS_CACHE_INDEX = ROOT / "analysis-cache-index.json"
DATASET_MANIFEST = ROOT / "数据集" / "视频数据集清单.json"
SEEKABLE_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".webm"}
CALIBRATION_ROOT = ROOT / "校准"
EDITOR_EXE = Path(r"F:\Release-Editor\Release-Editor\Release-Editor\editor\BlockPuzzleEditor.exe")
MODIFIED_CLIENT_ROOT = ROOT / "改造客户端" / "preview_client"
PREVIEW_EXE = MODIFIED_CLIENT_ROOT / "preview_client.exe"
STREAMING_ASSETS = MODIFIED_CLIENT_ROOT / "preview_client_Data" / "StreamingAssets"
SHARED_LIBRARY = Path(r"F:\Release-Editor\Release-Editor\Release-Editor\SharedImageLibrary")

DATASET_SPLITS = {
    "development": "开发集",
    "tuning": "调参集",
    "test": "测试集",
    "sealed_test": "封闭测试集",
}


def build_truth_progress(manifest_path: Path = DATASET_MANIFEST, task_root: Path = UPLOAD_ROOT) -> dict:
    """Return manifest-level truth progress, deduplicated by dataset ID."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    videos = manifest.get("videos", [])
    dataset_ids = {str(item.get("dataset_id", "")).upper() for item in videos}
    confirmed_by_id: dict[str, dict] = {}
    if task_root.is_dir():
        for task_dir in task_root.iterdir():
            if not task_dir.is_dir():
                continue
            match = re.search(r"(?:DEV|TUNE|TEST)-\d{3}", task_dir.name, re.IGNORECASE)
            if not match:
                continue
            dataset_id = match.group(0).upper()
            if dataset_id not in dataset_ids:
                continue
            status_path = task_dir / "动作确认状态.json"
            truth_path = task_dir / "确定性回放真值.json"
            if not status_path.is_file() or not truth_path.is_file():
                continue
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if status.get("status") != "confirmed":
                continue
            candidate = {
                "jobId": task_dir.name,
                "stepCount": int(status.get("stepCount", 0) or 0),
                "confirmedAt": str(status.get("confirmedAt", "")),
                "sortKey": str(status.get("confirmedAt", "")) or f"{task_dir.stat().st_mtime:020.6f}",
            }
            current = confirmed_by_id.get(dataset_id)
            if current is None or candidate["sortKey"] > current["sortKey"]:
                confirmed_by_id[dataset_id] = candidate

    items = []
    split_counts: dict[str, dict] = {}
    for video in videos:
        dataset_id = str(video.get("dataset_id", "")).upper()
        split = str(video.get("split", ""))
        split_stat = split_counts.setdefault(split, {
            "key": split,
            "label": DATASET_SPLITS.get(split, split or "未分组"),
            "completed": 0,
            "total": 0,
        })
        split_stat["total"] += 1
        confirmed = confirmed_by_id.get(dataset_id)
        if confirmed:
            split_stat["completed"] += 1
        items.append({
            "datasetId": dataset_id,
            "split": split,
            "splitLabel": split_stat["label"],
            "filename": video.get("filename") or Path(str(video.get("path", ""))).name,
            "status": "confirmed" if confirmed else "pending",
            "stepCount": confirmed["stepCount"] if confirmed else 0,
            "confirmedAt": confirmed["confirmedAt"] if confirmed else "",
            "jobId": confirmed["jobId"] if confirmed else "",
        })
    completed = len(confirmed_by_id)
    return {
        "ok": True,
        "summary": {"completed": completed, "total": len(items)},
        "splits": list(split_counts.values()),
        "items": items,
    }


def parse_byte_range(value: str, file_size: int) -> tuple[int, int]:
    """Parse one HTTP byte range and return an inclusive start/end pair."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (value or "").strip())
    if not match or file_size <= 0:
        raise ValueError("invalid byte range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("empty byte range")
    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid suffix range")
        return max(0, file_size - suffix_length), file_size - 1
    start = int(start_text)
    end = int(end_text) if end_text else file_size - 1
    if start >= file_size or end < start:
        raise ValueError("unsatisfiable byte range")
    return start, min(end, file_size - 1)


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" ._")
    return value[:80] or "未命名变体"


def canvas_size_for_aspect(value: str, fallback_width: int, fallback_height: int) -> tuple[int, int]:
    """Return a stable output canvas for requested batch variant ratios."""
    aspect = (value or "").strip()
    if aspect == "1:1":
        return 1080, 1080
    if aspect == "16:9":
        return 1920, 1080
    if aspect == "9:16":
        return 1080, 1920
    return fallback_width, fallback_height


def cleanup_replay_processes() -> None:
    """Stop stale replay helpers before starting the next recording."""
    subprocess.run(
        ["taskkill", "/F", "/IM", PREVIEW_EXE.name],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*recording_finalizer.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def install_uploaded_library_asset(file_item, asset_kind: str) -> dict:
    """Copy an uploaded image into SharedImageLibrary and register it."""
    if asset_kind not in {"block", "background"}:
        raise ValueError("assetKind must be block or background")
    if not SHARED_LIBRARY.is_dir():
        raise ValueError(f"找不到图片资源库：{SHARED_LIBRARY}")
    filename = safe_name(Path(getattr(file_item, "filename", "") or "asset.png").stem)
    suffix = Path(getattr(file_item, "filename", "") or "").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        suffix = ".png"
    prefix = "custom_block" if asset_kind == "block" else "custom_background"
    resource_id = f"{prefix}_{int(time.time() * 1000)}_{filename}"
    relative_dir = Path("image/block/custom") if asset_kind == "block" else Path("image/ui/background/custom")
    target_dir = SHARED_LIBRARY / relative_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{resource_id}{suffix}"
    with target_path.open("wb") as output:
        while chunk := file_item.file.read(1024 * 1024):
            output.write(chunk)

    manifest_path = SHARED_LIBRARY / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    sprite = str((relative_dir / target_path.name)).replace("\\", "/")
    if asset_kind == "block":
        entry = {
            "id": resource_id,
            "name": filename,
            "sprite": sprite,
            "shadowSprite": "",
            "lightSprite": "",
            "role": "",
            "clearEffectId": "kuai_xiaochu4",
            "clearEffectColor": "lan",
            "blockTipsColor": "lan",
            "effectColorIndex": 0,
            "categoryId": "custom",
        }
        items = manifest.setdefault("normalBlocks", [])
    else:
        entry = {
            "id": resource_id,
            "name": filename,
            "sprite": sprite,
            "shadowSprite": "",
            "lightSprite": "",
            "role": "Background",
            "clearEffectId": "",
            "clearEffectColor": "",
            "blockTipsColor": "",
            "effectColorIndex": 0,
            "categoryId": "custom",
        }
        items = manifest.setdefault("uiSprites", [])
    items[:] = [item for item in items if item.get("id") != resource_id]
    items.append(entry)
    write_json_atomic(manifest_path, manifest)
    return {
        "id": resource_id,
        "name": filename,
        "sprite": sprite,
        "path": str(target_path),
        "kind": asset_kind,
    }


def scaffold_config(name: str, output_dir: Path) -> dict:
    """A valid PreviewConfig-shaped scaffold. A real template is preferred."""
    return {
        "version": 1,
        "levelName": name,
        "gameMode": "Normal",
        "imageLibraryPath": str(SHARED_LIBRARY),
        "theme": {"backgroundId": "ui_background_bg_new_a", "panelId": "ui_panel_qipan", "panelEdgeId": "", "groupPanelId": "", "floorId": "ui_floor_block_di", "fingerId": "ui_finger_1"},
        "presentation": {"playSpeed": 1.0, "showBeginAnimation": True, "useSolidBeginAnimation": False, "beginAnimationColorIndex": 0, "showCombo": True, "showClear": True, "soundEnabled": True},
        "presetMode": {"enabled": False, "switchMode": "ALL_CLEAR"},
        "score": {"enabled": True, "initialScore": 0, "highestScore": 100000, "scoreMultiplier": 1, "showScore": True, "resetScoreOnPresetSwitch": False},
        "recording": {"enabled": True, "startDelay": 0.5, "outputDirectory": str(output_dir)},
        "autoPlay": {"enabled": True, "startDelay": 1.0, "stepDelay": 0.35},
        "randomGroup": {"useFixedGeneratedBlock": False, "fixedGeneratedBlockKind": "Normal", "fixedGeneratedBlockColor": 0, "fixedGeneratedBlockResourceId": "", "fixedGeneratedBlockClearEffectId": "", "fixedGeneratedBlockClearEffectColor": "", "fixedGeneratedBlockTipsColor": ""},
        "presets": [],
        "board": {"rows": 10, "cols": 10, "blocks": []},
        "rounds": [{"groups": [
            {"rows": 3, "cols": 3, "isCheckDie": True, "blocks": [{"row": 0, "col": 0, "kind": "Normal", "colorIndex": 4, "resourceId": "normal_block_green", "clearEffectId": "kuai_xiaochu6", "clearEffectColor": "lv", "blockTipsColor": "lv"}, {"row": 1, "col": 0, "kind": "Normal", "colorIndex": 4, "resourceId": "normal_block_green", "clearEffectId": "kuai_xiaochu6", "clearEffectColor": "lv", "blockTipsColor": "lv"}]},
            {"rows": 3, "cols": 3, "isCheckDie": True, "blocks": [{"row": 0, "col": 0, "kind": "Normal", "colorIndex": 1, "resourceId": "normal_block_purple", "clearEffectId": "kuai_xiaochu6", "clearEffectColor": "zi", "blockTipsColor": "zi"}, {"row": 0, "col": 1, "kind": "Normal", "colorIndex": 1, "resourceId": "normal_block_purple", "clearEffectId": "kuai_xiaochu6", "clearEffectColor": "zi", "blockTipsColor": "zi"}]},
            {"rows": 3, "cols": 3, "isCheckDie": True, "blocks": [{"row": 0, "col": 0, "kind": "Normal", "colorIndex": 6, "resourceId": "normal_block_yellow", "clearEffectId": "kuai_xiaochu6", "clearEffectColor": "huang", "blockTipsColor": "huang"}]}
        ]}],
    }


def validate_template_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise ValueError("基准关卡 JSON 顶层必须是对象")
    board = config.get("board") or {}
    rows, cols = board.get("rows"), board.get("cols")
    if not isinstance(rows, int) or not isinstance(cols, int) or not (3 <= rows <= 20 and 3 <= cols <= 20):
        raise ValueError("基准关卡棋盘行列无效")
    for block in board.get("blocks") or []:
        row, col = block.get("row"), block.get("col")
        if not isinstance(row, int) or not isinstance(col, int) or not (0 <= row < rows and 0 <= col < cols):
            raise ValueError("基准关卡包含越界棋盘方块")
    rounds = config.get("rounds") or []
    if not rounds:
        raise ValueError("基准关卡没有出块轮次")
    for round_index, round_data in enumerate(rounds, 1):
        groups = round_data.get("groups") or []
        if len(groups) != 3:
            raise ValueError(f"基准关卡第 {round_index} 轮必须正好包含左、中、右三个出块槽位")
        for group in groups:
            for block in group.get("blocks") or []:
                if int(block.get("row", -1)) < 0 or int(block.get("col", -1)) < 0:
                    raise ValueError(f"基准关卡第 {round_index} 轮包含无效方块坐标")


def prepare_config(template_path: str, variant: dict, output_dir: Path) -> tuple[dict, str]:
    source = "scaffold"
    if template_path:
        candidate = Path(template_path)
        if not candidate.is_file():
            raise ValueError(f"找不到基准关卡 JSON：{candidate}")
        try:
            config = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"基准关卡不是有效 JSON：{exc}") from exc
        validate_template_config(config)
        source = str(candidate)
    else:
        config = scaffold_config(variant["variantName"], output_dir)

    config = copy.deepcopy(config)
    config["levelName"] = variant["variantName"]
    config.setdefault("recording", {})
    config["recording"].update({"enabled": True, "startDelay": 0.5, "outputDirectory": str(output_dir)})
    config.setdefault("autoPlay", {})
    config["autoPlay"].update({"enabled": True, "startDelay": 1.0, "stepDelay": 0.35})
    return config, source


def block_for_color(hue: float) -> dict:
    choices = [
        (0, "normal_block_red", 2, "hong"), (12, "normal_block_orange", 3, "cheng"),
        (28, "normal_block_yellow", 6, "huang"), (58, "normal_block_green", 4, "lv"),
        (88, "normal_block_cyan", 4, "qing"), (112, "normal_block_blue", 0, "lan"),
        (138, "normal_block_purple", 1, "zi"), (165, "normal_block_pink", 7, "fen"),
    ]
    hue = hue % 180
    _, resource_id, color_index, effect_color = min(choices, key=lambda item: min(abs(hue - item[0]), 180 - abs(hue - item[0])))
    return {"kind": "Normal", "colorIndex": color_index, "resourceId": resource_id, "clearEffectId": "kuai_xiaochu6", "clearEffectColor": effect_color, "blockTipsColor": effect_color}


def preview_block(row: int, col: int, hue: float) -> dict:
    data = block_for_color(hue)
    data.update({"row": row, "col": col})
    return data


def board_config_from_state(state: list[list[int | None]]) -> dict:
    return {
        "rows": len(state),
        "cols": len(state[0]) if state else 0,
        "blocks": [
            preview_block(row, col, hue)
            for row, line in enumerate(state)
            for col, hue in enumerate(line)
            if hue is not None
        ],
    }


def group_config(group: dict | None) -> dict:
    if group is None:
        return {"rows": 1, "cols": 1, "isCheckDie": True, "blocks": []}
    return {
        "rows": group["rows"],
        "cols": group["cols"],
        "isCheckDie": True,
        "blocks": [preview_block(cell["row"], cell["col"], cell["hue"]) for cell in group["shape"]],
    }


def _shape_signature(group: dict | None):
    if group is None:
        return None
    return tuple((cell["row"], cell["col"]) for cell in group["shape"])


def rounds_for_segment(initial_groups: list[dict | None], events: list[dict]) -> list[dict]:
    current = copy.deepcopy(initial_groups)
    consumed = set()
    rounds = []
    for event in events:
        slot = event["sourceSlot"]
        event_group = event["group"]
        if slot in consumed or (
            current[slot] is not None
            and _shape_signature(current[slot]) != _shape_signature(event_group)
        ):
            rounds.append({"groups": [group_config(group) for group in current]})
            current = [None, None, None]
            consumed = set()
        current[slot] = event_group
        consumed.add(slot)
    if any(group is not None for group in current):
        rounds.append({"groups": [group_config(group) for group in current]})
    return rounds


def presets_from_timeline(timeline: dict) -> list[dict]:
    events = timeline.get("events", [])
    states = {state["stateIndex"]: state for state in timeline.get("stableStates", [])}
    if not events:
        return []
    segments = []
    active = []
    preset_breaks = preset_break_steps(timeline)
    for event in events:
        if active and event["stepIndex"] in preset_breaks:
            segments.append(active)
            active = []
        active.append(event)
    if active:
        segments.append(active)

    presets = []
    for index, segment in enumerate(segments, start=1):
        start_state = states[segment[0]["sourceStateIndex"]]
        presets.append({
            "name": f"source_preset_{index}",
            "board": board_config_from_state(start_state["board"]),
            "rounds": rounds_for_segment(start_state["groups"], segment),
        })
    return presets


def rounds_from_replay_candidates(candidates: list[dict]) -> list[dict]:
    events = [
        {
            "sourceSlot": candidate["sourceSlot"],
            "group": {
                "shape": candidate["shape"],
                "rows": max(cell["row"] for cell in candidate["shape"]) + 1,
                "cols": max(cell["col"] for cell in candidate["shape"]) + 1,
            },
        }
        for candidate in candidates
    ]
    return rounds_for_segment([None, None, None], events)


def filled_count(board: list[list[object | None]]) -> int:
    return sum(cell is not None for row in board for cell in row)


def board_bits(board: list[list[object | None]]) -> str:
    return "/".join(
        "".join("1" if cell is not None else "0" for cell in row)
        for row in board
    )


def clear_completed_lines(board: list[list[object | None]]) -> list[list[object | None]]:
    result = copy.deepcopy(board)
    rows = {index for index, row in enumerate(board) if row and all(cell is not None for cell in row)}
    cols = {
        col
        for col in range(len(board[0]) if board else 0)
        if all(board[row][col] is not None for row in range(len(board)))
    }
    for row in range(len(result)):
        for col in range(len(result[row])):
            if row in rows or col in cols:
                result[row][col] = None
    return result


def same_occupancy(left: list[list[object | None]], right: list[list[object | None]]) -> bool:
    return board_bits(left) == board_bits(right)


def preset_break_steps(timeline: dict) -> set[int]:
    events = timeline.get("events", [])
    transitions = timeline.get("transitions", [])
    breaks = set()
    for previous, current in zip(events, events[1:]):
        if any(
            transition.get("type") == "preset_load"
            and transition.get("fromState", -1) >= previous["targetStateIndex"]
            and transition.get("toState", 10**9) <= current["sourceStateIndex"]
            for transition in transitions
        ):
            breaks.add(current["stepIndex"])
    return breaks


def build_replay_truth(timeline: dict, source_video: str) -> dict:
    events = timeline.get("events", [])
    states = {state["stateIndex"]: state for state in timeline.get("stableStates", [])}
    if not events:
        raise ValueError("没有识别到可回放的放置步骤")

    steps = []
    transitions = []
    preset_breaks = preset_break_steps(timeline)
    for index, event in enumerate(events):
        next_source_index = (
            events[index + 1]["sourceStateIndex"]
            if index + 1 < len(events)
            else event["targetStateIndex"]
        )
        next_source_board = states[next_source_index]["board"]
        after_placement = event["afterBoard"]
        completed = bool(event.get("clearedRows") or event.get("clearedCols"))
        resolved_board = clear_completed_lines(after_placement) if completed else after_placement
        is_final_step = index == len(events) - 1
        clear_detection = completed and (
            filled_count(next_source_board) < filled_count(after_placement)
            or is_final_step
        )
        if not clear_detection:
            resolved_board = after_placement

        next_step_index = events[index + 1]["stepIndex"] if index + 1 < len(events) else None
        if next_step_index in preset_breaks:
            transitions.append(
                {
                    "afterStepIndex": event["stepIndex"],
                    "presetStateIndex": next_source_index,
                    "switchMode": "CustomBlock",
                    "expectedBoard": next_source_board,
                    "resolvedBoardBeforePreset": resolved_board,
                }
            )

        steps.append(
            {
                "stepIndex": event["stepIndex"],
                "originalStepIndex": event["stepIndex"],
                "sourceStateIndex": event.get("sourceStateIndex"),
                "targetStateIndex": event.get("targetStateIndex"),
                "roundIndex": (event["stepIndex"] - 1) // 3 + 1,
                "sourceSlot": event["sourceSlot"],
                "target": event["target"],
                "shape": event["group"]["shape"],
                "executeAt": event["time"],
                "clearDetection": clear_detection,
                "completedRowsAfterPlacement": event.get("clearedRows", []),
                "completedColsAfterPlacement": event.get("clearedCols", []),
                "expectedBoardBefore": event["beforeBoard"],
                "expectedBoardAfterPlacement": after_placement,
                "expectedBoardAfterResolution": resolved_board,
                "expectedBoardAtNextAction": next_source_board,
                "sourceStateIndex": event["sourceStateIndex"],
                "targetStateIndex": event["targetStateIndex"],
            }
        )

    return {
        "schemaVersion": 1,
        "sourceVideo": source_video,
        "grid": timeline["grid"],
        "sourceFrameRate": timeline.get("sourceFrameRate", 30.0),
        "initialBoard": states[events[0]["sourceStateIndex"]]["board"],
        "stepCount": len(steps),
        "steps": steps,
        "observedPresetTransitions": transitions,
    }


def board_added_cells(before_board: list, after_board: list) -> set[tuple[int, int]]:
    added = set()
    if not before_board or not after_board:
        return added
    for row, line in enumerate(after_board):
        for col, after in enumerate(line):
            before = before_board[row][col] if row < len(before_board) and col < len(before_board[row]) else None
            if before is None and after is not None:
                added.add((row, col))
    return added


def realigned_event_target(event: dict, rows: int, cols: int) -> dict:
    shape = event.get("group", {}).get("shape") or []
    current = event.get("target") or {"row": 0, "col": 0}
    if not shape:
        return current
    max_row = max(int(cell.get("row", 0)) for cell in shape)
    max_col = max(int(cell.get("col", 0)) for cell in shape)
    if rows <= max_row or cols <= max_col:
        return current
    before = event.get("beforeBoard") or []
    added = board_added_cells(before, event.get("afterBoard") or [])
    best = None
    for target_row in range(rows - max_row):
        for target_col in range(cols - max_col):
            placed = {
                (target_row + int(cell.get("row", 0)), target_col + int(cell.get("col", 0)))
                for cell in shape
            }
            overlaps = sum(
                1
                for row, col in placed
                if row < len(before)
                and col < len(before[row])
                and before[row][col] is not None
            )
            covered = len(placed & added)
            missing_added = max(0, len(added) - covered)
            extra_placed = max(0, len(placed) - covered)
            distance = abs(target_row - int(current.get("row", 0))) + abs(target_col - int(current.get("col", 0)))
            score = overlaps * 1000 + missing_added * 12 + extra_placed * 3 + distance
            candidate = (score, -covered, target_row, target_col)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        return current
    target = {"row": best[2], "col": best[3]}
    current_cells = {
        (int(current.get("row", 0)) + int(cell.get("row", 0)), int(current.get("col", 0)) + int(cell.get("col", 0)))
        for cell in shape
    }
    current_out = [
        cell for cell in current_cells if cell[0] < 0 or cell[1] < 0 or cell[0] >= rows or cell[1] >= cols
    ]
    if current_out or added:
        return target
    return current


def build_action_review(timeline: dict) -> list[dict]:
    events = timeline.get("events", [])
    states = {state["stateIndex"]: state for state in timeline.get("stableStates", [])}
    rows = int((timeline.get("grid") or {}).get("rows") or 0)
    cols = int((timeline.get("grid") or {}).get("cols") or 0)
    preset_breaks = preset_break_steps(timeline)
    effect_driven = timeline.get("recognitionStrategy") == "reverse_clear_v1"
    review = []
    for index, event in enumerate(events):
        if (
            timeline.get("recognitionStrategy") == "color_block_v1"
            and "opening_clear_effect_transition" in (event.get("candidateReasons") or [])
            and not event.get("placedCells")
        ):
            continue
        completed = bool(event.get("clearedRows") or event.get("clearedCols"))
        clear_effect = event.get("clearEffectEvidence")
        if effect_driven and clear_effect:
            clear_state = "on"
            clear_evidence = "已从连续视频帧识别到整行或整列消除特效"
        elif effect_driven and not completed:
            clear_state = "off"
            clear_evidence = "本步没有形成完整行列"
        elif effect_driven and index + 1 < len(events):
            next_board = states[events[index + 1]["sourceStateIndex"]]["board"]
            rows_persist = all(
                all(next_board[row][col] is not None for col in range(len(next_board[row])))
                for row in event.get("clearedRows", [])
            )
            cols_persist = all(
                all(next_board[row][col] is not None for row in range(len(next_board)))
                for col in event.get("clearedCols", [])
            )
            if rows_persist and cols_persist:
                clear_state = "off"
                clear_evidence = "完整行列保留到下一动作，未发生消除"
            else:
                clear_state = "unknown"
                clear_evidence = "未捕获到明确消除特效，需要人工核对"
        elif effect_driven:
            clear_state = "unknown"
            clear_evidence = "最后一步未捕获到明确消除特效，需要人工核对"
        elif event.get("clearMode") == "unknown":
            clear_state = "unknown"
            clear_evidence = "放置与消失同时发生，需要确认完整形状和消除状态"
        elif event.get("clearMode") == "immediate":
            clear_state = "on"
            clear_evidence = "放置瞬间形成完整行列，且后续棋盘已移除对应方块"
        elif not completed:
            clear_state = "off"
            clear_evidence = "本步没有形成完整行列"
        elif index + 1 < len(events) and events[index + 1]["stepIndex"] in preset_breaks:
            clear_state = "unknown"
            clear_evidence = "本步之后发生棋盘切换，不能用下一盘状态推断本步是否消除"
        elif index + 1 < len(events):
            next_board = states[events[index + 1]["sourceStateIndex"]]["board"]
            if filled_count(next_board) < filled_count(event["afterBoard"]):
                clear_state = "on"
                clear_evidence = "后续稳定棋盘已减少"
            else:
                clear_state = "off"
                clear_evidence = "完整行列持续保留到下一步"
        else:
            clear_state = "unknown"
            clear_evidence = "最后一步缺少后续稳定棋盘，需要人工确认"
        target = event.get("target") or {"row": 0, "col": 0}
        if timeline.get("recognitionStrategy") != "color_block_v1":
            target = realigned_event_target(event, rows, cols)
        else:
            shape = event.get("group", {}).get("shape") or []
            out_of_bounds = any(
                int(target.get("row", 0)) + int(cell.get("row", 0)) < 0
                or int(target.get("col", 0)) + int(cell.get("col", 0)) < 0
                or int(target.get("row", 0)) + int(cell.get("row", 0)) >= rows
                or int(target.get("col", 0)) + int(cell.get("col", 0)) >= cols
                for cell in shape
            )
            if out_of_bounds:
                target = realigned_event_target(event, rows, cols)
        if target != event.get("target"):
            event.setdefault("candidateReasons", []).append("target_realigned_to_observed_board_delta")
        review.append(
            {
                "stepIndex": event["stepIndex"],
                "originalStepIndex": event["stepIndex"],
                "sourceEventIndex": index,
                "time": round(float(event.get("time", 0.0) or 0.0), 3),
                "startTime": round(float(event.get("time", 0.0) or 0.0), 3),
                "sourceStateIndex": event.get("sourceStateIndex"),
                "targetStateIndex": event.get("targetStateIndex"),
                "resetBefore": event["stepIndex"] in preset_breaks,
                "sourceSlot": event["sourceSlot"],
                "target": target,
                "shape": [
                    {"row": cell["row"], "col": cell["col"]}
                    for cell in event["group"]["shape"]
                ],
                "beforeBoard": event.get("beforeBoard", []),
                "recognizedAfterBoard": event.get("afterBoard", []),
                "clearedRows": event.get("clearedRows", []),
                "clearedCols": event.get("clearedCols", []),
                "clearState": clear_state,
                "clearEvidence": clear_evidence,
                "clearEffectEvidence": event.get("clearEffectEvidence"),
                "confidence": event.get("confidence", "待确认"),
                "candidateReasons": event.get("candidateReasons", []),
                "candidateSolutions": event.get("candidateSolutions", []),
                "candidateCount": len(event.get("candidateSolutions", [])),
                "requiresConfirmation": event.get("confidence") != "verified" or event.get("sourceSlot", -1) < 0 or clear_state == "unknown",
                "framePath": event.get("framePath", ""),
                "evidenceFrames": event.get("evidenceFrames", {}),
                "evidenceTimes": event.get("evidenceTimes", {}),
                "timeRanges": event.get("timeRanges", {}),
                "annotationNotes": event.get("annotationNotes", ""),
            }
        )
    return review


def backfill_annotation_timing(analysis: dict) -> None:
    """Populate annotation timing for drafts created before time ranges existed."""
    actions = analysis.get("reviewActions") or []
    duration = max(0.0, float(analysis.get("duration") or 0.0))
    for action in actions:
        frames = action.get("evidenceFrames") or {}
        times = dict(action.get("evidenceTimes") or {})
        for key, path in frames.items():
            if times.get(key) is not None or not path:
                continue
            match = re.search(r"_([0-9]+(?:\.[0-9]+)?)s\.[^.]+$", str(path))
            if match:
                times[key] = round(float(match.group(1)), 3)
        action["evidenceTimes"] = times
        if action.get("timeRanges"):
            continue
        before = times.get("before")
        drag = times.get("action")
        placed = times.get("placed")
        cleared = times.get("cleared")
        if placed is None:
            continue
        before = placed if before is None else before
        drag = placed if drag is None else drag
        action["timeRanges"] = {
            "before": {"start": round(max(0.0, before - 0.12), 3), "end": round(before, 3)},
            "drag": {"start": round(min(drag, placed), 3), "end": round(max(drag, placed), 3)},
            "placed": {"start": round(placed, 3), "end": round(min(duration or placed + 0.12, placed + 0.12), 3)},
            "clear": {
                "start": round(cleared, 3) if cleared is not None else None,
                "end": round(min(duration or cleared + 0.4, cleared + 0.4), 3) if cleared is not None else None,
            },
        }


def attach_reference_boards(timeline: dict, reference_interactions: list) -> list:
    """Backfill the board visible before a cancelled drag for old tasks."""
    states = {
        int(state.get("stateIndex", index)): state
        for index, state in enumerate(timeline.get("stableStates") or [])
    }
    enriched = copy.deepcopy(reference_interactions or [])
    for interaction in enriched:
        if interaction.get("boardBefore"):
            continue
        source_state = states.get(int(interaction.get("sourceStateIndex", -1)))
        if source_state and source_state.get("board"):
            interaction["boardBefore"] = copy.deepcopy(source_state["board"])
    return enriched


def apply_confirmed_actions(
    timeline: dict,
    actions: list[dict],
    source_video: str,
    reference_interactions: list[dict] | None = None,
) -> dict:
    original_events = copy.deepcopy(timeline.get("recognizedEvents") or timeline.get("events", []))
    original_candidates = copy.deepcopy(
        timeline.get("recognizedReplayCandidates") or timeline.get("replayCandidates", [])
    )
    if not original_events:
        raise ValueError("识别时间线中没有可作为基准的动作")
    timeline.setdefault("recognizedEvents", copy.deepcopy(original_events))
    timeline.setdefault("recognizedReplayCandidates", copy.deepcopy(original_candidates))
    recognition_timeline = copy.deepcopy(timeline)
    recognition_timeline["events"] = copy.deepcopy(original_events)
    recognition_timeline["replayCandidates"] = copy.deepcopy(original_candidates)
    automatic_truth = build_replay_truth(recognition_timeline, source_video)
    actions = [copy.deepcopy(action) for action in actions if not action.get("deleted")]
    if not actions:
        raise ValueError("不能删除全部动作步骤")

    events = []
    filtered_candidates = []
    action_sources: list[int | None] = []
    for position, action in enumerate(actions):
        manual_added = bool(action.get("manualAdded"))
        if manual_added:
            previous_source = next((source for source in reversed(action_sources) if source is not None), None)
            fallback_index = previous_source if previous_source is not None else min(position, len(original_events) - 1)
            event = copy.deepcopy(original_events[fallback_index])
            candidate = copy.deepcopy(original_candidates[fallback_index])
            placed_range = (action.get("timeRanges") or {}).get("placed") or {}
            execute_at = placed_range.get("start")
            if execute_at is None:
                execute_at = (action.get("evidenceTimes") or {}).get("placed")
            event["time"] = float(execute_at if execute_at is not None else event.get("time", position + 1))
            event["manualAdded"] = True
            candidate["manualAdded"] = True
            source_index = None
        else:
            raw_source = action.get("sourceEventIndex")
            if raw_source is None:
                raw_source = int(action.get("originalStepIndex", position + 1)) - 1
            source_index = int(raw_source)
            if not (0 <= source_index < len(original_events)):
                raise ValueError(f"步骤{position + 1}找不到对应的原始识别动作")
            event = copy.deepcopy(original_events[source_index])
            candidate = copy.deepcopy(original_candidates[source_index])
        events.append(event)
        filtered_candidates.append(candidate)
        action_sources.append(source_index)

    for step_index, (event, action, candidate) in enumerate(zip(events, actions, filtered_candidates), 1):
        event["originalStepIndex"] = event.get("originalStepIndex", event.get("stepIndex"))
        event["stepIndex"] = step_index
        action["stepIndex"] = step_index
        candidate["stepIndex"] = step_index
    timeline["events"] = events
    timeline["replayCandidates"] = filtered_candidates
    rows, cols = timeline["grid"]["rows"], timeline["grid"]["cols"]
    states = {state["stateIndex"]: state for state in timeline.get("stableStates", [])}
    transitions = []
    for transition in automatic_truth.get("observedPresetTransitions", []):
        original_after = int(transition["afterStepIndex"]) - 1
        mapped_step = next(
            (step for step, source in reversed(list(enumerate(action_sources, 1))) if source == original_after),
            None,
        )
        if mapped_step is None:
            continue
        mapped = copy.deepcopy(transition)
        mapped["afterStepIndex"] = mapped_step
        transitions.append(mapped)
    transitions_by_step = {item["afterStepIndex"]: item for item in transitions}

    manual_boards: dict[int, list[list[object | None]]] = {}
    for step_index, action in enumerate(actions, 1):
        raw_board = action.get("manualBeforeBoard")
        if raw_board is None:
            continue
        if not isinstance(raw_board, list) or len(raw_board) != rows:
            raise ValueError(f"步骤{step_index}的人工棋盘行数不正确")
        normalized_board = []
        for row in raw_board:
            if not isinstance(row, list) or len(row) != cols:
                raise ValueError(f"步骤{step_index}的人工棋盘列数不正确")
            normalized_board.append([copy.deepcopy(cell) if cell is not None else None for cell in row])
        manual_boards[step_index] = normalized_board

    for step_index, board in manual_boards.items():
        if step_index == 1:
            continue
        after_step = step_index - 1
        transition = transitions_by_step.get(after_step, {"afterStepIndex": after_step, "switchMode": "CustomBlock"})
        transition.update({"expectedBoard": copy.deepcopy(board), "manualBoardCorrection": True})
        transitions_by_step[after_step] = transition
    transitions = [transitions_by_step[key] for key in sorted(transitions_by_step)]

    initial_board = copy.deepcopy(manual_boards.get(1, automatic_truth["initialBoard"]))
    current_board = copy.deepcopy(initial_board)
    confirmed_steps = []

    for index, (event, action) in enumerate(zip(events, actions)):
        step_index = index + 1
        if step_index in manual_boards:
            current_board = copy.deepcopy(manual_boards[step_index])
        if int(action.get("stepIndex", step_index)) != step_index:
            raise ValueError(f"步骤编号不连续：第 {step_index} 项")
        source_slot = int(action.get("sourceSlot", -1))
        if source_slot not in (0, 1, 2):
            raise ValueError(f"步骤{step_index}的出块槽位无效")
        target = action.get("target") or {}
        target_row, target_col = int(target.get("row", -1)), int(target.get("col", -1))
        raw_shape = action.get("shape") or []
        shape_cells = sorted({(int(cell["row"]), int(cell["col"])) for cell in raw_shape})
        if not shape_cells:
            raise ValueError(f"步骤{step_index}的方块形状不能为空")
        if min(row for row, _ in shape_cells) < 0 or min(col for _, col in shape_cells) < 0:
            raise ValueError(f"步骤{step_index}的形状坐标无效")
        clear_state = action.get("clearState")
        if clear_state not in ("on", "off"):
            raise ValueError(f"步骤{step_index}的消除状态尚未确认")

        before_board = copy.deepcopy(current_board)
        after_placement = copy.deepcopy(current_board)
        original_shape = event["group"].get("shape", [])
        hue = original_shape[0].get("hue", 60) if original_shape else 60
        placed = []
        for shape_row, shape_col in shape_cells:
            row, col = target_row + shape_row, target_col + shape_col
            if not (0 <= row < rows and 0 <= col < cols):
                raise ValueError(f"步骤{step_index}的方块超出棋盘")
            if after_placement[row][col] is not None:
                raise ValueError(f"步骤{step_index}落点与已有方块重叠：({row},{col})")
            after_placement[row][col] = hue
            placed.append({"row": row, "col": col})

        completed_rows = [row for row in range(rows) if all(cell is not None for cell in after_placement[row])]
        completed_cols = [col for col in range(cols) if all(after_placement[row][col] is not None for row in range(rows))]
        if clear_state == "on" and not (completed_rows or completed_cols):
            raise ValueError(f"步骤{step_index}启用了消除，但没有形成完整行列")
        resolved_board = clear_completed_lines(after_placement) if clear_state == "on" else copy.deepcopy(after_placement)

        shape = [{"row": row, "col": col, "hue": hue} for row, col in shape_cells]
        group = {
            "slot": source_slot,
            "shape": shape,
            "rows": max(row for row, _ in shape_cells) + 1,
            "cols": max(col for _, col in shape_cells) + 1,
            "cellCount": len(shape_cells),
        }
        event.update(
            {
                "sourceSlot": source_slot,
                "group": group,
                "target": {"row": target_row, "col": target_col},
                "placedCells": placed,
                "clearedRows": completed_rows,
                "clearedCols": completed_cols,
                "clearMode": "immediate" if clear_state == "on" else ("deferred" if completed_rows or completed_cols else "none"),
                "beforeBoard": before_board,
                "afterBoard": after_placement,
                "confidence": "confirmed",
                "verification": "manual_review_and_rule_simulation",
            }
        )
        timeline["replayCandidates"][index].update(
            {
                "sourceSlot": source_slot,
                "target": {"row": target_row, "col": target_col},
                "shape": shape,
                "cellCount": len(shape),
                "clearedRows": completed_rows,
                "clearedCols": completed_cols,
                "clearMode": event["clearMode"],
                "confidence": "confirmed",
            }
        )
        transition = transitions_by_step.get(step_index)
        expected_board_at_next_action = copy.deepcopy(transition["expectedBoard"]) if transition else copy.deepcopy(resolved_board)
        confirmed_steps.append(
            {
                "stepIndex": step_index,
                "roundIndex": (step_index - 1) // 3 + 1,
                "sourceSlot": source_slot,
                "target": {"row": target_row, "col": target_col},
                "shape": shape,
                "executeAt": event["time"],
                "clearDetection": clear_state == "on",
                "clearDecisionSource": "manual_confirmation",
                "completedRowsAfterPlacement": completed_rows,
                "completedColsAfterPlacement": completed_cols,
                "expectedBoardBefore": before_board,
                "expectedBoardAfterPlacement": after_placement,
                "expectedBoardAfterResolution": resolved_board,
                "expectedBoardAtNextAction": expected_board_at_next_action,
                "sourceStateIndex": event["sourceStateIndex"],
                "targetStateIndex": event["targetStateIndex"],
                "timeRanges": copy.deepcopy(action.get("timeRanges") or {}),
                "annotationNotes": str(action.get("annotationNotes") or "").strip(),
                "beforeBoardSource": "manual_correction" if step_index in manual_boards else "rule_simulation",
                "manualAdded": bool(action.get("manualAdded")),
            }
        )
        current_board = resolved_board
        if transition:
            transition["resolvedBoardBeforePreset"] = copy.deepcopy(resolved_board)
            current_board = copy.deepcopy(transition["expectedBoard"])

    raw_reference_interactions = (
        reference_interactions
        if reference_interactions is not None
        else timeline.get("referenceInteractions") or timeline.get("cancelledDrags") or []
    )
    confirmed_reference_interactions = []
    for reference_index, raw_interaction in enumerate(raw_reference_interactions, 1):
        if raw_interaction.get("deleted"):
            continue
        interaction = copy.deepcopy(raw_interaction)
        if interaction.get("type") != "cancelled_drag":
            raise ValueError(f"撤回交互 {reference_index} 的类型无效")
        source_slot = int(interaction.get("sourceSlot", -1))
        if source_slot not in (0, 1, 2):
            raise ValueError(f"撤回交互 {reference_index} 的来源槽位尚未确认")
        shape_cells = sorted({(int(cell["row"]), int(cell["col"])) for cell in interaction.get("shape") or []})
        if not shape_cells:
            raise ValueError(f"撤回交互 {reference_index} 的方块形状不能为空")
        if min(row for row, _ in shape_cells) < 0 or min(col for _, col in shape_cells) < 0:
            raise ValueError(f"撤回交互 {reference_index} 的方块形状坐标无效")
        hover_target = interaction.get("hoverTarget") or {}
        hover_row = int(hover_target.get("row", -1))
        hover_col = int(hover_target.get("col", -1))
        for shape_row, shape_col in shape_cells:
            if not (0 <= hover_row + shape_row < rows and 0 <= hover_col + shape_col < cols):
                raise ValueError(f"撤回交互 {reference_index} 的悬停位置超出棋盘")
        start_time = float(interaction.get("startTime", -1))
        end_time = float(interaction.get("endTime", -1))
        if start_time < 0 or end_time <= start_time:
            raise ValueError(f"撤回交互 {reference_index} 的起止时间无效")
        if not interaction.get("manuallyVerified"):
            raise ValueError(f"撤回交互 {reference_index} 尚未人工确认")
        normalized_hover_passes = []
        for pass_index, raw_pass in enumerate(interaction.get("hoverPasses") or [], 1):
            pass_target = raw_pass.get("target") or {}
            pass_row = int(pass_target.get("row", -1))
            pass_col = int(pass_target.get("col", -1))
            for shape_row, shape_col in shape_cells:
                if not (0 <= pass_row + shape_row < rows and 0 <= pass_col + shape_col < cols):
                    raise ValueError(f"撤回交互 {reference_index} 第 {pass_index} 次悬停位置超出棋盘")
            pass_start = float(raw_pass.get("startTime", -1))
            pass_end = float(raw_pass.get("endTime", -1))
            if pass_start < start_time or pass_end <= pass_start:
                raise ValueError(f"撤回交互 {reference_index} 第 {pass_index} 次悬停时间无效")
            if pass_end > end_time:
                raise ValueError(f"撤回交互 {reference_index} 的完全归位时间早于最后一次悬停结束")
            normalized_hover_passes.append(
                {
                    "passIndex": pass_index,
                    "startTime": round(pass_start, 3),
                    "endTime": round(pass_end, 3),
                    "target": {"row": pass_row, "col": pass_col},
                    "previewClearedRows": sorted({int(value) for value in raw_pass.get("previewClearedRows") or []}),
                    "previewClearedCols": sorted({int(value) for value in raw_pass.get("previewClearedCols") or []}),
                }
            )
        if normalized_hover_passes and end_time < max(item["endTime"] for item in normalized_hover_passes):
            raise ValueError(f"撤回交互 {reference_index} 的完全归位时间早于最后一次悬停结束")
        interaction.update(
            {
                "referenceIndex": len(confirmed_reference_interactions) + 1,
                "sourceSlot": source_slot,
                "shape": [{"row": row, "col": col} for row, col in shape_cells],
                "hoverTarget": {"row": hover_row, "col": hover_col},
                "startTime": round(start_time, 3),
                "endTime": round(end_time, 3),
                "returnCompleteTime": round(end_time, 3),
                "duration": round(end_time - start_time, 3),
                "returnedToSource": True,
                "boardMutation": False,
                "clearExecuted": False,
                "hoverPasses": normalized_hover_passes,
                "manualAdded": bool(interaction.get("manualAdded")),
            }
        )
        confirmed_reference_interactions.append(interaction)

    confirmed_reference_interactions.sort(key=lambda item: float(item["startTime"]))
    for reference_index, interaction in enumerate(confirmed_reference_interactions, 1):
        interaction["referenceIndex"] = reference_index
    for interaction in confirmed_reference_interactions:
        start_time = float(interaction["startTime"])
        prior_steps = [step["stepIndex"] for step in confirmed_steps if float(step["executeAt"]) <= start_time]
        later_steps = [step["stepIndex"] for step in confirmed_steps if float(step["executeAt"]) > start_time]
        interaction["afterStepIndex"] = max(prior_steps, default=0)
        interaction["beforeStepIndex"] = min(later_steps, default=None)
    timeline["referenceInteractions"] = copy.deepcopy(confirmed_reference_interactions)
    timeline["cancelledDrags"] = copy.deepcopy(confirmed_reference_interactions)

    timeline.setdefault("validation", {}).update(
        {
            "readyForAutomaticReplay": True,
            "reason": "all_actions_manually_confirmed_and_rule_simulated",
            "confirmedAt": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return {
        "schemaVersion": 3,
        "sourceVideo": source_video,
        "grid": timeline["grid"],
        "sourceFrameRate": timeline.get("sourceFrameRate", 30.0),
        "initialBoard": initial_board,
        "stepCount": len(confirmed_steps),
        "steps": confirmed_steps,
        "referenceInteractions": copy.deepcopy(confirmed_reference_interactions),
        "observedPresetTransitions": transitions,
        "reviewConfirmedAt": datetime.now().isoformat(timespec="seconds"),
    }


def apply_block_style(config: dict, block_style: str, block_resource_id: str = "") -> None:
    blocks = list(config.get("board", {}).get("blocks", []))
    for round_data in config.get("rounds", []):
        for group in round_data.get("groups", []):
            blocks.extend(group.get("blocks", []))
    if block_resource_id:
        for block in blocks:
            block.update(
                {
                    "kind": "Normal",
                    "resourceId": block_resource_id,
                    "clearEffectId": "kuai_xiaochu4",
                    "clearEffectColor": "lan",
                    "blockTipsColor": "lan",
                }
            )
        return
    if block_style != "jewel":
        return
    for block in blocks:
        block.update(
            {
                "kind": "Jewel",
                "resourceId": f"jewel_block_{block.get('colorIndex', 0) % 6 + 1}_1",
                "clearEffectId": "kuai_xiaochu4",
                "clearEffectColor": "lan",
                "blockTipsColor": "lan",
            }
        )


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def save_action_version(task_dir: Path, kind: str, payload: dict) -> Path:
    version_root = task_dir / "动作脚本版本"
    version_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    version_path = version_root / f"{stamp}_{safe_name(kind)}.json"
    write_json_atomic(
        version_path,
        {
            "savedAt": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "payload": payload,
        },
    )
    return version_path


def find_cached_video_analysis(video_sha256: str, recognition_strategy: str) -> dict | None:
    """Return a previous analysis job without scanning the bulky task tree."""
    if not video_sha256 or not ANALYSIS_CACHE_INDEX.is_file():
        return None
    try:
        index = json.loads(ANALYSIS_CACHE_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = index.get(f"{recognition_strategy}:{video_sha256}")
    if not entry:
        return None
    task_dir = UPLOAD_ROOT / safe_name(entry.get("jobId", ""))
    analysis_path = task_dir / "????.json"
    if not analysis_path.is_file():
        return None
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "ok": True,
        "jobId": task_dir.name,
        "analysis": analysis,
        "taskDirectory": str(task_dir),
        "cached": True,
    }


def remember_cached_video_analysis(video_sha256: str, recognition_strategy: str, task_dir: Path) -> None:
    if not video_sha256:
        return
    try:
        index = json.loads(ANALYSIS_CACHE_INDEX.read_text(encoding="utf-8")) if ANALYSIS_CACHE_INDEX.is_file() else {}
    except (OSError, json.JSONDecodeError):
        index = {}
    index[f"{recognition_strategy}:{video_sha256}"] = {
        "jobId": task_dir.name,
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    write_json_atomic(ANALYSIS_CACHE_INDEX, index)

def prepare_deterministic_client(task_dir: Path, payload: dict) -> dict:
    timeline_path = task_dir / "步骤时间线.json"
    if not timeline_path.is_file():
        raise ValueError("缺少步骤时间线，请先完成视频识别")
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    candidates = timeline.get("replayCandidates", [])
    if not candidates:
        raise ValueError("没有识别到可回放的放置步骤")

    truth_path = task_dir / "确定性回放真值.json"
    if truth_path.is_file():
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
    else:
        truth = build_replay_truth(timeline, str(payload.get("sourceVideo") or ""))
        write_json_atomic(truth_path, truth)

    output_dir = Path(payload.get("outputDirectory") or DEFAULT_VIDEO_ROOT)
    output_dir.mkdir(parents=True, exist_ok=True)
    name = payload.get("name") or f"{task_dir.name}_确定性回放"
    states = {state["stateIndex"]: state for state in timeline.get("stableStates", [])}
    events = timeline.get("events", [])
    transitions = truth.get("observedPresetTransitions", [])
    transitions_by_step = {item["afterStepIndex"]: item for item in transitions}

    config = scaffold_config(name, output_dir)
    config["board"] = board_config_from_state(truth["initialBoard"])
    config["rounds"] = rounds_from_replay_candidates(candidates)
    config["presets"] = []
    config["presetMode"] = {"enabled": False, "switchMode": "ALL_CLEAR"}
    config["autoPlay"] = {"enabled": False, "startDelay": 0.0, "stepDelay": 0.0}
    canvas_width, canvas_height = canvas_size_for_aspect(
        str(payload.get("aspectRatio") or ""),
        1080,
        1920,
    )
    config["recording"] = {
        "enabled": True,
        "startDelay": 0.5,
        "outputDirectory": str(output_dir),
        "width": canvas_width,
        "height": canvas_height,
    }
    config["presentation"].update({"showBeginAnimation": False, "useSolidBeginAnimation": False, "soundEnabled": True, "playSpeed": 1.0})
    config["presentation"].update({"width": canvas_width, "height": canvas_height, "aspectRatio": payload.get("aspectRatio") or ""})
    config["theme"]["backgroundId"] = payload.get("backgroundId") or config["theme"]["backgroundId"]
    apply_block_style(
        config,
        payload.get("blockStyle", "normal"),
        str(payload.get("blockResourceId") or ""),
    )

    runtime_steps = []
    selected = truth["steps"]
    for index, step in enumerate(selected):
        next_time = selected[index + 1]["executeAt"] if index + 1 < len(selected) else step["executeAt"] + 1.0
        expected_board = step.get("expectedBoardAfterResolution")
        transition = transitions_by_step.get(step["stepIndex"])
        if expected_board is None:
            if transition and "emptyStateIndex" in transition:
                expected_board = states[transition["emptyStateIndex"]]["board"]
            elif step["clearDetection"]:
                expected_board = clear_completed_lines(step["expectedBoardAfterPlacement"])
            else:
                expected_board = step["expectedBoardAfterPlacement"]
        runtime_steps.append(
            {
                "stepIndex": step["stepIndex"],
                "sourceSlot": step["sourceSlot"],
                "targetRow": step["target"]["row"],
                "targetCol": step["target"]["col"],
                "clearDetection": step["clearDetection"],
                "delayAfter": max(0.5, round(next_time - step["executeAt"], 3)),
                "expectedFilled": filled_count(expected_board),
                "expectedBits": board_bits(expected_board),
                "shape": [{"row": cell["row"], "col": cell["col"]} for cell in step["shape"]],
            }
        )

    replay_plan = {
        "enabled": True,
        "startDelay": 1.5,
        "defaultStepDelay": 0.5,
        "rows": truth["grid"]["rows"],
        "cols": truth["grid"]["cols"],
        "stopRecordingOnComplete": True,
        "steps": runtime_steps,
        "referenceInteractions": copy.deepcopy(truth.get("referenceInteractions") or []),
        "transitions": [],
    }

    transition_steps = sorted(item["afterStepIndex"] for item in transitions)
    for preset_index, transition in enumerate(transitions, start=2):
        preset_board = transition["expectedBoard"]
        start_step = transition["afterStepIndex"] + 1
        next_boundaries = [value for value in transition_steps if value >= start_step]
        end_step = min(next_boundaries) if next_boundaries else len(candidates)
        segment_candidates = [item for item in candidates if start_step <= item["stepIndex"] <= end_step]
        preset_config = copy.deepcopy(config)
        preset_config["levelName"] = f"{name}_棋盘{preset_index}"
        preset_config["board"] = board_config_from_state(preset_board)
        preset_config["rounds"] = rounds_from_replay_candidates(segment_candidates)
        preset_config.setdefault("recording", {}).update({"width": canvas_width, "height": canvas_height})
        preset_config.setdefault("presentation", {}).update({"width": canvas_width, "height": canvas_height, "aspectRatio": payload.get("aspectRatio") or ""})
        apply_block_style(
            preset_config,
            payload.get("blockStyle", "normal"),
            str(payload.get("blockResourceId") or ""),
        )
        write_json_atomic(STREAMING_ASSETS / f"deterministic_preset_{preset_index}.json", preset_config)
        replay_plan["transitions"].append(
            {
                "afterStepIndex": transition["afterStepIndex"],
                "presetIndex": preset_index,
                "expectedFilled": filled_count(preset_board),
                "expectedBits": board_bits(preset_board),
            }
        )

    write_json_atomic(STREAMING_ASSETS / "preview_config.json", config)
    write_json_atomic(STREAMING_ASSETS / "deterministic_replay.json", replay_plan)
    log_path = STREAMING_ASSETS / "deterministic_replay.log"
    if log_path.exists():
        log_path.unlink()
    task_plan_path = task_dir / "确定性客户端回放计划.json"
    write_json_atomic(task_plan_path, replay_plan)
    return {
        "configPath": str(STREAMING_ASSETS / "preview_config.json"),
        "planPath": str(task_plan_path),
        "runtimePlanPath": str(STREAMING_ASSETS / "deterministic_replay.json"),
        "logPath": str(log_path),
        "videoOutputDirectory": str(output_dir),
        "stepCount": len(runtime_steps),
        "presetCount": len(transitions),
    }


def find_board(frame) -> tuple[int, int, int, int]:
    import cv2
    import numpy as np
    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 45, 140)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h
        ratio = w / max(h, 1)
        cx, cy = x + w / 2, y + h / 2
        # The preview client can stretch an 8x8 board vertically in portrait
        # recordings. Grid periodicity is evaluated separately, so accept the
        # rendered panel's wider aspect range here.
        if area < width * height * 0.08 or not 0.45 <= ratio <= 1.35:
            continue
        if not width * 0.2 <= cx <= width * 0.8 or not height * 0.2 <= cy <= height * 0.78:
            continue
        candidates.append((area, x, y, w, h))
    if candidates:
        _, x, y, w, h = max(candidates)
        rows, cols, _ = detect_grid_size(frame, (x, y, w, h))
        # Compression can connect a board border to a long background edge,
        # producing a contour that touches the frame and spans extra width.
        # Use the independently detected row count to find the matching chain
        # of vertical grid boundaries inside that contour.
        if rows != cols and (x <= 2 or x + w >= width - 2):
            crop = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY).astype(float)
            profile = np.abs(np.diff(crop, axis=1)).mean(axis=0)
            best = None
            count = rows
            for pitch in range(max(12, int(w * 0.4 / count)), int(w / count) + 1):
                span = count * pitch
                for start in range(0, max(1, w - span)):
                    strengths = [
                        np.max(profile[max(0, start + index * pitch - 4):min(
                            len(profile), start + index * pitch + 5
                        )])
                        for index in range(count + 1)
                    ]
                    score = float(np.mean(strengths)) * span / w
                    candidate = (score, start, span)
                    if best is None or candidate > best:
                        best = candidate
            if best is not None and best[0] >= 8.0:
                x += best[1]
                w = best[2]
        return x, y, w, h
    return int(width * 0.05), int(height * 0.25), int(width * 0.9), int(height * 0.52)


def detect_grid_size(frame, board: tuple[int, int, int, int]) -> tuple[int, int, float]:
    """Detect periodic grid boundaries for boards between 6x6 and 12x12."""
    import cv2
    import numpy as np

    x, y, w, h = board
    gray = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY).astype(float)
    grad_x = np.abs(np.diff(gray, axis=1)).mean(axis=0)
    grad_y = np.abs(np.diff(gray, axis=0)).mean(axis=1)

    def best_count(profile, length):
        scores = []
        for count in range(6, 13):
            boundaries = [int(index * length / count) for index in range(1, count)]
            score = float(np.mean([
                np.max(profile[max(0, position - 4):min(len(profile), position + 5)])
                for position in boundaries
            ]))
            scores.append((score, count))
        scores.sort(reverse=True)
        confidence = scores[0][0] / max(scores[1][0], 0.001)
        return scores[0][1], confidence

    cols, col_confidence = best_count(grad_x, w)
    rows, row_confidence = best_count(grad_y, h)
    return rows, cols, round(min(row_confidence, col_confidence), 3)


def find_board_multiframe(video_path: Path) -> tuple[tuple[int, int, int, int], int, int, float, object, list[dict]]:
    """Locate a stable board using several frames and grid-vote consensus."""
    import cv2
    import numpy as np
    from collections import Counter

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError("无法读取该视频文件")
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    ratios = (0.12, 0.28, 0.45, 0.62, 0.78)
    candidates = []
    for ratio in ratios:
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(max(0, total - 1) * ratio))
        ok, frame = capture.read()
        if not ok:
            continue
        height, width = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 35, 120)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        ranked = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area_ratio = (w * h) / max(width * height, 1)
            aspect = w / max(h, 1)
            if not 0.08 <= area_ratio <= 0.68:
                continue
            if not 0.65 <= aspect <= 1.35:
                continue
            if y < height * 0.12 or y + h > height * 0.88:
                continue
            score = w * h * (1.0 - abs(1.0 - aspect) * 0.4)
            ranked.append((score, x, y, w, h))
        if not ranked:
            continue
        _, x, y, w, h = max(ranked)
        rows, cols, grid_confidence = detect_grid_size(frame, (x, y, w, h))
        candidates.append(
            {
                "ratio": ratio,
                "frame": frame,
                "box": (x, y, w, h),
                "rows": rows,
                "cols": cols,
                "gridConfidence": grid_confidence,
            }
        )
    capture.release()
    if len(candidates) < 2:
        raise ValueError("无法从多帧中找到可靠棋盘，请人工框选棋盘并输入行列数")

    grid_votes = Counter((item["rows"], item["cols"]) for item in candidates)
    (rows, cols), vote_count = grid_votes.most_common(1)[0]
    matching = [item for item in candidates if (item["rows"], item["cols"]) == (rows, cols)]
    if vote_count < 2:
        raise ValueError("多帧棋盘规格不一致，请人工输入棋盘行列数")
    boxes = np.array([item["box"] for item in matching], dtype=float)
    median_box = tuple(int(round(value)) for value in np.median(boxes, axis=0))
    deviations = np.max(np.abs(boxes - np.median(boxes, axis=0)), axis=1)
    stable = [item for item, deviation in zip(matching, deviations) if deviation <= max(median_box[2], median_box[3]) * 0.08]
    if len(stable) < 2:
        raise ValueError("多帧棋盘位置不稳定，请人工框选棋盘")
    stable_boxes = np.array([item["box"] for item in stable], dtype=float)
    board = tuple(int(round(value)) for value in np.median(stable_boxes, axis=0))
    confidence = round(float(np.median([item["gridConfidence"] for item in stable])), 3)
    representative = max(stable, key=lambda item: item["gridConfidence"])["frame"]
    diagnostics = [
        {
            "sampleRatio": item["ratio"],
            "box": {"x": item["box"][0], "y": item["box"][1], "width": item["box"][2], "height": item["box"][3]},
            "rows": item["rows"],
            "cols": item["cols"],
            "gridConfidence": item["gridConfidence"],
            "accepted": item in stable,
        }
        for item in candidates
    ]
    return board, rows, cols, confidence, representative, diagnostics


def analyse_video(
    video_path: Path,
    task_dir: Path,
    board_override: dict | None = None,
    recognition_strategy: str = "legacy",
    sample_fps_override: float | None = None,
    experiment_flags=None,
) -> dict:
    import cv2
    import numpy as np
    from recognition_experiments import parse_flags

    experiment_flags = parse_flags(experiment_flags)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError("无法读取该视频文件")
    fps = capture.get(cv2.CAP_PROP_FPS) or 30
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if total else 0
    capture.release()
    if board_override:
        x = int(board_override["x"])
        y = int(board_override["y"])
        w = int(board_override["width"])
        h = int(board_override["height"])
        rows = int(board_override["rows"])
        cols = int(board_override["cols"])
        if min(w, h) < 80 or not (3 <= rows <= 20 and 3 <= cols <= 20):
            raise ValueError("人工棋盘范围或网格规格无效")
        capture = cv2.VideoCapture(str(video_path))
        capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, round(total * 0.35)))
        ok, selected_frame = capture.read()
        capture.release()
        if not ok:
            raise ValueError("无法读取人工校准所需的视频帧")
        frame_height, frame_width = selected_frame.shape[:2]
        if x < 0 or y < 0 or x + w > frame_width or y + h > frame_height:
            raise ValueError("人工棋盘范围超出视频画面")
        grid_confidence = 1.0
        board_candidates = []
        board_source = "manual"
    else:
        (x, y, w, h), rows, cols, grid_confidence, selected_frame, board_candidates = find_board_multiframe(video_path)
        board_source = "automatic_multiframe"
    calibration_path = task_dir / "棋盘校准帧.jpg"
    ok, encoded = cv2.imencode(".jpg", selected_frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if ok:
        encoded.tofile(str(calibration_path))
    selected_state = _board_state(selected_frame, (x, y, w, h), rows, cols)
    blocks = []
    for row in range(rows):
        for col in range(cols):
            hue = selected_state[row][col]
            if hue is None:
                continue
            block = block_for_color(hue)
            block.update({"row": row, "col": col})
            blocks.append(block)
    material_profile = infer_material_profile(video_path)
    effective_recognition_strategy = recognition_strategy
    if recognition_strategy == "legacy":
        if material_profile == "color_block":
            effective_recognition_strategy = "color_block_v1"
        elif material_profile == "image_block":
            effective_recognition_strategy = "material_profile_v1"
    sample_fps = 12.0 if effective_recognition_strategy == "color_block_v1" else 6.0
    if sample_fps_override is not None:
        sample_fps = max(1.0, min(float(sample_fps_override), float(fps or sample_fps)))
    # A placement can complete in far less than 1/8 second. Scan at the
    # source frame rate (up to 30fps) and only collapse frames afterwards.
    timeline = build_timeline(
        video_path,
        (x, y, w, h),
        rows=rows,
        cols=cols,
        sample_fps=min(fps, sample_fps),
        recognition_strategy=effective_recognition_strategy,
        experiment_flags=experiment_flags,
    )
    if timeline["stableStates"]:
        initial_board = board_config_from_state(timeline["stableStates"][0]["board"])
        blocks = initial_board["blocks"]
    action_dir = task_dir / "动作帧"
    if action_dir.exists():
        shutil.rmtree(action_dir)
    action_dir.mkdir()
    capture = cv2.VideoCapture(str(video_path))
    states = {state["stateIndex"]: state for state in timeline.get("stableStates", [])}
    evidence_occupancy_model = load_model() if effective_recognition_strategy == "color_block_v1" else None

    def save_evidence(step_index, kind, second):
        if second is None:
            return ""
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, second) * 1000)
        ok, frame = capture.read()
        if not ok:
            return ""
        path = action_dir / f"step_{step_index:02d}_{kind}_{second:.3f}s.jpg"
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            return ""
        encoded.tofile(str(path))
        return str(path.relative_to(ROOT)).replace("\\", "/")

    def evidence_board(second):
        if second is None:
            return None
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, second) * 1000)
        ok, frame = capture.read()
        if not ok:
            return None
        return _board_state(
            frame,
            (x, y, w, h),
            rows,
            cols,
            occupancy_model=evidence_occupancy_model,
        )

    def board_signature(board):
        if not board:
            return ()
        return tuple(tuple(cell is not None for cell in row) for row in board)

    def choose_placed_time(event, after_state, next_event_time=None, clear_time=None):
        if after_state is None:
            return float(event.get("placedEvidenceTime") or event.get("time") or 0.0)
        start = float(after_state.get("startTime", after_state.get("time", 0.0)) or 0.0)
        end = float(after_state.get("endTime", after_state.get("time", start)) or start)
        if event.get("placedEvidenceTime") is not None:
            start = max(start, float(event["placedEvidenceTime"]))
        hard_end = end
        if next_event_time is not None:
            hard_end = min(hard_end, max(start, float(next_event_time) - 0.08))
        if clear_time is not None:
            hard_end = min(hard_end, max(start, float(clear_time) - 0.08))
        if hard_end < start:
            hard_end = start
        target_signature = board_signature(after_state.get("board"))
        duration = max(0.0, hard_end - start)
        candidates = [
            hard_end,
            start + duration * 0.80,
            start + duration * 0.65,
            start + duration * 0.50,
            start,
        ]
        seen = set()
        for second in candidates:
            second = round(max(start, min(hard_end, second)), 3)
            if second in seen:
                continue
            seen.add(second)
            if board_signature(evidence_board(second)) == target_signature:
                return second
        return round(hard_end, 3)

    for event_index, event in enumerate(timeline["events"]):
        before = states.get(event["sourceStateIndex"])
        after = states.get(event["targetStateIndex"])
        before_time = before["time"] if before else max(0.0, event["time"] - 0.2)
        action_time = round((before_time + event["time"]) / 2.0, 3)
        next_event_time = None
        if event_index + 1 < len(timeline["events"]):
            next_event_time = timeline["events"][event_index + 1]["time"]
        clear_effect = event.get("clearEffectEvidence") or {}
        clear_frame_time = event.get("effectFrameTime")
        clear_time = clear_effect.get("startTime")
        clear_end = clear_effect.get("endTime")
        may_have_immediate_clear = event.get("clearMode") == "immediate" or bool(event.get("removedCells"))
        if may_have_immediate_clear and clear_time is None:
            after_filled = filled_count(after["board"]) if after else None
            for state_index in range(event["targetStateIndex"] + 1, min(event["targetStateIndex"] + 5, len(states))):
                candidate = states.get(state_index)
                if candidate is None:
                    continue
                if next_event_time is not None and candidate["startTime"] >= next_event_time:
                    break
                if after_filled is not None and filled_count(candidate["board"]) < after_filled:
                    clear_time = candidate["time"]
                    clear_frame_time = clear_time
                    break
        after_time = choose_placed_time(event, after, next_event_time, clear_time)
        evidence = {
            "before": save_evidence(event["stepIndex"], "before", before_time),
            "action": save_evidence(event["stepIndex"], "action", action_time),
            "placed": save_evidence(event["stepIndex"], "placed", after_time),
            "cleared": save_evidence(event["stepIndex"], "cleared", clear_frame_time) if clear_frame_time is not None else "",
        }
        before_evidence_board = evidence_board(before_time)
        placed_evidence_board = evidence_board(after_time)
        if before_evidence_board is not None:
            event["beforeBoard"] = before_evidence_board
            event["beforeBoardSource"] = "evidence_frame"
        if placed_evidence_board is not None:
            event["afterBoard"] = placed_evidence_board
            event["afterBoardSource"] = "evidence_frame"
        placed_end = after.get("endTime", after_time) if after else after_time
        if clear_time is not None and clear_end is None:
            clear_end_limit = next_event_time if next_event_time is not None else clear_time + 0.5
            clear_end = max(clear_time, min(clear_end_limit, clear_time + 0.5))
        if clear_time is not None and clear_end is not None and clear_end < clear_time:
            clear_end = clear_time
        evidence_times = {
            "before": round(before_time, 3),
            "action": round(action_time, 3),
            "placed": round(after_time, 3),
            "cleared": round(clear_time, 3) if clear_time is not None else None,
        }
        time_ranges = {
            "before": {
                "start": round(before.get("startTime", before_time), 3) if before else round(max(0.0, before_time - 0.15), 3),
                "end": round(before.get("endTime", action_time), 3) if before else round(action_time, 3),
            },
            "drag": {"start": round(action_time, 3), "end": round(event["time"], 3)},
            "placed": {"start": round(after_time, 3), "end": round(placed_end, 3)},
            "clear": {
                "start": round(clear_time, 3) if clear_time is not None else None,
                "end": round(clear_end, 3) if clear_time is not None else None,
            },
        }
        event["evidenceFrames"] = evidence
        event["evidenceTimes"] = evidence_times
        event["timeRanges"] = time_ranges
        event["framePath"] = evidence["placed"] or evidence["action"] or evidence["before"]
    capture.release()
    timeline_path = task_dir / "步骤时间线.json"
    timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "video": str(video_path),
        "sourceVideoUrl": str(video_path.relative_to(ROOT)).replace("\\", "/"),
        "duration": round(duration, 2), "fps": round(fps, 2),
        "board": {"x": x, "y": y, "width": w, "height": h, "rows": rows, "cols": cols, "gridConfidence": grid_confidence, "source": board_source, "candidates": board_candidates, "detectedBlocks": blocks, "calibrationFrame": str(calibration_path.relative_to(ROOT)).replace("\\", "/")},
        "timeline": {"path": str(timeline_path), "recognitionVersion": timeline.get("recognitionVersion"), "sampleRate": timeline["sampleRate"], "sampleCount": timeline["sampleCount"], "eventCount": len(timeline["events"]), "events": timeline["events"], "referenceInteractions": timeline.get("referenceInteractions") or timeline.get("cancelledDrags") or [], "validation": timeline["validation"]},
        "confidence": "待确认",
        "recognitionStrategy": effective_recognition_strategy,
        "requestedRecognitionStrategy": recognition_strategy,
        "debugSampleFps": round(sample_fps, 3) if sample_fps_override is not None else None,
        "experimentFlags": experiment_flags.as_metadata(),
    }


class BridgeHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        if Path(urlparse(self.path).path).suffix.lower() in SEEKABLE_VIDEO_SUFFIXES:
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def serve_video_range(self, path: Path, range_header: str) -> None:
        stat = path.stat()
        try:
            start, end = parse_byte_range(range_header, stat.st_size)
        except (TypeError, ValueError):
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{stat.st_size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{stat.st_size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
        self.end_headers()
        try:
            with path.open("rb") as source:
                source.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = source.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/api/video/truth-progress"):
            self.handle_truth_progress()
            return
        if self.path.startswith("/api/video/status"):
            self.handle_video_status()
            return
        if self.path.startswith("/api/video/task"):
            self.handle_video_task()
            return
        if self.path.startswith("/api/video/versions"):
            self.handle_video_versions()
            return
        if self.path == "/api/editor/status":
            self.reply_json({"editorAvailable": EDITOR_EXE.is_file(), "previewAvailable": PREVIEW_EXE.is_file(), "editorPath": str(EDITOR_EXE), "taskRoot": str(TASK_ROOT)})
            return
        if self.path == "/api/calibration/screenshot":
            self.handle_calibration_screenshot()
            return
        if self.path in ("/", "/index.html"):
            self.path = "/index.html"
        range_header = self.headers.get("Range")
        if range_header:
            static_path = Path(self.translate_path(self.path))
            if static_path.is_file() and static_path.suffix.lower() in SEEKABLE_VIDEO_SUFFIXES:
                self.serve_video_range(static_path, range_header)
                return
        super().do_GET()

    def handle_truth_progress(self) -> None:
        try:
            self.reply_json(build_truth_progress())
        except FileNotFoundError:
            self.reply_json({"ok": False, "error": "找不到视频数据集清单"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"读取真值进度失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        if self.path == "/api/calibration/start":
            self.handle_calibration_start()
            return
        if self.path == "/api/calibration/save":
            self.handle_calibration_save()
            return
        if self.path == "/api/video/analyze":
            self.handle_video_analyze("legacy")
            return
        if self.path == "/api/video/analyze-reverse-clear":
            self.handle_video_analyze("reverse_clear_v1")
            return
        if self.path == "/api/video/debug-analyze":
            self.handle_video_debug_analyze()
            return
        if self.path == "/api/video/recalibrate":
            self.handle_video_recalibrate()
            return
        if self.path == "/api/video/capture-frame":
            self.handle_video_capture_frame()
            return
        if self.path == "/api/video/restore-version":
            self.handle_video_restore_version()
            return
        if self.path == "/api/video/confirm-actions":
            self.handle_video_confirm_actions()
            return
        if self.path == "/api/video/record":
            self.handle_video_record()
            return
        if self.path == "/api/video/replay":
            self.handle_video_replay()
            return
        if self.path == "/api/assets/upload":
            self.handle_asset_upload()
            return
        if self.path != "/api/editor/create-task":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            variant = payload["variant"]
            if not variant.get("variantName"):
                raise ValueError("缺少变体名称")
            if variant.get("gameplay") != "游戏玩法":
                raise ValueError("只有“游戏玩法”变体可提交至关卡编辑器")
            output_dir = Path(payload.get("outputDirectory") or DEFAULT_VIDEO_ROOT)
            output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            task_dir = TASK_ROOT / f"{stamp}_{safe_name(variant['variantName'])}"
            task_dir.mkdir(parents=True, exist_ok=False)
            config, source = prepare_config(payload.get("templatePath", ""), variant, output_dir)
            config_path = task_dir / "preview-config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            brief = {
                "createdAt": datetime.now().isoformat(timespec="seconds"),
                "sourceTemplate": source,
                "videoOutputDirectory": str(output_dir),
                "variant": variant,
                "nextSteps": [
                    "预览客户端已直接启动；自动放置与自动录制已开启，视频会输出到 videoOutputDirectory。",
                    "如需微调棋盘、方块组或触发效果，可在编辑器中加载本任务的 preview-config.json 后再保存。",
                    "如果使用基准关卡 JSON，棋盘、方块组、资源和主题将沿用该模板。",
                ],
            }
            (task_dir / "变体任务.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
            preview_started = False
            if PREVIEW_EXE.is_file():
                subprocess.Popen([str(PREVIEW_EXE), "--preview", str(config_path)], cwd=str(PREVIEW_EXE.parent))
                preview_started = True
            elif EDITOR_EXE.is_file():
                subprocess.Popen([str(EDITOR_EXE)], cwd=str(EDITOR_EXE.parent))
            subprocess.Popen(["explorer.exe", "/select," + str(config_path)])
            self.reply_json({"ok": True, "taskDirectory": str(task_dir), "configPath": str(config_path), "videoOutputDirectory": str(output_dir), "previewStarted": preview_started})
        except (KeyError, ValueError) as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # Keep the local UI useful if a Windows call fails.
            self.reply_json({"ok": False, "error": f"创建编辑器任务失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_asset_upload(self) -> None:
        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                },
            )
            asset_kind = str(form.getfirst("assetKind") or "")
            file_fields = form["files"] if "files" in form else []
            if not isinstance(file_fields, list):
                file_fields = [file_fields]
            assets = [
                install_uploaded_library_asset(file_item, asset_kind)
                for file_item in file_fields
                if getattr(file_item, "filename", "")
            ]
            if not assets:
                raise ValueError("没有收到图片文件")
            self.reply_json({"ok": True, "assets": assets})
        except ValueError as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"上传素材失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_video_analyze(self, recognition_strategy: str = "legacy") -> None:
        try:
            content_type, _ = cgi.parse_header(self.headers.get("Content-Type", ""))
            if content_type != "multipart/form-data":
                raise ValueError("请使用视频文件上传")
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers["Content-Type"]})
            file_item = form["video"] if "video" in form else None
            if file_item is None or not getattr(file_item, "filename", ""):
                raise ValueError("没有收到视频文件")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            strategy_suffix = "_reverse-clear-v1" if recognition_strategy == "reverse_clear_v1" else ""
            task_dir = UPLOAD_ROOT / f"{stamp}_{safe_name(Path(file_item.filename).stem)}{strategy_suffix}"
            task_dir.mkdir(parents=True)
            suffix = Path(file_item.filename).suffix.lower() or ".mp4"
            video_path = task_dir / f"source{suffix}"
            digest = hashlib.sha256()
            with video_path.open("wb") as target:
                while chunk := file_item.file.read(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
            video_sha256 = digest.hexdigest()
            cached = find_cached_video_analysis(video_sha256, recognition_strategy)
            if cached:
                shutil.rmtree(task_dir, ignore_errors=True)
                self.reply_json(cached)
                return
            try:
                analysis = analyse_video(
                    video_path,
                    task_dir,
                    recognition_strategy=recognition_strategy,
                )
                analysis["reviewActions"] = build_action_review(
                    json.loads((task_dir / "步骤时间线.json").read_text(encoding="utf-8"))
                )
            except ValueError as detection_error:
                import cv2

                capture = cv2.VideoCapture(str(video_path))
                fps = capture.get(cv2.CAP_PROP_FPS) or 30
                total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, round(total * 0.35)))
                ok, frame = capture.read()
                capture.release()
                if not ok:
                    raise detection_error
                frame_height, frame_width = frame.shape[:2]
                calibration_path = task_dir / "棋盘校准帧.jpg"
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                if not ok:
                    raise detection_error
                encoded.tofile(str(calibration_path))
                suggested_width = round(frame_width * 0.8)
                suggested_height = min(suggested_width, round(frame_height * 0.65))
                analysis = {
                    "video": str(video_path),
                    "duration": round(total / fps, 2) if total else 0,
                    "fps": round(fps, 2),
                    "calibrationRequired": True,
                    "recognitionStrategy": recognition_strategy,
                    "detectionError": str(detection_error),
                    "board": {
                        "x": round((frame_width - suggested_width) / 2),
                        "y": round(frame_height * 0.2),
                        "width": suggested_width,
                        "height": suggested_height,
                        "rows": 8,
                        "cols": 8,
                        "gridConfidence": 0,
                        "source": "manual_required",
                        "detectedBlocks": [],
                        "calibrationFrame": str(calibration_path.relative_to(ROOT)).replace("\\", "/"),
                    },
                    "timeline": {"sampleRate": 0, "sampleCount": 0, "eventCount": 0, "events": [], "validation": {"readyForAutomaticReplay": False, "reason": "manual_board_calibration_required"}},
                    "reviewActions": [],
                    "confidence": "需要人工棋盘校准",
                }
            (task_dir / "识别草稿.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
            write_json_atomic(
                task_dir / "analysis-cache.json",
                {
                    "sha256": video_sha256,
                    "recognitionStrategy": recognition_strategy,
                    "createdAt": datetime.now().isoformat(timespec="seconds"),
                    "sourceName": getattr(file_item, "filename", ""),
                },
            )
            remember_cached_video_analysis(video_sha256, recognition_strategy, task_dir)
            save_action_version(task_dir, "识别初稿", {"analysis": analysis})
            write_json_atomic(
                task_dir / "动作确认状态.json",
                {"status": "pending", "message": "请逐步确认动作脚本后再生成完整视频"},
            )
            self.reply_json({"ok": True, "jobId": task_dir.name, "analysis": analysis, "taskDirectory": str(task_dir)})
        except (KeyError, ValueError) as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"视频分析失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_video_debug_analyze(self) -> None:
        try:
            content_type, _ = cgi.parse_header(self.headers.get("Content-Type", ""))
            if content_type != "multipart/form-data":
                raise ValueError("请使用视频文件上传")
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers["Content-Type"]},
            )
            file_item = form["video"] if "video" in form else None
            if file_item is None or not getattr(file_item, "filename", ""):
                raise ValueError("没有收到视频文件")
            sample_fps = float(form.getfirst("sampleFps") or 12.0)
            if sample_fps <= 0:
                raise ValueError("调试帧率必须大于 0")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            task_dir = UPLOAD_ROOT / f"{stamp}_{safe_name(Path(file_item.filename).stem)}_debug-{sample_fps:g}fps"
            task_dir.mkdir(parents=True)
            suffix = Path(file_item.filename).suffix.lower() or ".mp4"
            video_path = task_dir / f"source{suffix}"
            with video_path.open("wb") as target:
                while chunk := file_item.file.read(1024 * 1024):
                    target.write(chunk)
            analysis = analyse_video(
                video_path,
                task_dir,
                recognition_strategy="legacy",
                sample_fps_override=sample_fps,
            )
            timeline = json.loads(Path(analysis["timeline"]["path"]).read_text(encoding="utf-8"))
            analysis["reviewActions"] = build_action_review(timeline)
            write_json_atomic(task_dir / "调试识别结果.json", analysis)
            self.reply_json({"ok": True, "jobId": task_dir.name, "analysis": analysis, "taskDirectory": str(task_dir)})
        except (KeyError, ValueError) as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"调试识别失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_video_recalibrate(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            task_dir = UPLOAD_ROOT / safe_name(payload["jobId"])
            source_files = [path for path in task_dir.glob("source.*") if path.is_file()]
            if len(source_files) != 1:
                raise ValueError("找不到当前任务的原视频")
            analysis = analyse_video(source_files[0], task_dir, payload.get("board") or {})
            timeline = json.loads(Path(analysis["timeline"]["path"]).read_text(encoding="utf-8"))
            analysis["reviewActions"] = build_action_review(timeline)
            write_json_atomic(task_dir / "识别草稿.json", analysis)
            save_action_version(task_dir, "人工棋盘校准后识别稿", {"analysis": analysis, "board": payload.get("board")})
            write_json_atomic(
                task_dir / "动作确认状态.json",
                {"status": "pending", "message": "棋盘已重新校准，请重新确认全部动作"},
            )
            self.reply_json({"ok": True, "jobId": task_dir.name, "analysis": analysis, "taskDirectory": str(task_dir)})
        except (KeyError, ValueError, TypeError) as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"人工校准重算失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_video_capture_frame(self) -> None:
        try:
            import cv2

            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            raw_job_id = str(payload.get("jobId", "")).strip()
            if not raw_job_id:
                raise ValueError("缺少 jobId")
            job_id = safe_name(raw_job_id)
            task_dir = UPLOAD_ROOT / job_id
            if not task_dir.is_dir():
                raise ValueError("找不到视频标注任务")
            source_files = [path for path in task_dir.iterdir() if path.suffix.lower() in SEEKABLE_VIDEO_SUFFIXES]
            if not source_files:
                raise ValueError("任务中找不到原视频")
            requested_time = max(0.0, float(payload.get("time", 0)))
            step_index = max(1, int(payload.get("stepIndex", 1)))
            evidence_key = str(payload.get("evidenceKey", "before"))
            if evidence_key not in {"before", "action", "placed", "cleared"}:
                raise ValueError("不支持的稳定态类型")

            capture = cv2.VideoCapture(str(source_files[0]))
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0)
            total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            frame_number = max(0, round(requested_time * fps))
            if total_frames:
                frame_number = min(frame_number, total_frames - 1)
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ok, frame = capture.read()
            capture.release()
            if not ok:
                raise ValueError("无法读取所选视频帧")

            frame_root = task_dir / "动作帧" / "人工标注"
            frame_root.mkdir(parents=True, exist_ok=True)
            frame_path = frame_root / f"步骤{step_index:03d}_{evidence_key}_帧{frame_number:06d}.jpg"
            encoded_ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not encoded_ok:
                raise ValueError("视频帧编码失败")
            encoded.tofile(str(frame_path))
            actual_time = round(frame_number / fps, 3)
            self.reply_json({
                "ok": True,
                "imagePath": str(frame_path.relative_to(ROOT)).replace("\\", "/"),
                "frameNumber": frame_number,
                "time": actual_time,
                "fps": round(fps, 6),
            })
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"截取视频帧失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_video_confirm_actions(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            task_dir = UPLOAD_ROOT / safe_name(payload["jobId"])
            timeline_path = task_dir / "步骤时间线.json"
            analysis_path = task_dir / "识别草稿.json"
            if not timeline_path.is_file() or not analysis_path.is_file():
                raise ValueError("找不到待确认的视频识别任务")
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            backfill_annotation_timing(analysis)
            if not analysis.get("sourceVideoUrl"):
                video_files = [path for path in task_dir.iterdir() if path.suffix.lower() in {".mp4", ".mov", ".m4v", ".avi", ".webm"}]
                if video_files:
                    analysis["sourceVideoUrl"] = str(video_files[0].relative_to(ROOT)).replace("\\", "/")
            truth = apply_confirmed_actions(
                timeline,
                payload.get("actions") or [],
                analysis.get("video", ""),
                payload.get("referenceInteractions"),
            )
            analysis["reviewActions"] = payload.get("actions") or []
            analysis.setdefault("timeline", {})["referenceInteractions"] = copy.deepcopy(
                truth.get("referenceInteractions") or []
            )
            write_json_atomic(analysis_path, analysis)
            write_json_atomic(timeline_path, timeline)
            write_json_atomic(task_dir / "确定性回放真值.json", truth)
            version_path = save_action_version(task_dir, "人工确认", {"truth": truth, "actions": payload.get("actions") or []})
            status = {
                "status": "confirmed",
                "stepCount": truth["stepCount"],
                "confirmedAt": truth["reviewConfirmedAt"],
            }
            write_json_atomic(task_dir / "动作确认状态.json", status)
            self.reply_json({"ok": True, **status, "versionPath": str(version_path)})
        except (KeyError, ValueError, TypeError) as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"保存动作确认失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_calibration_start(self) -> None:
        try:
            if not PREVIEW_EXE.is_file():
                raise ValueError("找不到预览客户端")
            CALIBRATION_ROOT.mkdir(exist_ok=True)
            config = scaffold_config("preview_calibration", CALIBRATION_ROOT)
            config["recording"]["enabled"] = False
            config["autoPlay"]["enabled"] = False
            config_path = CALIBRATION_ROOT / "calibration-preview-config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            subprocess.Popen([str(PREVIEW_EXE), "--preview", str(config_path)], cwd=str(PREVIEW_EXE.parent))
            time.sleep(2)
            self.reply_json({"ok": True, "configPath": str(config_path), "message": "校准预览已启动，请将预览窗口保持在前台，然后点击截图。"})
        except ValueError as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"启动校准预览失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_calibration_screenshot(self) -> None:
        try:
            import cv2
            import mss
            import numpy as np

            CALIBRATION_ROOT.mkdir(exist_ok=True)
            with mss.mss() as capture:
                monitor = capture.monitors[1]
                image = np.array(capture.grab(monitor))[:, :, :3]
            board = find_board(image)
            screenshot_path = CALIBRATION_ROOT / "screen.jpg"
            ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if not ok:
                raise ValueError("无法保存校准截图")
            encoded.tofile(str(screenshot_path))
            self.reply_json({"ok": True, "imagePath": "校准/screen.jpg", "screen": {"width": int(image.shape[1]), "height": int(image.shape[0])}, "suggestedBoard": {"x": board[0], "y": board[1], "width": board[2], "height": board[3]}})
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"截图失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_calibration_save(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            board = payload["board"]
            if min(board.get("width", 0), board.get("height", 0)) < 100:
                raise ValueError("棋盘校准区域过小")
            CALIBRATION_ROOT.mkdir(exist_ok=True)
            calibration = {"savedAt": datetime.now().isoformat(timespec="seconds"), "screen": payload.get("screen", {}), "board": board, "groupSlots": payload.get("groupSlots") or [{"x": 764, "y": 962}, {"x": 960, "y": 962}, {"x": 1156, "y": 962}], "cellWidth": board["width"] / 10, "cellHeight": board["height"] / 10}
            (CALIBRATION_ROOT / "preview-calibration.json").write_text(json.dumps(calibration, ensure_ascii=False, indent=2), encoding="utf-8")
            self.reply_json({"ok": True, "calibration": calibration})
        except (KeyError, ValueError) as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def handle_video_record(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            task_dir = UPLOAD_ROOT / safe_name(payload["jobId"])
            analysis_path = task_dir / "识别草稿.json"
            if not analysis_path.is_file():
                raise ValueError("找不到视频重建任务")
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            output_dir = Path(payload.get("outputDirectory") or DEFAULT_VIDEO_ROOT)
            output_dir.mkdir(parents=True, exist_ok=True)
            config = scaffold_config(payload.get("name") or task_dir.name, output_dir)
            blocks = copy.deepcopy(analysis["board"]["detectedBlocks"])
            block_resource_id = str(payload.get("blockResourceId") or "")
            if block_resource_id:
                for block in blocks:
                    block.update({"kind": "Normal", "resourceId": block_resource_id, "clearEffectId": "kuai_xiaochu4", "clearEffectColor": "lan", "blockTipsColor": "lan"})
            elif payload.get("blockStyle") == "jewel":
                for block in blocks:
                    block.update({"kind": "Jewel", "resourceId": f"jewel_block_{block['colorIndex'] % 6 + 1}_1", "clearEffectId": "kuai_xiaochu4", "clearEffectColor": "lan", "blockTipsColor": "lan"})
            config["board"] = {"rows": analysis["board"]["rows"], "cols": analysis["board"]["cols"], "blocks": blocks}
            config["theme"]["backgroundId"] = payload.get("backgroundId") or config["theme"]["backgroundId"]
            config_path = task_dir / "rebuild-preview-config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            if not PREVIEW_EXE.is_file():
                raise ValueError("找不到预览客户端")
            subprocess.Popen([str(PREVIEW_EXE), "--preview", str(config_path)], cwd=str(PREVIEW_EXE.parent))
            self.reply_json({"ok": True, "configPath": str(config_path), "videoOutputDirectory": str(output_dir), "detectedBlockCount": len(analysis["board"]["detectedBlocks"])})
        except (KeyError, ValueError) as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"启动重建录制失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_video_replay(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            task_dir = UPLOAD_ROOT / safe_name(payload["jobId"])
            review_status_path = task_dir / "动作确认状态.json"
            if review_status_path.is_file():
                review_status = json.loads(review_status_path.read_text(encoding="utf-8"))
                if review_status.get("status") != "confirmed" and not payload.get("allowDraftReplay"):
                    raise ValueError("动作脚本尚未全部确认，不能生成完整视频")
            if not PREVIEW_EXE.is_file():
                raise ValueError("找不到改造版预览客户端")
            result = prepare_deterministic_client(task_dir, payload)
            launched = not bool(payload.get("dryRun"))
            status_path = task_dir / "视频生成状态.json"
            target_name = f"{safe_name(payload.get('name') or task_dir.name)}_60fps.mp4"
            target_path = Path(result["videoOutputDirectory"]) / target_name
            if launched:
                cleanup_replay_processes()
                started_at = time.time()
                source_files = [path for path in task_dir.glob("source.*") if path.is_file()]
                source_width, source_height = 1080, 1920
                if source_files:
                    import cv2
                    capture = cv2.VideoCapture(str(source_files[0]))
                    source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or source_width
                    source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or source_height
                    capture.release()
                source_width, source_height = canvas_size_for_aspect(
                    str(payload.get("aspectRatio") or ""),
                    source_width,
                    source_height,
                )
                write_json_atomic(status_path, {"status": "running", "message": "客户端正在执行确定性回放", "outputVideo": str(target_path)})
                preview_process = subprocess.Popen([str(PREVIEW_EXE)], cwd=str(PREVIEW_EXE.parent))
                subprocess.Popen(
                    [
                        sys.executable,
                        str(ROOT / "recording_finalizer.py"),
                        result["videoOutputDirectory"],
                        str(target_path),
                        result["logPath"],
                        str(status_path),
                        str(started_at),
                        str(source_width),
                        str(source_height),
                        str(preview_process.pid),
                    ],
                    cwd=str(ROOT),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            self.reply_json({"ok": True, **result, "launched": launched, "driverLaunched": False, "controlMode": "internal-client", "statusPath": str(status_path), "outputVideo": str(target_path)})
        except (KeyError, ValueError) as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"启动完整回放失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_video_status(self) -> None:
        try:
            query = parse_qs(urlparse(self.path).query)
            job_id = safe_name(query.get("jobId", [""])[0])
            if not job_id:
                raise ValueError("缺少 jobId")
            status_path = UPLOAD_ROOT / job_id / "视频生成状态.json"
            if not status_path.is_file():
                self.reply_json({"ok": True, "status": "idle", "message": "尚未开始生成"})
                return
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.reply_json({"ok": True, **status})
        except ValueError as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"读取生成状态失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_video_task(self) -> None:
        try:
            query = parse_qs(urlparse(self.path).query)
            job_id = safe_name(query.get("jobId", [""])[0])
            if not job_id:
                raise ValueError("缺少 jobId")
            task_dir = UPLOAD_ROOT / job_id
            analysis_path = task_dir / "识别草稿.json"
            if not analysis_path.is_file():
                raise ValueError("找不到视频识别任务")
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            backfill_annotation_timing(analysis)
            timeline_path = task_dir / "步骤时间线.json"
            if timeline_path.is_file():
                timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
                references = (
                    analysis.get("timeline", {}).get("referenceInteractions")
                    or timeline.get("referenceInteractions")
                    or timeline.get("cancelledDrags")
                    or []
                )
                analysis.setdefault("timeline", {})["referenceInteractions"] = attach_reference_boards(
                    timeline, references
                )
            if not analysis.get("sourceVideoUrl"):
                video_files = [path for path in task_dir.iterdir() if path.suffix.lower() in {".mp4", ".mov", ".m4v", ".avi", ".webm"}]
                if video_files:
                    analysis["sourceVideoUrl"] = str(video_files[0].relative_to(ROOT)).replace("\\", "/")
            status_path = task_dir / "动作确认状态.json"
            review_status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {"status": "pending"}
            self.reply_json({"ok": True, "jobId": job_id, "taskDirectory": str(task_dir), "analysis": analysis, "reviewStatus": review_status})
        except ValueError as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"读取视频任务失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_video_versions(self) -> None:
        try:
            query = parse_qs(urlparse(self.path).query)
            job_id = safe_name(query.get("jobId", [""])[0])
            if not job_id:
                raise ValueError("缺少 jobId")
            version_root = UPLOAD_ROOT / job_id / "动作脚本版本"
            versions = []
            for path in sorted(version_root.glob("*.json"), reverse=True) if version_root.is_dir() else []:
                data = json.loads(path.read_text(encoding="utf-8"))
                versions.append({"id": path.name, "kind": data.get("kind", "未知"), "savedAt": data.get("savedAt", "")})
            self.reply_json({"ok": True, "versions": versions})
        except ValueError as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"读取动作版本失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_video_restore_version(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            task_dir = UPLOAD_ROOT / safe_name(payload["jobId"])
            version_name = Path(str(payload["versionId"])).name
            version_path = task_dir / "动作脚本版本" / version_name
            if not version_path.is_file():
                raise ValueError("找不到指定动作版本")
            version = json.loads(version_path.read_text(encoding="utf-8"))
            body = version.get("payload") or {}
            if "truth" in body:
                write_json_atomic(task_dir / "确定性回放真值.json", body["truth"])
                write_json_atomic(task_dir / "动作确认状态.json", {"status": "confirmed", "stepCount": body["truth"].get("stepCount", 0), "confirmedAt": datetime.now().isoformat(timespec="seconds"), "restoredFrom": version_name})
                restored_status = "confirmed"
                restored_analysis = None
            elif "analysis" in body:
                write_json_atomic(task_dir / "识别草稿.json", body["analysis"])
                write_json_atomic(task_dir / "动作确认状态.json", {"status": "pending", "message": "已恢复识别草稿，需要重新确认动作", "restoredFrom": version_name})
                restored_status = "pending"
                restored_analysis = body["analysis"]
            else:
                raise ValueError("动作版本内容无效")
            self.reply_json({"ok": True, "status": restored_status, "versionId": version_name, "analysis": restored_analysis})
        except (KeyError, ValueError, TypeError) as exc:
            self.reply_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.reply_json({"ok": False, "error": f"恢复动作版本失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def reply_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[bridge] {self.address_string()} - {fmt % args}")


def main() -> None:
    TASK_ROOT.mkdir(exist_ok=True)
    DEFAULT_VIDEO_ROOT.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 8765), BridgeHandler)
    print("变体生成器已启动：http://127.0.0.1:8765")
    webbrowser.open("http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
