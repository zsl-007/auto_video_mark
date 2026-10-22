import subprocess
from pathlib import Path
from app.core.config import SEGMENT_DURATION, SEGMENT_ENABLED
from app.core.hardware import encoder_args


def segment(input_path: Path, workdir: Path) -> list[Path]:
    if not SEGMENT_ENABLED:
        return [input_path]

    segment_dir = workdir / f"{input_path.stem}_segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(segment_dir / "seg_%04d.mp4")

    # 先尝试流拷贝
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", str(SEGMENT_DURATION),
        "-reset_timestamps", "1", pattern,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            *encoder_args(), "-c:a", "aac",
            "-f", "segment", "-segment_time", str(SEGMENT_DURATION),
            "-reset_timestamps", "1", pattern,
        ]
        subprocess.run(cmd, check=True, capture_output=True)

    segments = sorted(segment_dir.glob("seg_*.mp4"))
    return segments if segments else [input_path]