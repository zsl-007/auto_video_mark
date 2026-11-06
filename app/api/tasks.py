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
from app.db.models import Task, Segment
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