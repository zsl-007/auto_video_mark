import subprocess
from pathlib import Path
from app.core.hardware import encoder_args


def get_codecs(path: Path) -> tuple[str, str]:
    def probe(stream: str) -> str:
        return subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", stream,
                "-show_entries", "stream=codec_name",
                "-of", "default=nokey=1:noprint_wrappers=1",
                str(path),
            ],
            capture_output=True, text=True,
        ).stdout.strip()

    return probe("v:0"), probe("a:0")


def merge(segments: list[Path], output_path: Path, workdir: Path):
    list_file = workdir / "concat_list.txt"
    with open(list_file, "w") as f:
        for s in segments:
            f.write(f"file '{s.resolve()}'\n")

    codecs = [get_codecs(s) for s in segments]
    if len(set(codecs)) == 1:
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c", "copy", str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            *encoder_args(), "-c:a", "aac", str(output_path),
        ]
    subprocess.run(cmd, check=True, capture_output=True)