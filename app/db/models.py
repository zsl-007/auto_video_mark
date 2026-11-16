from datetime import datetime
from sqlalchemy import Column, String, Integer, Float, DateTime, Date, ForeignKey
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Task(Base):
    __tablename__ = "tasks"
    id = Column(String, primary_key=True)
    source_path = Column(String, nullable=False)
    output_path = Column(String)
    status = Column(String, nullable=False, default="queued")
    error_message = Column(String)
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)          # ← 新增：真正开始处理时刻
    finished_at = Column(DateTime)         # ← 新增：终态时刻
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Segment(Base):
    __tablename__ = "segments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=False, index=True)
    index = Column(Integer, nullable=False)
    path = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")  # pending/kept/dropped
    frame_ratio = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)


class ErrorLog(Base):
    __tablename__ = "error_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey("tasks.id"), index=True)
    stage = Column(String, nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)