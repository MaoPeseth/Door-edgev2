# Door-Edge Session Log — 2026-08-06

**Date:** August 6, 2026
**Scope:** Prepare this machine (10.4.70.114) for 24/7 Door-Edge operation:
full smoke test, fix runtime blockers, cut Docker, add systemd supervision.

---

## Summary

The edge system is now running 24/7 as a systemd service on this machine, with
native (non-Docker) dependencies. Live verification passed: face recognition,
Cloud sync, SSE, MQTT, and the ESP32 door controller are all connected and
working.

---

## Changes Made

### 1. Removed Docker from the project and the machine

Docker was used only for Mosquitto and Redis (old `door-redis` on port 6380).
Both are now native services, so the whole Docker layer was cut.

| Item | Action |
|------|--------|
| `docker-compose.yml` | Deleted (only defined Mosquitto) |
| `mosquitto/mosquitto.conf` | Deleted (snap Mosquitto already uses an identical config) |
| `Dockerfile` | Deleted (unused deployment attempt) |
| `door-mosquitto`, `door-redis` containers | Removed |
| Docker engine | Left installed at the time; optional purge commands in section 3 |

### 2. Fixed `config.py` — wrong Redis port and camera index

| Setting | Old | New | Reason |
|---------|-----|-----|--------|
| `REDIS_PORT` | `6380` | `6379` | Redis on this machine is native on 6379; 6380 was the old Docker port |
| `CAMERA_INDEX` | `0` | `1` | This machine's camera is `/dev/video1` (index 0 does not exist) |

### 3. Removed manual MQTT reconnect thread

**File:** `core/mqtt_publisher.py`

**Issue:** `_reconnect_loop()` ran a manual `reconnect()` every 2 s while paho's
`loop_start()` (loop_forever) already auto-reconnects. The two paths fought each
other and churned the broker session.

**Fix:** Removed the thread and `_reconnect_loop()`; paho's built-in reconnect
handles it. (Note: session churn observed in testing was actually two edge
instances sharing client id `edge-door-01` — see section 5.)

### 4. Replaced Redis with Redis Stack (RediSearch)

**Problem:** Native `redis-server` (7.0.15) has **no RediSearch module**, so the
HNSW vector index `door_face_index` could not be created. Embeddings were stored
(HSET works) but search always failed → `"No enrolled faces available"` and
`0 face(s) enrolled` on every run.

**Fix:**
- Installed `redis-stack-server` (7.4.0-v8) from the Redis official repo.
- Redis publishes the stack package only for **jammy** — the repo line in
  `/etc/apt/sources.list.d/redis.list` uses `jammy main` on this noble machine
  (deps are just `libssl-dev`, `libgomp1`).
- Disabled plain `redis-server.service`, enabled `redis-stack-server.service`
  (same port 6379).
- No data migration needed — Redis held only 2 Door-Edge keys, recreated by the
  next sync.

### 5. Created systemd unit `door-edge.service`

Runs `edge_app.py` 24/7 with auto-start on boot and auto-restart on crash.

| Setting | Value |
|---------|-------|
| `ExecStart` | `.../Door-Edge_new_machine/venv/bin/python edge_app.py` |
| `User`/`Group` | `tee` / `video` (camera access) |
| `Requires` / `After` | `redis-stack-server.service`, `snap.mosquitto.mosquitto.service`, `network-online.target` |
| `Restart` | `always`, `RestartSec=5` |
| `Environment` | `DISPLAY=:1` — **must match the GDM session display number** (this machine runs Xorg on `:1`, not `:0`) |
| Shutdown | `SIGINT` (graceful) |

**Install:**
```bash
sudo install -m 644 /tmp/opencode/door-edge.service /etc/systemd/system/door-edge.service
sudo systemctl daemon-reload
sudo systemctl enable --now door-edge
```

---

## Test Results (smoke test, this machine)

| Check | Result |
|-------|--------|
| Redis ping (6379) | ✅ PONG |
| RediSearch module | ✅ loaded, `door_face_index` created |
| MQTT broker roundtrip (1883) | ✅ |
| Camera (`/dev/video1`, V4L2) | ✅ 640×480 reads OK |
| Models (InsightFace + YOLO anti-spoof + hand) | ✅ loaded, CPU OpenVINO |
| Cloud API (`https://desktop-796gse4.tailb6b460.ts.net/`) | ✅ revision match |
| SSE sync + allowlist | ✅ 2 students, 3 cards |
| Card allowlist → ESP32 (`door/sync/card_uid`) | ✅ 3 cards sent |
| Live face match | ✅ `Recognized Mao Peseth (TEE) sim=0.762` |
| ESP32 online via MQTT | ✅ `door/status` online |
| `door-edge` service | ✅ active, auto-restart, ~680 MB RSS (models in RAM) |

---

## Troubleshooting notes

- **Display not shown after restart:** Xorg runs on `:1` (GDM), the unit had
  `DISPLAY=:0` → `TclError: couldn't connect to display ":0"`. Fixed by
  `DISPLAY=:1`. The display only exists while a desktop session is logged in;
  headless boot is fine (system keeps running, no UI).
- **`Failed to enable redis-stack-server`: unit does not exist** → the package
  was never installed (the repo line must be `jammy`, not `noble`).
- **MQTT session churn ("session taken over")**: two `edge_app.py` instances
  with the same client id. Always check for stray processes before restarting:
  `ps aux | grep edge_app`.

---

## Remaining work (not done this session)

1. ESP32 hardware wiring + RFID/relay tests (`docs/REAL_TESTING_PLAN.md` T1–T9)
2. Flash the wall door with `ESP_mqtt` firmware (after hardware tests pass)
3. Failure drills: broker down / Cloud down / edge killed
4. Full enrollment E2E via the real registration app
5. Hand-verification calibration on camera
6. Optional: purge Docker engine
   ```bash
   sudo systemctl disable --now docker
   sudo apt purge -y docker-ce docker-ce-cli docker-ce-rootless-extras docker-buildx-plugin docker-compose-plugin containerd.io
   sudo apt autoremove -y && sudo rm -rf /var/lib/docker /var/lib/containerd /etc/docker
   ```
7. Housekeeping: outdated docs (TESTING_GUIDE/REAL_TESTING_PLAN still reference
   Docker/6380), leftover files (`dump.rdb`, `esp_test/` copies, `event_queue.jsonl`)
