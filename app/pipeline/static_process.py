"""静态处理：检测、完全去重和静态不足回收。"""

import math
import re
import subprocess
from pathlib import Path

from app.core.config import MPDECIMATE_FRAC


def _get_total_frames(video_path: Path) -> int:
    """通过 ffprobe 获取视频实际读取帧数。"""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-count_frames",
        "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames,nb_frames",
        "-of", "default=nokey=1:noprint_wrappers=1",
        str(video_path),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
    )

    for value in result.stdout.splitlines():
        value = value.strip()
        if value.isdigit():
            count = int(value)
            if count > 0:
                return count

    raise RuntimeError(f"无法获取视频总帧数: {video_path}")


def _count_frames_after_mpdecimate(
    video_path: Path,
    max_drop: int = 0,
) -> int:
    """
    统计 mpdecimate 过滤后输出的帧数。

    max_drop=0：不限制连续丢帧，执行完全去重。
    max_drop>0：限制连续丢帧数量，保留部分静态帧。
    """
    filter_expr = (
        f"mpdecimate=frac={MPDECIMATE_FRAC}:max={int(max_drop)}"
    )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-i", str(video_path),
        "-vf", filter_expr,
        "-an",
        "-progress", "pipe:1",
        "-f", "null",
        "-",
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"mpdecimate 处理失败: {video_path}\n"
            f"{result.stderr[-1000:]}"
        )

    frames = re.findall(
        r"(?m)^frame=\s*(\d+)\s*$",
        result.stdout,
    )

    if not frames:
        raise RuntimeError(
            f"无法获取 mpdecimate 输出帧数: {video_path}"
        )

    return int(frames[-1])


def process_static(segment_path: Path) -> float:
    """
    返回分段动态帧保留比例。

    计算方式：
        完全去重后保留帧数 / 原视频总帧数

    例如：
        0.085 表示完全去重后保留了原始视频 8.5% 的帧。
    """
    total_frames = _get_total_frames(segment_path)

    dynamic_frames = _count_frames_after_mpdecimate(
        segment_path,
        max_drop=0,
    )

    if total_frames <= 0:
        raise RuntimeError(f"视频总帧数无效: {segment_path}")

    return min(1.0, dynamic_frames / total_frames)


def is_static_segment(
    segment_path: Path,
    threshold: float | None = None,
) -> bool:
    """判断分段是否低于动态帧比例阈值。"""
    if threshold is None:
        from app.core.runtime_settings import get_settings
        threshold = float(
            get_settings()["static_process_threshold"]
        )

    return process_static(segment_path) < threshold


