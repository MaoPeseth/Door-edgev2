# Whole System — TEE Door Access Control (Face + RFID)

A complete smart-door system at RUPP (STEM) that lets students unlock a door
with **face recognition** (edge PC + camera) or **RFID cards** (ESP32 EM4100
reader), with a Cloud dashboard owning the roster, live access logs, and
Telegram security alerts.

---

## 1. Architecture Overview

```
                          ┌─────────────────────┐
                          │  CLOUD (dashboard)  │
                          │  members, embeddings │
                          │  access logs, alerts │
                          │  Telegram alerts     │
                          └──────┬───────────┬───┘
                                 │ HTTPS/API │ SSE sync-stream
                                 ▼           ▼
                          ┌──────────────────────┐
                          │   EDGE PC            │
                          │  10.4.70.114         │
                          │  face recognition    │
                          │  sync + allowlist    │
                          │  offline queue       │
                          │  kiosk display       │
                          └──────┬───────────┬───┘
                                 │ MQTT      │ MQTT
                                 ▼           ▼
                          ┌──────────────────────┐
                          │   ESP32 DOOR CTRL    │
                          │  relay (lock)        │
                          │  EM4100 RFID reader  │
                          │  buzzer, LEDs        │
                          │  bypass/exit switch  │
                          └──────────────────────┘
```

Three tiers, one shared MQTT broker (Mosquitto, `10.4.70.114:1883`):

| Tier | Runs on | Responsibility |
|------|---------|----------------|
| **Cloud** | Remote dashboard (`tee-doorlock-dashboard.local`) | Owns the member roster, face embeddings, card allowlist, access logs, Telegram alerts |
| **Edge PC** | Local machine (this one, Ubuntu) | Face recognition, sync with Cloud, MQTT hub to the door, kiosk display, offline buffering |
| **ESP32 door** | ESP32-S3 at the door | Relay/lock, RFID verification, buzzer/LEDs, bypass & exit switches, offline-first allowlist |

---

## 2. Edge PC (`Door-Edge V2/`)

### 2.1 Startup order (`edge_app.py`)

1. Cap OpenVINO CPU threads (`INFERENCE_THREADS=4`).
2. Load models — InsightFace (faces), MiniFASNet liveness (REAL/FAKE), YOLO (hand).
3. Connect MQTT + subscribe (`door/status`, `door/access/log`, `door/enroll/capture`).
4. Start camera worker + kiosk display (work immediately on last-synced data).
5. Register door presence (`POST /api/edge/doors`) + start heartbeat (60 s beats, 180 s TTL).
6. First sync from Cloud (blocking) — embeddings → Redis, allowlist → ESP32.
7. Start SSE listener + 5-minute consistency check.

Runs 24/7 as a systemd unit `door-edge.service` (user `tee`, `DISPLAY=:1`).

### 2.2 Core modules (`core/`)

| Module | Role |
|--------|------|
| `models.py` | Loads all models once; thread-safe inference behind a lock; ROI cropping |
| `insightface_ov.py` | InsightFace `buffalo_sc` (det_500m + rec_mbf) converted to native OpenVINO IR, 640×640 detector, 512-dim embeddings |
| `camera_worker.py` | Two threads: **capture** (full camera fps → shared state) + **inference** (capped at 10 fps). Liveness (REAL/FAKE), face matching, hand verification, distance gate, unlock/deny decisions. Auto-reconnects to a dead USB camera |
| `face_matcher.py` | Redis-Stack **HNSW** vector index (`door_face_index`, cosine, 512-dim). `door_person:*` hash keys |
| `liveness.py` | MiniFASNet v2 + v1se ensemble (native OpenVINO IR, 80×80 face crop) → softmax `[live, print, replay]`; liveness score = p_live |
| `sync_agent.py` | Revision-based sync from Cloud: embeddings + students → Redis, card allowlist → ESP32 over MQTT |
| `sse_client.py` | SSE listener on `/api/edge/sync-stream` — `sync`, `enroll`, `enroll_cancel`, `heartbeat` |
| `cloud_client.py` | HTTP client for all Cloud API endpoints (retries with backoff) |
| `mqtt_publisher.py` | Paho client, subscribes to ESP32 messages, publishes unlock/denied/health/sync/alert |
| `alert_tracker.py` | Warning thresholds: unknown face (3 attempts) / unknown card (every tap) / spoof episodes; resets on success or timeout |
| `heartbeat.py` | Door presence: register + verify room name at startup, status POSTs every 60 s |
| `enroll_relay.py` | SSE `enroll` → arm ESP32 (`door/cmd/enroll`); captured card relayed to Cloud; watchdog auto-disarm |
| `offline_queue.py` | JSONL buffer (`event_queue.jsonl`) for events/alerts when the Cloud is down; in-order flush on recovery |
| `screen_state.py` | Thread-safe shared state (frame + seq, results, events, status) |
| `screen_display.py` | Tkinter fullscreen kiosk: live feed + overlays, status panel, last-access, alert banner, event ticker, credits |
| `face_image.py` | Encode frame/crop → base64 JPEG for Cloud/Telegram photos |
| `roi.py` | Normalized ROI rectangle (`roi_zone.json`); only detections inside count |
| `anti_spoof.py` | **Removed (2026-08-15)** — replaced by `liveness.py` (MiniFASNet REAL/FAKE) |

