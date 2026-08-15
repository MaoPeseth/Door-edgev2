# Door-Edge Maintenance Log

**Date:** August 2, 2026
**Purpose:** Record code review findings and fixes applied to the Door-Edge system.

---

## Summary

A full code review of the project was performed. All issues found were fixed and
documented below. One outdated planning document was removed.

---

## Changes Made

### 1. Removed Outdated Document

| File | Action | Reason |
|------|--------|--------|
| `edge-api-plan.md` | Deleted | Plan to build a local Flask API server on the Edge machine. Never implemented and superseded by the Cloud API + SSE sync approach (`core/cloud_client.py`, `core/sse_client.py`, `core/sync_agent.py`). |

---

### 2. Fixed: Duplicated Dead Code in Camera Worker

**File:** `core/camera_worker.py`

**Issue:** In `_process()`, two code blocks were duplicated:
- `yolo_dets = []` was assigned twice
- `FaceMatcher.search(embedding)` was executed twice (wasting a Redis search on every frame)

**Fix:** Removed the duplicate block. Only one anti-spoof note, one `yolo_dets = []`, and one `FaceMatcher.search()` call remain.

---

### 3. Fixed: Blocking MQTT Reconnect Deadlock Risk

**File:** `core/mqtt_publisher.py`

**Issue:** `_on_disconnect()` contained a blocking `while` loop with `time.sleep()` calls. This callback runs **inside paho-mqtt's own network thread** — blocking it prevents the client loop from running, which can deadlock reconnection and starve the network thread.

**Fix:**
- `_on_disconnect()` now only logs and sets `_connected = False` (returns immediately).
- Added a dedicated background thread `MQTTReconnect` that checks connection status every 2 seconds and calls `reconnect()` when disconnected.
- `disconnect()` now stops and joins the reconnect thread before shutting down.

---

### 4. Fixed: Undefined `publishAccessLog` Member in ESP32 Firmware

**Files:**
- `esp_test/door_lock.h`
- `esp_test/esp_door_control.ino`
- Also synced to backup copies `esp_test (2)/` and `esp_test (3)/`

**Issue:** `onUnlock()` in the main sketch set `door.publishAccessLog = true;`, but `DoorLock` had no such member — the firmware would not compile, and even if it did, the face-unlock access was never logged to Edge.

**Fix:**
- Added a public `bool publishAccessLog = false;` member to `DoorLock`.
- `onUnlock()` now stores the pending person data (`pendingFaceLogPersonID`, `pendingFaceLogName`) and sets the flag.
- `loop()` step 4 flushes the flag: publishes `door/access/log` with method `"face"`, `granted=true`, then clears the flag. Publishing is done outside the MQTT callback to avoid blocking the connection.

---

### 5. Reviewed: Mosquitto Configuration (No Change Needed)

**File:** `mosquitto/mosquitto.conf`

```
listener 1883 0.0.0.0
allow_anonymous true
```

**Verdict:** Correct for local-network testing (Edge PC broker + ESP32). No change required.

---

## Noted But Not Changed

| Item | Note |
|------|------|
| `config.py` → `CLOUD_API_URL = "http://192.168.1.100:5000"`, `CLOUD_API_KEY = "your_api_key_here"` | Deployment values — must be set to the real Cloud server IP and API key before production use. |
| `redis` Docker container on port 6380 | Started manually per docs (`docker run -d --name redis -p 6380:6379 redis:alpine`), not part of `docker-compose.yml`. Consider adding it to compose for reproducibility. |

---

## Files Touched

```
Door-Edge_new_machine/
├── edge-api-plan.md                        (DELETED)
├── core/
│   ├── camera_worker.py                    (removed duplicated block)
│   └── mqtt_publisher.py                   (background reconnect thread)
└── esp_test/                               (publishAccessLog fix)
    ├── door_lock.h
    └── esp_door_control.ino
```

---

## Verification

- [x] `python3 -m py_compile core/camera_worker.py` — passed
- [x] `python3 -m py_compile core/mqtt_publisher.py` — passed
- [ ] ESP32 firmware compile — pending (Arduino IDE)

**Recommended test after this change:**
1. Start Edge + broker, disconnect broker, verify Edge reconnects automatically (background thread).
2. Upload ESP32 firmware, confirm no compile errors and that a face unlock now appears in `door/access/log`.

---

## Screen Display Implementation (2026-08-02)

Implemented the kiosk door display planned in `docs/DISPLAY_DESIGN.md`.

### New files
| File | Description |
|------|-------------|
| `core/screen_state.py` | Thread-safe shared state: camera frame + seq, detection results, last access, alert banner, event ticker, status flags |
| `core/screen_display.py` | Tkinter fullscreen UI (daemon thread): camera feed + overlays, status panel, last-access panel, warning banner, event ticker, sponsor + developer credits bar |

