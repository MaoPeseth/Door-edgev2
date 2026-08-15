"""
core/roi.py
Region-of-interest zone for camera detections.

A single normalized rectangle (x1, y1, x2, y2) in 0-1 frame coordinates.
Detections whose bbox center falls inside the zone are processed; everything
outside is ignored at the detection source (core/models.py). The zone is set
in calibration mode on the display (press 'c', click two corners) and stored
in roi_zone.json next to config.py.
"""
import json
import os

import config as cfg

_ZONE_FILE = os.path.join(cfg.BASE_DIR, "roi_zone.json")

_zone = None  # cached (x1, y1, x2, y2) normalized, or None


def _load_zone():
    global _zone
    if _zone is not None:
        return _zone
    try:
        with open(_ZONE_FILE) as f:
            data = json.load(f)
        zone = tuple(float(v) for v in data["zone"])
        _zone = zone if data.get("enabled", True) else (0.0, 0.0, 1.0, 1.0)
        print(f"[ROI] Zone loaded: {_zone}")
    except FileNotFoundError:
        _zone = (0.0, 0.0, 1.0, 1.0)
    except Exception as e:
        print(f"[ROI] Zone load failed ({e}) — using full frame")
        _zone = (0.0, 0.0, 1.0, 1.0)
    return _zone


def get_zone():
    """Current zone as (x1, y1, x2, y2) normalized, or None if disabled."""
    if not cfg.ROI_ENABLED:
        return None
    return _load_zone()


def in_roi(bbox, frame_shape):
    """True if the bbox center falls inside the configured zone."""
    if not cfg.ROI_ENABLED:
        return True
    z = _load_zone()
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    w = float(frame_shape[1])
    h = float(frame_shape[0])
    return (z[0] * w <= cx <= z[2] * w) and (z[1] * h <= cy <= z[3] * h)


def save_zone(zone_norm):
    """Persist a normalized (x1, y1, x2, y2) zone and refresh the cache."""
    global _zone
    z = tuple(float(v) for v in zone_norm)
    with open(_ZONE_FILE, "w") as f:
        json.dump({"enabled": True, "zone": list(z)}, f, indent=2)
    _zone = z
    print(f"[ROI] Zone saved: {_zone}")
    return _zone
