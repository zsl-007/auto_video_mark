from typing import List, Tuple

import cv2
import numpy as np
from ultralytics import YOLO


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