### 2.3 Models

| Model | Source | Input | Purpose |
|-------|--------|-------|---------|
| InsightFace det_500m + rec_mbf | `baffolo_sc_openvino_model/` (from `convert.py`) | 640×640 / 112×112 | Face detect + 512-dim embedding |
| MiniFASNet v2 + v1se | `liveness_models/` (converted from garciafido ONNX exports) | 80×80 face crop | Face liveness anti-spoof — REAL (live) vs FAKE (printed photo / screen replay) |
| YOLO hand | `hand_detection_openvino_model/` | 416×416 (fixed) | Raise-hand gesture confirmation |

### 2.4 Face recognition flow (per inference frame)

```
frame → [ROI filter] → InsightFace detect+embed (largest face)
     → distance gate (far) first: face too far ⇒ "Move closer" prompt
       only — no liveness, no alert (an 80×80 crop of a far face is mostly
       background and can be misjudged as FAKE)
     → MiniFASNet liveness (2-model ensemble, 10-frame score smoothing;
       score < 0.90 ⇒ FAKE → blocks unlock immediately, but the alert chain
       (deny + spoof_detected event + /alert photo) fires only after
       LIVENESS_FAKE_STREAK = 5 consecutive FAKE frames; once per episode,
       15 s min interval)
     → distance gate (close): face too close ⇒ "Step back" deny event
       (runs after liveness so a close-up photo attack still hits FAKE)
     → Redis HNSW match (SIMILARITY_THRESHOLD = 0.5)
         ├─ match → hand verification (hand held still near face,
         │          HAND_STREAK_FRAMES ≈ 0.6 s) → CONFIRM_FRAMES (3) →
         │          MQTT unlock + Cloud access_granted event
         └─ no match → unknown: only a hand-raised attempt counts
              → /events per counted attempt (once per second),
                /alert at WARNING_THRESHOLD (3), then silent for
                FACE_ALERT_MIN_INTERVAL (15 s)
```

### 2.5 Data stores

| Store | What | Notes |
|-------|------|-------|
| Redis-Stack `:6379` | Face embedding index `door_face_index` | Persists (`dump.rdb`); cleared + reloaded at startup sync (Cloud authoritative) |
| `roi_zone.json` | Detection zone (normalized rect) | Set in calibration mode (press `c` on display) |
| `event_queue.jsonl` | Offline event/alert buffer | Flushed in order when Cloud returns |
| `registration.db` | Legacy local DB | Deprecated — Cloud API is the source of truth |

---

## 3. ESP32 Door Controller (`ESP_mqtt/`)

MQTT-only firmware (v3.2.0-mqtt), derived from production `ESP32_1CH_RUPPV2`
with the HTTP route cut. Board: `esp32:esp32:esp32s3` (16 MB).

### 3.1 Hardware (unchanged production pinout, `configuration.h`)

| GPIO | Function |
|------|----------|
| 6 | Relay (door lock) |
| 7 | WiFi LED |
| 8 | Status LED |
| 9 | Buzzer |
| 10 | RFID RX — HardwareSerial1, EM4100 reader (125 kHz, **10-digit decimal UID**) |
| 41 | Bypass switch (INPUT_PULLUP, low = bypass) |
| 42 | Exit switch (INPUT_PULLUP, low edge = open) |

### 3.2 Firmware behaviour

- **Offline-first**: RFID + LittleFS allowlist work with no broker, no edge, no Cloud.
- **Allowlist**: `TagManager.h`, LittleFS, max 500 tags, write-then-rename,
  atomic bulk load (`full_sync` replaces the whole table).
- **Card decision**: read UID → lookup → granted? `openDoor()` (relay 3 s +
  short buzz) + publish `door/access/log`. Denied → alarm buzz + log with reason.
- **Bypass**: overrides everything (relay held open). **Exit switch**: open from inside.
- **Lock state is not persisted** — a reboot leaves the door locked.
- **MQTT**: QoS 1, `cleanSession=false` — broker queues commands while the door
  is down and delivers on reconnect. `door/status` is retained + LWT.