def recover_static_frames(
    segment_path: Path,
    output_path: Path,
    target_ratio: float,
) -> float:
    """
    静态不足回收。

    动态帧比例统一定义为：

        原视频完全去重后保留的动态帧数
        --------------------------------
             回收后视频的实际总帧数

    通过调整 mpdecimate max 参数，尽量保留静态帧，
    同时使动态帧比例达到目标值。

    返回实际计算得到的动态帧比例。
    """
    target_ratio = float(target_ratio)

    if not 0.0 < target_ratio <= 1.0:
        raise ValueError(
            "静态不足回收目标比例必须大于 0 且不超过 1"
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # 原视频实际总帧数
    total_frames = _get_total_frames(segment_path)

    if total_frames <= 0:
        raise RuntimeError(
            f"原视频总帧数无效: {segment_path}"
        )

    # 原视频完全去重后保留的动态帧数
    dynamic_frames = _count_frames_after_mpdecimate(
        segment_path,
        max_drop=0,
    )

    if dynamic_frames <= 0:
        raise RuntimeError(
            f"没有检测到可保留的动态帧: {segment_path}"
        )

    # 达到目标比例允许的最大总帧数
    max_allowed_frames = int(
        math.floor(dynamic_frames / target_ratio)
    )

    max_allowed_frames = min(
        total_frames,
        max_allowed_frames,
    )

    # --------------------------------------------------
    # 搜索 max 参数
    # --------------------------------------------------

    best_max_drop = 0

    if max_allowed_frames < total_frames:

        low = 0
        high = 1

        high_count = _count_frames_after_mpdecimate(
            segment_path,
            max_drop=high,
        )

        # 指数搜索，寻找满足目标比例的上界
        while (
            high_count > max_allowed_frames
            and high < total_frames
        ):
            low = high

            high = min(
                high * 2,
                total_frames,
            )

            high_count = _count_frames_after_mpdecimate(
                segment_path,
                max_drop=high,
            )

        if high_count <= max_allowed_frames:

            # 二分搜索，找到满足条件的最小 max
            while high - low > 1:

                mid = (low + high) // 2

                mid_count = _count_frames_after_mpdecimate(
                    segment_path,
                    max_drop=mid,
                )

                if mid_count <= max_allowed_frames:
                    high = mid
                else:
                    low = mid

            best_max_drop = high

        else:
            # 无法找到合适参数时，使用完全去重兜底
            best_max_drop = 0

    from app.core.hardware import encoder_args

    def encode_recovered(max_drop: int):
        """执行一次回收编码。"""

        filter_expr = (
            f"mpdecimate=frac={MPDECIMATE_FRAC}:"
            f"max={max_drop},"
            "setpts=N/FRAME_RATE/TB"
        )

        cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(segment_path),
            "-vf", filter_expr,
            "-an",
            *encoder_args(),
            "-movflags", "+faststart",
            str(output_path),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                "静态不足回收编码失败："
                f"{result.stderr[-1500:]}"
            )

        if (
            not output_path.exists()
            or output_path.stat().st_size <= 0
        ):
            raise RuntimeError(
                f"静态不足回收输出文件为空: {output_path}"
            )

        # 只读取回收视频实际总帧数。
        # 不再对重新编码后的视频重复运行 mpdecimate。
        recovered_total_frames = _get_total_frames(
            output_path
        )

        if recovered_total_frames <= 0:
            raise RuntimeError(
                f"无法获取回收视频总帧数: {output_path}"
            )

        actual_ratio = min(
            1.0,
            dynamic_frames / recovered_total_frames,
        )

        return actual_ratio, recovered_total_frames

    try:

        # --------------------------------------------------
        # 第一次：按搜索得到的 max 参数回收
        # --------------------------------------------------

        actual_ratio, recovered_total_frames = encode_recovered(
            best_max_drop
        )

        # --------------------------------------------------
        # 第二次：如果仍未达标，完全去重兜底
        # --------------------------------------------------

        if actual_ratio + 1e-6 < target_ratio:

            if best_max_drop != 0:

                actual_ratio, recovered_total_frames = (
                    encode_recovered(0)
                )

        # --------------------------------------------------
        # 最终验证
        # --------------------------------------------------

        if actual_ratio + 1e-6 < target_ratio:

            raise RuntimeError(
                "静态不足回收最终仍未达到目标："
                f"目标比例={target_ratio:.4%}，"
                f"实际比例={actual_ratio:.4%}，"
                f"原始动态帧数={dynamic_frames}，"
                f"回收后总帧数={recovered_total_frames}，"
                f"max={best_max_drop}"
            )

        return actual_ratio

    except Exception:
        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass

        raise

def drop_static_frames(
    segment_path: Path,
    output_path: Path,
) -> Path:
    """
    完全去重，删除近似重复帧。

    输出视频会缩短时长，并且不保留音频。
    """
    from app.core.hardware import encoder_args

    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(segment_path),
        "-vf",
        (
            f"mpdecimate=frac={MPDECIMATE_FRAC},"
            "setpts=N/FRAME_RATE/TB"
        ),
        "-an",
        *encoder_args(),
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"帧级去重失败: {result.stderr[-1000:]}"
        )

    if (
        not output_path.exists()
        or output_path.stat().st_size <= 0
    ):
        raise RuntimeError(
            f"帧级去重输出为空: {output_path}"
        )

    return output_path