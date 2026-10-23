"""input/ 与 output/ 目录的文件浏览与删除。

安全：
- 文件名只取 basename，resolve() 后校验父目录，杜绝 ../ 穿越。
- 删除 input 前检查 processing 状态；queued 任务标记 failed 后再删。
- 删除 output 前检查 merging 状态，避免删掉正在写入的产物。
- 任务与文件按 Path(source_path).name 匹配，取最新一条。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from app.api.tasks import get_db
from app.core.config import INPUT_DIR, OUTPUT_DIR
from app.db.models import Task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["files"])

RUNNING_STATUSES: tuple[str, ...] = (
    "prechecking", "compressing", "segmenting", "static_processing", "anonymizing", "merging",
)
TASK_LOOKBACK = timedelta(days=30)


def _safe_path(base: Path, filename: str) -> Path:
    if not filename or filename != Path(filename).name:
        raise HTTPException(status_code=400, detail="非法文件名")
    base_resolved = base.resolve()
    target = (base_resolved / filename).resolve()
    if target.parent != base_resolved:
        raise HTTPException(status_code=400, detail="非法文件名")
    return target


def _file_entry(path: Path) -> dict:
    st = path.stat()
    return {
        "filename": path.name,
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


def _latest_tasks_by_filename(db: Session) -> dict[str, Task]:
    cutoff = datetime.utcnow() - TASK_LOOKBACK
    rows = (
        db.query(Task)
        .filter(or_(Task.status.in_(RUNNING_STATUSES + ("queued",)),
                    Task.created_at >= cutoff))
        .order_by(desc(Task.created_at))
        .all()
    )
    latest: dict[str, Task] = {}
    for t in rows:
        src = t.source_path
        if not src:
            continue
        name = Path(src).name
        if name and name not in latest:
            latest[name] = t
    return latest


def _map_status(task: Optional[Task]) -> str:
    if task is None:
        return "unprocessed"
    status = (task.status or "").lower()
    if status in RUNNING_STATUSES:
        return "processing"
    if status == "completed":
        return "success"
    if status == "failed":
        return "failed"
    return "unprocessed"


@router.get("/input-files")
def list_input_files(db: Session = Depends(get_db)):
    if not INPUT_DIR.exists():
        return []
    latest = _latest_tasks_by_filename(db)
    result = []
    for p in INPUT_DIR.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        entry = _file_entry(p)
        task = latest.get(p.name)
        entry["status"] = _map_status(task)
        entry["task_id"] = task.id if task else None
        entry["error_message"] = (
            task.error_message
            if task is not None and entry["status"] == "failed" else None
        )
        result.append(entry)
    result.sort(key=lambda x: x["mtime"], reverse=True)
    return result


@router.delete("/input-files/{filename}")
def delete_input_file(filename: str, db: Session = Depends(get_db)):
    path = _safe_path(INPUT_DIR, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    running = db.query(Task).filter(Task.status.in_(RUNNING_STATUSES)).all()
    for t in running:
        if Path(t.source_path or "").name == path.name:
            raise HTTPException(status_code=409, detail="文件正在处理中，无法删除")

    # 排队任务：标记 failed 后再删文件，避免 worker 取到不存在的源
    queued = db.query(Task).filter(Task.status == "queued").all()
    canceled = 0
    for t in queued:
        if Path(t.source_path or "").name != path.name:
            continue
        t.status = "failed"
        t.error_message = "源文件已被用户删除"
        canceled += 1
    if canceled:
        db.commit()
        logger.info("删除 %s 前取消了 %d 个排队任务", path.name, canceled)

    path.unlink()
    logger.info("已删除 input 文件：%s", path.name)
    return {"ok": True, "filename": path.name}


@router.get("/output-files")
def list_output_files():
    if not OUTPUT_DIR.exists():
        return []
    result = []
    for p in OUTPUT_DIR.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        result.append(_file_entry(p))
    result.sort(key=lambda x: x["mtime"], reverse=True)
    return result


@router.delete("/output-files/{filename}")
def delete_output_file(filename: str, db: Session = Depends(get_db)):
    path = _safe_path(OUTPUT_DIR, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    if db.query(Task).filter(Task.status == "merging").first() is not None:
        raise HTTPException(status_code=409, detail="有任务正在合并输出，请稍后再试")

    path.unlink()
    logger.info("已删除 output 文件：%s", path.name)
    return {"ok": True, "filename": path.name}