"""按配置分段。settings 由 scheduler 传入快照；单独调用时从运行时配置读。"""
import subprocess
from pathlib import Path

from app.core.hardware import encoder_args


def segment(input_path: Path, workdir: Path, settings: dict | None = None) -> list[Path]:
    if settings is None:
        from app.core.runtime_settings import get_settings
        settings = get_settings()

    if not settings["segment_enabled"]:
        return [input_path]

    duration = int(settings["segment_duration"])
    segment_dir = workdir / f"{input_path.stem}_segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(segment_dir / "seg_%04d.mp4")

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", str(duration),
        "-reset_timestamps", "1", pattern,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            *encoder_args(), "-c:a", "aac",
            "-f", "segment", "-segment_time", str(duration),
            "-reset_timestamps", "1", pattern,
        ]
        subprocess.run(cmd, check=True, capture_output=True)

    segments = sorted(segment_dir.glob("seg_*.mp4"))
    return segments if segments else [input_path]