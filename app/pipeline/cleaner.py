"""
workdir 与 input/.staging 自动清理器。

设计要点：
1. 只清理 mtime 超过阈值的文件/目录，源文件与 output 永不触碰。
2. 扫描正在运行的任务（状态非 completed/failed），保护其 stem 相关的中间文件。
3. 清理 input/.staging 中上传中断残留的 .part 文件（阈值独立，较短）。
4. 记录释放的字节数，便于观察磁盘水位。
5. 响应 asyncio.CancelledError，在 lifespan 关闭时立即退出。
"""
import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from app.core.config import (
    CLEANUP_INTERVAL,
    INPUT_DIR,
    WORKDIR,
    WORKDIR_MAX_AGE,
)
from app.db.models import Task
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

STAGING_DIR = INPUT_DIR / ".staging"
STAGING_MAX_AGE = int(os.getenv("STAGING_MAX_AGE", "3600"))

# 视为“已完成”的状态，这些任务的中间文件不再受保护
_TERMINAL_STATUSES = ("completed", "failed")


# ==================== 工具函数 ====================

def _active_stems() -> set[str]:
    """返回所有未终态任务对应的源文件 stem 集合。

    这些 stem 的中间文件（如 {stem}_segments/、{stem}_anon_0000.mp4）
    不应被清理，避免与正在运行的任务撞车。
    """
    db = SessionLocal()
    try:
        rows = db.query(Task.source_path).filter(
            ~Task.status.in_(_TERMINAL_STATUSES)
        ).all()
        return {Path(r[0]).stem for r in rows if r[0]}
    finally:
        db.close()


def _is_protected(name: str, active_stems: set[str]) -> bool:
    """判断 workdir 下的某项是否属于正在运行的任务。"""
    if not active_stems:
        return False
    if name == "concat_list.txt":
        # 共享清单文件：只要有活跃任务，就保留
        return True
    for stem in active_stems:
        if name == stem or name.startswith(f"{stem}_") or name.startswith(f"{stem}."):
            return True
    return False


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                continue
    return total


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ==================== 清理核心 ====================

def _clean_once() -> dict:
    """执行一轮清理，返回统计信息。

    stats 字段：
        workdir_files  删除的 workdir 文件数
        workdir_dirs   删除的 workdir 目录数
        staging_files  删除的 staging 残留数
        skipped        因受保护而跳过的项数
        freed_bytes    累计释放字节
    """
    now = time.time()
    cutoff = now - WORKDIR_MAX_AGE
    staging_cutoff = now - STAGING_MAX_AGE
    active_stems = _active_stems()

    stats = {
        "workdir_files": 0,
        "workdir_dirs": 0,
        "staging_files": 0,
        "skipped": 0,
        "freed_bytes": 0,
    }

    # ---------- workdir ----------
    if WORKDIR.exists():
        for entry in WORKDIR.iterdir():
            try:
                if _is_protected(entry.name, active_stems):
                    stats["skipped"] += 1
                    continue
                st = entry.stat()
                if st.st_mtime >= cutoff:
                    continue
                if entry.is_file():
                    size = st.st_size
                    entry.unlink()
                    stats["workdir_files"] += 1
                    stats["freed_bytes"] += size
                elif entry.is_dir():
                    size = _dir_size(entry)
                    shutil.rmtree(entry, ignore_errors=True)
                    stats["workdir_dirs"] += 1
                    stats["freed_bytes"] += size
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning("清理 workdir 项 %s 失败: %s", entry, e)

    # ---------- input/.staging ----------
    if STAGING_DIR.exists():
        for entry in STAGING_DIR.iterdir():
            try:
                if not entry.is_file():
                    continue
                st = entry.stat()
                if st.st_mtime >= staging_cutoff:
                    continue
                size = st.st_size
                entry.unlink()
                stats["staging_files"] += 1
                stats["freed_bytes"] += size
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning("清理 staging 项 %s 失败: %s", entry, e)

    return stats


# ==================== 循环入口 ====================

async def cleaner_loop():
    """周期性执行清理。启动时先跑一轮，然后按 CLEANUP_INTERVAL 间隔重复。"""
    logger.info(
        "清理器已启动: workdir=%s(保留 %ds), staging=%s(保留 %ds), 间隔 %ds",
        WORKDIR, WORKDIR_MAX_AGE, STAGING_DIR, STAGING_MAX_AGE, CLEANUP_INTERVAL,
    )
    while True:
        try:
            stats = await asyncio.to_thread(_clean_once)
            total_removed = (
                stats["workdir_files"] + stats["workdir_dirs"] + stats["staging_files"]
            )
            if total_removed:
                logger.info(
                    "清理完成: workdir 文件=%d 目录=%d, staging 残留=%d, 跳过=%d, 释放=%s",
                    stats["workdir_files"],
                    stats["workdir_dirs"],
                    stats["staging_files"],
                    stats["skipped"],
                    _human_size(stats["freed_bytes"]),
                )
            else:
                logger.debug("清理完成: 无可清理项（跳过 %d）", stats["skipped"])
        except asyncio.CancelledError:
            logger.info("清理器收到取消信号，退出")
            raise
        except Exception:
            logger.exception("清理任务异常")

        await asyncio.sleep(CLEANUP_INTERVAL)