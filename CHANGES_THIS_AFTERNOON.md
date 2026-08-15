# Changes This Afternoon

Session date: 2026-08-05

## 1. Unknown-face `/api/edge/events` rapid firing — FIXED

**Problem:** while an unknown face was present with a hand raised, the edge app
posted `/api/edge/events` at inference rate (up to 10/s). Each frame returned
the *same* attempt count (inside `ATTEMPT_MIN_INTERVAL`), but `_send_denied()`
posted anyway.

**Fix:** `core/alert_tracker.py` — `record_unknown_face()` now returns `0` when
the attempt count did **not** change (still inside `ATTEMPT_MIN_INTERVAL`).
`_send_denied()` sees `fail_count = 0` and skips the POST. `/events` now fire
only on actual counted attempts (once per second), `/alert` still fires at
`WARNING_THRESHOLD` as before.

**Do not change** this logic — the counting behaviour is now as intended.

## 2. Hand-verification flicker / false triggers — REBUILT

**Problem:** the label flickered between "Unknown" and "ACCESS DENIED" without
a raised hand, because the hand check ran per-frame with no temporal stability,
and the geometry was either too loose (hands near the face) or too strict
(hand had to be directly over the face).

**Current behaviour (`core/camera_worker.py`):**
- `_hand_raised_near_face()` — zone check:
  - hand center no lower than chin line + 15% of face height
  - hand must be **beside** or **above** the face with at least
    `HAND_MIN_CLEARANCE` face-widths of gap (forehead palm / facepalm / cheek
    brush excluded)
  - hand center within `HAND_SIDE_FACTOR` face-widths of the face centre
- `_hand_present()` — temporal gates:
  - motion gate: if the hand moves more than `HAND_MAX_MOVE_PER_FRAME`
    face-widths between consecutive frames it is a swipe (hair brush, wave,
    scratch) and resets the streak
  - hold requirement: the hand must stay still in the zone for
    `HAND_STREAK_FRAMES` consecutive inference frames (~0.6 s at 10 fps)
- `_hand_streak` / `_hand_pos` are inference-thread-only state; `_boxes_overlap`
  was removed (dead code)

## 3. `HAND_IMGSZ` — reverted to 416

`640` was tried to catch angled/turned-away hands, but the exported OpenVINO
hand model has a **fixed input shape `[1,3,416,416]`** and throws
`Can't set the input tensor ... incompatible`. Reverted to 416.

If higher input resolution is ever needed, re-export the model first:
`YOLO(<source .pt>).export(imgsz=640)`.

## Files changed

| File | What |
|---|---|
| `config.py` | `HAND_SIDE_FACTOR`, `HAND_MIN_CLEARANCE`, `HAND_STREAK_FRAMES`, `HAND_MAX_MOVE_PER_FRAME`; `HAND_IMGSZ` comment |
| `core/alert_tracker.py` | `record_unknown_face()` returns 0 on unchanged attempt count |
| `core/camera_worker.py` | `_hand_raised_near_face()` rewritten (zone + clearance), `_hand_present()` added (streak + motion gate), `_boxes_overlap()` removed |

## Tuning knobs (`config.py`)

| Setting | Default | Effect |
|---|---|---|
| `HAND_STREAK_FRAMES` | 6 | How long (in frames @10fps) the hand must be held still. Higher = fewer false triggers, longer delay (~0.6 s default) |
| `HAND_MAX_MOVE_PER_FRAME` | 0.5 | Max hand movement (face-widths) per frame; above this = swipe, resets the streak. Lower to reject slower gestures |
| `HAND_SIDE_FACTOR` | 2.0 | Max horizontal reach (face-widths from face centre). Higher accepts hands further out to the side |
| `HAND_MIN_CLEARANCE` | 0.3 | Min gap (face-widths) between hand centre and face box edge. Higher = hand must be further from the face |
| `HAND_IMGSZ` | 416 | Must match the exported model's fixed input shape |
| `ATTEMPT_MIN_INTERVAL` | 1.0 | Seconds between counted unknown-face attempts (drives `/events` rate) |
| `WARNING_THRESHOLD` | 3 | Counted attempts before the `/alert` fires |

## How to use

A raised hand that triggers an access attempt must:
1. be held **beside or above** the face (not over it) with a visible gap,
2. stay roughly still,
3. remain there for ~0.6 s.

Example: hand held in a stop/pledge pose at the side of the head. Gestures that
do **not** trigger: brushing hair, waving, scratching the cheek, palm on the
forehead, hands at chest level.
