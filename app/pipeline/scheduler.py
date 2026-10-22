import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional

from app.core.config import (
    OUTPUT_DIR, WORKDIR, MERGE_ENABLED,
    MAX_WORKERS_PER_STAGE, QUEUE_MAXSIZE,
    RESOURCE_CHECK_INTERVAL,
)
from app.core.resources import resource_ok
from app.db.session import SessionLocal
from app.db.models import Task, Segment, ErrorLog
from app.pipeline.precheck import precheck
from app.pipeline.segment import segment
from app.pipeline.static_detect import is_static
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
            else:
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
                logger.exception("任务处理失败: %s", task_id)
                self._mark_failed(task_id, "unknown", str(e))
            finally:
                self.queue.task_done()

    async def _process(self, task_id: str):
        db = SessionLocal()
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            db.close()
            return

        try:
            source = Path(task.source_path)
            stem = source.stem

            task.status = "prechecking"
            db.commit()
            await manager.broadcast({"task_id": task_id, "status": "prechecking"})
            prechecked = await asyncio.to_thread(precheck, source, WORKDIR)

            task.status = "segmenting"
            db.commit()
            await manager.broadcast({"task_id": task_id, "status": "segmenting"})
            segments = await asyncio.to_thread(segment, prechecked, WORKDIR)

            task.status = "static_checking"
            db.commit()
            await manager.broadcast({"task_id": task_id, "status": "static_checking"})

            kept = []
            for idx, seg in enumerate(segments):
                ratio = await asyncio.to_thread(
                    __import__("app.pipeline.static_detect", fromlist=["detect_static"])
                    .detect_static, seg
                )
                is_dropped = ratio < 0.5
                db.add(Segment(
                    task_id=task_id, index=idx,
                    path=str(seg),
                    status="dropped" if is_dropped else "kept",
                    frame_ratio=ratio,
                ))
                db.commit()
                if not is_dropped:
                    kept.append(seg)

            if not kept:
                raise RuntimeError("所有分段均为静态，无可用输出")

            task.status = "anonymizing"
            db.commit()
            await manager.broadcast({"task_id": task_id, "status": "anonymizing"})

            anonymized = []
            for i, seg in enumerate(kept):
                out = WORKDIR / f"{stem}_anon_{i:04d}.mp4"
                await asyncio.to_thread(anonymize, seg, out)
                anonymized.append(out)

            final_output = OUTPUT_DIR / f"{stem}_anonymized.mp4"
            if MERGE_ENABLED and len(anonymized) > 1:
                task.status = "merging"
                db.commit()
                await manager.broadcast({"task_id": task_id, "status": "merging"})
                await asyncio.to_thread(merge, anonymized, final_output, WORKDIR)
            elif anonymized:
                final_output = anonymized[0]

            task.status = "completed"
            task.output_path = str(final_output)
            db.commit()
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

    def _mark_failed(self, task_id: str, stage: str, message: str):
        db = SessionLocal()
        try:
            task = db.query(Task).filter(Task.id == task_id).first()
            if task:
                task.status = "failed"
                task.error_message = message
                task.retry_count = (task.retry_count or 0) + 1
            db.add(ErrorLog(task_id=task_id, stage=stage, message=message))
            db.commit()
        finally:
            db.close()