"""
tests/test_camera_helpers.py  — Tier 1: pure camera_worker helper logic.

No camera, no ML models — just the bbox geometry / IR-mode heuristics the
inference thread depends on.
"""
import numpy as np
import pytest

import config as cfg
import core.camera_worker as cw


def _frame(h=480, w=640):
    return np.zeros((h, w, 3), dtype=np.uint8)


# ── geometry primitives ──────────────────────────────────────────────────────

def test_hand_center():
    assert cw._hand_center((10, 20, 30, 40)) == (20.0, 30.0)


def test_bbox_area():
    assert cw._bbox_area((0, 0, 4, 5)) == 20


def test_boxes_overlap():
    a = (0, 0, 10, 10)
    assert cw._boxes_overlap(a, a, iou_thresh=0.3) is True          # identical
    assert cw._boxes_overlap(a, (5, 0, 10, 10)) is True             # IoU 0.5
    assert cw._boxes_overlap(a, (9, 0, 10, 10)) is False            # IoU 0.1
    assert cw._boxes_overlap(a, (10, 0, 20, 10)) is False           # touching
    assert cw._boxes_overlap(a, (100, 100, 200, 200)) is False      # disjoint


# ── crop region ──────────────────────────────────────────────────────────────

def test_crop_region_includes_face_and_hand():
    face = (100, 100, 200, 200)
    hand = (300, 100, 360, 160)
    frame = _frame()
    # xs 100..360 (w=260, pad=104) → x1=0, x2=464
    # ys 100..200 (h=100, pad=40)  → y1=60, y2=240
    assert cw._crop_region(face, hand, frame.shape) == (0, 60, 464, 240)


def test_crop_region_face_only_when_no_hand():
    face = (100, 100, 200, 200)
    assert cw._crop_region(face, None, _frame().shape) == (60, 60, 240, 240)


def test_crop_region_clamps_to_frame():
    face = (0, 0, 20, 20)
    hand = (60, 0, 80, 20)
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert cw._crop_region(face, hand, frame.shape) == (0, 0, 100, 28)


# ── hand-raised heuristic ────────────────────────────────────────────────────

FACE = (100, 100, 200, 200)   # w=100, h=100; centre x=150


def test_hand_raised_beside_face():
    assert cw._hand_raised_near_face(FACE, (300, 100, 360, 160)) is True


def test_hand_raised_above_face():
    assert cw._hand_raised_near_face(FACE, (130, 20, 190, 50)) is True


def test_hand_low_at_chest_rejected():
    assert cw._hand_raised_near_face(FACE, (300, 300, 360, 360)) is False


def test_hand_covering_face_rejected():
    assert cw._hand_raised_near_face(FACE, (105, 105, 195, 195)) is False


def test_hand_brushing_cheek_rejected():
    assert cw._hand_raised_near_face(FACE, (210, 105, 290, 195)) is False


def test_hand_behind_face_rejected_by_depth():
    assert cw._hand_raised_near_face(FACE, (300, 100, 320, 140)) is False


def test_depth_ratio_override_loosens_gate():
    assert cw._hand_raised_near_face(FACE, (300, 100, 320, 140),
                                     depth_ratio=0.1) is True


def test_hand_far_outside_band_rejected():
    assert cw._hand_raised_near_face(FACE, (700, 100, 800, 160)) is False


# ── IR-mode heuristic ────────────────────────────────────────────────────────

def test_gray_frame_is_ir():
    gray = np.full((100, 100, 3), 128, dtype=np.uint8)
    assert cw._is_ir_mode(gray) is True


def test_saturated_frame_is_not_ir():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    frame[:, :, 0] = 255            # solid blue → max HSV saturation
    assert cw._is_ir_mode(frame) is False


def test_ir_mode_disabled_by_config(monkeypatch):
    monkeypatch.setattr(cfg, "IR_MODE_ENABLED", False)
    gray = np.full((100, 100, 3), 128, dtype=np.uint8)
    assert cw._is_ir_mode(gray) is False


# ── distance check ───────────────────────────────────────────────────────────

def test_check_distance():
    frame = _frame()                 # width 640
    assert cw._check_distance((0, 0, 400, 100), frame.shape) == "too_close"
    assert cw._check_distance((0, 0, 50, 100), frame.shape) == "too_far"
    assert cw._check_distance((0, 0, 100, 100), frame.shape) is None


def test_check_distance_min_ratio_override():
    frame = _frame()
    assert cw._check_distance((0, 0, 50, 100), frame.shape,
                              min_ratio=0.05) is None