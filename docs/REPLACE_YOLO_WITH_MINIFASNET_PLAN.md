# Integration Plan — Replace YOLO Anti-Spoof with MiniFASNet Liveness (Real/Fake)

Date: 2026-08-15
Status: IMPLEMENTED — 2026-08-15 (see verification notes below)

## Goal

Replace the YOLO object-detector anti-spoof (`Antispoofing_openvino_model/`, classes
`screen` / `printed_photo`) with a **face-liveness** check using
`garciafido/minifasnet-v2-anti-spoofing-onnx` ONNX models already downloaded in
`~/Desktop/my_project/spoofing test/models/`:

- `minifasnet_v2.onnx` — MiniFASNet v2, input 80×80×3 RGB face crop → 3 logits
- `minifasnet_v1se.onnx` — v1-SE variant, same I/O, used in an **ensemble**
- (`face_detection_yunet_2023mar.onnx` — NOT needed; InsightFace already detects faces)

Decision semantics change from *"screen/printed_photo object in frame?"* to
*"is the face live?"*:

| New result | Meaning | Action |
|-----------|---------|--------|
| **REAL**  | liveness score ≥ threshold | pass → continue normal flow (distance → match → hand → unlock) |
| **FAKE**  | liveness score < threshold | **same as current SPOOF path**: block unlock + `spoof_detected` event + `/alert` photo (1 per episode, 15 s min interval, `spoof_clear` on REAL) |

The rest of the system (MQTT, Cloud events, alerts, ESP32) is unchanged.

## Files to touch

| File | Change |
|------|--------|
| `config.py` | Remove/replace `YOLO_*` anti-spoof block; add `LIVENESS_*` settings (paths, threshold, smoothing, enabled) |
| `core/models.py` | Replace `_load_yolo()` with `_load_liveness()` (onnxruntime); add `get_liveness(face_crop)`; drop `get_yolo()`; keep hand model untouched |
| `core/camera_worker.py` | Replace frame-level YOLO check in `_process()` with per-face liveness check (largest face only) |
| `core/anti_spoof.py` | Replace with `core/liveness.py` (ONNX wrapper: crop → preprocess → ensemble → score), or rewrite in place |
| `docs/` | Update `WHOLE_SYSTEM.md`, `SYSTEM_MAP.md`, model table |

Untouched: hand verification, matcher, sync, MQTT, alerts (only the spoof alert
channel is reused), ESP32 firmware.

## New config (`config.py`)

```python
# ── Liveness Anti-Spoof (MiniFASNet, Real/Fake) ───────────────────────────────
LIVENESS_ENABLED   = True
LIVENESS_MODEL_DIR = os.path.join(BASE_DIR, "..", "..", "spoofing test", "models")
# or a copy inside the V2 folder — decide in step 0
LIVENESS_MODELS    = ["minifasnet_v2.onnx", "minifasnet_v1se.onnx"]  # ensemble
LIVENESS_THRESHOLD = 0.99      # smoothed liveness score cutoff for REAL
LIVENESS_SMOOTHING = 15        # temporal window size (matches webcam test deque)
LIVENESS_INPUT_SIZE = 80       # model input (80x80x3 RGB)
LIVENESS_DEVICE     = "CPU"    # onnxruntime provider
```

## New inference path (`_process()` in `core/camera_worker.py`)

Current spoof block (lines ~301–328) becomes a per-face liveness block **after**
face detection:

```
faces = ModelHub.get_faces(frame)          # existing InsightFace
face  = largest face                        # existing
crop  = align+resize 80x80 RGB from face.bbox (margin ~10%, center crop)

score = ModelHub.get_liveness(crop)         # ensemble avg over v2 + v1se
smoothed = score_buffer.append(score).avg() # deque(maxlen=LIVENESS_SMOOTHING)

if smoothed < LIVENESS_THRESHOLD:           # FAKE
    spoof_present = True
    # SAME actions as current spoof path (camera_worker.py:305-328):
    #   - _reset(); display "FAKE - NO ACCESS" (red)
    #   - _send_denied("spoof_detected", frame) → MQTT deny + Cloud event
    #   - alert_tracker.record_spoof(face_frame=frame)  → /alert photo (Telegram)
    #   - alert_tracker.spoof_clear() on the next REAL frame (episode re-arm)
else:                                       # REAL
    spoof_present = False
    alert_tracker.spoof_clear()
    continue → distance gate → HNSW match → hand → confirm → unlock (unchanged)
```

Notes:
- Liveness runs on the **largest face only** (same face the rest of the flow uses).
- Only run liveness when a face exists — no more whole-frame background scans,
  which also removes the old ROI-crop YOLO cost.
- `spoof_present` still guards the "recognized face while fake" branch
  (`camera_worker.py:444`) — a matched face never unlocks while the current
  frame's face is FAKE.
- The `unknown_face` flow stays decoupled (runs even during a FAKE episode),
  exactly as today (`camera_worker.py:389`).

## New `core/liveness.py` (replaces `core/anti_spoof.py`)

