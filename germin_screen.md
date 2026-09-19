# Door-Edge — How the Screen Display Works (in detail)

This document explains how the fullscreen kiosk screen (`core/screen_display.py`) works and how it connects to the rest of the Door-Edge system.

---

## 1. The data pipeline: `core/screen_state.py` (the bridge)

Everything hinges on a single shared, thread-safe object called `state` (`core/screen_state.py`). It is a `ScreenState` instance guarded by a `threading.Lock`. Threads **write** into it; the display thread **reads** from it.

The four types of data it carries:

| Field | Contents | Written by |
|-------|----------|------------|
| `frame` / `frame_seq` | Latest camera image (BGR) + a counter incremented on every new frame | `camera_worker` |
| `draw_cmds` / `yolo_dets` / `state` / `confirm` | Inference results: box+label+color commands, spoof detections, door state string (`idle`/`confirming`/`unlocked`), confirmation progress count | `camera_worker` |
| `last_access` / `alert_active` / `alert_text` / `events` | Access results, warning flag, rolling audit log (deque, max 20) | `camera_worker`, `mqtt_publisher`, `alert_tracker` |
| `mqtt_connected` / `cloud_connected` / `enrolled` | Status flags | various |

> The display only *reads*. The lock makes this safe across threads.

---

## 2. Who writes to shared state

- **`camera_worker.py`** runs capture + inference threads. On each inference frame it calls:
  - `state.set_frame(frame)` — sends the raw image
  - `state.set_results([{"bbox":…, "label":…, "color":…}], yolo_dets, disp_state, confirm)` — overlays + door state
  - `state.set_last_access({...})` + `state.add_event(...)` on grant/deny
  - `state.set_alert(True, "3 attempts")` when the alert tracker fires

- **`mqtt_publisher.py`** pushes RFID events (`state.add_event(...)`, `state.set_last_access(...)`) for card taps.

- **`alert_tracker.py`** toggles `state.set_alert(...)` on/off.

---

## 3. Startup wiring: `edge_app.py`

In `main()` (`edge_app.py`), after the camera worker starts, the display is created and launched:

```python
display = ScreenDisplay(mqtt_publisher=mqtt, sync_agent=sync)
display.start()
```

Two live references are passed in so the display can query live status directly:
- `mqtt_publisher` → `is_connected()` (MQTT broker) and `is_esp_online()` (ESP32 door controller)
- `sync_agent` → `is_connected()` (Cloud) and `policy()` (the schedule bundle)

`ScreenDisplay.start()` only spawns a **daemon thread** and returns immediately, so the app can continue its (slow, blocking) Cloud init.

---

## 4. The display thread (`ScreenDisplay._run`)

The display runs on its own daemon thread — it **owns** the Qt event loop and every `QWidget`. It:

1. Creates a `QApplication` and builds the window (`_DisplayWindow`).
2. Shows fullscreen (or windowed via `DISPLAY_FULLSCREEN`).
3. Registers keyboard shortcuts: **`Esc`** (close display / cancel ROI calibration) and **`c`** (toggle ROI calibration).
4. Starts a `QTimer` that fires every `DISPLAY_FPS` tick and calls `_tick()`.
5. Enters `app.exec()`.

---

## 5. The per-tick cycle

`_tick()` calls `self._window.update_from_state(self)`, which:

1. **`get_live_data()`** — reads every value out of `state` plus the live MQTT/Cloud/schedule objects into one dict.
2. **`_render_feed()`** — *only if the frame sequence changed*: copies the BGR frame, draws the ROI zone, bboxes, spoof boxes and HUD text with OpenCV, upscales, converts BGR → `QImage` → `QPixmap`, and sets it on the camera `QLabel`.
3. **`_update_beacon()`** — maps the door state (`idle`/`confirming`/`unlocked`, or `alert`) to the animated beacon ring + header state word.
4. **`_update_status()`** — updates the enrolled metric and the MQTT/ESP/Cloud/Schedule health rows/dots.
5. **`_update_panels()`** — updates the camera HUD badge, hand-raise progress bar, sidebar + on-feed warning overlays, the last-access panel (with its auto-clear), and rebuilds the audit log only when events change.

Every setter calls `_repolish()` (unpolish + polish) so QSS re-reads the dynamic `state`/`accent`/`tone`/`verdict` properties and the colors update correctly.

---

## 6. The signature: `_Beacon` (animated state ring)

A `QTimer` (~30 fps) advances a phase value; `paintEvent` redraws the ring each frame:
- a **breathing radial glow** (sine-driven),
- an **expanding + fading pulse ring** (fired on state change, looped while verifying/open/alert),
- a **crisp static ring**.

The color and timing come from `_STATE_COLOR` / `_BEACON` keyed by the door state.

---

## 7. ROI calibration

Pressing `c` sets `_calibrating = True`. Clicking the camera maps widget coords → normalized frame coords (`_feed_to_norm`, using `_frame_w/h` set during feed render), stores corner 1, then corner 2 → `save_zone()` writes `roi_zone.json`. `camera_worker` reads the same zone to ignore detections outside it.

---

## 8. Shutdown

`edge_app.py` calls `display.stop()` → sets a stop flag → `_tick` notices, calls `window.shutdown()` (stops all widget `QTimer`s **on the Qt thread**, avoiding cross-thread timer kills), closes, and quits. `stop()` joins the thread. This is why there are no "timer from another thread" crashes.

---

## 9. Key config knobs (`config.py`)

```python
DISPLAY_ENABLED          # flips the whole display on/off (headless mode)
DISPLAY_FULLSCREEN       # True = fullscreen kiosk, False = windowed
DISPLAY_FPS              # max redraws per second
DISPLAY_FEED_SCALE       # upscale factor for the camera feed
DISPLAY_ACCESS_CLEAR_MS  # auto-clear "last access" panel (ms)
DISPLAY_SPONSORS         # sponsor credits (footer)
DISPLAY_DEVELOPERS       # developer credits (footer)
LOGO_PATH                # path to the brand logo (header)
```

---

## 10. Theme

The current theme is **light**: white background with **blue text** so the text never hides on white.

- App background: `#FFFFFF` (white)
- Card surface: `#F2F5FB` (white with a blue tint)
- Nested / recessed surface: `#E9EEF7`
- Hairline borders: `#D4DEEB`
- Primary text: `#1B3A8F` (deep blue)
- Secondary text: `#3F66C4`
- Muted text: `#7B91C4`

State accents are darkened to remain legible on white:

| State | Color |
|-------|-------|
| idle / locked | `#52617B` (slate) |
| verifying | `#B45309` (amber) |
| open / granted | `#15803D` (green) |
| alert / deny | `#DC2626` (red) |
| informational | `#0369A1` (cyan) |

> The camera feed well stays black (a live video screen), and the animated beacon paints its ring/glow in the darkened accents, which read clearly on the white background.
