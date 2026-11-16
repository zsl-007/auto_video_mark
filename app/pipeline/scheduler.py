import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime

from app.core.config import (
    INPUT_DIR, OUTPUT_DIR, WORKDIR,
    MAX_WORKERS_PER_STAGE, QUEUE_MAXSIZE,
    RESOURCE_CHECK_INTERVAL,
)

import json
import re
import subprocess

from app.core.resources import resource_ok
from app.core.runtime_settings import get_settings
from app.db.session import SessionLocal
from app.db.models import Task, Segment, ErrorLog
from app.pipeline.precheck import precheck
from app.pipeline.compress import compress
from app.pipeline.segment import segment
from app.pipeline.static_process import ( process_static, drop_static_frames, recover_static_frames, )
from app.pipeline.anonymize import anonymize
from app.pipeline.merge import merge
from app.ws.progress import manager

from app.pipeline.task_control import (TaskControl,TaskStopped,TaskCancelled,)

import cv2
import threading
import time

logger = logging.getLogger(__name__)

def _get_video_frame_count(path: Path) -> int:
    """
    获取视频总帧数，只读取视频元数据，不解码全部视频。
    """
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return 0
        return max(0,int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
    finally:
        cap.release()

def _probe_video_metadata(path: Path) -> dict:
    """
    使用 ffprobe 读取视频元数据。

    duration：视频时长
    width/height：视频分辨率
    format_tags/stream_tags：媒体元数据，用于提取拍摄日期
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )

        data = json.loads(result.stdout or "{}")

        format_info = data.get("format", {})
        streams = data.get("streams", [])

        duration = float(format_info.get("duration") or 0)

        width = 0
        height = 0

        for stream in streams:
            if stream.get("codec_type") == "video":
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
                break

        # ffprobe 时长获取失败时，使用 OpenCV 元数据兜底
        if duration <= 0:
            cap = cv2.VideoCapture(str(path))
            try:
                if cap.isOpened():
                    fps = cap.get(cv2.CAP_PROP_FPS) or 0
                    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0

                    if fps > 0:
                        duration = frames / fps
            finally:
                cap.release()

        if duration <= 0 or width <= 0 or height <= 0:
            raise RuntimeError(
                f"无法获取有效视频时长或分辨率: {path}"
            )

        return {
            "duration": duration,
            "width": width,
            "height": height,
            "format_tags": format_info.get("tags", {}),
            "streams": streams,
        }

    except Exception as e:
        raise RuntimeError(
            f"读取视频元数据失败: {path}, {e}"
        ) from e


def _get_capture_date(source: Path) -> str:
    """
    从原始视频元数据中提取拍摄日期。

    优先级：
    1. creation_time
    2. com.apple.quicktime.creationdate
    3. date

    如果原视频没有可用的拍摄日期元数据，
    则使用原视频文件的修改日期作为兜底。
    """
    try:
        info = _probe_video_metadata(source)

        tag_groups = [
            info.get("format_tags", {}),
        ]

        for stream in info.get("streams", []):
            tag_groups.append(stream.get("tags", {}))

        preferred_keys = (
            "creation_time",
            "com.apple.quicktime.creationdate",
            "date",
        )

        for preferred in preferred_keys:
            for tags in tag_groups:
                for key, value in tags.items():
                    if key.lower() != preferred or not value:
                        continue

                    # 兼容：
                    # 2026-09-11T08:30:00Z
                    # 2026:09:11 08:30:00
                    # 20260911
                    match = re.search(
                        r"(\d{4})[-:]?(\d{2})[-:]?(\d{2})",
                        str(value),
                    )

                    if match:
                        return "".join(match.groups())

    except Exception:
        logger.warning(
            "读取原视频拍摄日期元数据失败: %s",
            source,
            exc_info=True,
        )

    # 没有拍摄日期元数据时，使用文件修改日期兜底
    return datetime.fromtimestamp(
        source.stat().st_mtime
    ).strftime("%Y%m%d")


def _get_original_stem(source: Path) -> str:
    """
    获取原始文件名主体，去掉 watcher.py 添加的目录前缀。

    例如：
    input/校园出入口与周界/校门/
        校园出入口与周界_校门_ID65543.mp4

    还原为：
        ID65543
    """
    original_stem = source.stem

    try:
        relative_parent = source.parent.relative_to(INPUT_DIR)
        folder_parts = list(relative_parent.parts)
    except ValueError:
        folder_parts = []

    if folder_parts:
        prefix = "_".join(folder_parts) + "_"

        if original_stem.startswith(prefix):
            original_stem = original_stem[len(prefix):]

    return original_stem


def _build_output_filename(
    source: Path,
    output_path: Path,
    capture_date: str,
    segment_index: int | None = None,
) -> Path:
    """
    生成最终输出视频文件名。

    单文件或合并输出：
        ID65543-目录1-目录2-时长-宽x高-拍摄日期.mp4

    多分段且未合并：
        ID65543_0001-目录1-目录2-时长-宽x高-拍摄日期.mp4

    时长和分辨率均读取最终输出视频本身。
    """
    info = _probe_video_metadata(output_path)

    # 四舍五入为整数秒
    duration_seconds = int(info["duration"] + 0.5)

    resolution = (
        f"{info['width']}x{info['height']}"
    )

    original_stem = _get_original_stem(source)

    try:
        relative_parent = source.parent.relative_to(INPUT_DIR)
        folder_parts = list(relative_parent.parts)
    except ValueError:
        folder_parts = []

    # 多分段时，把编号放在原始 ID 后面
    if segment_index is not None:
        original_stem = (
            f"{original_stem}_{segment_index:04d}"
        )

    name_parts = [
        original_stem,
        *folder_parts,
        str(duration_seconds),
        resolution,
        capture_date,
    ]

    filename = "-".join(
        part for part in name_parts if part
    )

    target = OUTPUT_DIR / (
        f"{filename}{output_path.suffix.lower()}"
    )

    # 防止不同任务产生同名文件时覆盖已有结果
    if target.exists() and target.resolve() != output_path.resolve():
        index = 1

        while True:
            candidate = OUTPUT_DIR / (
                f"{filename}-{index}{output_path.suffix.lower()}"
            )

            if not candidate.exists():
                target = candidate
                break

            index += 1

    return target


def _rename_output_files(
    source: Path,
    output_paths: list[Path],
) -> list[Path]:
    """
    批量重命名最终输出文件。

    只有实际产生多个独立输出文件时，
    才添加 _0001、_0002 等分段编号。
    """
    capture_date = _get_capture_date(source)

    multi_output = len(output_paths) > 1

    renamed_paths = []

    for index, output_path in enumerate(output_paths):
        segment_index = index + 1 if multi_output else None

        target = _build_output_filename(
            source=source,
            output_path=output_path,
            capture_date=capture_date,
            segment_index=segment_index,
        )

        if output_path.resolve() != target.resolve():
            output_path.rename(target)

        logger.info(
            "最终输出视频命名完成: %s -> %s",
            output_path.name,
            target.name,
        )

        renamed_paths.append(target)

    return renamed_paths

def _merge_small_video_groups(
    video_paths: list[Path],
    stem: str,
    min_duration: int = 300,
) -> list[Path]:
    """
    只合并实际时长小于 min_duration 的连续视频分段。

    规则：
    1. 使用匿名化后视频的实际时长判断。
    2. 时长 >= min_duration：直接保留，不参与合并。
    3. 时长 < min_duration：进入待合并组。
    4. 连续短视频达到 2 个及以上时执行合并。
    5. 只有 1 个短视频时不执行合并，按原有规则检查并丢弃。
    6. 合并后检查实际时长，低于 min_duration 则丢弃。
    7. 保持原视频顺序。
    """

    if not video_paths:
        return []

    if min_duration <= 0:
        raise ValueError("小视频时长阈值必须大于 0")

    output_paths: list[Path] = []
    pending: list[Path] = []

    def flush_pending():
        """处理连续的小于阈值的视频分组。"""
        nonlocal pending

        if not pending:
            return

        # --------------------------------------------------
        # 只有一个短视频：不合并，检查是否达到保留阈值
        # --------------------------------------------------

        if len(pending) == 1:
            video_path = pending[0]
            info = _probe_video_metadata(video_path)
            duration = float(info["duration"])

            if duration >= min_duration:
                output_paths.append(video_path)

                logger.info(
                    "小视频保留：%s，时长 %.2f 秒",
                    video_path.name,
                    duration,
                )
            else:
                logger.info(
                    "小视频丢弃：%s，时长 %.2f 秒，"
                    "低于阈值 %d 秒",
                    video_path.name,
                    duration,
                    min_duration,
                )

            pending = []
            return

        # --------------------------------------------------
        # 多个连续短视频：执行合并
        # --------------------------------------------------

        merged_path = WORKDIR / (
            f"{stem}_smallmerge_{uuid.uuid4().hex}.mp4"
        )

        logger.info(
            "开始合并短视频：%d 个，阈值 %d 秒",
            len(pending),
            min_duration,
        )

        try:
            merge(
                list(pending),
                merged_path,
                WORKDIR,
            )

            if not merged_path.exists():
                raise RuntimeError(
                    f"小视频合并失败，未生成文件：{merged_path}"
                )

            info = _probe_video_metadata(merged_path)
            merged_duration = float(info["duration"])

            if merged_duration >= min_duration:
                output_paths.append(merged_path)

                logger.info(
                    "小视频合并完成并保留：%s，合并后 %.2f 秒",
                    merged_path.name,
                    merged_duration,
                )
            else:
                logger.info(
                    "小视频合并结果丢弃：%s，合并后 %.2f 秒，"
                    "低于阈值 %d 秒",
                    merged_path.name,
                    merged_duration,
                    min_duration,
                )

                merged_path.unlink(missing_ok=True)

        except Exception:
            try:
                merged_path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "清理小视频合并失败产物异常：%s",
                    merged_path,
                )
            raise

        finally:
            pending = []

    # --------------------------------------------------
    # 按视频原始顺序判断
    # --------------------------------------------------

    for video_path in video_paths:
        info = _probe_video_metadata(video_path)
        duration = float(info["duration"])

        if duration < min_duration:
            # 只有小于小视频时长阈值的分段才进入合并组
            pending.append(video_path)

            logger.info(
                "发现短视频：%s，时长 %.2f 秒，低于阈值 %d 秒",
                video_path.name,
                duration,
                min_duration,
            )
            continue

        # 遇到达到阈值的视频：
        # 先处理之前积累的短视频。
        flush_pending()

        # 当前视频直接保留，绝不参与小视频合并。
        output_paths.append(video_path)

        logger.info(
            "正常分段直接保留：%s，时长 %.2f 秒，"
            "达到小视频阈值 %d 秒",
            video_path.name,
            duration,
            min_duration,
        )

    # 处理最后一组短视频
    flush_pending()

    logger.info(
        "小视频处理完成：原视频 %d 个，最终保留 %d 个，"
        "小视频时长阈值 %d 秒",
        len(video_paths),
        len(output_paths),
        min_duration,
    )

    if not output_paths:
        raise RuntimeError(
            f"小视频处理后没有可保留的视频："
            f"所有短视频处理结果均低于 {min_duration} 秒"
        )

    return output_paths


class PipelineScheduler:
    _active_instance = None # 保存当前运行的调度器实例，供 tasks.py 的控制 API 使用

    def __init__(self):
        self.queue: asyncio.Queue[Optional[str]] = asyncio.Queue(
            maxsize=QUEUE_MAXSIZE
        )
        self.workers: list[asyncio.Task] = []
        self._running = False
        self._resource_task: Optional[asyncio.Task] = None
        self.gpu_sem: Optional[asyncio.Semaphore] = None

        # 每个任务独立的控制器
        self.controls: dict[str, TaskControl] = {}

        # 供 API 获取当前运行的调度器实例
        PipelineScheduler._active_instance = self

    @classmethod
    def get_active_instance(cls):
        return cls._active_instance

    def get_control(self, task_id: str) -> TaskControl:
        control = self.controls.get(task_id)

        if control is None:
            control = TaskControl()
            self.controls[task_id] = control

        return control

    async def checkpoint(self, task_id: str):
        control = self.get_control(task_id)
        await control.checkpoint_async()

    def set_stage(self, task_id: str, status: str):
        control = self.get_control(task_id)
        control.current_stage = status

    def pause_task(self, task_id: str):
        self.get_control(task_id).pause()

    def resume_task(self, task_id: str):
        self.get_control(task_id).resume()

    def stop_task(self, task_id: str):
        self.get_control(task_id).request_stop()

    def cancel_task(self, task_id: str):
        self.get_control(task_id).request_cancel()

    async def start(self):
        self._running = True
        settings = get_settings()
        gpu_limit = max(1, int(settings.get("anonymize_parallel_segments", 4)))
        self.gpu_sem = asyncio.Semaphore(gpu_limit)
        max_tasks = max(1, int(settings.get("max_concurrent_tasks", MAX_WORKERS_PER_STAGE)))
        logger.info("调度器已启动，worker 数: %d，GPU 段级并发上限: %d",
                    max_tasks, gpu_limit)

        for _ in range(max_tasks):
            self.workers.append(asyncio.create_task(self._worker()))
        self._resource_task = asyncio.create_task(self._resource_monitor())

    async def stop(self):
        self._running = False
        for _ in range(len(self.workers)):
            await self.queue.put(None)
        await asyncio.gather(*self.workers, return_exceptions=True)
        if self._resource_task:
            self._resource_task.cancel()
        logger.info("调度器已停止")

    async def enqueue(self, task_id: str):
        self.get_control(task_id)
        await self.queue.put(task_id)
        logger.info("任务入队: %s", task_id)

    async def _resource_monitor(self):
        while self._running:
            if not resource_ok():
                logger.warning("资源水位偏高，暂停取新任务")
            await asyncio.sleep(RESOURCE_CHECK_INTERVAL)


    async def _worker(self):
        terminal_statuses = {
            "completed",
            "failed",
            "stopped",
            "cancelled",
        }

        while self._running:
            task_id = await self.queue.get()

            if task_id is None:
                self.queue.task_done()
                break

            try:
                # 用户可能在任务排队期间已经取消、停止或删除任务
                db = SessionLocal()
                try:
                    task = db.query(Task).filter(
                        Task.id == task_id
                    ).first()

                    if not task or task.status in terminal_statuses:
                        self.controls.pop(task_id, None)
                        continue
                finally:
                    db.close()

                if not resource_ok():
                    await self.queue.put(task_id)
                    await asyncio.sleep(RESOURCE_CHECK_INTERVAL)
                    continue

                try:
                    await self._process(task_id)

                except Exception as e:
                    logger.exception( "任务处理异常：%s", task_id, )

                    # _process() 正常情况下已经处理了失败状态。
                    # 这里仅处理 _process() 尚未成功写入失败状态的情况。
                    check_db = SessionLocal()

                    try:
                        task = check_db.query(Task).filter( Task.id == task_id ).first()

                        needs_mark_failed = (
                            task is not None
                            and task.status not in terminal_statuses
                        )

                    finally:
                        check_db.close()

                    if needs_mark_failed:
                        try:
                            self._mark_failed( task_id, "unknown", str(e), )

                        except Exception:
                            logger.exception( "兜底 mark_failed 失败：%s", task_id, )

            finally:
                self.queue.task_done()

    async def _process(self, task_id: str):
        db = SessionLocal()
        task = db.query(Task).filter( Task.id == task_id ).first()

        if not task:
            db.close()
            self.controls.pop(task_id, None)
            return

        # 防止 Worker 取出任务后，
        # 用户已经通过 API 将任务停止、取消或删除。
        if task.status in { "completed", "failed", "stopped", "cancelled", }:
            db.close()
            self.controls.pop(task_id, None)
            logger.info( "任务已经处于终态，跳过执行：%s，状态：%s", task_id, task.status, )
            return

        stem: str = ""
        success = False
        cancelled = False

        try:
            # 任务正式开始前，先检查控制状态
            await self.checkpoint(task_id)

            settings = get_settings()

            task.started_at = datetime.utcnow()
            task.status = "prechecking"

            self.set_stage(task_id, "prechecking")

            db.commit()

            source = Path(task.source_path)
            stem = source.stem

            await self.checkpoint(task_id)
            task.status = "prechecking"
            self.set_stage(task_id, "prechecking")
            db.commit()
            await manager.broadcast({"task_id": task_id,"status": "prechecking",})
            prechecked = await asyncio.to_thread(precheck, source, WORKDIR,)
            # 预检完成后检查
            await self.checkpoint(task_id)

            await self.checkpoint(task_id)
            task.status = "compressing"
            self.set_stage(task_id, "compressing")
            db.commit()
            await manager.broadcast({ "task_id": task_id, "status": "compressing", })
            compressed = await asyncio.to_thread( compress, prechecked, WORKDIR, settings, stem, )
            await self.checkpoint(task_id)

            await self.checkpoint(task_id)
            task.status = "segmenting"
            self.set_stage(task_id, "segmenting")
            db.commit()
            await manager.broadcast({ "task_id": task_id, "status": "segmenting", })
            segments = await asyncio.to_thread( segment, compressed, WORKDIR, settings, )
            await self.checkpoint(task_id)

            # --------------------------------------------------
            # 静态处理阶段
            # --------------------------------------------------

            if settings["static_process_enabled"]:
                task.status = "static_processing"
                self.set_stage(task_id, "static_processing")
                db.commit()

                await manager.broadcast({
                    "task_id": task_id,
                    "status": "static_processing",
                })

                threshold = float(
                    settings["static_process_threshold"]
                )

                frame_drop = bool(
                    settings.get(
                        "static_frame_drop_enabled",
                        False,
                    )
                )

                recovery_enabled = bool(
                    settings.get(
                        "static_recovery_enabled",
                        False,
                    )
                )

                kept = []

                for idx, seg in enumerate(segments):

                    # 每个分段处理前检查
                    await self.checkpoint(task_id)

                    # 原始动态帧比例
                    ratio = await asyncio.to_thread(
                        process_static,
                        seg,
                    )

                    # 静态检测完成后检查
                    await self.checkpoint(task_id)

                    logger.info(
                        "静态检测：%s，动态帧比例 %.2f%%，阈值 %.2f%%",
                        seg.name,
                        ratio * 100,
                        threshold * 100,
                    )

                    # --------------------------------------------------
                    # 情况一：动态帧比例低于阈值
                    # --------------------------------------------------

                    if ratio < threshold:

                        # 未开启回收：维持原有行为，直接丢弃。
                        if not recovery_enabled:

                            logger.info(
                                "分段丢弃：%s，动态帧比例 %.2f%%",
                                seg.name,
                                ratio * 100,
                            )

                            db.add(Segment(
                                task_id=task_id,
                                index=idx,
                                path=str(seg),
                                status="dropped",
                                frame_ratio=ratio,
                            ))

                            db.commit()
                            continue

                        # 开启回收：删除部分静态帧。
                        recovered_path = WORKDIR / (
                            f"{stem}_recovered_{idx:04d}_"
                            f"{uuid.uuid4().hex[:8]}.mp4"
                        )

                        try:
                            await self.checkpoint(task_id)

                            final_ratio = await asyncio.to_thread(
                                recover_static_frames,
                                seg,
                                recovered_path,
                                threshold,
                            )

                            await self.checkpoint(task_id)

                            logger.info(
                                "静态不足回收成功：%s，"
                                "原比例 %.2f%%，回收后 %.2f%%",
                                seg.name,
                                ratio * 100,
                                final_ratio * 100,
                            )

                            db.add(Segment(
                                task_id=task_id,
                                index=idx,
                                path=str(recovered_path),
                                status="kept",
                                frame_ratio=final_ratio,
                            ))

                            db.commit()

                            kept.append(recovered_path)

                            # 非常重要：
                            # 回收成功后不能再执行完全去重。
                            continue

                        except (TaskStopped, TaskCancelled):
                            raise
                        except Exception as e:

                            logger.exception(
                                "静态不足回收失败，丢弃该分段：%s，失败原因：%s",
                                seg,
                                str(e),
                            )

                            # 回收失败不能把未达标原视频当作成功输出。
                            db.add(Segment(
                                task_id=task_id,
                                index=idx,
                                path=str(seg),
                                status="dropped",
                                frame_ratio=ratio,
                            ))

                            db.commit()

                            # 清理可能产生的失败输出。
                            try:
                                if recovered_path.exists():
                                    recovered_path.unlink()
                            except OSError:
                                logger.warning(
                                    "清理回收失败文件异常：%s",
                                    recovered_path,
                                )

                            continue

                    # --------------------------------------------------
                    # 情况二：动态帧比例达到阈值
                    # --------------------------------------------------

                    if frame_drop:

                        deduped = WORKDIR / (
                            f"{stem}_dedup_{idx:04d}.mp4"
                        )

                        try:
                            await self.checkpoint(task_id)
                            await asyncio.to_thread(
                                drop_static_frames,
                                seg,
                                deduped,
                            )
                            await self.checkpoint(task_id)

                            db.add(Segment(
                                task_id=task_id,
                                index=idx,
                                path=str(deduped),
                                status="kept",
                                frame_ratio=ratio,
                            ))

                            db.commit()

                            kept.append(deduped)

                        except (TaskStopped, TaskCancelled):
                            raise
                        except Exception as e:

                            logger.warning(
                                "帧级完全去重失败，回退原分段："
                                "%s (%s)",
                                seg,
                                e,
                            )

                            db.add(Segment(
                                task_id=task_id,
                                index=idx,
                                path=str(seg),
                                status="kept",
                                frame_ratio=ratio,
                            ))

                            db.commit()

                            kept.append(seg)

                    else:

                        db.add(Segment(
                            task_id=task_id,
                            index=idx,
                            path=str(seg),
                            status="kept",
                            frame_ratio=ratio,
                        ))

                        db.commit()

                        kept.append(seg)

                if not kept:
                    raise RuntimeError(
                        "没有可用分段：所有分段均被静态筛选丢弃，"
                        "或静态不足回收失败"
                    )

            else:

                # 关闭静态处理时，保持原有行为。
                kept = list(segments)

                for idx, seg in enumerate(segments):
                    db.add(Segment(
                        task_id=task_id,
                        index=idx,
                        path=str(seg),
                        status="kept",
                        frame_ratio=None,
                    ))

                db.commit()

            """
            task.status = "anonymizing"
            db.commit()
            await manager.broadcast({"task_id": task_id, "status": "anonymizing"})
            
            async def _anon_one(idx: int, seg: Path):
                out = WORKDIR / f"{stem}_anon_{idx:04d}.mp4"
                async with self.gpu_sem:
                    await asyncio.to_thread(anonymize, seg, out, settings)
                return idx, out

            logger.info("任务 %s 匿名化：%d 段，GPU 并发上限 %d",
                        task_id, len(kept),
                        self.gpu_sem._value if self.gpu_sem else 1)

            results = await asyncio.gather(
                *[_anon_one(i, s) for i, s in enumerate(kept)],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, BaseException):
                    raise r
            results.sort(key=lambda x: x[0])
            anonymized = [r[1] for r in results]
            """
            # --------------------------------------------------
            # 匿名化阶段：汇总整个任务的帧处理进度
            # --------------------------------------------------
            await self.checkpoint(task_id)
            task.status = "anonymizing"
            self.set_stage(task_id, "anonymizing")
            db.commit()            

            # 每个分段的总帧数
            segment_totals = await asyncio.to_thread(
                lambda: [
                    _get_video_frame_count(seg)
                    for seg in kept
                ]
            )

            # 每个分段当前已处理帧数
            segment_done = [0] * len(kept)

            # 总帧数
            total_frames = sum(segment_totals)

            # 多线程回调共享状态
            progress_lock = threading.Lock()
            last_broadcast = [0.0]

            loop = asyncio.get_running_loop()

            def publish_progress(force=False):
                """
                汇总各分段进度。
                最多约每秒向前端推送一次，减少 WebSocket 开销。
                """
                with progress_lock:
                    current_frames = sum(segment_done)
                    current_total = sum(segment_totals)

                    now = time.monotonic()

                    # 限制消息发送频率
                    if (
                        not force
                        and now - last_broadcast[0] < 1.0
                        and current_frames < current_total
                    ):
                        return

                    last_broadcast[0] = now

                    message = {
                        "task_id": task_id,
                        "event": "frame_progress",
                        "frame_current": current_frames,
                        "frame_total": current_total,
                    }

                # 回调可能来自 asyncio.to_thread 的工作线程
                # 使用事件循环线程发送 WebSocket
                def send():
                    asyncio.create_task(
                        manager.broadcast(message)
                    )

                loop.call_soon_threadsafe(send)

            # 先发送一次初始进度
            await manager.broadcast({
                "task_id": task_id,
                "event": "frame_progress",
                "frame_current": 0,
                "frame_total": total_frames,
            })

            async def _anon_one(idx: int, seg: Path):
                out = WORKDIR / f"{stem}_anon_{idx:04d}.mp4"

                def on_frame_progress(current, total):
                    """
                    保留你当前完整的帧进度回调代码，不需要修改。
                    """
                    with progress_lock:
                        if total > 0:
                            segment_totals[idx] = total

                        segment_done[idx] = max(
                            0,
                            min(
                                int(current),
                                segment_totals[idx],
                            ) if segment_totals[idx] > 0
                            else int(current)
                        )

                    publish_progress()

                # 等待 GPU 资源期间也可以响应暂停、停止和取消
                await self.checkpoint(task_id)

                async with self.gpu_sem:

                    await self.checkpoint(task_id)

                    await asyncio.to_thread(
                        anonymize,
                        seg,
                        out,
                        settings,
                        on_frame_progress,
                        self.get_control(task_id).checkpoint,
                    )

                return idx, out

            logger.info(
                "任务 %s 匿名化：%d 段，GPU 并发上限 %d，总帧数 %d",
                task_id,
                len(kept),
                self.gpu_sem._value if self.gpu_sem else 1,
                total_frames,
            )

            results = await asyncio.gather(
                *[
                    _anon_one(i, s)
                    for i, s in enumerate(kept)
                ],
                return_exceptions=True,
            )

            for r in results:
                if isinstance(r, BaseException):
                    raise r

            results.sort(key=lambda x: x[0])
            anonymized = [r[1] for r in results]

            await self.checkpoint(task_id)

            # 所有分段匿名化完成，强制推送最终帧数
            with progress_lock:
                for idx in range(len(segment_done)):
                    if segment_totals[idx] > 0:
                        segment_done[idx] = segment_totals[idx]

            publish_progress(force=True)            

            # --------------------------------------------------
            # 小视频合并阶段
            # 必须在匿名化完成之后、最终输出之前执行。
            # --------------------------------------------------

            small_video_merge_enabled = bool(
                settings.get(
                    "small_video_merge_enabled",
                    False,
                )
            )

            if (
                small_video_merge_enabled
                and settings["segment_enabled"]
                and not settings["merge_enabled"]
            ):

                # 多个视频时，可能需要实际执行合并。
                # 只有一个视频时，也必须执行阈值检查。
                if len(anonymized) > 1:

                    task.status = "merging"
                    self.set_stage(task_id, "merging")
                    db.commit()

                    await manager.broadcast({
                        "task_id": task_id,
                        "status": "merging",
                    })

                await self.checkpoint(task_id)

                anonymized = await asyncio.to_thread(
                    _merge_small_video_groups,
                    anonymized,
                    stem,
                    int(settings.get("small_video_min_duration", 300)),
                )

                await self.checkpoint(task_id)

            """
            final_output = OUTPUT_DIR / f"{stem}_anonymized.mp4"

            if len(anonymized) == 1:
                await asyncio.to_thread(shutil.move, str(anonymized[0]), str(final_output))
            elif settings["merge_enabled"]:
                task.status = "merging"
                db.commit()
                await manager.broadcast({"task_id": task_id, "status": "merging"})
                await asyncio.to_thread(merge, anonymized, final_output, WORKDIR)
            else:
                logger.warning(
                    "merge_enabled=False 且有 %d 段，全部输出到 output 目录（不做合并）",
                    len(anonymized),
                )
                for i, src in enumerate(anonymized):
                    dst = OUTPUT_DIR / f"{stem}_anonymized_{i:04d}.mp4"
                    await asyncio.to_thread(shutil.move, str(src), str(dst))
                final_output = OUTPUT_DIR / f"{stem}_anonymized_0000.mp4"
            """

            # --------------------------------------------------
            # 最终输出视频
            # --------------------------------------------------

            # 最终输出前，最后一次检查控制状态
            await self.checkpoint(task_id)

            # 进入最终输出阶段
            task.status = "finalizing"
            self.set_stage(task_id, "finalizing")
            db.commit()

            await manager.broadcast({ "task_id": task_id, "status": "finalizing", })

            final_output = OUTPUT_DIR / f"{stem}_anonymized.mp4"

            output_paths = []

            if len(anonymized) == 1:

                # 只有一个匿名化结果，直接输出
                await asyncio.to_thread(
                    shutil.move,
                    str(anonymized[0]),
                    str(final_output),
                )

                output_paths = [final_output]

            elif settings["merge_enabled"]:

                await asyncio.to_thread(
                    merge,
                    anonymized,
                    final_output,
                    WORKDIR,
                )

                output_paths = [final_output]

            else:

                # 关闭合并：每个分段单独输出
                logger.warning(
                    "merge_enabled=False 且有 %d 段，分别输出视频",
                    len(anonymized),
                )

                for i, src in enumerate(anonymized):
                    dst = OUTPUT_DIR / (
                        f"{stem}_anonymized_{i:04d}.mp4"
                    )

                    await asyncio.to_thread(
                        shutil.move,
                        str(src),
                        str(dst),
                    )

                    output_paths.append(dst)


            # --------------------------------------------------
            # 按最终输出视频的实际信息生成文件名
            # --------------------------------------------------

            output_paths = await asyncio.to_thread(
                _rename_output_files,
                source,
                output_paths,
            )

            # 数据库仍然记录第一个输出文件
            final_output = output_paths[0]            

            task.status = "completed"
            task.output_path = str(final_output)
            task.finished_at = datetime.utcnow()
            db.commit()

            success = True

            await manager.broadcast({
                "task_id": task_id, "status": "completed",
                "output": str(final_output),
            })


        except TaskStopped as e:
            db.rollback()

            stopped_db = SessionLocal()
            try:
                stopped_task = stopped_db.query(Task).filter(
                    Task.id == task_id
                ).first()

                if stopped_task:
                    stopped_task.status = "stopped"
                    stopped_task.error_message = str(e)
                    stopped_task.finished_at = datetime.utcnow()
                    stopped_db.commit()
            finally:
                stopped_db.close()

            await manager.broadcast({
                "task_id": task_id,
                "status": "stopped",
            })

        except TaskCancelled as e:
            db.rollback()

            cancelled_db = SessionLocal()
            try:
                cancelled_task = cancelled_db.query(Task).filter(
                    Task.id == task_id
                ).first()

                if cancelled_task:
                    cancelled_task.status = "cancelled"
                    cancelled_task.error_message = str(e)
                    cancelled_task.finished_at = datetime.utcnow()
                    cancelled_db.commit()
            finally:
                cancelled_db.close()

            # 取消任务时清理中间文件
            if stem:
                try:
                    await asyncio.to_thread(
                        _cleanup_intermediates,
                        stem,
                    )
                except Exception:
                    logger.exception(
                        "取消任务后清理中间文件失败: %s",
                        stem,
                    )

            await manager.broadcast({
                "task_id": task_id,
                "status": "cancelled",
            })

        except Exception as e:
            db.rollback()

            self._mark_failed(
                task_id,
                task.status or "unknown",
                str(e),
            )

            await manager.broadcast({
                "task_id": task_id,
                "status": "failed",
                "error": str(e),
            })

            raise

        finally:
            db.close()
            # 只在成功路径清理中间文件；清理失败不影响任务状态
            if success and stem:
                try:
                    await asyncio.to_thread(_cleanup_intermediates, stem)
                except Exception:
                    logger.exception("清理中间文件异常: %s", stem)
            # 任务结束后释放控制器
            self.controls.pop(task_id, None)                    

    def _mark_failed(self, task_id: str, stage: str, message: str):
        db = SessionLocal()
        try:
            task = db.query(Task).filter(Task.id == task_id).first()
            if task:
                task.status = "failed"
                task.error_message = message
                task.retry_count = (task.retry_count or 0) + 1
                task.finished_at = datetime.utcnow()
                
            db.add(ErrorLog(task_id=task_id, stage=stage, message=message))
            db.commit()
        finally:
            db.close()


def _cleanup_intermediates(stem: str):
    """删除 workdir 中该视频的中间产物（不动共享的 concat_list.txt）。

    precheck 修复 / compress 压缩 / 均不触发 会产生不同分段目录名，全部覆盖：
    - 两者都未触发：{stem}_segments/
    - precheck 修复但未压缩：{stem}_prechecked_segments/
    - compress 压缩（无论 precheck 是否修复）：{stem}_compressed_segments/
    """
    candidates: list[Path] = [
        WORKDIR / f"{stem}_prechecked.mp4",
        WORKDIR / f"{stem}_compressed.mp4",
        WORKDIR / f"{stem}_segments",
        WORKDIR / f"{stem}_prechecked_segments",
        WORKDIR / f"{stem}_compressed_segments",
    ]
    candidates.extend(WORKDIR.glob(f"{stem}_anon_*.mp4"))
    candidates.extend(WORKDIR.glob(f"{stem}_dedup_*.mp4"))
    candidates.extend(WORKDIR.glob(f"{stem}_recovered_*.mp4"))
    candidates.extend(WORKDIR.glob(f"{stem}_smallmerge_*.mp4"))    

    for p in candidates:
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except Exception as e:
            logger.warning("清理中间文件失败: %s (%s)", p, e)