- `LivenessModel` class:
  - load both models once via **OpenVINO** (`Core().read_model` on the IR files —
    the venv's `onnxruntime` is not needed at inference) or `onnxruntime`
    if ONNX files are preferred
  - `predict(face_crop_bgr) -> dict(liveness_score, p_live, p_print, p_replay)`
    - preprocess: BGR→RGB, resize 80×80, /255, CHW, float32, `input` node
    - softmax on the 3-logit `output`
    - ensemble: average softmax probs across the two models
    - `liveness_score = p_live` (same as webcam test)
- Temporal smoothing stays in `camera_worker.py` (per-face state, like the
  existing `_hand_streak` pattern) — no global state in the model class.

## Step 0 — prerequisites (before implementation)

1. **Format decision — ONNX vs OpenVINO IR.** Verified 2026-08-15: both ONNX
   models convert with `openvino.convert_model` (venv 2026.2.1) to IR in ~0.2 s,
   outputs numerically identical (maxdiff 5.96e-07). **Recommended: use IR** —
   matches the rest of the stack (InsightFace is already native IR via
   `core/insightface_ov.py`), no `onnxruntime` dependency at inference, one
   runtime/threading model. Converted files:
   `minifasnet_v2.xml/.bin` (1.72 MB), `minifasnet_v1se.xml/.bin` (1.74 MB).
2. Decide model location: keep `models/` in `spoofing test/` (path points there)
   **or** copy the ONNX/IR files into `Door-Edge V2/liveness_models/` so the
   project is self-contained. (Recommended: copy into V2.)
3. Confirm the converted IR models run under the V2 venv (smoke test on a
   sample face — conversion already verified numerically).
4. (Optional) Finish `test_minifasnet.py` so `test_minifasnet_webcam.py` runs —
   useful for tuning the threshold against the real door camera.

## Verification plan

1. Smoke test: load `LivenessModel` in the V2 venv, run on real face → score near
   1.0 (REAL); printed photo / phone screen → score drops (FAKE).
2. Run `edge_app.py` with `LIVENESS_ENABLED=True`:
   - real face → unlock works (existing hand + confirm flow)
   - hold up printed photo → red `FAKE - NO ACCESS`, deny MQTT, Cloud
     `spoof_detected` event, one `/alert` with photo, 15 s cooldown, no flicker
     re-alerts while continuously fake; clears when photo removed
   - known face + photo in frame → still blocked (spoof_present guard)
3. Offline test: Cloud down → FAKE still blocks + queue buffers events.
4. Perf: measure inference thread fps before/after (MiniFASNet 80×80 is much
   cheaper than YOLO 416 on the ROI crop).

## Implemented verification (2026-08-15)

- **CRITICAL BUG FOUND + FIXED:** the first implementation normalized `pixel / 255`
  (per garciafido's README) — WRONG. Upstream minivision `test.py` uses their own
  `src/data_io/transform.py` `to_tensor()`, where `div(255)` is **commented out**
  ("modify by zkx"). Empirically, `/255` collapses the model: every input →
  `replay=0.955` constant, so real faces were blocked as FAKE. **Fix:** feed raw
  float32 0-255 BGR (matches upstream + yakhyo + LocalAI reference impls).
- **CRITICAL BUG FOUND + FIXED #2 (2026-08-15):** output class order. The ONNX
  output is `[print, live, replay]` — the LIVE class is **index 1**, not 0
  (upstream `test.py`: `if label == 1: "Real Face"`; also documented in
  `spoofing test/CHANGES.md`). The first implementation read index 0 as
  liveness, so every real face scored ~0 → FAKE. **Fix:** `p_live = avg[1]`,
  `p_print = avg[0]`, `p_replay = avg[2]` in `core/liveness.py` and in
  `spoofing test/test_minifasnet.py`.
- Post-fix verification through the production path (ModelHub → 15-frame
  smoothing → threshold 0.99): real samples T1/T2 → **0.9999 / 0.9977 REAL**;
  fake samples F1/F2/F3 → **0.69 / 0.52 / 0.43 FAKE**. Clean separation.
- IR vs ONNX: identical logits (maxdiff 1.4e-06, FP32 rounding) at raw range.
- Community sample set (yakhyo/QingHeYang, byte-identical files): fake samples
  F2/F3 correctly `replay≈0.97-0.98`; the "real" T1/T2 samples classify as
  `print` under EVERY preprocessing variant tested (BGR/RGB, raw//255, tight
  crop, ImageNet norm, both scales) — they are poor test images, not evidence
  of a bug. Real-webcam scoring still needs on-site tuning.
- Recreated the missing `spoofing test/test_minifasnet.py` (YuNet + ensemble,
  same preprocessing as production) so `test_minifasnet_webcam.py` runs again
  for on-site threshold tuning.
- **POST-IMPLEMENTATION FIX (2026-08-15, on-site testing):** a face standing
  **too far** from the camera could trigger a FAKE alert — the 80×80 liveness
  crop is mostly upscaled background at that distance, so the model misjudged
  it. **Fix:** the distance gate now runs **before** liveness:
  `too_far → "Move closer" prompt + return` (no deny, no event, no `/alert`).
  Liveness only runs when the face is at a valid distance. The `too_close`
  branch stays **after** liveness so a close-up photo attack still hits the
  FAKE alert channel instead of a plain "too close" deny.
- Trigger semantics (final, 2026-08-15): the alert chain is now debounced —
  a FAKE score blocks unlock immediately (`spoof_present`), but the alert
  (MQTT deny + Cloud event + `/alert` photo) fires only after
  `LIVENESS_FAKE_STREAK = 5` **consecutive** FAKE frames (~0.5 s at 10 fps),
  so one blur/glare frame can never raise an alert. Any REAL frame resets the
  streak. `/alert` stays once per episode (15 s min interval, re-armed by
  `spoof_clear`).
- **Threshold tuned on-site:** user lowered `LIVENESS_THRESHOLD` 0.99 → 0.98 →
  **0.90** during live testing (less false FAKE on borderline frames). Docs
  (`WHOLE_SYSTEM.md`, `SYSTEM_MAP.md`) updated to match; re-tune with
  `test_minifasnet_webcam.py --threshold 0.90` if needed.