import asyncio
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Dict

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

# 记录"由本 handler 重命名产生的目标路径"，避免 watchdog 的 on_moved
# 事件对同一文件二次入队。超过 _RENAME_DEDUP_TTL 秒后自动失效。
_RENAME_DEDUP_TTL = 30.0


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


def _is_under_input(path: Path) -> bool:
    """路径是否位于 INPUT_DIR 之下（含 INPUT_DIR 根本身）。"""
    try:
        path.relative_to(INPUT_DIR)
        return True
    except ValueError:
        return False


def _apply_subdir_prefix(path: Path) -> Path:
    """
    按相对 INPUT_DIR 的子目录层级为文件名加上前缀：子目录名_原文件名。
    已包含该前缀的文件名保持不变；文件直接位于 INPUT_DIR 根目录时不加前缀。

    示例：input/医院/大厅/a.mp4 -> input/医院/大厅/医院_大厅_a.mp4
    """
    try:
        rel = path.relative_to(INPUT_DIR)
    except ValueError:
        return path

    # 位于根目录：不加前缀
    if rel.parent == Path('.'):
        return path

    prefix = '_'.join(rel.parent.parts) + '_'
    original_name = path.name

    # 已经带有前缀：保持不变
    if original_name.startswith(prefix):
        return path

    candidate = path.with_name(prefix + original_name)
    if not candidate.exists():
        return candidate

    # 目标已存在：加序号避让，避免覆盖
    stem, suffix = Path(original_name).stem, Path(original_name).suffix
    index = 1
    while True:
        candidate = path.with_name(f"{prefix}{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


class InputHandler(FileSystemEventHandler):
    def __init__(self, scheduler: PipelineScheduler, loop: asyncio.AbstractEventLoop):
        self.scheduler = scheduler
        self.loop = loop
        self._renamed: Dict[Path, float] = {}
        self._renamed_lock = threading.Lock()

    # ---------- 事件入口 ----------

    def on_created(self, event):
        """用户手动拖拽/复制/下载到 input 目录（含子目录）时触发，需要等待文件写入稳定。"""
        if event.is_directory:
            return
        path = Path(event.src_path)
        if not _is_under_input(path):
            return
        if path.suffix.lower() not in SUPPORTED:
            return
        threading.Thread(
            target=self._wait_and_enqueue, args=(path,), daemon=True,
        ).start()

    def on_moved(self, event):
        """API 上传临时文件 → input 的原子重命名，或用户移动文件进来。"""
        if event.is_directory:
            return
        dest = Path(event.dest_path)
        if not _is_under_input(dest):
            return
        if dest.suffix.lower() not in SUPPORTED:
            return
        if not dest.exists():
            return

        # 本 handler 刚产生的重命名事件 → 跳过，避免重复入队
        if self._consume_renamed(dest):
            return

        self._rename_and_enqueue(dest)

    # ---------- 内部逻辑 ----------

    def _wait_and_enqueue(self, path: Path):
        if not wait_until_stable(
            path, FILE_STABLE_CHECK_INTERVAL, FILE_STABLE_MAX_WAIT
        ):
            logger.warning("文件未稳定或不存在，跳过: %s", path)
            return
        if not path.exists():
            return
        self._rename_and_enqueue(path)

    def _rename_and_enqueue(self, path: Path):
        new_path = _apply_subdir_prefix(path)
        final_path = path
        if new_path != path:
            try:
                path.rename(new_path)
                final_path = new_path
                self._remember_renamed(new_path)
                logger.info("文件已重命名: %s → %s", path, new_path)
            except OSError as exc:
                # 重命名失败时退化为使用原路径入队，保证任务不丢失
                logger.error(
                    "文件重命名失败，将使用原路径入队: %s → %s: %s",
                    path, new_path, exc,
                )
        self._enqueue(final_path)

    def _remember_renamed(self, path: Path):
        now = time.time()
        with self._renamed_lock:
            # 顺手清理过期条目
            expired = [p for p, t in self._renamed.items()
                       if now - t > _RENAME_DEDUP_TTL]
            for p in expired:
                self._renamed.pop(p, None)
            self._renamed[path] = now

    def _consume_renamed(self, path: Path) -> bool:
        with self._renamed_lock:
            ts = self._renamed.pop(path, None)
        if ts is None:
            return False
        return (time.time() - ts) <= _RENAME_DEDUP_TTL

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
    observer.schedule(handler, str(INPUT_DIR), recursive=True)
    observer.start()
    logger.info("目录监听已启动: %s", INPUT_DIR)
    return observer