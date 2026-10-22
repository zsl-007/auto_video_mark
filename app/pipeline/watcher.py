import asyncio
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from app.core.config import (
    INPUT_DIR, FILE_STABLE_CHECK_INTERVAL, FILE_STABLE_MAX_WAIT,
)
from app.db.session import SessionLocal
from app.db.models import Task
from app.pipeline.scheduler import PipelineScheduler

logger = logging.getLogger(__name__)
SUPPORTED = {".mp4", ".mov", ".mkv", ".avi"}


def wait_until_stable(path: Path, interval: float, max_wait: int) -> bool:
    """等待文件大小连续两次采样不变，或超时。"""
    last_size = -1
    elapsed = 0.0
    while elapsed < max_wait:
        if not path.exists():
            return False
        current = path.stat().st_size
        if current > 0 and current == last_size:
            return True
        last_size = current
        time.sleep(interval)
        elapsed += interval
    return False


class InputHandler(FileSystemEventHandler):
    def __init__(self, scheduler: PipelineScheduler, loop: asyncio.AbstractEventLoop):
        self.scheduler = scheduler
        self.loop = loop

    def on_created(self, event):
        """用户手动拖拽/复制/下载到 input 目录时触发，需要等待文件写入稳定。"""
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.parent != INPUT_DIR:
            return
        if path.suffix.lower() not in SUPPORTED:
            return
        threading.Thread(
            target=self._wait_and_enqueue, args=(path,), daemon=True,
        ).start()

    def on_moved(self, event):
        """API 上传临时文件 → input 根目录的原子重命名，或用户移动文件进来。"""
        if event.is_directory:
            return
        dest = Path(event.dest_path)
        if dest.parent != INPUT_DIR:
            return
        if dest.suffix.lower() not in SUPPORTED:
            return
        if not dest.exists():
            return
        self._enqueue(dest)

    def _wait_and_enqueue(self, path: Path):
        if not wait_until_stable(
            path, FILE_STABLE_CHECK_INTERVAL, FILE_STABLE_MAX_WAIT
        ):
            logger.warning("文件未稳定或不存在，跳过: %s", path)
            return
        self._enqueue(path)

    def _enqueue(self, path: Path):
        db = SessionLocal()
        try:
            task_id = str(uuid.uuid4())
            db.add(Task(id=task_id, source_path=str(path), status="queued"))
            db.commit()
        except Exception:
            logger.exception("创建任务失败: %s", path)
            return
        finally:
            db.close()
        asyncio.run_coroutine_threadsafe(
            self.scheduler.enqueue(task_id), self.loop
        )
        logger.info("任务已创建: %s → %s", path, task_id)


def start_watcher(scheduler: PipelineScheduler, loop: asyncio.AbstractEventLoop):
    handler = InputHandler(scheduler, loop)
    observer = Observer()
    observer.schedule(handler, str(INPUT_DIR), recursive=False)
    observer.start()
    logger.info("目录监听已启动: %s", INPUT_DIR)
    return observer