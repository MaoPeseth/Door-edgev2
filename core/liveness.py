"""
core/liveness.py
MiniFASNet face-liveness anti-spoof (REAL/FAKE) via native OpenVINO IR.

Replaces the old YOLO screen/printed_photo detector. Ensemble of two
MiniFASNet variants, each 80x80x3 BGR input → 3 logits → softmax:

  - minifasnet_v2   — crop margin scale 2.7 (matches upstream 2.7_80x80)
  - minifasnet_v1se — crop margin scale 4.0 (matches upstream 4_0_0_80x80)

CRITICAL: the output class order is [print, live, replay] — the LIVE class
is index 1 (upstream test.py: `if label == 1: "Real Face"`). Verified on
sample images: real faces → p_live ≈ 1.0, fakes → p_live < 0.1.

Reference preprocessing (garciafido ONNX port + upstream minivision test.py):
  1. Crop a `scale`x face box centered on the bbox, clamped to the frame.
  2. Resize to 80x80, BGR, no alignment warp.
  3. Feed raw float32 0-255 BGR, HWC → NCHW — NO /255 (upstream's custom
     ToTensor has the `div(255)` commented out: "modify by zkx"; /255
     collapses the model's outputs, verified empirically).
  4. Softmax over the 3-class output. Liveness score = p_live (index 1).

Temporal smoothing lives in camera_worker (same pattern as the hand streak).
"""
import os

import cv2
import numpy as np

import config as cfg


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x, axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


class LivenessModel:
    """Loaded once at startup; thread-safe behind the ModelHub lock."""

    def __init__(self, model_dir=None, names=None, scales=None, device=None,
                 input_size=None):
        from openvino import Core

        self._core = Core()
        self._compiled = []      # (compiled_model, input_name, output_name, scale)
        names     = names     or cfg.LIVENESS_MODELS
        scales    = scales    or cfg.LIVENESS_SCALES
        model_dir = model_dir or cfg.LIVENESS_MODEL_DIR
        device    = device    or cfg.LIVENESS_OV_DEVICE
        self.input_size = input_size or cfg.LIVENESS_INPUT_SIZE

        for name in names:
            xml = os.path.join(model_dir, name + ".xml")
            if not os.path.exists(xml):
                print(f"[Liveness] model not found: {xml!r} — skipping")
                continue
            model = self._core.read_model(xml)
            compiled = self._core.compile_model(model, device)
            in_name  = compiled.input(0).get_any_name()
            out_name = compiled.output(0).get_any_name()
            scale = scales.get(name, 2.7)
            self._compiled.append((compiled, in_name, out_name, scale))

        if not self._compiled:
            raise FileNotFoundError(f"No liveness models loaded from {model_dir!r}")
        print(f"[Liveness] {len(self._compiled)} model(s) ready "
              f"({', '.join(names)} on {device})")

    @staticmethod
    def _crop_face(frame: np.ndarray, bbox, scale: float, out_size: int) -> np.ndarray:
        """Crop `scale`x the face box around its centre, clamped to the frame,
        then resize to `out_size`x`out_size`. Returns the BGR crop."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = (int(v) for v in bbox)
        bw, bh = max(1, x2 - x1), max(1, y2 - y1)
        scale = min((h - 1) / bh, (w - 1) / bw, scale)
        nw, nh = bw * scale, bh * scale
        cx, cy = x1 + bw / 2, y1 + bh / 2
        cx1 = max(0, int(cx - nw / 2))
        cy1 = max(0, int(cy - nh / 2))
        cx2 = min(w - 1, int(cx + nw / 2))
        cy2 = min(h - 1, int(cy + nh / 2))
        if cx2 <= cx1 or cy2 <= cy1:
            return frame
        crop = frame[cy1:cy2 + 1, cx1:cx2 + 1]
        return cv2.resize(crop, (out_size, out_size))

    def predict(self, frame: np.ndarray, bbox) -> dict:
        """frame: BGR image. bbox: (x1, y1, x2, y2).

        Returns {'liveness_score', 'p_live', 'p_print', 'p_replay'} averaged
        across the ensemble, or None on inference error. Class order of the
        model output is [print, live, replay] — live is index 1.
        """
        try:
            probs = []
            for compiled, in_name, out_name, scale in self._compiled:
                crop = self._crop_face(frame, bbox, scale, self.input_size)
                blob = crop.astype(np.float32).transpose(2, 0, 1)[None]
                logits = compiled({in_name: blob})[out_name]
                probs.append(_softmax(np.asarray(logits))[0])
            avg = np.mean(probs, axis=0)
            p_print, p_live, p_replay = avg
            return {
                "liveness_score": float(p_live),
                "p_live":         float(p_live),
                "p_print":        float(p_print),
                "p_replay":       float(p_replay),
            }
        except Exception as e:
            print(f"[Liveness] inference error: {e}")
            return None