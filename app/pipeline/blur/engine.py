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

from .detectors import ( PlateDetectorYOLO, FaceDetectorHaar, FaceDetectorYOLO, TextDetectorYOLO, PersonDetectorYOLO,)

from .blur_ops import ( gaussian_inplace,  pixelate_inplace,  face_blur_inplace, )
from .utils import ( choose_device, expand_box, expand_face_box, nms_merge,)

from .tracking import UltralyticsByteTrack

logger = logging.getLogger(__name__)

_tls = threading.local()

class _DetectorBundle:
    __slots__ = ("plate_detector","face_detector","face_is_yolo","text_detector","person_detector",)

    def __init__(self,plate_detector,face_detector,face_is_yolo,text_detector=None,person_detector=None,):
        self.plate_detector = plate_detector
        self.face_detector = face_detector
        self.face_is_yolo = face_is_yolo
        self.text_detector = text_detector
        self.person_detector = person_detector

def _build_detectors(plate_weights,face_weights,text_weights,person_weights,device,conf,
                    use_plate,use_face,face_backend,use_text,text_conf,use_person,person_conf,):
    plate_detector = ( PlateDetectorYOLO(plate_weights, device, conf) if use_plate else None )
    face_detector = None
    face_is_yolo = False
    if use_face:
        if face_backend == "yolo":
            face_detector = FaceDetectorYOLO( face_weights, device, conf  )
            face_is_yolo = True
        else:
            face_detector = FaceDetectorHaar()
    text_detector = ( TextDetectorYOLO(text_weights, device, text_conf) if use_text else None )
    person_detector = ( PersonDetectorYOLO( person_weights, device, person_conf, )  if use_person else None )

    return _DetectorBundle( plate_detector, face_detector, face_is_yolo, text_detector, person_detector, )

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
def process_video( input_path: Path, output_path: Path, settings: dict,
                imgsz: int | None = None, batch_size: int | None = None, progress_callback=None,):
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

    # 人体匿名化
    anonymize_person = bool( settings.get("anonymize_person", False) )
    person_anonymize_part = str( settings.get("person_anonymize_part", "head") )
    if person_anonymize_part not in ("head", "body"):
        person_anonymize_part = "head"
    person_conf = float(  getattr(cfg, "PERSON_DETECTOR_CONF", 0.35) )
    # 人体专用模糊强度
    person_blur_strength = int( settings.get("person_blur_strength",getattr(cfg, "PERSON_BLUR_STRENGTH", 7), ) )
    person_blur_strength = max(1, min(10, person_blur_strength))
    # 人体头部区域比例
    person_head_ratio = float( settings.get( "person_head_ratio", getattr(cfg, "PERSON_HEAD_RATIO", 0.30), ))
    person_head_ratio = max(0.10, min(0.80, person_head_ratio))

    anonymize_text = bool(settings.get("anonymize_text", False))
    text_conf = float(settings.get("text_conf", getattr(cfg, "TEXT_CONF", 0.25)))
    text_iou_threshold = float(settings.get("text_iou_threshold",
                                            getattr(cfg, "TEXT_IOU_THRESHOLD", 0.15)))
    text_expand_scale = float(settings.get("text_expand_scale",
                                           getattr(cfg, "TEXT_EXPAND_SCALE", 1.15)))
    device = choose_device(ANONYMIZE_DEVICE)

    tracking_enabled = bool(settings.get("tracking_enabled", True))
    tracking_track_buffer = int(settings.get("tracking_track_buffer", 8))
    tracking_high_thresh = float(settings.get("tracking_high_thresh", 0.25))
    tracking_low_thresh = float(settings.get("tracking_low_thresh", 0.10))
    tracking_new_track_thresh = float(settings.get("tracking_new_track_thresh", 0.25))
    tracking_match_thresh = float(settings.get("tracking_match_thresh", 0.8))
    tracking_smooth_alpha = float(settings.get("tracking_smooth_alpha", 0.65))
    tracking_predict_lost = bool(settings.get("tracking_predict_lost", True))

    conf = 0.35
    scale = 1.35
    
    blur_strength = float(BLUR_STRENGTH)
    # 人脸专用匿名化参数
    face_blur_strength = int( settings.get( "face_blur_strength", getattr(cfg, "FACE_BLUR_STRENGTH", 7), ) )
    face_expand_scale = float( settings.get( "face_expand_scale", getattr(cfg, "FACE_EXPAND_SCALE", 1.8), ) )
    face_blur_strength = max(1, min(10, face_blur_strength))
    face_expand_scale = max(1.0, min(2.5, face_expand_scale))    

    plate_weights = str(MODELS_DIR / "license_plate_detector.pt")
    face_weights = str(MODELS_DIR / "yolov8n-face.pt")
    text_weights = str(MODELS_DIR / "yolov8s-text.pt")
    person_weights = str(MODELS_DIR / "yolov8n.pt")

    # text_conf 只在启用文字匿名化时进 cache_key，避免未启用时无谓重载模型
    cache_key = (
        plate_weights, face_weights, text_weights, person_weights, device, conf,
        anonymize_plate, anonymize_face, FACE_DETECTOR, anonymize_text,
        text_conf if anonymize_text else None,
        anonymize_person,
        person_conf if anonymize_person else None,
    )

    def factory():
        return _build_detectors(
            plate_weights, face_weights, text_weights, person_weights, device, conf,
            anonymize_plate, anonymize_face, FACE_DETECTOR, anonymize_text, text_conf, anonymize_person, person_conf,
        )
    bundle = _get_detectors(cache_key, factory)

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开输入视频: {input_path}")

    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # 视频总帧数，仅读取一次元数据，不增加视频推理开销
    total_frames = max( 0, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) )
    processed_frames = 0

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (W, H))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"无法创建输出视频: {output_path}")

    # 每个视频分段独立创建跟踪器。
    # 人脸和车牌必须使用不同 tracker。
    plate_tracker = None
    face_tracker = None
    person_tracker = None

    tracker_kwargs = {
        "track_buffer": tracking_track_buffer,
        "high_thresh": tracking_high_thresh,
        "low_thresh": tracking_low_thresh,
        "new_track_thresh": tracking_new_track_thresh,
        "match_thresh": tracking_match_thresh,
        "smooth_alpha": tracking_smooth_alpha,
        "predict_lost": tracking_predict_lost,
    }

    if tracking_enabled:
        if bundle.plate_detector is not None:
            plate_tracker = UltralyticsByteTrack(**tracker_kwargs)

        # Haar 本身不是 YOLO 检测器，也可以用 ByteTrack 关联，
        # 但第一版建议优先让 YOLO 人脸检测器启用跟踪。
        if bundle.face_detector is not None:
            face_tracker = UltralyticsByteTrack(**tracker_kwargs)

        if bundle.person_detector is not None:
            person_tracker = UltralyticsByteTrack(
                **tracker_kwargs
            )

    frame_buffer: list = []

    def flush_batch():
        nonlocal processed_frames

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

        if bundle.person_detector is not None:
            person_boxes = bundle.person_detector.batch_call(
                frame_buffer,
                imgsz=imgsz,
            )
        else:
            person_boxes = [[] for _ in range(n)]

        if bundle.text_detector is not None:
            text_boxes = bundle.text_detector.batch_call(frame_buffer, imgsz=imgsz)
        else:
            text_boxes = [[] for _ in range(n)]            


        def _coordinates(boxes):
            """兼容四列坐标和五列坐标（带 confidence）。"""
            return [tuple(map(int, box[:4])) for box in boxes]

        def _blur_boxes(frame, boxes, is_face=False):
            """
            车牌和人脸分别处理：
            - 车牌继续使用原有扩框和模糊强度
            - 人脸使用专用扩框和模糊强度
            """
            boxes = nms_merge(boxes, iou_thresh=0.5)

            for x1, y1, x2, y2 in boxes:

                if is_face:
                    # 人脸框重点向上扩张，覆盖头发和头顶
                    x1, y1, x2, y2 = expand_face_box( x1, y1, x2, y2, face_expand_scale, W, H, )

                    if blur_method in ("gaussian", "pixelate"):
                        face_blur_inplace( frame, x1, y1, x2, y2, method=blur_method, strength=face_blur_strength, )

                else:
                    # 车牌保持原来的处理逻辑
                    x1, y1, x2, y2 = expand_box( x1, y1, x2, y2, scale, W, H, )

                    if blur_method == "gaussian":
                        gaussian_inplace( frame, x1, y1, x2, y2, blur_strength, )
                    else:
                        pixelate_inplace( frame, x1, y1, x2, y2, )

        def _blur_person_boxes(frame, boxes):
            """
            人体匿名化。

            body：模糊完整人体。
            head：仅模糊人体框上部，覆盖头部、头发和侧脸。

            person_blur_strength：1~10，越大模糊越强。
            person_head_ratio：头部区域占人体检测框高度的比例。
            """
            for x1, y1, x2, y2 in _coordinates(boxes):

                # 限制检测框范围
                x1 = max(0, min(W - 1, x1))
                y1 = max(0, min(H - 1, y1))
                x2 = max(0, min(W, x2))
                y2 = max(0, min(H, y2))

                bw = x2 - x1
                bh = y2 - y1

                if bw <= 1 or bh <= 1:
                    continue

                if person_anonymize_part == "head":
                    # 按设置的比例截取人体框上部
                    head_h = max( 1, int(bh * person_head_ratio) )
                    y2 = min(H, y1 + head_h)

                # 使用与人脸相同的专用模糊算法
                # 同时支持马赛克和高斯模糊
                face_blur_inplace( frame, x1, y1, x2, y2, method=blur_method, strength=person_blur_strength, )

        for i, frame in enumerate(frame_buffer):
            # --------------------------------------------------
            # 1. 车牌检测 + 独立跟踪
            # --------------------------------------------------
            current_plate_boxes = plate_boxes[i]

            if plate_tracker is not None:
                current_plate_boxes = plate_tracker.update( current_plate_boxes, frame, )

            plate_regions = _coordinates(current_plate_boxes)

            # --------------------------------------------------
            # 2. 人脸检测 + 独立跟踪
            # --------------------------------------------------
            current_face_boxes = face_boxes[i]

            if face_tracker is not None:
                current_face_boxes = face_tracker.update( current_face_boxes, frame, )

            face_regions = _coordinates(current_face_boxes)

            # 人脸和车牌的跟踪状态互相独立。
            # 最后只合并匿名化区域，不合并跟踪身份。
            #_blur_boxes( frame, plate_regions + face_regions, )
            # 车牌独立处理
            if plate_regions:
                _blur_boxes( frame, plate_regions, is_face=False, )

            # 人脸独立处理
            if face_regions:
                _blur_boxes( frame, face_regions, is_face=True, )            

            # --------------------------------------------------
            # 3. 人体检测 + 独立跟踪 + 匿名化
            # --------------------------------------------------
            if bundle.person_detector is not None:
                current_person_boxes = person_boxes[i]

                if person_tracker is not None:
                    current_person_boxes = person_tracker.update( current_person_boxes, frame, )

                if current_person_boxes:
                    _blur_person_boxes( frame, current_person_boxes, )

            # --------------------------------------------------
            # 3. 文字匿名化保持独立
            # --------------------------------------------------
            if text_boxes[i]:
                t_boxes = nms_merge( text_boxes[i], iou_thresh=text_iou_threshold, )

                for x1, y1, x2, y2 in t_boxes:
                    x1, y1, x2, y2 = expand_box( x1, y1, x2, y2, text_expand_scale, W, H, )

                    if blur_method == "gaussian":
                        gaussian_inplace( frame, x1, y1, x2, y2, blur_strength, )
                    else:
                        pixelate_inplace( frame, x1, y1, x2, y2, )

            writer.write(frame)

        frame_buffer.clear()

        # 按批次汇报进度，不逐帧发送消息
        processed_frames += n
        if progress_callback is not None:
            try:
                progress_callback( processed_frames, total_frames, )
            except Exception:
                logger.warning( "视频帧进度回调失败", exc_info=True, )

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