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
    "static_recovery_enabled",
    "small_video_merge_enabled",
    "small_video_min_duration",
    "anonymize_face",
    "face_blur_strength",
    "face_expand_scale",
    "anonymize_person",
    "person_anonymize_part",
    "person_blur_strength",
    "person_head_ratio",            
    "anonymize_plate",
    "anonymize_text",              # 新增文本检测 # 开关
    "text_conf",                   # 新增文本检测 # 文字检测置信度
    "text_iou_threshold",          # 新增文本检测 # 文字 NMS 阈值（越小越不易合并相邻文字）
    "text_expand_scale",           # 新增文本检测 # 文字框扩张比例（文字框小，无需像车牌那样扩 1.35）
    "blur_method",
    "merge_enabled",
    "video_compress_enabled",
    "min_resolution",
    "max_concurrent_tasks",             # 新增
    "anonymize_parallel_segments",
    "anonymize_batch_size",
    "anonymize_imgsz",
    "gpu_mem_threshold", 
    "tracking_enabled",
    "tracking_track_buffer",
    "tracking_high_thresh",
    "tracking_low_thresh",
    "tracking_new_track_thresh",
    "tracking_match_thresh",
    "tracking_smooth_alpha",
    "tracking_predict_lost",       
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
        "static_recovery_enabled": bool(config.STATIC_RECOVERY_ENABLED),
        "small_video_merge_enabled": bool(config.SMALL_VIDEO_MERGE_ENABLED),
        "small_video_min_duration": int(config.SMALL_VIDEO_MIN_DURATION),
        "anonymize_face": bool(config.ANONYMIZE_FACE),
        "face_blur_strength": int(config.FACE_BLUR_STRENGTH),
        "face_expand_scale": float(config.FACE_EXPAND_SCALE),  
        "anonymize_person": bool(config.ANONYMIZE_PERSON),
        "person_anonymize_part": str(config.PERSON_ANONYMIZE_PART),
        "person_blur_strength": int(config.PERSON_BLUR_STRENGTH),
        "person_head_ratio": float(config.PERSON_HEAD_RATIO),        
        "anonymize_plate": bool(config.ANONYMIZE_PLATE),
        "anonymize_text": bool(config.ANONYMIZE_TEXT),                     # 新增
        "text_conf": float(config.TEXT_CONF),                              # 新增
        "text_iou_threshold": float(config.TEXT_IOU_THRESHOLD),            # 新增
        "text_expand_scale": float(config.TEXT_EXPAND_SCALE),              # 新增        
        "blur_method": str(config.BLUR_METHOD),
        "merge_enabled": bool(config.MERGE_ENABLED),
        "max_concurrent_tasks": int(config.MAX_CONCURRENT_TASKS),   # 新增
        "anonymize_parallel_segments": int(config.ANONYMIZE_PARALLEL_SEGMENTS),
        "anonymize_batch_size": int(config.ANONYMIZE_BATCH_SIZE),
        "anonymize_imgsz": int(config.ANONYMIZE_IMGSZ),
        "gpu_mem_threshold": float(config.GPU_MEM_THRESHOLD),
        "tracking_enabled": bool(config.TRACKING_ENABLED),
        "tracking_track_buffer": int(config.TRACKING_TRACK_BUFFER),
        "tracking_high_thresh": float(config.TRACKING_HIGH_THRESH),
        "tracking_low_thresh": float(config.TRACKING_LOW_THRESH),
        "tracking_new_track_thresh": float(config.TRACKING_NEW_TRACK_THRESH),
        "tracking_match_thresh": float(config.TRACKING_MATCH_THRESH),
        "tracking_smooth_alpha": float(config.TRACKING_SMOOTH_ALPHA),
        "tracking_predict_lost": bool(config.TRACKING_PREDICT_LOST),                
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

    if "static_recovery_enabled" in patch:
        clean["static_recovery_enabled"] = bool( patch["static_recovery_enabled"] )

    if "small_video_merge_enabled" in patch:
        clean["small_video_merge_enabled"] = bool( patch["small_video_merge_enabled"] )

    if "small_video_min_duration" in patch:
        try:
            v = int(patch["small_video_min_duration"])
        except (TypeError, ValueError):
            raise SettingsValidationError( "small_video_min_duration 必须是整数" )
        if not 1 <= v <= 7200:
            raise SettingsValidationError( "small_video_min_duration 须在 1–7200 秒之间" )
        clean["small_video_min_duration"] = v

    if "anonymize_face" in patch:
        clean["anonymize_face"] = bool(patch["anonymize_face"])

    if "face_blur_strength" in patch:
        try:
            v = int(patch["face_blur_strength"])
        except (TypeError, ValueError):
            raise SettingsValidationError( "face_blur_strength 必须是整数" )
        if not 1 <= v <= 10:
            raise SettingsValidationError( "face_blur_strength 须在 1–10 之间" )
        clean["face_blur_strength"] = v

    if "face_expand_scale" in patch:
        try:
            v = float(patch["face_expand_scale"])
        except (TypeError, ValueError):
            raise SettingsValidationError( "face_expand_scale 必须是数字" )
        if not 1.0 <= v <= 2.5:
            raise SettingsValidationError( "face_expand_scale 须在 1.0–2.5 之间" )
        clean["face_expand_scale"] = v        

    if "anonymize_person" in patch:
        clean["anonymize_person"] = bool( patch["anonymize_person"] )

    if "person_anonymize_part" in patch:
        v = str(patch["person_anonymize_part"])
        if v not in ("head", "body"):
            raise SettingsValidationError( "person_anonymize_part 必须是 head 或 body" )
        clean["person_anonymize_part"] = v

    if "person_blur_strength" in patch:
        try:
            v = int(patch["person_blur_strength"])
        except (TypeError, ValueError):
            raise SettingsValidationError("person_blur_strength 必须是整数")
        if not 1 <= v <= 10:
            raise SettingsValidationError("person_blur_strength 须在 1–10 之间")
        clean["person_blur_strength"] = v

    if "person_head_ratio" in patch:
        try:
            v = float(patch["person_head_ratio"])
        except (TypeError, ValueError):
            raise SettingsValidationError("person_head_ratio 必须是数字")
        if not 0.10 <= v <= 0.80:
            raise SettingsValidationError("person_head_ratio 须在 0.10–0.80 之间")
        clean["person_head_ratio"] = v

    if "anonymize_plate" in patch:
        clean["anonymize_plate"] = bool(patch["anonymize_plate"])

    if "anonymize_text" in patch:
        clean["anonymize_text"] = bool(patch["anonymize_text"])

    if "text_conf" in patch:
        try:
            v = float(patch["text_conf"])
        except (TypeError, ValueError):
            raise SettingsValidationError("text_conf 必须是数字")
        if not (0.01 <= v <= 1.0):
            raise SettingsValidationError("text_conf 须在 0.01–1.0 之间")
        clean["text_conf"] = v

    if "text_iou_threshold" in patch:
        try:
            v = float(patch["text_iou_threshold"])
        except (TypeError, ValueError):
            raise SettingsValidationError("text_iou_threshold 必须是数字")
        if not (0.01 <= v <= 1.0):
            raise SettingsValidationError("text_iou_threshold 须在 0.01–1.0 之间")
        clean["text_iou_threshold"] = v

    if "text_expand_scale" in patch:
        try:
            v = float(patch["text_expand_scale"])
        except (TypeError, ValueError):
            raise SettingsValidationError("text_expand_scale 必须是数字")
        if not (1.0 <= v <= 2.0):
            raise SettingsValidationError("text_expand_scale 须在 1.0–2.0 之间")
        clean["text_expand_scale"] = v
        
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

    if "max_concurrent_tasks" in patch:
        try:
            v = int(patch["max_concurrent_tasks"])
        except (TypeError, ValueError):
            raise SettingsValidationError("max_concurrent_tasks 必须是整数")
        if not (1 <= v <= 16):
            raise SettingsValidationError("max_concurrent_tasks 须在 1–16 之间")
        clean["max_concurrent_tasks"] = v
        
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

    if "tracking_enabled" in patch:
        clean["tracking_enabled"] = bool(patch["tracking_enabled"])

    if "tracking_predict_lost" in patch:
        clean["tracking_predict_lost"] = bool(patch["tracking_predict_lost"])

    if "tracking_track_buffer" in patch:
        v = int(patch["tracking_track_buffer"])
        if not 1 <= v <= 120:
            raise SettingsValidationError("tracking_track_buffer 须在 1–120 帧之间")
        clean["tracking_track_buffer"] = v

    for key in (
        "tracking_high_thresh",
        "tracking_low_thresh",
        "tracking_new_track_thresh",
        "tracking_match_thresh",
        "tracking_smooth_alpha",
    ):
        if key in patch:
            v = float(patch[key])
            if not 0.0 <= v <= 1.0:
                raise SettingsValidationError(f"{key} 须在 0–1 之间")
            clean[key] = v

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