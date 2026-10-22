from fastapi import APIRouter
from app.core import config

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings():
    return {
        "segment_duration": config.SEGMENT_DURATION,
        "segment_enabled": config.SEGMENT_ENABLED,
        "static_threshold": config.STATIC_THRESHOLD,
        "anonymize_face": config.ANONYMIZE_FACE,
        "anonymize_plate": config.ANONYMIZE_PLATE,
        "blur_method": config.BLUR_METHOD,
        "merge_enabled": config.MERGE_ENABLED,
        "max_upload_size": config.MAX_UPLOAD_SIZE,
        "supported_extensions": sorted(config.SUPPORTED_EXTENSIONS),
    }