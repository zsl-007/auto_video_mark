import json
import subprocess
from pathlib import Path
from app.core.hardware import encoder_args

INCOMPATIBLE_CODECS = {"amr_nb", "dts", "eac3", "truehd"}


def probe_streams(input_path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_streams", str(input_path),
        ],
        capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def precheck(input_path: Path, workdir: Path) -> Path:
    info = probe_streams(input_path)
    needs_fix = any(
        s.get("codec_name", "") in INCOMPATIBLE_CODECS
        for s in info.get("streams", [])
    )
    if not needs_fix:
        return input_path

    fixed = workdir / f"{input_path.stem}_prechecked.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        *encoder_args(), "-c:a", "aac",
        "-movflags", "+faststart", str(fixed),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return fixed