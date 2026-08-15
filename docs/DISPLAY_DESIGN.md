# Door-Edge Screen Display Design Document

**Date:** August 2, 2026
**Status:** Pending review — not yet implemented
**Goal:** Replace the current `cv2.imshow` preview window with a fullscreen, kiosk-style door info display on the Edge PC (Ubuntu).

---

## 1. Overview

The Door-Edge system currently shows a small OpenCV preview window ("Door-Edge | q = quit") with bounding boxes and a basic HUD. This document plans a proper door-side info display:

- Live camera feed with detection overlays (as today)
- Access status panel (last access, granted/denied, similarity)
- System status (enrolled faces, MQTT, Cloud)
- Warning alert banner (suspicious access)
- Event ticker (recent access events)
- **Sponsor bar + developer credits** (always visible bottom bar)
- Fullscreen, kiosk-style, optimized to run 24/7

---

## 2. Technology Choice

| Option | Verdict |
|--------|---------|
| **Tkinter + Pillow** (chosen) | Built into Python, light (fits Intel N100 Mini PC), `python3-tk` = one apt install. Kiosk-proven for 24/7 unattended displays. |
| PySide6 / Qt | Nicer visuals but ~150MB+ extra dependency and RAM — overkill for a door info display |
| Web dashboard (Flask + browser) | Extra moving parts; unnecessary since the monitor is attached to the Edge PC |

### Why Tkinter + Pillow is fine for 24/7 operation
- Static memory footprint (~1-2 MB for the display)
- No GPU/graphics dependencies
- Stable in daemon threads — a display crash can never kill the main system
- The two known 24/7 traps are avoided by design:
  1. **Keep a reference to every `ImageTk.PhotoImage`** — otherwise memory creeps up over hours
  2. **Run the display in a daemon thread** — independent from camera/inference threads

---

## 3. Performance Impact (screen ON vs headless)

Inference stays the bottleneck; the display only *draws* already-computed results and never re-runs models.

| Component | Headless server | + Screen display |
|-----------|-----------------|------------------|
| Capture (320x240) | ~1-2% of one core | same |
| Inference (InsightFace + 2× YOLO + Redis search) | ~60-100 ms/frame — **the real bottleneck** | unchanged |
| Display render (15 FPS cap) | — |  |
| RAM | baseline | **+1-2 MB** |

### Safeguards built in
1. **Display FPS cap (~15 FPS)** — camera may run higher; display skips frames
2. **Idle skip** — if frame/state hasn't changed, skip redraw entirely (~0% cost when idle)
3. **Event-driven Tk loop** — no busy-waiting; sleeps until next update
4. **`DISPLAY_ENABLED = False`** — headless mode for machines without a monitor; zero rendering

---

## 4. Architecture

```
camera_worker.py (inference thread) ──► shared state (extended _result_lock)
                                          ├─ draw commands (bboxes, labels)
                                          ├─ state (idle/confirming/unlocked)
                                          ├─ person name + similarity
                                          ├─ alert flag
                                          └─ event log (last N events)
core/screen_display.py (NEW, daemon thread) ◄─ reads shared state
                                          └─ renders fullscreen Tk UI
edge_app.py ──► starts display thread (replaces cv2 preview)
```

Key principle: **reuse the existing shared-state pattern** in `camera_worker.py` (`_result_lock`, `_draw_cmds`, `_state_disp`). Inference logic itself is unchanged.

---

## 5. Screen Layout (fullscreen, kiosk-style)

```
┌─────────────────────────────────────────────────────────────┐
│  DOOR-01            [OPEN green / LOCKED gray / ALERT red]  │
├──────────────────────────────┬──────────────────────────────┤
│                              │  ENROLLED: 150 faces         │
│                              │  MQTT: ● connected           │
│                              │  CLOUD: ● connected          │
│   LIVE CAMERA FEED           │                              │
│   (bbox + labels drawn       │  ── LAST ACCESS ──           │
│    as today)                 │  [BIG TEXT] Sok Dara         │
│                              │  ✓ ACCESS GRANTED (0.85)     │
│                              │  (auto-clears after ~5 s)    │
│                              │                              │
│                              │  ── WARNING ──               │
│                              │  ⚠ 3 unknown attempts        │
│                              │  (red banner while active)   │
│                              │                              │
│                              │  ── EVENT TICKER ──          │
│                              │  12:00:01 Sok Dara (face) ✓  │
│                              │  11:59:58 Unknown card ✗     │
├──────────────────────────────┴──────────────────────────────┤
│  ⚑ Sponsored by:Dr. Thap tharoeun, Prof. Koung Samnang
│  ⚙ Developed by: Mao Peseht · Kouch Mengsrun · Chay Chhnlong · Tm Pannak
│      Door-Edge v1.0                                         │
└─────────────────────────────────────────────────────────────┘
```

### Sponsor & credits bar
- **Always visible** at the bottom (two lines), so sponsors and developers are credited at all times
- **Text-only for now** — configurable; logo images can be added later (Pillow `ImageTk` already in the stack)
- All names defined in `config.py` — no code edits needed to change them:
  - `DISPLAY_SPONSORS = ["Sponsor A", "Sponsor B"]` (empty list = hide the line)
  - `DISPLAY_DEVELOPERS = ["Developer 1", "Developer 2", "Developer 3", "Developer 4"]` (placeholders for now)
  - `DISPLAY_VERSION = "v1.0"`
