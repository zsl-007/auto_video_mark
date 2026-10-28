"""进程内视频匿名化引擎：替代原 privacy-blur CLI。

关键优化：
- thread-local 模型缓存：同一线程只加载一次 YOLO 权重，跨分段复用
- batch 推理：按 anonymize_batch_size 攒帧批量前向，提升 GPU 利用率
- imgsz 动态传入：切 imgsz 不重新加载模型，模型缓存 key 不含 imgsz
- 输出 mp4v（与原 cli.py 一致，便于下游 merge 逻辑兼容）
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import cv2

from app.core.config import (
    ANONYMIZE_DEVICE, BLUR_STRENGTH, FACE_DETECTOR, MODELS_DIR,
)
from .detectors import PlateDetectorYOLO, FaceDetectorHaar, FaceDetectorYOLO
from .blur_ops import gaussian_inplace, pixelate_inplace
from .utils import choose_device, expand_box, nms_merge

logger = logging.getLogger(__name__)

_tls = threading.local()


class _DetectorBundle:
    __slots__ = ("plate_detector", "face_detector", "face_is_yolo")

    def __init__(self, plate_detector, face_detector, face_is_yolo):
        self.plate_detector = plate_detector
        self.face_detector = face_detector
        self.face_is_yolo = face_is_yolo


def _build_detectors(plate_weights, face_weights, device, conf,
                     use_plate, use_face, face_backend) -> _DetectorBundle:
    plate_detector = PlateDetectorYOLO(plate_weights, device, conf) if use_plate else None
    face_detector = None
    face_is_yolo = False
    if use_face:
        if face_backend == "yolo":
            face_detector = FaceDetectorYOLO(face_weights, device, conf)
            face_is_yolo = True
        else:
            face_detector = FaceDetectorHaar()
    return _DetectorBundle(plate_detector, face_detector, face_is_yolo)


def _get_detectors(cache_key, factory) -> _DetectorBundle:
    cache = getattr(_tls, "cache", None)
    if cache is None:
        cache = {}
        _tls.cache = cache
    bundle = cache.get(cache_key)
    if bundle is None:
        bundle = factory()
        cache[cache_key] = bundle
        logger.info("线程 %s 首次加载匿名化模型 key=%s",
                    threading.get_ident(), cache_key)
    return bundle


def process_video(input_path: Path, output_path: Path, settings: dict,
                  imgsz: int | None = None, batch_size: int | None = None):
    """进程内完成一段视频的人脸/车牌模糊。"""
    from app.core import config as cfg

    if imgsz is None:
        imgsz = int(settings.get("anonymize_imgsz",
                                 getattr(cfg, "ANONYMIZE_IMGSZ", 640)))
    if batch_size is None:
        batch_size = int(settings.get("anonymize_batch_size",
                                      getattr(cfg, "ANONYMIZE_BATCH_SIZE", 1)))

    blur_method = settings["blur_method"]
    anonymize_face = bool(settings["anonymize_face"])
    anonymize_plate = bool(settings["anonymize_plate"])
    device = choose_device(ANONYMIZE_DEVICE)

    conf = 0.35
    scale = 1.35
    blur_strength = float(BLUR_STRENGTH)
    plate_weights = str(MODELS_DIR / "license_plate_detector.pt")
    face_weights = str(MODELS_DIR / "yolov8n-face.pt")

    # 注意：imgsz 不进 cache_key，模型加载与 imgsz 解耦
    cache_key = (
        plate_weights, face_weights, device, conf,
        anonymize_plate, anonymize_face, FACE_DETECTOR,
    )

    def factory():
        return _build_detectors(
            plate_weights, face_weights, device, conf,
            anonymize_plate, anonymize_face, FACE_DETECTOR,
        )

    bundle = _get_detectors(cache_key, factory)

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开输入视频: {input_path}")

    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (W, H))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"无法创建输出视频: {output_path}")

    frame_buffer: list = []

    def flush_batch():
        if not frame_buffer:
            return
        n = len(frame_buffer)
        if bundle.plate_detector is not None:
            plate_boxes = bundle.plate_detector.batch_call(frame_buffer, imgsz=imgsz)
        else:
            plate_boxes = [[] for _ in range(n)]

        if bundle.face_detector is not None:
            if bundle.face_is_yolo:
                face_boxes = bundle.face_detector.batch_call(frame_buffer, imgsz=imgsz)
            else:
                face_boxes = [bundle.face_detector(f) for f in frame_buffer]
        else:
            face_boxes = [[] for _ in range(n)]

        for i, frame in enumerate(frame_buffer):
            boxes = plate_boxes[i] + face_boxes[i]
            boxes = nms_merge(boxes, iou_thresh=0.5)
            for (x1, y1, x2, y2) in boxes:
                x1, y1, x2, y2 = expand_box(x1, y1, x2, y2, scale, W, H)
                if blur_method == "gaussian":
                    gaussian_inplace(frame, x1, y1, x2, y2, blur_strength)
                else:
                    pixelate_inplace(frame, x1, y1, x2, y2)
            writer.write(frame)
        frame_buffer.clear()

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                flush_batch()
                break
            frame_buffer.append(frame)
            if batch_size <= 1 or len(frame_buffer) >= batch_size:
                flush_batch()
    finally:
        cap.release()
        writer.release()

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"输出文件为空: {output_path}")