### Modified files
| File | Change |
|------|--------|
| `core/camera_worker.py` | Publishes frames/results to `screen_state` instead of `cv2.imshow`; no OpenCV window anymore; `_send_denied()` now returns bool; access results + events pushed to state; removed duplicate Cloud reports |
| `core/mqtt_publisher.py` | Now subscribes to `door/access/log`; RFID events from ESP32 appear on the display; added `is_connected()` |
| `core/sync_agent.py` | Added `is_connected()` (SSE stream status = Cloud status on display) |
| `core/alert_tracker.py` | Warning triggers show the red banner on display; counters reset clears it |
| `edge_app.py` | Starts `ScreenDisplay` after the camera worker (only when `DISPLAY_ENABLED`) |
| `config.py` | Added display settings + sponsor/developer placeholders + `MQTT_TOPIC_ACCESS_LOG` |
| `requirements.txt` | Added `Pillow` |

### Behavior
- **ESC** closes the display only — system continues headless.
- Display FPS capped (`DISPLAY_FPS=15`), feed redrawn only on new frame, ticker rebuilt only on change → negligible CPU.
- `DISPLAY_ENABLED=False` → headless, zero rendering.

### Ubuntu requirement (NOT installed yet)
The display needs the system Tkinter package:
```bash
sudo apt install python3-tk
# disable screen blanking so the display never sleeps:
xset s off -dpms
```
Without it, the Edge runs headless and logs: `[Display] Missing dependency (...) — display disabled, running headless`.

### Verification
- [x] `py_compile` on all modified Python files — passed
- [x] Smoke test: new modules import; shared-state round-trip OK
- [x] `Pillow` present in venv
- [ ] `python3-tk` install on Ubuntu (tomorrow with hardware)
- [ ] Fullscreen UI renders; face/RFID events appear on display (tomorrow with hardware)

---

## Temporary Mock Cloud API + Fixes (2026-08-02)

While the real Cloud API endpoints are pending, enrollments can now go
**directly to the edge** via a local mock Cloud server in `Testing/`.
See `docs/TESTING_MOCK_CLOUD_PLAN.md` for the full plan and elimination steps.

### New files
| File | Description |
|------|-------------|
| `Testing/mock_cloud_server.py` | Local mock Cloud API (stdlib only, port 5005): `sync-status`, `students`, `embeddings/all`, SSE stream, `POST /api/enroll` + `/api/delete`. Data persists in `Testing/data.json`. |
| `Testing/send_enroll.py` | CLI test client (enroll/delete, optional real embedding file). |
| `Testing/README.md` | Usage, endpoint table, elimination steps. |
| `docs/TESTING_MOCK_CLOUD_PLAN.md` | Plan + verification checklist. |

### Fixed: `edge_app.py` NameError (would crash on the real machine)
`edge_app.py` used `cfg.DISPLAY_ENABLED` (line 88, display start) but never
imported `config` — with `DISPLAY_ENABLED=True` the app would crash right
after starting the camera worker. Fixed with `import config as cfg`.
(Found by the full-system run with the mock server.)

### Fixed: SSE never delivered events to the edge
1. `http.server` HTTP/1.0 + no Content-Length → urllib3 buffers the body
   until connection close, so `requests` clients received nothing.
   Fixed with `protocol_version = "HTTP/1.1"` + chunked transfer encoding.
2. Change detection was count-based — re-enrolling the same student changed
   no counts, so no SSE push. Fixed with a change counter + last-changed id,
   and deletes now push `member_deleted`/`embedding_deleted`.

### Temporary config change (REVERT when Cloud is ready)
```python
CLOUD_API_URL = "http://localhost:5005"   # TEMP: Testing/mock_cloud_server.py
CLOUD_API_KEY = "mock-key"                # TEMP
```

### Verification (all on this dev machine)
- [x] Mock server: GET endpoints, enroll/delete, `data.json` persistence
- [x] SSE via the real `core/sse_client.SSEClient`: heartbeat, `member_updated`+`embedding_updated` on new enroll AND on upsert, `member_deleted`+`embedding_deleted` on delete
- [x] Full `edge_app.py` run against the mock: models load, MQTT connected, initial sync → `3 face(s) enrolled in Redis`, SSE stream connected
- [x] **Live enrollment test**: enroll while `edge_app.py` is running → SSE push → `Member updated`/`Embedding updated` → Redis `num_docs` 3→4, no restart
- [x] Graceful shutdown (SIGINT): SSE, Sync, MQTT all stop cleanly
- [ ] Hardware test on the Mini PC: real camera feed + display + registration software pointed at `http://<edge-ip>:5005`

