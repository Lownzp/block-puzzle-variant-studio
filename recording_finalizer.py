import json
import subprocess
import sys
import time
from pathlib import Path


def write_status(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    output_dir = Path(sys.argv[1])
    target_path = Path(sys.argv[2])
    replay_log = Path(sys.argv[3])
    status_path = Path(sys.argv[4])
    started_at = float(sys.argv[5])
    canvas_width = int(sys.argv[6]) if len(sys.argv) > 6 else 1080
    canvas_height = int(sys.argv[7]) if len(sys.argv) > 7 else 1920
    preview_pid = int(sys.argv[8]) if len(sys.argv) > 8 and sys.argv[8].isdigit() else 0
    deadline = time.time() + 180

    try:
        missing_preview_checks = 0
        while time.time() < deadline:
            if replay_log.is_file():
                log = replay_log.read_text(encoding="utf-8", errors="replace")
                failures = ("VALIDATION_FAILED", "STEP_FAILED", "PRESET_FAILED", "RUNTIME_NOT_READY")
                failure = next((line for line in log.splitlines() if any(token in line for token in failures)), None)
                if failure:
                    raise RuntimeError(failure)
                if "RECORDING_STOPPED" in log:
                    break
            if preview_pid:
                completed = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {preview_pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if str(preview_pid) not in completed.stdout:
                    missing_preview_checks += 1
                    if missing_preview_checks >= 3:
                        raise RuntimeError("preview client exited before recording completed")
                else:
                    missing_preview_checks = 0
            time.sleep(1)
        else:
            raise TimeoutError("等待客户端完成录制超时")

        recordings = [
            path
            for path in output_dir.glob("GameRecording_*.mp4")
            if path.stat().st_mtime >= started_at - 3
        ]
        if not recordings:
            raise FileNotFoundError("客户端完成回放，但没有找到本次录制文件")
        source = max(recordings, key=lambda path: path.stat().st_mtime)

        from imageio_ffmpeg import get_ffmpeg_exe

        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = target_path.with_name(target_path.stem + ".encoding.mp4")
        video_filter = (
            f"fps=60,scale={canvas_width}:{canvas_height}:"
            "force_original_aspect_ratio=increase,"
            f"crop={canvas_width}:{canvas_height},setsar=1"
        )
        command = [
            get_ffmpeg_exe(), "-y", "-i", str(source),
            "-vf", video_filter,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "160k",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr[-1200:])
        temporary.replace(target_path)
        write_status(
            status_path,
            {
                "status": "complete",
                "message": "确定性回放和 60fps 转码已完成",
                "sourceRecording": str(source),
                "outputVideo": str(target_path),
                "fps": 60,
                "completedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        return 0
    except Exception as exc:
        write_status(status_path, {"status": "failed", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
