from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.api.tasks import get_db
from app.db.models import ErrorLog

router = APIRouter(prefix="/api/errors", tags=["errors"])


@router.get("")
def list_errors(db: Session = Depends(get_db)):
    return db.query(ErrorLog).order_by(ErrorLog.created_at.desc()).all()