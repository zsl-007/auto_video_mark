import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from app.core.config import BASE_DIR
from app.core.logging import setup_logging
from app.db.migrate import init_db
from app.db.session import SessionLocal
from app.db.models import Task
from app.pipeline.scheduler import PipelineScheduler
from app.pipeline.watcher import start_watcher
from app.pipeline.cleaner import cleaner_loop
from app.ws.progress import manager
from app.api import tasks, errors, stats, settings, upload

setup_logging()
logger = logging.getLogger(__name__)

scheduler = PipelineScheduler()
observer = None
cleaner_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global observer, cleaner_task
    init_db()
    await scheduler.start()
    loop = asyncio.get_running_loop()
    observer = start_watcher(scheduler, loop)
    cleaner_task = asyncio.create_task(cleaner_loop())

    # 恢复未完成的 queued 任务
    db = SessionLocal()
    try:
        pending = db.query(Task).filter(Task.status == "queued").all()
        for t in pending:
            await scheduler.enqueue(t.id)
        if pending:
            logger.info("恢复 %d 个 queued 任务", len(pending))
    finally:
        db.close()

    logger.info("系统已启动")
    yield
    observer.stop()
    observer.join()
    cleaner_task.cancel()
    await scheduler.stop()
    logger.info("系统已停止")


app = FastAPI(title="Video Anonymizer", lifespan=lifespan)

app.include_router(tasks.router)
app.include_router(errors.router)
app.include_router(stats.router)
app.include_router(settings.router)
app.include_router(upload.router)

@app.websocket("/ws/progress")
async def ws_progress(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

from app.core.config import OUTPUT_DIR
if OUTPUT_DIR.exists():
    app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")

frontend_dir = BASE_DIR / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