- **NTP**: syncs time (ICT-7) for tag expiry and log timestamps.
- **ArduinoOTA** for network firmware updates (with `prepareForFirmwareWrite()`
  → lock door + announce offline before reboot).

### 3.3 MQTT topic contract

| Topic | Direction | Payload / meaning |
|-------|-----------|-------------------|
| `door/status` | ESP → edge | Retained `"online"`/`"offline"` (also LWT) |
| `door/access/log` | ESP → edge | `{granted, method, name, person_id, card_uid, reason, timestamp}` on every card decision |
| `door/enroll/capture` | ESP → edge | `{tagID}` — card captured while enrolment armed |
| `door/cmd/unlock` | edge → ESP | `{door_id, person_id, name, similarity, method, timestamp}` → `openDoor()` |
| `door/cmd/lock` | edge → ESP | → `lockDoor()` |
| `door/access/denied` | edge → ESP | `{reason}` — `spoof_detected` → alarm + stay locked; `unknown_face` → short buzz; others → deny print |
| `door/alert/warning` | edge → ESP | `{type: "warning" | "alert_cleared"}` → alarm pattern / clear |
| `door/sync/card_uid` | edge → ESP | `{type: "full_sync"|"add"|"delete", cards: [...]}` — allowlist push (10-digit UIDs only) |
| `door/cmd/enroll` | edge → ESP | `{enroll: bool, timeoutS}` — arm/disarm enrolment mode |

---

## 4. Cloud API contract

