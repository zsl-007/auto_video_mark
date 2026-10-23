"""静态画面比例检测。"""
import re
import subprocess
from pathlib import Path

from app.core.config import MPDECIMATE_FRAC


def detect_static(segment_path: Path) -> float:
    cmd = [
        "ffmpeg", "-i", str(segment_path),
        "-vf", f"mpdecimate=frac={MPDECIMATE_FRAC}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    frames = re.findall(r"frame=\s*(\d+)", result.stderr)
    if not frames:
        return 1.0
    kept = int(frames[-1])

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames",
            "-of", "default=nokey=1:noprint_wrappers=1",
            str(segment_path),
        ],
        capture_output=True, text=True,
    )
    try:
        total = int(probe.stdout.strip())
    except ValueError:
        return 1.0
    return kept / total if total else 1.0


def is_static(segment_path: Path, threshold: float | None = None) -> bool:
    if threshold is None:
        from app.core.runtime_settings import get_settings
        threshold = float(get_settings()["static_threshold"])
    return detect_static(segment_path) < threshold