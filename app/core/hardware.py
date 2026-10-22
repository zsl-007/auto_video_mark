import functools
import subprocess


@functools.lru_cache(maxsize=1)
def best_encoder() -> str:
    """返回最佳可用的 ffmpeg 视频编码器名称。"""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True, text=True,
    )
    encoders = result.stdout
    if "h264_nvenc" in encoders:
        return "h264_nvenc"
    if "h264_qsv" in encoders:
        return "h264_qsv"
    return "libx264"


def encoder_args() -> list[str]:
    enc = best_encoder()
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "23"]
    return ["-c:v", enc, "-preset", "p4", "-cq", "23"]