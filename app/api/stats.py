from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.api.tasks import get_db
from app.db.models import Task, Segment

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    return {
        "tasks_total": db.query(func.count(Task.id)).scalar() or 0,
        "tasks_failed": db.query(func.count(Task.id)).filter(
            Task.status == "failed"
        ).scalar() or 0,
        "segments_dropped": db.query(func.count(Segment.id)).filter(
            Segment.status == "dropped"
        ).scalar() or 0,
    }