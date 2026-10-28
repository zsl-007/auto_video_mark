import functools
import logging
import subprocess

logger = logging.getLogger(__name__)

_NVENC = "h264_nvenc"
_QSV = "h264_qsv"
_SW = "libx264"


def _probe_cmd(encoder: str) -> list[str]:
    """用 0.1 秒的极短输入真实跑一次编码，验证运行时可用性。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "color=c=black:s=640x480:d=0.1",
        "-frames:v", "1",
        "-c:v", encoder,
    ]
    if encoder == _NVENC:
        cmd += ["-preset", "p1", "-cq", "30"]
    elif encoder == _QSV:
        cmd += ["-global_quality", "30"]
    else:
        cmd += ["-preset", "ultrafast", "-crf", "30"]
    cmd += ["-f", "null", "-"]
    return cmd


def _encoder_works(encoder: str) -> bool:
    try:
        r = subprocess.run(
            _probe_cmd(encoder),
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("编码器 %s 探测异常: %s", encoder, exc)
        return False
    if r.returncode != 0:
        lines = (r.stderr or "").strip().splitlines()
        tail = lines[-1] if lines else ""
        logger.info("编码器 %s 运行时不可用，跳过。原因: %s", encoder, tail)
        return False
    return True


@functools.lru_cache(maxsize=1)
def best_encoder() -> str:
    """返回运行时确实可用的最佳编码器。

    ffmpeg -encoders 只表示编译期支持，不代表容器内 GPU 可用。
    必须实际编码一次验证，否则会在真实任务上因 "Could not open encoder" 崩溃。
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        logger.exception("无法执行 ffmpeg -encoders，回退 libx264")
        return _SW

    compiled = result.stdout or ""
    candidates: list[str] = []
    if _NVENC in compiled:
        candidates.append(_NVENC)
    if _QSV in compiled:
        candidates.append(_QSV)
    candidates.append(_SW)  # 永远兜底

    for enc in candidates:
        if _encoder_works(enc):
            logger.info("选用视频编码器: %s", enc)
            return enc
        logger.warning("编码器不可用，继续尝试下一个: %s", enc)

    logger.error("所有编码器探测失败，强制使用 libx264（可能导致编码错误）")
    return _SW


def encoder_args() -> list[str]:
    enc = best_encoder()
    if enc == _NVENC:
        return ["-c:v", enc, "-preset", "p4", "-cq", "23"]
    if enc == _QSV:
        return ["-c:v", enc, "-global_quality", "23"]
    return ["-c:v", _SW, "-preset", "medium", "-crf", "23"]