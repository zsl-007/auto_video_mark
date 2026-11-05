from typing import List, Tuple

import cv2
import numpy as np
from ultralytics import YOLO

from pathlib import Path

class PlateDetectorYOLO:
    def __init__(self, weights: str, device: str = "cpu", conf: float = 0.35, imgsz: int = 960):
        self.model = YOLO(weights)
        self.device = device
        self.conf = conf
        self.imgsz = imgsz

    def __call__(self, frame, imgsz=None):
        return self.batch_call([frame], imgsz=imgsz)[0]


    def batch_call(self, frames, imgsz=None):
        if not frames:
            return []

        results = self.model(
            frames,
            device=self.device,
            conf=self.conf,
            imgsz=imgsz if imgsz is not None else self.imgsz,
            verbose=False,
        )

        out = []

        for res in results:
            if res.boxes is None or len(res.boxes) == 0:
                out.append([])
                continue

            xyxy = res.boxes.xyxy.cpu().numpy()
            conf = res.boxes.conf.cpu().numpy()

            frame_boxes = []

            for box, score in zip(xyxy, conf):
                x1, y1, x2, y2 = map(int, box)
                frame_boxes.append((x1, y1, x2, y2, float(score)))

            out.append(frame_boxes)

        return out



class FaceDetectorHaar:
    def __init__(self):
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self.cascade = cv2.CascadeClassifier(path)

    def __call__(self, frame) -> List[Tuple[int, int, int, int]]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self.cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30),
        )
        return [(x, y, x + w, y + h) for (x, y, w, h) in faces]


class FaceDetectorYOLO:
    def __init__(self, weights: str = "yolov8n-face.pt", device: str = "cpu",
                 conf: float = 0.35, imgsz: int = 960):
        self.model = YOLO(weights)
        self.device = device
        self.conf = max(0.25, conf)
        self.imgsz = imgsz

    def __call__(self, frame, imgsz=None) -> List[Tuple[int, int, int, int]]:
        return self.batch_call([frame], imgsz=imgsz)[0]


    def batch_call(self, frames, imgsz=None):
        if not frames:
            return []

        results = self.model(
            frames,
            device=self.device,
            conf=self.conf,
            imgsz=imgsz if imgsz is not None else self.imgsz,
            verbose=False,
        )

        out = []

        for res in results:
            if res.boxes is None or len(res.boxes) == 0:
                out.append([])
                continue

            xyxy = res.boxes.xyxy.cpu().numpy()
            conf = res.boxes.conf.cpu().numpy()

            frame_boxes = []

            for box, score in zip(xyxy, conf):
                x1, y1, x2, y2 = map(int, box)
                frame_boxes.append((x1, y1, x2, y2, float(score)))

            out.append(frame_boxes)

        return out

class PersonDetectorYOLO:
    """COCO YOLO 人体检测器，只检测 person 类别（class_id=0）。"""

    def __init__(
        self,
        weights: str,
        device: str = "cpu",
        conf: float = 0.35,
        imgsz: int = 960,
    ):
        if not Path(weights).is_file():
            raise FileNotFoundError(
                f"人体检测模型不存在：{weights}。"
                "请将 yolov8n.pt 放入 models/ 目录。"
            )

        self.model = YOLO(weights)
        self.device = device
        self.conf = conf
        self.imgsz = imgsz

    def __call__(self, frame, imgsz=None):
        return self.batch_call([frame], imgsz=imgsz)[0]

    def batch_call(self, frames, imgsz=None):
        if not frames:
            return []

        results = self.model(
            frames,
            device=self.device,
            conf=self.conf,
            imgsz=imgsz if imgsz is not None else self.imgsz,
            classes=[0],       # COCO person
            verbose=False,
        )

        output = []

        for result in results:
            if result.boxes is None or len(result.boxes) == 0:
                output.append([])
                continue

            xyxy = result.boxes.xyxy.cpu().numpy()
            scores = result.boxes.conf.cpu().numpy()

            frame_boxes = []

            for box, score in zip(xyxy, scores):
                x1, y1, x2, y2 = map(int, box)

                if x2 > x1 and y2 > y1:
                    frame_boxes.append(
                        (x1, y1, x2, y2, float(score))
                    )

            output.append(frame_boxes)

        return output

class TextDetectorYOLO:
    """YOLOv8 场景文字检测器（yolov8s-text.pt）。"""

    def __init__(self, weights: str, device: str = "cpu",
                 conf: float = 0.25, imgsz: int = 960):
        self.model = YOLO(weights)
        self.device = device
        self.conf = conf
        self.imgsz = imgsz

    def __call__(self, frame, imgsz=None):
        return self.batch_call([frame], imgsz=imgsz)[0]

    def batch_call(self, frames, imgsz=None):
        if not frames:
            return []
        results = self.model(
            frames, device=self.device, conf=self.conf,
            imgsz=imgsz if imgsz is not None else self.imgsz,
            verbose=False,
        )
        out = []
        for res in results:
            if res.boxes is None or len(res.boxes) == 0:
                out.append([])
            else:
                out.append([tuple(map(int, xy)) for xy in res.boxes.xyxy.cpu().numpy()])
        return out