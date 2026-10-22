import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.core.config import INPUT_DIR, MAX_UPLOAD_SIZE, SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/upload", tags=["upload"])

CHUNK_SIZE = 1024 * 1024
STAGING_DIR = INPUT_DIR / ".staging"


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _resolve_target(safe_name: str) -> Path:
    """同名文件自动加序号后缀，返回不冲突的最终路径。"""
    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix.lower()
    target = INPUT_DIR / safe_name
    counter = 1
    while target.exists():
        target = INPUT_DIR / f"{stem}_{counter}{suffix}"
        counter += 1
    return target

@router.post("")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    # 只保留文件名，防止路径穿越
    safe_name = Path(file.filename).name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {suffix}，仅支持 {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = STAGING_DIR / f"{uuid.uuid4().hex}{suffix}.part"

    size = 0
    try:
        with open(temp_path, "wb") as f:
            while True:
                chunk = await file.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过大小限制（{_human_size(MAX_UPLOAD_SIZE)}）",
                    )
                f.write(chunk)
    except HTTPException:
        temp_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        temp_path.unlink(missing_ok=True)
        logger.exception("上传写入失败")
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")
    finally:
        await file.close()

    if size == 0:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件为空")

    target = _resolve_target(safe_name)
    try:
        # 原子重命名，触发 watchdog 的 on_moved 事件
        os.replace(temp_path, target)
    except OSError as e:
        temp_path.unlink(missing_ok=True)
        logger.exception("移动上传文件失败")
        raise HTTPException(status_code=500, detail=f"移动文件失败: {e}")

    logger.info("上传完成: %s → %s (%s)", safe_name, target.name, _human_size(size))
    return {
        "filename": target.name,
        "path": str(target),
        "size": size,
        "size_human": _human_size(size),
    }