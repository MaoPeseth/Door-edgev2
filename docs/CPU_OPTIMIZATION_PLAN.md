# Plan: Reduce Edge CPU Usage (CPU Optimization)

**Date:** 2026-08-02
**Status:** IMPLEMENTED & VERIFIED — see section 8 (as-built) for measurements
**Measured problem (2026-08-02):** `edge_app.py` consumed **631% CPU**
(~6 of 12 cores, 43 threads) on the current Mini PC (i7-10750H, 6C/12T,
16 GB RAM). Load average ~14.8 on 12 threads. Memory/disk/network are all
light — the load is almost entirely inference.

---

## 1. Root cause

`core/camera_worker.py` `_infer_loop()` runs the **full model pipeline on
every camera frame with no throttle** (comment: "runs continuously, no frame
skipping"). Each frame costs ~60-100 ms of CPU across 3 stacked models
(InsightFace det 640×320 → rec 112×112, YOLO spoof 416×416, YOLO hand
416×416) + Redis search, serialized by one lock. OpenVINO/onnxruntime worker
threads also spin between inferences.

**Key finding:** `config.py` already defines `INFERENCE_EVERY_N = 3`
("run inference every Nth frame") but **nothing in the code ever reads it** —
the throttle was planned but never wired up.

## 2. Changes (final — in priority order)

| # | File | Change | Why |
|---|------|--------|-----|
| 1 | `core/camera_worker.py` | `_infer_loop()` capped at `INFERENCE_FPS` (10 fps) via a time-based interval (not frame-count, so it works regardless of camera fps) | Biggest win. The models already bottleneck at ~10-16 fps, so a 10 fps cap causes **no recognition change** |
| 2 | `config.py` + `edge_app.py` | New `INFERENCE_THREADS = 4`; at startup set OpenVINO `INFERENCE_NUM_THREADS=4`, `NUM_STREAMS=1`, `OMP_NUM_THREADS=4` | Stops OpenVINO/OpenMP worker threads from spanning all 12 cores |
| 3 | `config.py` | Add `INFERENCE_FPS = 10`; annotate `INFERENCE_EVERY_N` as legacy/unused | Single source of throttle truth |
| 4 | `config.py` | `YOLO_DEBUG = False` (cosmetic — display stops drawing YOLO boxes) | Minor display saving only |

**Attempted but REVERTED:** `YOLO_IMGSZ`/`HAND_IMGSZ` 416→320. Both YOLO
detectors are **trained at 416**; running at 320 killed detections, so they
stay at 416. The imgsz change was never the main saving.

Not changing (verified unnecessary): camera resolution (already 320×240),
`DISPLAY_FPS`/`DISPLAY_FEED_SCALE` (display ≈2-4% of one core by design,
per `docs/DISPLAY_DESIGN.md` §3), match thresholds, `CONFIRM_FRAMES`,
GPU offload, multi-processing.

## 3. Reasons this does NOT reduce accuracy / performance

- **Recognition accuracy**: identical models, same 512-dim embedding, same
  Redis cosine search, same `SIMILARITY_THRESHOLD`. Skipping frames changes
  only *how often* a face is checked, not how a checked frame is evaluated.
- **Unlock latency**: confirmation needs `CONFIRM_FRAMES=3` consecutive
  matches ≈ 0.3 s at 10 fps — effectively the same as today (the loop already
  runs at model speed, ~10-16 fps).
- **Spoof robustness**: spoof YOLO stays at its trained 416 px input, so
  spoof detection robustness is untouched. Distance-gate + hand-verification
  + 3-frame confirmation all still apply independently.

## 4. Expected CPU after changes (actual: see section 8)

Estimated **~150-200%** (≈1.5-2 of 12 cores); measured **~250%** (see §8).
Load average ~14.8 → ~5-6 on this 12-thread machine.

## 5. Files touched

| File | Change |
|------|--------|
| `core/camera_worker.py` | Throttled `_infer_loop` (read from `cfg.INFERENCE_FPS`) |
| `config.py` | `INFERENCE_FPS=10`, `INFERENCE_THREADS=4`, `YOLO_IMGSZ`/`HAND_IMGSZ` stay **416** (trained at 416 — 320 attempt reverted), `YOLO_DEBUG=False`, `INFERENCE_EVERY_N` marked legacy |
| `edge_app.py` | At startup: cap OpenVINO `INFERENCE_NUM_THREADS`/`NUM_STREAMS` + `OMP_NUM_THREADS` from config |
| `docs/CPU_OPTIMIZATION_PLAN.md` | This plan + as-built record |
| `docs/MAINTENANCE_LOG.md` | Log entry |

## 6. Verification (done on this machine, camera attached)

1. [x] `py_compile` on all touched modules
2. [x] Run `edge_app.py` (~40 s, display/window disabled)
3. [x] Measure `ps -p <pid> -o %cpu` → steady **~250%** (was ~630), i.e. ~60% reduction
4. [x] Startup log identical: models → sync → `2 face(s) enrolled in Redis` → camera → SSE connected
5. [ ] Full accuracy confirmed at the physical door (hardware test checklist remains)

Note: accuracy can only be validated with real people at the door, but the
recognition path (models/thresholds/embeddings) is unchanged by this plan.

## 7. Out of scope / deliberate trade-offs

- Detection resolution 640×320 for InsightFace is the biggest per-frame cost
  and is kept (determines small-face recall at the door).
- No GPU offload (Intel iGPU would save more but adds driver complexity).
- `YOLO_DEBUG` kept on during tuning; flip off after.

## 8. As-built — what was actually done (2026-08-02)

Implemented per plan with two small deviations:
- Thread-cap property on this OpenVINO (2026.2.1) is `INFERENCE_NUM_THREADS`
  (the classic `THREADS`/`CPU_THREADS_NUM` keys are not supported). Set to
  `cfg.INFERENCE_THREADS` (4) with `NUM_STREAMS=1`.
- `YOLO_DEBUG` left OFF; mention requires touching `config.py` only.

Added `_cap_openvino_threads()` to `edge_app.py`, called before model load,
which also sets `OMP_NUM_THREADS`.

### Measured result (this machine, camera attached, display disabled)

| Metric | Before | After (without imgsz shrink — see note) |
|--------|--------|-------|
| `edge_app.py` CPU | **~631%** (6.3/12 cores, load ~14.8) | **~250%** (≈2.5/12 cores) |
| Startup log | models + sync + camera + SSE OK | identical (`[OpenVINO] CPU threads capped to 4`) |

~60% CPU reduction with zero code-path change to recognition. Remaining
headroom if ever needed: lower `INFERENCE_FPS` (10 → 8) only.

### Change to this section: imgsz 320 is REVERTED to 416

Reducing `YOLO_IMGSZ`/`HAND_IMGSZ` to 320 was intended as a secondary win,
but in practice the YOLO detectors (trained at 416) **stopped detecting
entirely** at 320 input, so the change was reverted. This does **not** undo
the CPU win: the real savings came from the `INFERENCE_FPS` throttle +
thread caps, not the imgsz change.

| File | Final change |
|------|--------------|
| `config.py` | `INFERENCE_FPS=10`, `INFERENCE_THREADS=4`, `YOLO_IMGSZ=416` (trained at 416, keep), `HAND_IMGSZ=416` (trained at 416), `YOLO_DEBUG=False`, `INFERENCE_EVERY_N` commented as legacy |
| `core/camera_worker.py` | `_infer_loop()` throttled by `INFERENCE_FPS` (time-based interval) |
| `edge_app.py` | `_cap_openvino_threads()` caps OpenVINO threads + `OMP_NUM_THREADS` before model load |