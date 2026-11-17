
"""视频合并工具。

采用 FFmpeg concat filter 解码后拼接，避免 concat demuxer
直接流复制时因输入文件时长或时间戳不准确而产生花屏、丢帧。
"""

import json
import subprocess
from pathlib import Path
import logging

from app.core.hardware import encoder_args

logger = logging.getLogger(__name__)

def _get_stream_info(path: Path) -> list[dict]:
    """获取视频流信息。"""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        (
            "stream=codec_type,codec_name,profile,"
            "width,height,pix_fmt,r_frame_rate,avg_frame_rate,"
            "sample_rate,channels,channel_layout"
        ),
        "-of", "json",
        str(path),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
    )

    data = json.loads(result.stdout or "{}")
    return data.get("streams", [])


def merge(
    segments: list[Path],
    output_path: Path,
    workdir: Path,
):
    """按顺序解码、拼接并重新编码多个视频。"""

    if not segments:
        raise ValueError("没有可合并的视频")

    if len(segments) == 1:
        raise ValueError("只有一个视频，无需执行合并")

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    workdir.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        # --------------------------------------------------
        # 1. 检查所有输入文件
        # --------------------------------------------------

        stream_infos = []

        for path in segments:

            if not path.exists():
                raise FileNotFoundError(
                    f"待合并视频不存在: {path}"
                )

            streams = _get_stream_info(path)

            video_streams = [
                s for s in streams
                if s.get("codec_type") == "video"
            ]

            audio_streams = [
                s for s in streams
                if s.get("codec_type") == "audio"
            ]

            if len(video_streams) != 1:
                raise RuntimeError(
                    f"视频必须且只能包含一个视频流: {path}"
                )

            if len(audio_streams) > 1:
                raise RuntimeError(
                    f"暂不支持多个音频流: {path}"
                )

            stream_infos.append({
                "video": video_streams[0],
                "has_audio": len(audio_streams) == 1,
            })

        # 所有文件必须具有相同的音频结构。
        audio_flags = {
            info["has_audio"]
            for info in stream_infos
        }

        if len(audio_flags) != 1:
            raise RuntimeError(
                "待合并视频的音频流结构不一致"
            )

        has_audio = stream_infos[0]["has_audio"]

        # --------------------------------------------------
        # 2. 构建 FFmpeg 输入参数
        # --------------------------------------------------

        cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
        ]

        for path in segments:
            cmd.extend([
                "-i",
                str(path),
            ])

        # --------------------------------------------------
        # 3. 构建 concat filter
        # --------------------------------------------------

        filters = []
        concat_inputs = []

        for index in range(len(segments)):

            # 视频时间戳归零，并统一像素格式。
            video_label = f"v{index}"

            filters.append(
                f"[{index}:v:0]"
                f"setpts=PTS-STARTPTS,"
                f"format=yuv420p"
                f"[{video_label}]"
            )

            concat_inputs.append(
                f"[{video_label}]"
            )

            # 如果存在音频，也同步归零。
            if has_audio:

                audio_label = f"a{index}"

                filters.append(
                    f"[{index}:a:0]"
                    f"asetpts=PTS-STARTPTS"
                    f"[{audio_label}]"
                )

                concat_inputs.append(
                    f"[{audio_label}]"
                )

        concat_filter = (
            "".join(concat_inputs)
            + f"concat=n={len(segments)}:"
            + f"v=1:a={1 if has_audio else 0}"
        )

        if has_audio:
            concat_filter += "[outv][outa]"
        else:
            concat_filter += "[outv]"

        filters.append(concat_filter)

        filter_complex = ";".join(filters)

        cmd.extend([
            "-filter_complex",
            filter_complex,
            "-map",
            "[outv]",
        ])


        # --------------------------------------------------
        # 4. 构建编码参数
        # --------------------------------------------------

        selected_encoder_args = encoder_args()

        encode_cmd = list(cmd)

        if has_audio:
            encode_cmd.extend([
                "-map", "[outa]",
                "-c:a", "aac",
            ])
        else:
            encode_cmd.append("-an")

        encode_cmd.extend([
            "-movflags", "+faststart",
            str(output_path),
        ])

        # --------------------------------------------------
        # 5. 执行合并，硬件编码失败时回退 CPU
        # --------------------------------------------------

        def run_merge(codec_args: list[str]):
            """
            使用指定编码器执行视频合并。
            每次执行前删除可能残留的不完整输出文件。
            """

            try:
                if output_path.exists():
                    output_path.unlink()
            except OSError as e:
                raise RuntimeError(
                    f"无法清理旧的合并输出文件: {e}"
                ) from e

            run_cmd = (
                encode_cmd[:-len([
                    "-movflags", "+faststart",
                    str(output_path),
                ])]
                + codec_args
                + [
                    "-movflags", "+faststart",
                    str(output_path),
                ]
            )

            result = subprocess.run(
                run_cmd,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr[-3000:]
                    or "FFmpeg 合并编码失败"
                )

            if (
                not output_path.exists()
                or output_path.stat().st_size <= 0
            ):
                raise RuntimeError(
                    "FFmpeg 合并完成，但输出文件为空"
                )

        try:
            # 优先使用硬件编码器或当前探测到的编码器
            run_merge(selected_encoder_args)

        except Exception as hardware_error:

            # 只有硬件编码器失败时才回退 CPU。
            # libx264 自身失败时直接抛出错误。
            hardware_encoder_selected = any(
                encoder in selected_encoder_args
                for encoder in (
                    "h264_nvenc",
                    "h264_qsv",
                )
            )

            if not hardware_encoder_selected:
                raise RuntimeError(
                    f"视频合并失败: {hardware_error}"
                ) from hardware_error

            logger.warning(
                "视频合并硬件编码失败，尝试回退 libx264。"
                "原始错误: %s",
                hardware_error,
            )

            try:
                run_merge([
                    "-c:v", "libx264",
                    "-preset", "medium",
                    "-crf", "23",
                ])

                logger.info(
                    "视频合并已通过 libx264 CPU 编码完成: %s",
                    output_path,
                )

            except Exception as cpu_error:
                raise RuntimeError(
                    "视频合并失败：硬件编码和 CPU 编码均失败。"
                    f"硬件编码错误: {hardware_error}；"
                    f"CPU 编码错误: {cpu_error}"
                ) from cpu_error

        # --------------------------------------------------
        # 6. 检查输出是否可以正常读取
        # --------------------------------------------------

        output_streams = _get_stream_info(output_path)

        output_video = [
            s for s in output_streams
            if s.get("codec_type") == "video"
        ]

        if len(output_video) != 1:
            raise RuntimeError(
                "合并后的视频流数量异常"
            )

        logger_message = (
            f"视频合并成功：输入 {len(segments)} 段，"
            f"输出 {output_path.name}"
        )

        print(logger_message)

        return output_path

    except Exception:
        # 删除不完整的输出文件，避免被误认为合并成功。
        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass

        raise
