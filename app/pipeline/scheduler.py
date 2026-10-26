import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime

from app.core.config import (
    OUTPUT_DIR, WORKDIR,
    MAX_WORKERS_PER_STAGE, QUEUE_MAXSIZE,
    RESOURCE_CHECK_INTERVAL,
)
from app.core.resources import resource_ok
from app.core.runtime_settings import get_settings
from app.db.session import SessionLocal
from app.db.models import Task, Segment, ErrorLog
from app.pipeline.precheck import precheck
from app.pipeline.compress import compress
from app.pipeline.segment import segment
from app.pipeline.static_process import process_static, drop_static_frames
from app.pipeline.anonymize import anonymize
from app.pipeline.merge import merge
from app.ws.progress import manager

logger = logging.getLogger(__name__)


class PipelineScheduler:
    def __init__(self):
        self.queue: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.workers: list[asyncio.Task] = []
        self._running = False
        self._resource_task: Optional[asyncio.Task] = None

    async def start(self):
        self._running = True
        for _ in range(MAX_WORKERS_PER_STAGE):
            self.workers.append(asyncio.create_task(self._worker()))
        self._resource_task = asyncio.create_task(self._resource_monitor())
        logger.info("调度器已启动，worker 数: %d", len(self.workers))

    async def stop(self):
        self._running = False
        for _ in range(len(self.workers)):
            await self.queue.put(None)
        await asyncio.gather(*self.workers, return_exceptions=True)
        if self._resource_task:
            self._resource_task.cancel()
        logger.info("调度器已停止")

    async def enqueue(self, task_id: str):
        await self.queue.put(task_id)
        logger.info("任务入队: %s", task_id)

    async def _resource_monitor(self):
        while self._running:
            if not resource_ok():
                logger.warning("资源水位偏高，暂停取新任务")
            await asyncio.sleep(RESOURCE_CHECK_INTERVAL)

    async def _worker(self):
        while self._running:
            task_id = await self.queue.get()
            if task_id is None:
                self.queue.task_done()
                break
            if not resource_ok():
                await self.queue.put(task_id)
                await asyncio.sleep(RESOURCE_CHECK_INTERVAL)
                self.queue.task_done()
                continue
            try:
                await self._process(task_id)
            except Exception as e:
                # _process 内部已 mark_failed；这里再兜底一次，防止未知异常吞掉队列
                logger.exception("任务处理异常: %s", task_id)
                try:
                    self._mark_failed(task_id, "unknown", str(e))
                except Exception:
                    logger.exception("兜底 mark_failed 失败: %s", task_id)
            finally:
                self.queue.task_done()

    async def _process(self, task_id: str):
        db = SessionLocal()
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            db.close()
            return

        stem: str = ""
        success = False

        try:
            settings = get_settings()

            task.started_at = datetime.utcnow()
            task.status = "prechecking"
            db.commit()            

            source = Path(task.source_path)
            stem = source.stem

            task.status = "prechecking"
            db.commit()
            await manager.broadcast({"task_id": task_id, "status": "prechecking"})
            prechecked = await asyncio.to_thread(precheck, source, WORKDIR)

            task.status = "compressing"
            db.commit()
            await manager.broadcast({"task_id": task_id, "status": "compressing"})
            compressed = await asyncio.to_thread(compress, prechecked, WORKDIR, settings, stem)

            task.status = "segmenting"
            db.commit()
            await manager.broadcast({"task_id": task_id, "status": "segmenting"})
            segments = await asyncio.to_thread(segment, compressed, WORKDIR, settings)

            if settings["static_process_enabled"]:
                task.status = "static_processing"
                db.commit()
                await manager.broadcast({"task_id": task_id, "status": "static_processing"})

                threshold = settings["static_process_threshold"]
                frame_drop = settings.get("static_frame_drop_enabled", False)
                kept = []

                for idx, seg in enumerate(segments):
                    ratio = await asyncio.to_thread(process_static, seg)
                    is_dropped = ratio < threshold

                    if is_dropped:
                        db.add(Segment(
                            task_id=task_id, index=idx,
                            path=str(seg),
                            status="dropped",
                            frame_ratio=ratio,
                        ))
                        db.commit()
                        continue

                    if frame_drop:
                        deduped = WORKDIR / f"{stem}_dedup_{idx:04d}.mp4"
                        try:
                            await asyncio.to_thread(drop_static_frames, seg, deduped)
                            db.add(Segment(
                                task_id=task_id, index=idx,
                                path=str(deduped),
                                status="kept",
                                frame_ratio=ratio,
                            ))
                            db.commit()
                            kept.append(deduped)
                        except Exception as e:
                            logger.warning("帧级去重失败，回退原分段: %s (%s)", seg, e)
                            db.add(Segment(
                                task_id=task_id, index=idx,
                                path=str(seg),
                                status="kept",
                                frame_ratio=ratio,
                            ))
                            db.commit()
                            kept.append(seg)
                    else:
                        db.add(Segment(
                            task_id=task_id, index=idx,
                            path=str(seg),
                            status="kept",
                            frame_ratio=ratio,
                        ))
                        db.commit()
                        kept.append(seg)

                if not kept:
                    raise RuntimeError("所有分段均为静态，无可用输出")
            else:
                # 关闭静态处理（保持上一轮实现）
                kept = list(segments)
                for idx, seg in enumerate(segments):
                    db.add(Segment(
                        task_id=task_id, index=idx,
                        path=str(seg),
                        status="kept",
                        frame_ratio=None,
                    ))
                db.commit()

            task.status = "anonymizing"
            db.commit()
            await manager.broadcast({"task_id": task_id, "status": "anonymizing"})
            
            anonymized = []
            for i, seg in enumerate(kept):
                out = WORKDIR / f"{stem}_anon_{i:04d}.mp4"
                await asyncio.to_thread(anonymize, seg, out, settings)
                anonymized.append(out)

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

            task.status = "completed"
            task.output_path = str(final_output)
            task.finished_at = datetime.utcnow()
            db.commit()

            success = True

            await manager.broadcast({
                "task_id": task_id, "status": "completed",
                "output": str(final_output),
            })

        except Exception as e:
            db.rollback()
            self._mark_failed(task_id, task.status or "unknown", str(e))
            await manager.broadcast({
                "task_id": task_id, "status": "failed",
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

    for p in candidates:
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        except Exception as e:
            logger.warning("清理中间文件失败: %s (%s)", p, e)