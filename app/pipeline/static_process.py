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

    通过调整 mpdecimate 的 max 参数，逐步减少静态帧，
    使最终视频的动态帧比例达到 target_ratio。

    动态帧比例 = 完全去重后保留的帧数 / 回收后总帧数

    返回最终文件实际检测到的动态帧比例。
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

    total_frames = _get_total_frames(segment_path)

    # 完全去重后保留的动态帧数。
    # max=0 表示不限制连续丢帧，得到完全去重结果。
    dynamic_frames = _count_frames_after_mpdecimate(
        segment_path,
        max_drop=0,
    )

    if dynamic_frames <= 0:
        raise RuntimeError(
            f"视频没有可保留的动态帧: {segment_path}"
        )

    # 回收后允许保留的最大总帧数。
    # 例如动态帧 8500，目标比例 0.6：
    # 最多允许保留 floor(8500 / 0.6) = 14166 帧。
    max_allowed_frames = min(
        total_frames,
        int(math.floor(dynamic_frames / target_ratio)),
    )

    # --------------------------------------------------
    # 搜索正确的 mpdecimate max 参数
    # --------------------------------------------------
    #
    # 注意：
    # max=0 是特殊值，表示不限制连续丢帧。
    # 对于正数 max：
    #   max 越大，允许连续丢弃的静态帧越多；
    #   输出帧数通常越少。
    #
    # 因此从 max=1 开始递增，寻找第一个满足：
    # 输出总帧数 <= max_allowed_frames 的参数。
    #
    # 不能把 max=0 和正数参数放在同一个单调搜索区间里。
    # --------------------------------------------------

    best_max_drop = 0

    if max_allowed_frames < total_frames:
        low = 0
        high = 1

        high_count = _count_frames_after_mpdecimate(
            segment_path,
            max_drop=high,
        )

        # 指数搜索：寻找一个能够达到目标比例的上界。
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

            # 二分搜索：
            # 找到满足目标比例的最小正数 max。
            # 这样可以尽可能多地保留静态帧。
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
            # 极端情况下，如果正数 max 无法达到目标，
            # 使用 max=0 的完全去重结果兜底。
            best_max_drop = 0

    from app.core.hardware import encoder_args

    filter_expr = (
        f"mpdecimate=frac={MPDECIMATE_FRAC}:"
        f"max={best_max_drop},"
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

    try:
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

        # 对最终生成的视频重新检测，不能只相信搜索估算值。
        actual_ratio = process_static(output_path)

        if actual_ratio + 1e-6 < target_ratio:
            raise RuntimeError(
                "静态不足回收后比例仍未达标："
                f"目标={target_ratio:.4f}，"
                f"实际={actual_ratio:.4f}，"
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