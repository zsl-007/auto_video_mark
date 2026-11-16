"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.db.models import Task
from app.db.models import Task, Segment

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("")
def list_tasks(db: Session = Depends(get_db)):
    return db.query(Task).order_by(Task.created_at.desc()).all()


@router.get("/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task

@router.get("/{task_id}/segments")
def get_task_segments(task_id: str, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    segs = db.query(Segment).filter(Segment.task_id == task_id).order_by(Segment.index).all()
    return [
        {
            "id": s.id, "index": s.index, "path": s.path,
            "status": s.status, "frame_ratio": s.frame_ratio,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in segs
    ]
"""
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.session import SessionLocal

from app.db.models import Task, Segment, ErrorLog
from datetime import datetime
from app.pipeline.scheduler import PipelineScheduler
from app.ws.progress import manager

from app.core.config import OUTPUT_DIR

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 多段输出：辅助函数
# ---------------------------------------------------------------------------

# output_path 形如：{stem}_anonymized_0000.mp4
#_SEG_OUTPUT_RE = re.compile(r"^(?P<stem>.+)_anonymized_\d{4}\.mp4$")
_SEG_OUTPUT_RE = re.compile(
    r"^(?P<stem>.+)_\d{4}-.+\.mp4$"
)
_MULTI_OUTPUT_RE_CACHE: dict[str, re.Pattern] = {}

"""
def _multi_output_pattern(stem: str) -> re.Pattern:
    pat = _MULTI_OUTPUT_RE_CACHE.get(stem)
    if pat is None:
        pat = re.compile(rf"^{re.escape(stem)}_anonymized_\d{{4}}\.mp4$")
        _MULTI_OUTPUT_RE_CACHE[stem] = pat
    return pat
"""
def _multi_output_pattern(stem: str) -> re.Pattern:
    pat = _MULTI_OUTPUT_RE_CACHE.get(stem)

    if pat is None:
        pat = re.compile(
            rf"^{re.escape(stem)}_\d{{4}}-.+\.mp4$"
        )

        _MULTI_OUTPUT_RE_CACHE[stem] = pat

    return pat

def _collect_output_files(task: Task) -> list[str]:
    """
    收集某任务的全部输出文件名。

    新格式多分段：
        ID65543_0001-目录-时长-分辨率-日期.mp4

    旧格式多分段：
        原文件名_anonymized_0000.mp4

    单文件或合并文件：
        直接返回 task.output_path 对应文件。
    """
    files: list[str] = []

    if task.status == "completed" and task.output_path:
        out_name = Path(task.output_path).name

        # 新格式：ID65543_0001-目录-时长-分辨率-日期.mp4
        new_match = re.match(
            r"^(?P<stem>.+)_\d{4}-.+\.mp4$",
            out_name,
        )

        # 旧格式：原文件名_anonymized_0000.mp4
        old_match = re.match(
            r"^(?P<stem>.+)_anonymized_\d{4}\.mp4$",
            out_name,
        )

        match = new_match or old_match

        if match:
            stem = match.group("stem")

            if new_match:
                pattern = re.compile(
                    rf"^{re.escape(stem)}_\d{{4}}-.+\.mp4$"
                )
            else:
                pattern = re.compile(
                    rf"^{re.escape(stem)}_anonymized_\d{{4}}\.mp4$"
                )

            try:
                for p in sorted(OUTPUT_DIR.iterdir()):
                    if p.is_file() and pattern.match(p.name):
                        files.append(p.name)

            except FileNotFoundError:
                pass

    # 单文件、合并文件，或者扫描失败时回退
    if not files and task.output_path:
        files = [Path(task.output_path).name]

    return files

def _enrich_task_dict(task: Task) -> dict:
    """把 Task ORM 对象转成 dict，并补充 output_files 列表。"""
    data = {c.name: getattr(task, c.name) for c in task.__table__.columns}
    data["output_files"] = _collect_output_files(task)
    return data


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.get("")
def list_tasks(db: Session = Depends(get_db)):
    tasks = db.query(Task).order_by(Task.created_at.desc()).all()
    return [_enrich_task_dict(t) for t in tasks]


@router.get("/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _enrich_task_dict(task)


@router.get("/{task_id}/segments")
def get_task_segments(task_id: str, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    segs = db.query(Segment).filter(Segment.task_id == task_id).order_by(Segment.index).all()
    return [
        {
            "id": s.id, "index": s.index, "path": s.path,
            "status": s.status, "frame_ratio": s.frame_ratio,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in segs
    ]


# ---------------------------------------------------------------------------
# 任务操作 API
# ---------------------------------------------------------------------------

ACTIVE_STATUSES = {
    "queued",
    "prechecking",
    "compressing",
    "segmenting",
    "static_processing",
    "anonymizing",
    "merging",
    "paused",
}

TERMINAL_STATUSES = {
    "completed",
    "failed",
    "stopped",
    "cancelled",
}

# 正在执行停止或取消操作的中间状态
CONTROL_STATUSES = {
    "stopping",
    "cancelling",
}


def _get_scheduler():
    scheduler = PipelineScheduler.get_active_instance()

    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="任务调度器尚未启动",
        )

    return scheduler


def _get_task_or_404(task_id: str, db: Session):
    task = db.query(Task).filter(
        Task.id == task_id
    ).first()

    if not task:
        raise HTTPException(
            status_code=404,
            detail="任务不存在",
        )

    return task


@router.post("/{task_id}/pause")
async def pause_task(
    task_id: str,
    db: Session = Depends(get_db),
):
    task = _get_task_or_404(task_id, db)

    if task.status not in {
        "queued",
        "prechecking",
        "compressing",
        "segmenting",
        "static_processing",
        "anonymizing",
        "merging",
    }:
        raise HTTPException(
            status_code=409,
            detail=f"当前状态不能暂停：{task.status}",
        )

    scheduler = _get_scheduler()
    scheduler.pause_task(task_id)

    task.status = "paused"
    db.commit()

    await manager.broadcast({
        "task_id": task_id,
        "status": "paused",
    })

    return {
        "task_id": task_id,
        "status": "paused",
        "message": "任务已请求暂停",
    }


@router.post("/{task_id}/resume")
async def resume_task(
    task_id: str,
    db: Session = Depends(get_db),
):
    task = _get_task_or_404(task_id, db)

    if task.status != "paused":
        raise HTTPException(
            status_code=409,
            detail="只有已暂停的任务才能继续",
        )

    scheduler = _get_scheduler()
    control = scheduler.get_control(task_id)

    # 排队任务恢复后仍然保持排队状态；
    # 已经开始处理的任务恢复到原处理阶段。
    resume_status = (
        control.current_stage
        if task.started_at
        else "queued"
    )

    scheduler.resume_task(task_id)

    task.status = resume_status
    db.commit()

    await manager.broadcast({
        "task_id": task_id,
        "status": resume_status,
    })

    return {
        "task_id": task_id,
        "status": resume_status,
        "message": "任务已继续",
    }


@router.post("/{task_id}/stop")
async def stop_task(
    task_id: str,
    db: Session = Depends(get_db),
):
    task = _get_task_or_404(task_id, db)

    if task.status not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"当前状态不能停止：{task.status}",
        )

    scheduler = _get_scheduler()
    scheduler.stop_task(task_id)

    # 尚未开始处理的排队任务可以直接结束
    if task.status == "queued":
        task.status = "stopped"
        task.finished_at = datetime.utcnow()

        scheduler.controls.pop(task_id, None)

        response_status = "stopped"
    else:
        task.status = "stopping"
        response_status = "stopping"

    db.commit()

    await manager.broadcast({
        "task_id": task_id,
        "status": response_status,
    })

    return {
        "task_id": task_id,
        "status": response_status,
        "message": "已提交停止请求",
    }


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    db: Session = Depends(get_db),
):
    task = _get_task_or_404(task_id, db)

    if task.status not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"当前状态不能取消：{task.status}",
        )

    scheduler = _get_scheduler()
    scheduler.cancel_task(task_id)

    # 尚未开始处理的排队任务可以直接取消
    if task.status == "queued":
        task.status = "cancelled"
        task.error_message = "用户取消了任务"
        task.finished_at = datetime.utcnow()

        scheduler.controls.pop(task_id, None)

        response_status = "cancelled"
    else:
        task.status = "cancelling"
        response_status = "cancelling"

    db.commit()

    await manager.broadcast({
        "task_id": task_id,
        "status": response_status,
    })

    return {
        "task_id": task_id,
        "status": response_status,
        "message": "已提交取消请求",
    }


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    db: Session = Depends(get_db),
):
    task = _get_task_or_404(task_id, db)

    if task.status not in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="只能删除已结束的任务，请先停止或取消任务",
        )

    # 注意：只删除数据库记录。
    # 不删除 input/ 原始视频，也不删除 output/ 输出视频。

    db.query(Segment).filter(
        Segment.task_id == task_id
    ).delete(synchronize_session=False)

    db.query(ErrorLog).filter(
        ErrorLog.task_id == task_id
    ).delete(synchronize_session=False)

    db.delete(task)
    db.commit()

    return {
        "task_id": task_id,
        "message": "任务记录及关联数据已删除",
    }
