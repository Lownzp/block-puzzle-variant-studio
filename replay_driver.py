"""Mouse replay for the preview client. Runs only after the user starts a replay job."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path


user32 = ctypes.windll.user32
LEFT_DOWN = 0x0002
LEFT_UP = 0x0004
KEY_UP = 0x0002
SW_RESTORE = 9
SW_MINIMIZE = 6
VK_MENU = 0x12
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002


def focus_preview() -> None:
    window = user32.FindWindowW(None, "preview_client")
    if not window:
        raise RuntimeError("preview_client window was not found")
    user32.ShowWindow(window, SW_RESTORE)
    user32.SetWindowPos(window, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.SetForegroundWindow(window)
    user32.keybd_event(VK_MENU, 0, KEY_UP, 0)
    time.sleep(0.3)


def set_chatgpt_visible(visible: bool) -> None:
    window = user32.FindWindowW(None, "ChatGPT")
    if window:
        user32.ShowWindow(window, SW_RESTORE if visible else SW_MINIMIZE)
        time.sleep(0.4)


def move_and_drag(source: dict, target: dict) -> None:
    focus_preview()
    user32.SetCursorPos(int(source["x"]), int(source["y"]))
    time.sleep(0.18)
    user32.mouse_event(LEFT_DOWN, 0, 0, 0, 0)
    time.sleep(0.12)
    user32.SetCursorPos(int(target["x"]), int(target["y"]))
    time.sleep(0.25)
    user32.mouse_event(LEFT_UP, 0, 0, 0, 0)


def apply_preset(plan: dict, preset_index: int) -> None:
    config_path = Path(plan["activeConfigPath"])
    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(plan["presetConfigs"][preset_index], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_path, config_path)


def capture_screen(path: Path) -> None:
    import cv2
    import mss
    import numpy as np

    with mss.mss() as capture:
        image = np.array(capture.grab(capture.monitors[1]))[:, :, :3]
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if ok:
        encoded.tofile(str(path))


def press_stop_recording() -> None:
    user32.keybd_event(0x53, 0, 0, 0)
    time.sleep(0.08)
    user32.keybd_event(0x53, 0, KEY_UP, 0)


def main(plan_path: str) -> None:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    debug_dir = Path(plan.get("debugDirectory") or Path(plan_path).with_suffix(""))
    debug_dir.mkdir(parents=True, exist_ok=True)
    capture_debug = bool(plan.get("captureDebug"))
    log = []
    # Give preview_client enough time to load resources and begin recording.
    time.sleep(6.5)
    set_chatgpt_visible(False)
    try:
        for step in plan["steps"]:
            move_and_drag(step["source"], step["target"])
            if "switchPresetIndex" in step:
                time.sleep(float(step["waitBeforeSwitch"]))
                apply_preset(plan, int(step["switchPresetIndex"]))
                # Hot reload polling and scene application need a little margin.
                time.sleep(max(2.5, float(step["waitAfterSwitch"])))
                if capture_debug:
                    capture_screen(debug_dir / f"preset_{int(step['switchPresetIndex']) + 1}.jpg")
            else:
                time.sleep(float(step.get("waitAfter", 1.0)))
            if capture_debug:
                capture_screen(debug_dir / f"step_{step['stepIndex']:02d}.jpg")
            log.append({"stepIndex": step["stepIndex"], "completedAt": time.time(), "target": step["target"]})
        press_stop_recording()
        time.sleep(1.0)
    except Exception as exc:
        log.append({"error": repr(exc), "failedAt": time.time()})
        raise
    finally:
        (debug_dir / "replay-log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        set_chatgpt_visible(True)


if __name__ == "__main__":
    main(sys.argv[1])
