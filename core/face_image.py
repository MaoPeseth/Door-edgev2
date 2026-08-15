"""
core/face_image.py
Encoding for the `face_image` field on events and alerts.

The backend caps images at 10 MB but sets no request-size limit, so the edge
caps the frame instead: JPEG at ~640x480, quality ~75 (roughly 50 KB raw,
~67 KB base64). Always resize/re-encode — never forward the raw camera frame.
"""
import base64

import cv2

MAX_W = 640
MAX_H = 480
QUALITY = 75


def encode_face_image(frame, crop=None) -> str | None:
    """Camera frame → base64 JPEG (bare, no data: URI prefix).

    `crop` is an optional (x1, y1, x2, y2) region to cut the frame down to
    (e.g. the person's face + raised hand) before encoding — kept None to
    send the full frame. The region is clamped to the frame bounds.

    Frames smaller than the 640x480 cap (e.g. the native 320x240 feed) are
    sent at native size — never upscaled. Returns None on failure; the event
    then still sends as text.
    """
    try:
        if crop is not None:
            x1, y1, x2, y2 = map(int, crop)
            h, w = frame.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 > x1 and y2 > y1:
                frame = frame[y1:y2, x1:x2].copy()
        h, w = frame.shape[:2]
        scale = min(MAX_W / w, MAX_H / h, 1.0)
        if scale < 1.0:
            frame = cv2.resize(frame, (int(round(w * scale)), int(round(h * scale))))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
        if not ok:
            return None
        return base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception as e:
        print(f"[FaceImage] Encode failed: {e}")
        return None
