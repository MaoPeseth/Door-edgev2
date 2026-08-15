# Real-System Testing Plan — Tomorrow (Aug 3, 2026)

**Scope:** full-stack verification of Door-Edge ↔ `ESP_mqtt` over the local MQTT
broker, with the mock Cloud (real Cloud not ready yet). ESP = **bare ESP32-S3,
no wiring** — observed via serial monitor. Camera/face part runs on this dev
machine (10.4.70.114).

## 1. Environment snapshot (verified running today)

| Service | Where | Port | Status |
|---------|-------|------|--------|
| Mock Cloud (`Testing/mock_cloud_server.py`) | 10.4.70.114 | 5005 | running |
| Mosquitto (Docker, **anonymous allowed**) | 10.4.70.114 | 1883 | running |
| Redis (Docker `door-redis`) | 10.4.70.114 | 6380 | running |
| `edge_app.py` (camera + display) | 10.4.70.114 | — | running |
| ESP32-S3 (bare board) | WiFi `LAB_TEED` | 1883 out | to flash |

**One config edit needed before flash** (I'll apply on your go):
```c
// configuration.h
#define MQTT_HOST "10.4.70.114"      // dev machine LAN IP (never localhost)
```
Credentials: broker is anonymous → `MQTT_USER`/`MQTT_PASSWORD` in `secrets.h`
are accepted as-is (no change needed).

## 2. Pre-flight checklist

- [ ] ESP32-**S3** board confirmed (pins 41/42 don't exist on classic ESP32)
- [ ] `MQTT_HOST = 10.4.70.114` set, sketch compiles in Arduino IDE (board
      `esp32:esp32:esp32s3`, 16 MB flash)
- [ ] Flash over USB, serial monitor @ 115200
- [ ] PC can reach broker: `mosquitto_pub -h 10.4.70.114 -t probe -m hi` (no error)

## 3. Test sequence (observe ESP serial + broker console)

### T1 — Boot & WiFi
**Action:** power on. **Expected serial:**
```
Allowlist: 0 tag(s) → Connecting to WiFi.... → WiFi connected! IP: ...
NTP time synced → MQTT connected
```
**Fail paths:** WiFi down → LED blink + `WiFi down, reconnecting...` every 10 s
(RFID-less, still loops fine); broker down → `MQTT connect failed (state N)`.

### T2 — Broker presence (retained + LWT)
**Action:** on PC: `mosquitto_sub -h 10.4.70.114 -t 'door/#' -v` (keep open).
**Expected:** `door/status online` appears when ESP connects. Power off the ESP →
`door/status offline` (will message) within ~1 keepalive.

### T3 — Unlock command → relay state machine (no relay attached)
**Action:** `mosquitto_pub -h 10.4.70.114 -t door/cmd/unlock -m '{}'`
**Expected serial:** `MQTT unlock` → `Door opened` → (3 s) `Door auto-locked`.
Also test `door/cmd/lock`.

### T4 — Card allowlist (TagManager on LittleFS)
```
mosquitto_pub -t door/sync/card_uid -m '{"type":"add","card_uid":"0000012345","name":"Sok","person_id":"STU-001"}'
→ serial: MQTT add 0000012345 -> stored

mosquitto_pub -t door/sync/card_uid -m '{"type":"full_sync","cards":[
  {"card_uid":"0000012345","name":"Sok","person_id":"STU-001"},
  {"card_uid":"0000099999","name":"Dara","person_id":"STU-002"}]}'
→ serial: MQTT full_sync: 2 tag(s)

mosquitto_pub -t door/sync/card_uid -m '{"type":"delete","card_uid":"0000099999"}'
→ serial: MQTT delete 0000099999 -> removed
```
**Persistence:** reboot ESP → `Allowlist: 1 tag(s)` (delete/clear tested; never
power-cut during save — write-then-rename protects the table anyway).

### T5 — Bad UIDs rejected by design
**Action:** `add` with `"card_uid":"A1B2C3D4"` (hex, not 10-digit).
**Expected serial:** `card add rejected: not a 10-digit UID (A1B2C3D4)` —
allowlist unchanged. (Real registration EMIDs are 10-digit, so they pass.)

### T6 — Alert commands
```
mosquitto_pub -t door/alert/warning -m '{"type":"warning"}'      → MQTT alert: warning
mosquitto_pub -t door/alert/warning -m '{"type":"alert_cleared"}' → MQTT alert: cleared
```

### T7 — End-to-end: Cloud enroll → edge → face → ESP unlock
1. Enroll a student into mock: `venv/bin/python Testing/send_enroll.py --student-id STU-001 --name "Sok Dara" --card 0000012345`
2. Edge (running) gets SSE `member_updated` + `embedding_updated` → Redis
3. **Face match**: ⚠ needs a REAL embedding — mock's random embeddings won't match
   a camera face. Two options:
   - (a) enroll via the real registration app in "edge testing" mode pointed at
     `http://10.4.70.114:5005` (real photo → real embedding) — then stand in front
     of the camera
   - (b) skip real face: verify the unlock path by publishing the same JSON the
     edge would (`door/cmd/unlock`) — confirms ESP side; camera path already
     proven in earlier runs
4. Expected chain: camera match → `[MQTT] UNLOCK` (edge console) → `MQTT unlock`
   + `Door opened` (ESP serial) → `door/access/log` publish for card events.

### T8 — Failure drills
- [ ] Stop broker (docker stop door-mosquitto): ESP logs `MQTT connect failed`,
      loop unaffected; restart broker → `MQTT connected` (QoS1/cleanSession=false
      delivers commands sent while it was down)
- [ ] Kill mock cloud: edge retries sync, camera keeps working, ESP unaffected
- [ ] Kill edge: ESP's RFID/allowlist logic still live (nothing wired, but loop
      stays responsive — serial ticks status LED)

### T9 — RFID (deferred until wiring)
Card scan → grant/deny → `door/access/log` → edge display ticker. Requires
EM4100 reader on GPIO 10 (next session).

## 4. Success criteria for tomorrow

- T1–T6 fully green on serial + broker console (no crashes, no unexpected restarts)
- T7 path (a) or (b): unlock command delivered over MQTT and acted on
- T8: graceful degrade on every outage
- T9 explicitly parked (no wiring tomorrow)

## 5. Rollback / notes

- Wall-mounted door **must NOT be flashed** — this test uses a spare board.
- If the bare board misbehaves, reflash original production binary from
  `ESP32_1CH_RUPPV3/ESP32_1CH_RUPPV2/build/`.
- After testing: keep `MQTT_HOST` as LAN IP (mDNS not used for broker).

## 6. Open inputs (need before tomorrow)

1. Go-ahead to set `MQTT_HOST = 10.4.70.114` in `ESP_mqtt/configuration.h`
2. Confirm which S3 board is available (devkit with USB CDC → serial over USB is fine)
3. T7 choice: real registration-app enrollment (a) or direct-MQTT verification (b)
