"""视频合并工具。"""

import json
import subprocess
import uuid
from pathlib import Path

from app.core.hardware import encoder_args


def _get_stream_info(path: Path) -> list[dict]:
    """获取视频流参数，用于判断是否可以安全进行流复制。"""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        (
            "stream=codec_type,codec_name,profile,"
            "width,height,pix_fmt,r_frame_rate,avg_frame_rate,"
            "sample_rate,channels,channel_layout,time_base"
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


def _stream_signature(streams: list[dict]) -> tuple:
    """提取用于比较的流参数。"""
    fields = (
        "codec_type",
        "codec_name",
        "profile",
        "width",
        "height",
        "pix_fmt",
        "r_frame_rate",
        "avg_frame_rate",
        "sample_rate",
        "channels",
        "channel_layout",
        "time_base",
    )

    return tuple(
        tuple(stream.get(field) for field in fields)
        for stream in streams
    )


def _escape_concat_path(path: Path) -> str:
    """
    转义 FFmpeg concat demuxer 文件路径。

    兼容路径中包含空格、单引号和反斜杠的情况。
    """
    value = str(path.resolve())
    value = value.replace("\\", "\\\\")
    value = value.replace("'", "'\\''")
    return value


def merge(
    segments: list[Path],
    output_path: Path,
    workdir: Path,
):
    """按顺序合并多个视频。"""
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

    # 每次合并使用独立列表文件，避免并发任务相互覆盖。
    list_file = workdir / (
        f"concat_{uuid.uuid4().hex}.txt"
    )

    try:
        with list_file.open(
            "w",
            encoding="utf-8",
        ) as file:
            for segment in segments:
                if not segment.exists():
                    raise FileNotFoundError(
                        f"待合并视频不存在: {segment}"
                    )

                file.write(
                    f"file '{_escape_concat_path(segment)}'\n"
                )

        stream_infos = [
            _get_stream_info(path)
            for path in segments
        ]

        # concat demuxer 要求各输入具有相同的流结构。
        topology = [
            tuple(
                stream.get("codec_type")
                for stream in streams
            )
            for streams in stream_infos
        ]

        if len(set(topology)) != 1:
            raise RuntimeError(
                "待合并视频的音视频流结构不一致，"
                "无法安全合并"
            )

        signatures = [
            _stream_signature(streams)
            for streams in stream_infos
        ]

        same_streams = len(set(signatures)) == 1

        has_audio = any(
            stream.get("codec_type") == "audio"
            for stream in stream_infos[0]
        )

        base_cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
        ]

        if same_streams:
            # 流参数完全一致时，优先直接复制，避免重新编码。
            cmd = [
                *base_cmd,
                "-c", "copy",
                "-movflags", "+faststart",
                str(output_path),
            ]
        else:
            # 流参数不一致时统一重新编码。
            cmd = [
                *base_cmd,
                *encoder_args(),
            ]

            if has_audio:
                cmd.extend([
                    "-c:a", "aac",
                ])
            else:
                cmd.append("-an")

            cmd.extend([
                "-movflags", "+faststart",
                str(output_path),
            ])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"视频合并失败: {result.stderr[-1500:]}"
            )

        if (
            not output_path.exists()
            or output_path.stat().st_size <= 0
        ):
            raise RuntimeError(
                f"视频合并输出为空: {output_path}"
            )

        return output_path

    finally:
        try:
            if list_file.exists():
                list_file.unlink()
        except OSError:
            pass