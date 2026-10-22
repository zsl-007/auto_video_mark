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
