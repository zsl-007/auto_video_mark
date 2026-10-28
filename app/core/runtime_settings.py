"""运行时配置：默认值来自 app.core.config，覆盖值持久化到 data/settings.json。

设计：
- get_settings() 返回合并 dict（可编辑 + 只读），供 API 与流水线共用。
- 任务开始时读取一次快照，运行中任务使用快照；新任务使用最新配置。
- threading.RLock + mtime 缓存避免频繁读盘；原子写（tempfile + os.replace）防崩溃。
- 覆盖文件位于 config.DB_DIR（BASE_DIR / "data"），Docker 挂载持久化。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from typing import Any

from app.core import config

logger = logging.getLogger(__name__)

OVERRIDE_PATH = config.DB_DIR / "settings.json"

_lock = threading.RLock()
_cache: dict[str, Any] | None = None
_cache_mtime: float | None = None

EDITABLE_KEYS = (
    "segment_enabled",
    "segment_duration",
    "static_process_enabled",
    "static_process_threshold",
    "static_frame_drop_enabled",
    "anonymize_face",
    "anonymize_plate",
    "blur_method",
    "merge_enabled",
    "video_compress_enabled",
    "min_resolution",
    "anonymize_parallel_segments",
    "anonymize_batch_size",
    "anonymize_imgsz",
    "gpu_mem_threshold",    
)
ALLOWED_BLUR_METHODS = ("pixelate", "gaussian")


class SettingsValidationError(ValueError):
    """字段校验失败，API 层转换为 HTTP 400。"""


def _defaults() -> dict[str, Any]:
    return {
        "video_compress_enabled": bool(config.VIDEO_COMPRESS_ENABLED),
        "min_resolution": int(config.MIN_RESOLUTION),
        "segment_enabled": bool(config.SEGMENT_ENABLED),
        "segment_duration": int(config.SEGMENT_DURATION),
        "static_process_enabled": bool(config.STATIC_PROCESS_ENABLED),
        "static_process_threshold": float(config.STATIC_PROCESS_THRESHOLD),
        "static_frame_drop_enabled": bool(config.STATIC_FRAME_DROP_ENABLED),
        "anonymize_face": bool(config.ANONYMIZE_FACE),
        "anonymize_plate": bool(config.ANONYMIZE_PLATE),
        "blur_method": str(config.BLUR_METHOD),
        "merge_enabled": bool(config.MERGE_ENABLED),
        "anonymize_parallel_segments": int(config.ANONYMIZE_PARALLEL_SEGMENTS),
        "anonymize_batch_size": int(config.ANONYMIZE_BATCH_SIZE),
        "anonymize_imgsz": int(config.ANONYMIZE_IMGSZ),
        "gpu_mem_threshold": float(config.GPU_MEM_THRESHOLD),        
    }


def _readonly() -> dict[str, Any]:
    return {
        "max_upload_size": int(config.MAX_UPLOAD_SIZE),
        "supported_extensions": sorted(config.SUPPORTED_EXTENSIONS),
        "input_dir": str(config.INPUT_DIR),
        "output_dir": str(config.OUTPUT_DIR),
    }


def _validate(patch: dict[str, Any]) -> dict[str, Any]:
    unknown = set(patch) - set(EDITABLE_KEYS)
    if unknown:
        raise SettingsValidationError(f"不允许修改的字段：{', '.join(sorted(unknown))}")
    clean: dict[str, Any] = {}

    if "segment_enabled" in patch:
        clean["segment_enabled"] = bool(patch["segment_enabled"])

    if "segment_duration" in patch:
        try:
            v = int(patch["segment_duration"])
        except (TypeError, ValueError):
            raise SettingsValidationError("segment_duration 必须是整数")
        if not (1 <= v <= 7200):
            raise SettingsValidationError("segment_duration 须在 1–7200 秒之间")
        clean["segment_duration"] = v

    if "static_process_enabled" in patch:
        clean["static_process_enabled"] = bool(patch["static_process_enabled"])

    if "static_process_threshold" in patch:           # ← 改名
        try:
            v = float(patch["static_process_threshold"])
        except (TypeError, ValueError):
            raise SettingsValidationError("static_process_threshold 必须是数字")
        if not (0.0 <= v <= 1.0):
            raise SettingsValidationError("static_process_threshold 须在 0–1 之间")
        clean["static_process_threshold"] = v

    if "static_frame_drop_enabled" in patch:
        clean["static_frame_drop_enabled"] = bool(patch["static_frame_drop_enabled"])

    if "anonymize_face" in patch:
        clean["anonymize_face"] = bool(patch["anonymize_face"])

    if "anonymize_plate" in patch:
        clean["anonymize_plate"] = bool(patch["anonymize_plate"])

    if "blur_method" in patch:
        v = str(patch["blur_method"])
        if v not in ALLOWED_BLUR_METHODS:
            raise SettingsValidationError(
                f"blur_method 必须是 {'/'.join(ALLOWED_BLUR_METHODS)} 之一"
            )
        clean["blur_method"] = v

    if "merge_enabled" in patch:
        clean["merge_enabled"] = bool(patch["merge_enabled"])

    if "video_compress_enabled" in patch:
        clean["video_compress_enabled"] = bool(patch["video_compress_enabled"])

    if "min_resolution" in patch:
        try:
            v = int(patch["min_resolution"])
        except (TypeError, ValueError):
            raise SettingsValidationError("min_resolution 必须是整数")
        if not (144 <= v <= 2160):
            raise SettingsValidationError("min_resolution 须在 144–2160 之间")
        clean["min_resolution"] = v

    if "anonymize_parallel_segments" in patch:
        try:
            v = int(patch["anonymize_parallel_segments"])
        except (TypeError, ValueError):
            raise SettingsValidationError("anonymize_parallel_segments 必须是整数")
        if not (1 <= v <= 16):
            raise SettingsValidationError("anonymize_parallel_segments 须在 1–16 之间")
        clean["anonymize_parallel_segments"] = v

    if "anonymize_batch_size" in patch:
        try:
            v = int(patch["anonymize_batch_size"])
        except (TypeError, ValueError):
            raise SettingsValidationError("anonymize_batch_size 必须是整数")
        if not (1 <= v <= 16):
            raise SettingsValidationError("anonymize_batch_size 须在 1–16 之间")
        clean["anonymize_batch_size"] = v
    
    if "anonymize_imgsz" in patch:
        try:
            v = int(patch["anonymize_imgsz"])
        except (TypeError, ValueError):
            raise SettingsValidationError("anonymize_imgsz 必须是整数")
        if not (320 <= v <= 1280):
            raise SettingsValidationError("anonymize_imgsz 须在 320–1280 之间")
        if v % 32 != 0:
            raise SettingsValidationError("anonymize_imgsz 须为 32 的倍数（如 320/480/640/800/960/1280）")
        clean["anonymize_imgsz"] = v

    if "gpu_mem_threshold" in patch:
        try:
            v = float(patch["gpu_mem_threshold"])
        except (TypeError, ValueError):
            raise SettingsValidationError("gpu_mem_threshold 必须是数字")
        if not (0.1 <= v <= 1.0):
            raise SettingsValidationError("gpu_mem_threshold 须在 0.1–1.0 之间")
        clean["gpu_mem_threshold"] = v
        
    return clean

def _read_override() -> dict[str, Any]:
    if not OVERRIDE_PATH.exists():
        return {}
    try:
        raw = OVERRIDE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("覆盖文件根节点必须是对象")
        return {k: v for k, v in data.items() if k in EDITABLE_KEYS}
    except Exception:
        logger.exception("读取配置覆盖文件失败，回退到默认值：%s", OVERRIDE_PATH)
        return {}


def _atomic_write(data: dict[str, Any]) -> None:
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".settings.", suffix=".tmp", dir=str(OVERRIDE_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, OVERRIDE_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _merge(override: dict[str, Any]) -> dict[str, Any]:
    merged = _defaults()
    merged.update(override)
    merged.update(_readonly())
    return merged


def get_settings() -> dict[str, Any]:
    global _cache, _cache_mtime
    with _lock:
        try:
            mtime = OVERRIDE_PATH.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if _cache is not None and mtime == _cache_mtime:
            return dict(_cache)
        override = _read_override()
        _cache = _merge(override)
        _cache_mtime = mtime
        return dict(_cache)


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    global _cache, _cache_mtime
    clean = _validate(patch)
    with _lock:
        override = _read_override()
        override.update(clean)
        _atomic_write(override)
        try:
            _cache_mtime = OVERRIDE_PATH.stat().st_mtime
        except FileNotFoundError:
            _cache_mtime = None
        _cache = _merge(override)
        logger.info("配置已更新：%s", clean)
        return dict(_cache)