"""视频压缩（预检后、分段前）：降低分辨率以加速后续 AI 处理。

行为：
- 未开启压缩，直接返回原路径。
- 探测高度失败、或高度 ≤ 目标分辨率，直接返回原路径（不重编码）。
- 高度 > 目标分辨率：等比缩放到目标高度（宽度按比例且取偶数），重编码输出
  {stem}_compressed.mp4。
- 编码器失败时自动用 libx264 重试一次，避免硬件编码器运行时不可用导致任务失败。
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from app.core.hardware import encoder_args

logger = logging.getLogger(__name__)

# libx264 兜底参数（与 hardware.encoder_args 中软件分支一致）
_SW_FALLBACK = ["-c:v", "libx264", "-preset", "medium", "-crf", "23"]


class CompressError(RuntimeError):
    pass


def _probe_height(path: Path) -> int | None:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=height",
                "-of", "default=nokey=1:noprint_wrappers=1",
                str(path),
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return int(out) if out else None
    except (subprocess.CalledProcessError, ValueError):
        return None


def _ffmpeg_cmd(input_path: Path, out: Path, min_h: int, codec_args: list[str]) -> list[str]:
    return [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
        "-vf", f"scale=-2:{min_h}",
        "-pix_fmt", "yuv420p",
        *codec_args,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]


def _run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stderr or "")


def compress(
    input_path: Path,
    workdir: Path,
    settings: dict | None = None,
    stem: str | None = None,
) -> Path:
    if settings is None:
        from app.core.runtime_settings import get_settings
        settings = get_settings()

    if not settings.get("video_compress_enabled", True):
        logger.info("未启用视频压缩，跳过: %s", input_path.name)
        return input_path

    try:
        min_h = int(settings.get("min_resolution", 480))
    except (TypeError, ValueError):
        min_h = 480

    height = _probe_height(input_path)
    if height is None:
        logger.warning("无法探测视频分辨率，跳过压缩: %s", input_path.name)
        return input_path

    if height <= min_h:
        logger.info(
            "视频高度 %dp ≤ 目标 %dp，跳过压缩: %s", height, min_h, input_path.name,
        )
        return input_path

    if stem is None:
        stem = input_path.stem

    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / f"{stem}_compressed.mp4"

    # 首选编码器 + libx264 兜底；若首选已经是 libx264 则只跑一次
    first_args = encoder_args()
    is_sw = "libx264" in first_args
    attempts: list[tuple[str, list[str]]] = [("首选编码器", first_args)]
    if not is_sw:
        attempts.append(("libx264 兜底", _SW_FALLBACK))

    last_err = ""
    for label, codec_args in attempts:
        try:
            if out.exists():
                out.unlink()
        except OSError:
            pass

        code, err = _run(_ffmpeg_cmd(input_path, out, min_h, codec_args))
        if code == 0 and out.exists() and out.stat().st_size > 0:
            if label != "首选编码器":
                logger.warning("压缩在 %s 下成功", label)
            src_size = input_path.stat().st_size
            dst_size = out.stat().st_size
            logger.info(
                "压缩完成: %s (%dp → %dp, %d → %d bytes)",
                out.name, height, min_h, src_size, dst_size,
            )
            return out

        last_err = err
        logger.warning(
            "压缩失败（%s，code=%d）：%s", label, code, err[-300:].strip(),
        )

    raise CompressError(f"ffmpeg 压缩失败: {last_err[-500:]}")