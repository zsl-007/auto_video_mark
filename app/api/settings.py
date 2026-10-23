"""运行配置的读取与修改。持久化逻辑在 app.core.runtime_settings。"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core import runtime_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_enabled: bool | None = None
    segment_duration: int | None = Field(default=None, ge=1, le=7200)
    static_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    anonymize_face: bool | None = None
    anonymize_plate: bool | None = None
    blur_method: str | None = Field(default=None, pattern="^(pixelate|gaussian)$")
    merge_enabled: bool | None = None
    video_compress_enabled: bool | None = None
    min_resolution: int | None = Field(default=None, ge=144, le=2160)


@router.get("")
def get_settings() -> dict[str, Any]:
    return runtime_settings.get_settings()


@router.put("")
def update_settings(patch: SettingsPatch) -> dict[str, Any]:
    payload = patch.model_dump(exclude_unset=True, exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    try:
        return runtime_settings.update_settings(payload)
    except runtime_settings.SettingsValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("写入配置失败")
        raise HTTPException(status_code=500, detail="配置写入失败") from exc