- If the sponsor list grows long later, it can rotate as a ticker — out of scope for v1

---

## 6. Shared State (extended)

Extend the existing `_result_lock` data in `camera_worker.py`:

```python
# Current (exists today)
_draw_cmds    : [{"bbox", "label", "color"}]
_state_disp   : "idle" | "confirming" | "unlocked"
_confirm_disp : int

# New (added by this plan)
_last_access  : {person_id, name, similarity, method, granted, timestamp} or None
_alert_active : bool
_alert_text   : str            # e.g. "3 unknown attempts"
_event_log    : deque(maxlen=20)  # recent access events for the ticker
```

Who writes what:
- **Inference thread** (`camera_worker._process`) — on match/deny/spoof: update `_last_access`, `_event_log`, `_alert_active`
- **Display thread** (`screen_display.py`) — reads everything under the lock; renders
- **MQTT/callback side** — RFID events come from the ESP32 via `door/access/log`; the display can also show them. (Edge currently doesn't subscribe to that topic — **note:** add an MQTT subscription for RFID events so the ticker shows RFID access too. Small addition to `mqtt_publisher.py`.)

---

## 7. Files Touched

| File | Change |
|------|--------|
| `core/screen_display.py` | **NEW** — Tkinter fullscreen display thread, reads shared state |
| `core/camera_worker.py` | Extend shared state; remove `cv2.imshow` preview; keep capture loop |
| `core/mqtt_publisher.py` | Add optional subscription to `door/access/log` (RFID events → ticker) |
| `edge_app.py` | Start display thread (only if `DISPLAY_ENABLED`) |
| `config.py` | Add display settings (see below) |
| `requirements.txt` | Add `Pillow` |
| `docs/` | This document + update `MAINTENANCE_LOG.md` |

### Config additions (`config.py`)

```python
# ── Screen Display ───────────────────────────────────────────────────────────
DISPLAY_ENABLED     = True     # False = headless mode (no monitor)
DISPLAY_FULLSCREEN  = True     # kiosk mode
DISPLAY_FPS         = 15       # max redraws per second
DISPLAY_ACCESS_CLEAR_MS = 5000 # auto-clear "last access" after 5 s

# ── Sponsor & Credits ────────────────────────────────────────────────────────
DISPLAY_SPONSORS    = ["Sponsor 1", "Sponsor 2"]   # empty = hide line
DISPLAY_DEVELOPERS  = ["Dev 1", "Dev 2", "Dev 3", "Dev 4"]  # 4 placeholders
DISPLAY_VERSION     = "v1.0"
```

---

## 8. Behavior Rules

| Event | Display reaction |
|-------|------------------|
| Face match confirmed | Green state + big name + "ACCESS GRANTED (sim)" + event in ticker |
| Access denied (unknown/spoof/too close) | Red/orange + reason + event in ticker |
| Warning alert triggered (threshold reached) | Red banner "⚠ N unknown attempts" until cleared/success |
| Successful access by anyone | Warning banner cleared |
| No activity / idle | Camera feed continues; last-access panel auto-clears; credits bar stays |
| Display thread crash | Logs error; system continues headless (daemon thread) |
| `DISPLAY_ENABLED = False` | No Tk window at all — zero rendering cost |

---

## 9. Ubuntu Deployment Notes

- Install dependency: `sudo apt install python3-tk`
- Disable screen blanking so the display never sleeps: `xset s off -dpms`
- Kiosk style: window has no title bar; `ESC` quits (admin), otherwise fullscreen
- Wayland note: Tk runs via XWayland — fine for this use case

---

## 10. Implementation Steps (after review)

1. `config.py` — add display settings + sponsor/developer placeholders
2. `core/screen_display.py` — Tk root + camera label + status panels + credits bar; FPS cap + idle skip; daemon thread with clean shutdown
3. `core/camera_worker.py` — extend shared state (`_last_access`, `_alert_active`, `_event_log`); remove `cv2.imshow`/`waitKey`; keep frame publishing
4. `core/mqtt_publisher.py` — subscribe to `door/access/log`; forward RFID events to display state
5. `edge_app.py` — start display thread after camera worker
6. `requirements.txt` — add `Pillow`
7. Update `docs/MAINTENANCE_LOG.md`

---

## 11. Testing Plan (tomorrow)

| Test | Expected |
|------|----------|
| `python3 -c "import tkinter"` | No error (python3-tk installed) |
| Run `edge_app.py` | Fullscreen UI with feed + panels + credits bar |
| Face match | Green panel + name + ticker entry |
| Unknown face / spoof | Red panel + reason + ticker entry |
| Unknown card 3× | Warning banner appears |
| Successful access | Banner clears |
| Walk away / idle | Feed continues; no crash; low CPU (check `top`) |
| `DISPLAY_ENABLED=False` | Headless — no window, system works as before |
| 24 h soak (optional) | Memory stable (no PhotoImage leak) |

---

**Review status:** ⬜ Pending review
**Once approved → build in the order of section 10.**
