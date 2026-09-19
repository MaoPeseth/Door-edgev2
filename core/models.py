"""
core/models.py
Loads InsightFace, MiniFASNet liveness and the hand YOLO once at startup.
Thread-safe via shared lock.
Reused from attendance system pattern.
"""
import os
import threading

import numpy as np

import config as cfg
from core.roi import in_roi


class ModelHub:
    _lock        = threading.Lock()
    face_model   = None
    liveness_model = None
    liveness_enabled = False
    hand_model   = None
    hand_enabled = False

    @classmethod
    def load(cls):
        cls._load_insightface()
        cls._load_liveness()
        cls._load_hand_model()

    @classmethod
    def _load_insightface(cls):
        from core.insightface_ov import FaceAnalysisOV
        app = FaceAnalysisOV(cfg.INSIGHTFACE_MODEL_DIR, device=cfg.INSIGHTFACE_OV_DEVICE)
        app.prepare(det_thresh=cfg.DET_THRESH)
        cls.face_model = app
        print(f"[ModelHub] InsightFace (native OpenVINO) ready (device={cfg.INSIGHTFACE_OV_DEVICE})")

    @classmethod
    def _load_liveness(cls):
        if not cfg.LIVENESS_ENABLED:
            print("[ModelHub] Liveness anti-spoof disabled")
            return
        try:
            from core.liveness import LivenessModel
            cls.liveness_model   = LivenessModel()
            cls.liveness_enabled = True
            print("[ModelHub] MiniFASNet liveness (REAL/FAKE) ready")
        except Exception as e:
            print(f"[ModelHub] Liveness load failed: {e}")

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
    def get_faces(cls, frame: np.ndarray, min_face_box_size=None) -> list:
        with cls._lock:
            faces = cls.face_model.get(frame)
        out = []
        min_w = min_face_box_size if min_face_box_size is not None \
            else getattr(cfg, "MIN_FACE_BOX_SIZE", 0)
        for f in faces:
            if not in_roi(f.bbox, frame.shape):
                continue
            if min_w and (f.bbox[2] - f.bbox[0]) < min_w:
                continue
            out.append(f)
        return out

    @classmethod
    def get_liveness(cls, frame: np.ndarray, bbox) -> dict:
        """MiniFASNet liveness on the given face bbox.

        Returns {'liveness_score', 'p_live', 'p_print', 'p_replay'} or None
        when liveness is disabled/failed to load (no check = pass through).
        """
        if not cls.liveness_enabled or cls.liveness_model is None:
            return None
        with cls._lock:
            return cls.liveness_model.predict(frame, bbox)

    @classmethod
    def get_hand_detections(cls, frame: np.ndarray, conf=None) -> list:
        if not cls.hand_enabled or cls.hand_model is None:
            return []
        conf = conf if conf is not None else getattr(cfg, "HAND_CONF", 0.4)
        with cls._lock:
            try:
                results = cls.hand_model.predict(
                    frame,
                    conf=conf,
                    iou=cfg.HAND_IOU,
                    imgsz=cfg.HAND_IMGSZ,
                    verbose=False,
                )
                dets = []
                min_w = getattr(cfg, "MIN_HAND_BOX_SIZE", 0)
                for r in results:
                    for box in r.boxes:
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        if not in_roi((x1, y1, x2, y2), frame.shape):
                            continue
                        if min_w and (x2 - x1) < min_w:
                            continue
                        dets.append({
                            "bbox": (x1, y1, x2, y2),
                            "conf": float(box.conf[0]),
                            "cls":  int(box.cls[0]),
                        })
                return dets
            except Exception as e:
                print(f"[ModelHub] Hand detection error: {e}")
                return []