---

## CPU Optimization: Inference Throttle (2026-08-02)

Second plan + as-built in `docs/CPU_OPTIMIZATION_PLAN.md` (section 8 = measurements).

**Problem:** `edge_app.py` consumed **~631% CPU** (~6 of 12 cores, load ~14.8 on 12 threads). Cause: `_infer_loop()` ran the full model pipeline on **every** camera frame with no throttle — the `INFERENCE_EVERY_N` config (defined) was never read by any code.

**Fixes applied:**
| File | Change |
|------|--------|
| `config.py` | `INFERENCE_FPS=10`, `INFERENCE_THREADS=4`, `YOLO_DEBUG=False`, `INFERENCE_EVERY_N` marked legacy. `YOLO_IMGSZ`/`HAND_IMGSZ` left/back at **416** (trained at 416 — a 320 test killed detections, reverted) |
| `core/camera_worker.py` | `_infer_loop()` capped at `INFERENCE_FPS` (time-based interval; works for any camera fps) |
| `edge_app.py` | `_cap_openvino_threads()` sets OpenVINO `INFERENCE_NUM_THREADS=4` + `NUM_STREAMS=1` + `OMP_NUM_THREADS=4` before models compile |

**Result (measured, camera attached):** ~631% → **~250%** (~2.5 of 12 cores), ~60% steady-state reduction. Recognition accuracy unchanged (same models, thresholds, embedding pipeline; only frame cadence changed). Verification: identical startup log (models → sync → camera → SSE), `2 face(s) enrolled in Redis` against the mock. The 250% figure still held after `YOLO_IMGSZ` reverted to 416 — the savings come from the throttle + thread caps, not the imgsz change.

**Notes:**
- OpenVINO 2026.2.1 property key is `INFERENCE_NUM_THREADS` (the classic `THREADS` / `CPU_THREADS_NUM` keys are unsupported).
- Fixed a `_cap_opencl_threads` → `_cap_openvino_threads` typo during first verification run.
- If more headroom is ever needed: lower `INFERENCE_FPS` or `YOLO_IMGSZ=256`.

---

## ESP_mqtt: MQTT-Only Door Firmware (2026-08-02)

New Arduino sketch at `ESP_mqtt/` (sibling of `Door-Edge_new_machine/`), derived from
the production `ESP32_1CH_RUPPV2` (v3.0.0) per `docs/ESP32_MQTT_ADAPTATION_PLAN.md`.

**Design locked with user:** ESP is **MQTT-only**; HTTP REST route cut; only the
production pinout + hardware modules are reused.

**Reused verbatim:** `configuration.h` pin table (relay GP6, buzzer 9, status 8,
wifi 7, RFID RX 10, bypass 41, exit 42), `EM4100.h`, `TagManager.h` (LittleFS,
≤500 tags, write-then-rename), bypass/exit/auto-lock/buzzer logic, NTP, ArduinoOTA.

**Cut:** `WebServer.h`/`HTTPClient.h`, all `server.on()` routes (`/doorOpen`,
`/addTag`, `/deleteTag`, `/enroll/*`, `/update`, ...), `authorized()`/`hasValidKey()`,
`tickAllowlistSync()`, `postEvent()`/`tickEvents()` HTTP queue, UDP discovery,
enrollment. HTTP keys removed from `secrets.h`.

**New MQTT contract (topics in `configuration.h`):**
- ESP → edge: `door/status` (retained + LWT), `door/access/log` (per card decision)
- edge → ESP: `door/cmd/unlock` → `openDoor()`, `door/cmd/lock`, `door/alert/warning`,
  `door/sync/card_uid` (`full_sync`/`add`/`delete`)

**Details:**
- `full_sync` parses the `cards[]` array with a small depth-aware JSON walker,
  rejects non-10-digit EM4100 UIDs, and commits atomically via
  `beginBulkLoad/bulkAdd/commitBulkLoad` (two passes so the count is exact).
- `MQTT_BUFFER_BYTES=4096` (PubSubClient default 256 would drop full_sync).
- QoS 1 + `cleanSession=false` on commands so the broker queues them while the
  door is down; `door/access/log` published QoS 1, dropped silently if offline.
- Verified statically (no `arduino-cli` on this machine): zero WebServer/HTTPClient/
  Update refs, balanced braces (escape-aware scan), no duplicate defs, all config
  tokens resolve. **Not yet compiled/flashed** — needs Arduino IDE (board
  `esp32:esp32:esp32s3`, 16 MB) or `arduino-cli`.