Base: `http://tee-doorlock-dashboard.local/` (Caddy → backend). All requests
need header `X-API-Key`. Backend LAN: `http://10.4.70.189`.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/edge/sync-status` | `{revision, roster_revision, total_embeddings, total_students}` |
| `GET /api/edge/rooms` | Room names (presence verification) |
| `GET /api/students` | Paginated member roster (`all_rooms` flag, rooms are *names*) |
| `GET /api/embeddings/all` | 512-dim float embeddings (or `?format=base64`) |
| `GET /api/edge/allowlist?room=…` | Card allowlist, plain text, one UID per line; `&since=` → 304 when unchanged |
| `GET /api/edge/sync-stream` | SSE: `sync` (revisions), `enroll`, `enroll_cancel`, `heartbeat` |
| `POST /api/edge/doors` | Register door device |
| `POST /api/edge/doors/status` | Presence heartbeat |
| `POST /api/edge/events` | Access events (`access_granted`, `unknown_face`, `unknown_card`, `spoof_detected`) |
| `POST /api/edge/alert` | Threshold-crossing alerts (Telegram) |
| `POST /api/edge/doors/<room>/enroll/capture` | Relay enrolled card |

**Sync contract (revision-based):** revisions are opaque strings, compare with
`!=`. Any change → SSE `sync` event (also sent on connect) and/or the edge's
5-minute consistency poll; the edge re-fetches whichever revision moved and
**full-mirrors** its local state (clear + reload). The Cloud is authoritative —
an empty Cloud empties the edge (face index) and the door (card allowlist).

---

## 5. Data / event flows

### 5.1 Face unlock
1. Camera → InsightFace match → hand verification → 3 confirmed frames.
2. Edge publishes `door/cmd/unlock` (ESP32 opens relay 3 s) + POSTs
   `access_granted` to Cloud → access log + dashboard entry.
3. Alert counters reset; `alert_cleared` published to ESP32.

### 5.2 RFID unlock
1. ESP32 reads card → LittleFS lookup (granted/denied).
2. Publishes `door/access/log` → edge updates display; granted events also
   POSTed to Cloud (`report_rfid_match`).
3. Unknown card → edge forwards `unknown_card` to Cloud (audit + Telegram
   every tap by default) + ESP32 alarm buzz.

### 5.3 Sync (Cloud → edge → ESP32)
1. Cloud bumps `revision`/`roster_revision` on any member/embedding change.
2. SSE `sync` event (or 5-min poll) → edge re-fetches embeddings (→ Redis,
   full mirror) and students (→ card UID map).
3. Roster change → `GET /api/edge/allowlist` → `full_sync` to ESP32 over MQTT
   (atomic replace of the door's LittleFS allowlist).

### 5.4 Enrolment (card)
1. Dashboard initiates enrolment → SSE `enroll` → edge arms ESP32
   (`door/cmd/enroll`, 60 s timeout + watchdog).
2. ESP32 captures the card while armed → `door/enroll/capture` → edge relays
   to Cloud (`enroll/capture`) → Cloud stores the UID → disarms.

---

## 6. Security / alerting

| Channel | Trigger | Actions |
|---------|---------|---------|
| Unknown face | Hand-raised attempt counted (1/s) | `POST /events` per attempt; ESP32 deny buzz; display event |
| Face warning | `WARNING_THRESHOLD` (3) attempts in window | `POST /alert` (+ JPEG photo) → Telegram; ESP32 alarm; red display banner; silent for `FACE_ALERT_MIN_INTERVAL` |
| Unknown card | Every denied tap (default) | Cloud audit + Telegram; ESP32 alarm buzz |
| Fake face (liveness) | MiniFASNet score < `LIVENESS_THRESHOLD` (0.90) episode, ≥ 5 consecutive FAKE frames | Blocks ALL unlocks immediately (first FAKE frame); own `/alert` channel after the 5-frame streak (once per episode, 15 s min interval); ESP32 stays locked + alarm |
| Distance gate | Face too close (> 50 % frame) / too far (< 8 %) | Blocked with on-screen instruction |

---

## 7. Offline / resilience design

- **ESP32**: RFID + allowlist work with zero network.
- **Edge**: runs on the last-synced Redis data + last allowlist push; events
  buffered in `event_queue.jsonl` and flushed when the Cloud returns.
- **MQTT QoS 1 + cleanSession=false**: commands to the door are queued by the
  broker while it is down and delivered on reconnect.
- **Camera**: auto-reopens a dropped USB camera every `CAMERA_RETRY_DELAY`;
  inference skips stale frames (no CPU burn while offline).
- **Systemd**: `door-edge.service` — auto-restart on crash (`Restart=always`),
  depends on Redis-Stack, Mosquitto (snap), and network.

---

## 8. Deployment & operations

- **Edge**: `sudo systemctl restart door-edge` (service = `door-edge.service`,
  venv Python at `Door-Edge V2/venv/bin/python edge_app.py`).
- **Start / stop**: `sudo systemctl start|stop|restart door-edge`; logs via
  `journalctl -u door-edge -f`.
- **Services required**: `redis-stack-server` (:6379), `snap.mosquitto.mosquitto` (:1883).
- **Camera**: `/dev/video1` (set `CAMERA_INDEX` in `config.py`).
- **Display**: needs `DISPLAY=:1` (GDM/Xorg on this machine) + `python3-tk`.
- **Config**: everything in `config.py` — camera, thresholds, topics, Cloud URL/key, timings.
- **Credentials**: `secrets.h` (ESP32, gitignored) — WiFi, MQTT, OTA.

---

## 9. Repository layout

```
~/Desktop/my_project/
├── Door-Edge V2/               # Edge PC (Python, main project — live systemd unit)
│   ├── edge_app.py             # entry point
│   ├── config.py               # all settings
│   ├── core/                   # all edge modules (see §2.2)
│   ├── baffolo_sc_openvino_model/   # InsightFace OpenVINO IR
│   ├── liveness_models/        # MiniFASNet v2 + v1se OpenVINO IR (anti-spoof, replaces Antispoofing_openvino_model/)
│   ├── hand_detection_openvino_model/ # YOLO hand
│   ├── docs/                   # plans, logs, guides (see §10)
│   └── venv/                   # Python environment
├── spoofing test/              # MiniFASNet ONNX originals + webcam test
├── ESP_mqtt/                   # current door firmware (MQTT-only)
│   ├── ESP_mqtt.ino
│   ├── configuration.h         # pins, topics, identity
│   ├── secrets.h               # credentials (gitignored)
│   ├── EM4100.h                # 125 kHz RFID reader
│   └── TagManager.h            # LittleFS allowlist
├── ESP32_1CH_RUPPV3/           # production firmware v3 (HTTP version, superseded)
└── ESP_mqtt.zip                # firmware archive
```

---

## 10. Documentation index

| File | Content |
|------|---------|
| `docs/MAINTENANCE_LOG.md` | All fixes, features and verification logs (newest at the end) |
| `docs/SESSION_LOG_2026-08-06.md` | 24/7 deployment session (Docker removed, systemd, Redis-Stack) |
| `CHANGES_THIS_AFTERNOON.md` | Hand verification + unknown-face event fixes |
| `docs/ESP32_MQTT_ADAPTATION_PLAN.md` | HTTP → MQTT firmware adaptation plan |
| `docs/CLOUD_SIDE_FIXES_REQUEST.md` / `CLOUD_SIDE_RESPONSE.md` | Cloud API contract discussions |
| `docs/sync_agent_design.md` | Original sync + MQTT design |
| `docs/TESTING_GUIDE.md`, `REAL_TESTING_PLAN.md`, `TESTING_MOCK_CLOUD_PLAN.md` | Testing plans |
| `docs/DISPLAY_DESIGN.md`, `ROI_ZONE_PLAN.md`, `CPU_OPTIMIZATION_PLAN.md`, `EDGE_PC_TODO.md` | Feature/optimization plans |
| `door-edge-deployment-guide.md` | Deployment guide (new machine) |
