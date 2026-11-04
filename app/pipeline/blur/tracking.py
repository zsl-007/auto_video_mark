
"""Ultralytics 内置 ByteTrack 适配器。

注意：
1. YOLO 检测仍由上层 batch_call() 批量完成。
2. 本类只负责逐帧更新跟踪状态，不执行模型推理。
3. 每个视频分段单独创建实例，禁止跨任务共享 tracker。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Iterable

import numpy as np
import torch

from ultralytics.engine.results import Boxes
from ultralytics.trackers.byte_tracker import BYTETracker


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)

    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)

    return inter / max(area_a + area_b - inter, 1e-6)


class UltralyticsByteTrack:
    """对 Ultralytics BYTETracker 的轻量封装。"""

    def __init__(
        self,
        track_buffer: int = 8,
        high_thresh: float = 0.25,
        low_thresh: float = 0.10,
        new_track_thresh: float = 0.25,
        match_thresh: float = 0.8,
        smooth_alpha: float = 0.65,
        predict_lost: bool = True,
    ):
        if track_buffer < 1:
            raise ValueError("track_buffer 必须大于 0")

        if not 0.0 <= smooth_alpha < 1.0:
            raise ValueError("smooth_alpha 必须在 [0, 1) 之间")

        args = SimpleNamespace(
            tracker_type="bytetrack",
            track_high_thresh=high_thresh,
            track_low_thresh=low_thresh,
            new_track_thresh=new_track_thresh,
            track_buffer=track_buffer,
            match_thresh=match_thresh,
            fuse_score=True,
        )

        self.tracker = BYTETracker(args)
        self.track_buffer = track_buffer
        self.smooth_alpha = smooth_alpha
        self.predict_lost = predict_lost

        # track_id -> 上一帧平滑后的坐标
        self._smooth_boxes: dict[int, np.ndarray] = {}

    @staticmethod
    def _normalize_detections(
        detections: Iterable,
    ) -> list[tuple[float, float, float, float, float]]:
        """接受 (x1,y1,x2,y2) 或 (x1,y1,x2,y2,confidence)。"""
        normalized = []

        for item in detections:
            if len(item) < 4:
                continue

            x1, y1, x2, y2 = map(float, item[:4])
            conf = float(item[4]) if len(item) >= 5 else 1.0

            if x2 <= x1 or y2 <= y1:
                continue

            normalized.append((x1, y1, x2, y2, conf))

        return normalized

    def _smooth(
        self,
        track_id: int,
        box: np.ndarray,
    ) -> np.ndarray:
        previous = self._smooth_boxes.get(track_id)

        if previous is None:
            result = box.copy()
        else:
            alpha = self.smooth_alpha
            result = alpha * previous + (1.0 - alpha) * box

        self._smooth_boxes[track_id] = result.copy()
        return result

    def update(self, detections, frame) -> list[tuple[int, int, int, int]]:
        """更新一帧跟踪状态，返回需要匿名化的区域。

        返回值包含：
        - 已成功关联的跟踪框
        - 未能关联的原始检测框（隐私兜底）
        - 尚在 track_buffer 范围内的丢失目标预测框
        """
        h, w = frame.shape[:2]
        detections = self._normalize_detections(detections)

        if detections:
            data = np.asarray(
                [
                    [x1, y1, x2, y2, conf, 0]
                    for x1, y1, x2, y2, conf in detections
                ],
                dtype=np.float32,
            )
        else:
            data = np.empty((0, 6), dtype=np.float32)

        # Boxes 是 Ultralytics 的检测结果容器。
        # BYTETracker 使用其 xywh/conf/cls 信息进行关联。
        boxes = Boxes(
            torch.from_numpy(data),
            orig_shape=(h, w),
        )

        self.tracker.update(boxes, frame)

        output = []
        matched_detection_indices = set()
        active_ids = set()

        # 已关联目标
        for track in self.tracker.tracked_stracks:
            if not getattr(track, "is_activated", True):
                continue

            track_id = int(track.track_id)

            #raw_box = np.asarray(track.tlbr, dtype=np.float32)
            if hasattr(track, "xyxy"):
                raw_box = np.asarray(track.xyxy, dtype=np.float32)
                #print('读取跟踪框坐标，track类型--xyxy')
            elif hasattr(track, "tlbr"):
                raw_box = np.asarray(track.tlbr, dtype=np.float32)
                #print('读取跟踪框坐标，track类型--tlbr')
            elif hasattr(track, "tlwh"):
                x, y, w, h = np.asarray(track.tlwh, dtype=np.float32)[:4]
                raw_box = np.asarray([x, y, x + w, y + h], dtype=np.float32)
                #print('读取跟踪框坐标，track类型--tlwh')
            else:
                #logger.warning( "无法读取跟踪框坐标，track 类型：%s", type(track).__name__, )
                #print('无法读取跟踪框坐标')
                continue

            if raw_box.shape[0] < 4:
                continue

            raw_box = self._smooth(track_id, raw_box[:4])
            active_ids.add(track_id)

            output.append(raw_box)

            # 记录与当前 track 对应的检测框。
            # 未匹配检测框会作为隐私兜底保留。
            for i, det in enumerate(detections):
                if i in matched_detection_indices:
                    continue

                if _iou(raw_box, det[:4]) >= 0.05:
                    matched_detection_indices.add(i)
                    break

        # 丢失目标：ByteTrack 内部仍保留 Kalman 预测状态。
        if self.predict_lost:
            for track in self.tracker.lost_stracks:
                track_id = int(track.track_id)

                end_frame = getattr(
                    track, "end_frame", getattr(track, "frame_id", 0)
                )
                lost_frames = self.tracker.frame_id - int(end_frame)

                if lost_frames < 1 or lost_frames > self.track_buffer:
                    continue

                #raw_box = np.asarray(track.tlbr, dtype=np.float32)
                if hasattr(track, "xyxy"):
                    raw_box = np.asarray(track.xyxy, dtype=np.float32)
                    #print('读取跟踪框坐标，track类型--xyxy')
                elif hasattr(track, "tlbr"):
                    raw_box = np.asarray(track.tlbr, dtype=np.float32)
                    #print('读取跟踪框坐标，track类型--tlbr')
                elif hasattr(track, "tlwh"):
                    x, y, w, h = np.asarray(track.tlwh, dtype=np.float32)[:4]
                    raw_box = np.asarray([x, y, x + w, y + h], dtype=np.float32)
                    #print('读取跟踪框坐标，track类型--tlwh')
                else:
                    #logger.warning( "无法读取跟踪框坐标，track 类型：%s", type(track).__name__, )
                    #print('无法读取跟踪框坐标')
                    continue

                if raw_box.shape[0] < 4:
                    continue

                predicted_box = self._smooth(track_id, raw_box[:4])
                output.append(predicted_box)

        # 隐私兜底：
        # 未能关联到 track 的检测框仍然需要模糊，
        # 避免新目标刚出现时因尚未建立 track 而漏处理。
        for i, det in enumerate(detections):
            if i not in matched_detection_indices:
                output.append(np.asarray(det[:4], dtype=np.float32))

        # 清理已经彻底移除的 track 的平滑状态。
        valid_ids = set()

        for track in self.tracker.tracked_stracks:
            valid_ids.add(int(track.track_id))

        for track in self.tracker.lost_stracks:
            valid_ids.add(int(track.track_id))

        self._smooth_boxes = {
            track_id: box
            for track_id, box in self._smooth_boxes.items()
            if track_id in valid_ids
        }

        # 裁剪坐标，避免预测框越界。
        final_boxes = []

        for box in output:
            x1, y1, x2, y2 = box

            x1 = int(max(0, min(w - 1, x1)))
            y1 = int(max(0, min(h - 1, y1)))
            x2 = int(max(0, min(w, x2)))
            y2 = int(max(0, min(h, y2)))

            if x2 > x1 and y2 > y1:
                final_boxes.append((x1, y1, x2, y2))

        return final_boxes

    def reset(self):
        """重置跟踪状态。"""
        self.tracker.reset()
        self._smooth_boxes.clear()
