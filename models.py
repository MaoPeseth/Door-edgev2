"""
core/models.py
Loads InsightFace and YOLO once at startup. Thread-safe via shared lock.
Reused from attendance system pattern.
"""
import os
import threading

import numpy as np

import config as cfg


class ModelHub:
    _lock        = threading.Lock()
    face_model   = None
    yolo_model   = None
    yolo_enabled = False
    hand_model   = None
    hand_enabled = False

    @classmethod
    def load(cls):
        cls._load_insightface()
        cls._load_yolo()
        cls._load_hand_model()

    @classmethod
    def _load_insightface(cls):
        from core.insightface_ov import FaceAnalysisOV
        app = FaceAnalysisOV(cfg.INSIGHTFACE_MODEL_DIR, device=cfg.INSIGHTFACE_OV_DEVICE)
        app.prepare(det_thresh=cfg.DET_THRESH)
        cls.face_model = app
        print(f"[ModelHub] InsightFace (native OpenVINO) ready (device={cfg.INSIGHTFACE_OV_DEVICE})")

    @classmethod
    def _load_yolo(cls):
        if not cfg.YOLO_ENABLED:
            print("[ModelHub] YOLO disabled")
            return
        if not os.path.exists(cfg.YOLO_MODEL_PATH):
            print(f"[ModelHub] YOLO model not found: {cfg.YOLO_MODEL_PATH!r} — spoof check disabled")
            return
        try:
            from ultralytics import YOLO
            cls.yolo_model   = YOLO(cfg.YOLO_MODEL_PATH)
            cls.yolo_enabled = True
            print("[ModelHub] YOLO anti-spoof ready")
        except Exception as e:
            print(f"[ModelHub] YOLO load failed: {e}")

    @classmethod
    def _load_hand_model(cls):
        if not cfg.HAND_VERIFICATION_ENABLED:
            print("[ModelHub] Hand verification disabled")
            return
        if not os.path.exists(cfg.HAND_MODEL_PATH):
            print(f"[ModelHub] Hand model not found: {cfg.HAND_MODEL_PATH!r} — hand verification disabled")
            return
        try:
            from ultralytics import YOLO
            cls.hand_model   = YOLO(cfg.HAND_MODEL_PATH)
            cls.hand_enabled = True
            print("[ModelHub] Hand verification ready")
        except Exception as e:
            print(f"[ModelHub] Hand model load failed: {e}")

    @classmethod
    def get_faces(cls, frame: np.ndarray) -> list:
        with cls._lock:
            return cls.face_model.get(frame)

    @classmethod
    def get_yolo(cls, frame: np.ndarray) -> list:
        if not cls.yolo_enabled or cls.yolo_model is None:
            return []
        with cls._lock:
            try:
                results = cls.yolo_model.predict(
                    frame,
                    conf=cfg.YOLO_CONF,
                    iou=cfg.YOLO_IOU,
                    imgsz=cfg.YOLO_IMGSZ,
                    verbose=False,
                )
                dets = []
                for r in results:
                    for box in r.boxes:
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        dets.append({
                            "bbox": (x1, y1, x2, y2),
                            "conf": float(box.conf[0]),
                            "cls":  int(box.cls[0]),
                        })
                return dets
            except Exception as e:
                print(f"[ModelHub] YOLO error: {e}")
                return []

    @classmethod
    def get_hand_detections(cls, frame: np.ndarray) -> list:
        if not cls.hand_enabled or cls.hand_model is None:
            return []
        with cls._lock:
            try:
                results = cls.hand_model.predict(
                    frame,
                    conf=cfg.HAND_CONF,
                    iou=cfg.HAND_IOU,
                    imgsz=cfg.HAND_IMGSZ,
                    verbose=False,
                )
                dets = []
                for r in results:
                    for box in r.boxes:
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        dets.append({
                            "bbox": (x1, y1, x2, y2),
                            "conf": float(box.conf[0]),
                            "cls":  int(box.cls[0]),
                        })
                return dets
            except Exception as e:
                print(f"[ModelHub] Hand detection error: {e}")
                return []