**Test commands (after flash):**
```
mosquitto_pub -t door/cmd/unlock -m '{"method":"face"}'
mosquitto_pub -t door/sync/card_uid -m '{"type":"add","card_uid":"0000012345","name":"Sok","person_id":"STU-001"}'
mosquitto_pub -t door/sync/card_uid -m '{"type":"full_sync","cards":[{"card_uid":"0000012345","name":"Sok","person_id":"STU-001"}]}'
mosquitto_pub -t door/alert/warning -m '{"type":"warning"}'
mosquitto_sub -t 'door/#' -v
```

---

## Stale Data on Startup Sync — Cloud Deletions Not Mirrored (2026-08-13)

**File:** `core/sync_agent.py`

**Issue:** The Cloud had no registered members/embeddings (data deleted on the
Cloud side), but the Edge still recognized the old people locally. The startup
sync (`_do_initial_sync()`) fetched embeddings but **never cleared the Redis
index first**, so with an empty Cloud it printed `Loaded 0 embeddings to Redis`
while the previous sync's entries stayed alive in Redis (`door_person:*`) —
stale faces kept unlocking the door.

Only `_resync_embeddings()` (triggered by an SSE `sync` event or the 5-minute
consistency poll — both require the Cloud `revision` to change) called
`FaceMatcher.clear()` before reloading.

**Fix:** `_do_initial_sync()` now calls `FaceMatcher.clear()` before loading
whenever the Cloud responds (200), making the startup path mirror deletions the
same way the resync path already did. An empty Cloud now empties the index; a
*reachable* Cloud response is treated as authoritative. If the Cloud fetch
fails, the index is untouched (offline-first: last-known-good data kept).

**Behavior after fix:**
- Cloud empty + reachable → `[Sync] Loaded 0 embeddings to Redis`, index cleared,
  `0 face(s) enrolled in Redis`, camera reports "No enrolled faces available".
- Cloud unreachable → old index kept, door keeps working offline.

**Verification:**
- [x] `python3 -m py_compile core/sync_agent.py` — passed
- [ ] Restart `door-edge` with an empty Cloud → `redis-cli FT.INFO door_face_index | grep num_docs` = 0
- [ ] Restart with a non-empty Cloud → index repopulated, count matches `total_embeddings`

**Note (remaining gap, Cloud-side):** if members are deleted on the Cloud
*without* bumping `revision`/`roster_revision` (direct DB edit, reset), the
edge's SSE/consistency paths never fire — only the next startup (or a manual
restart) will now clear them.

---

## USB Camera Auto-Reconnect (2026-08-13)

**Files:** `core/camera_worker.py`, `config.py`

**Issue:** The USB camera is physically unstable (loose connector / marginal
power — touching it resets or drops the UVC device), and the edge never
recovered: `_capture_loop()` opened the camera **once** at startup and, on
`cap.read()` failure, just slept and retried the **same dead handle** forever.
A camera that died at startup or mid-run meant face recognition stayed dead
until `systemctl restart door-edge`. While offline, the inference thread also
kept re-running the model at 10 fps on the frozen last frame, burning CPU for
nothing.

**Fix:**
- `_capture_loop()` is now an outer retry loop:
  - failed open → log + event + retry every `CAMERA_RETRY_DELAY` (5 s), forever;
  - a run of `CAMERA_FAIL_LIMIT` (30) consecutive read failures (~1.5 s) is
    treated as a dead device → handle released, display shows
    "Waiting for camera…", reopen loop starts;
  - recovery is automatic — once `/dev/videoN` is back, the door resumes
    recognizing with no restart.
- "Camera online / Camera offline — reconnecting" events appear on the display
  ticker; the frame is cleared while offline (screen already renders
  "Waiting for camera…" for a `None` frame).
- Inference thread now skips frames whose sequence number hasn't advanced
  (`_last_frame_seq`), so a stalled camera costs ~0 CPU instead of 10 fps of
  model inference on a stale frame.

**New config:** `CAMERA_RETRY_DELAY = 5.0`, `CAMERA_FAIL_LIMIT = 30`.

**Verification:**
- [x] `python3 -m py_compile core/camera_worker.py config.py` — passed
- [ ] Unplug the camera while `door-edge` is running → "Camera offline" event,
      feed clears; plug it back → reconnects automatically, recognition resumes.
- [ ] Start `door-edge` with no camera plugged → keeps retrying (log line every
      5 s), connects automatically when the camera appears.

**Remaining hardware note:** if the port keeps dropping the device, prefer a
short high-quality cable and/or a powered USB hub — software can now recover
from the drops, but it cannot fix a marginal electrical connection.

