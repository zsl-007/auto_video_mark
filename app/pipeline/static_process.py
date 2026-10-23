"""静态处理：用 mpdecimate 评估分段的保留帧比例，供调度器判定是否丢弃。

保留原有检测逻辑，仅统一口径为"静态处理"。
"""
import re
import subprocess
from pathlib import Path

from app.core.config import MPDECIMATE_FRAC


def process_static(segment_path: Path) -> float:
    """返回分段的保留帧比例（0–1）。1.0 表示完全非静态（全保留）。"""
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


def is_static_segment(segment_path: Path, threshold: float | None = None) -> bool:
    """是否判定为静态分段。threshold 为 None 时从运行时配置读取。"""
    if threshold is None:
        from app.core.runtime_settings import get_settings
        threshold = float(get_settings()["static_process_threshold"])
    return process_static(segment_path) < threshold