# ESP32_1CH_RUPPV2 → MQTT-Only Door: Adaptation Plan

**Status:** Draft — awaiting approval. Nothing has been changed in the firmware.
**Date:** Aug 2, 2026
**Design basis:** `Door-Edge_new_machine/docs/sync_agent_design.md` § MQTT Topics (Edge → ESP32) — the ESP is **MQTT-only**; the existing HTTP REST route is **cut**.

## 1. Design decision (locked)

We **reuse only the hardware/pin configuration** of the existing production door
(`ESP32_1CH_RUPPV2`, v3.0.0) and **replace its HTTP interface with the edge's MQTT
contract**. The door remains an **offline-first** local controller (RFID still
works if the broker/edge is down) exactly like today — only the control channel changes.

## 2. Reused pinout — unchanged (`configuration.h:79-87`)

| GPIO | Function | Direction | Firmware users |
|------|----------|-----------|----------------|
| 6 | Relay (door lock) | OUTPUT | `updateRelayOutput()`, `lockDoor()`, `openDoor()` |
| 7 | WiFi LED | OUTPUT | `setup()` |
| 8 | Status LED | OUTPUT | `tickStatusLed()` |
| 9 | Buzzer | OUTPUT | `tickBuzzer()`, `startShortBuzz()` |
| 10 | RFID RX (HardwareSerial1) | INPUT | `EM4100.h` (`rfid.readCardID()` → 10-digit) |
| 41 | Bypass switch | INPUT_PULLUP | `applyBypassState()` in `loop()` |
| 42 | Exit switch | INPUT_PULLUP | `loop()` edge → `openDoor()` |

**No pin changes, no extra driver library required** — pins are plain `#define`s;
drivers `EM4100.h` (10 kHz RFID) and `TagManager.h` (LittleFS allowlist, max 500 tags,
write-then-rename) are kept verbatim.

## 3. How to read the existing firmware (file map)

| File | Responsibility |
|------|----------------|
| `ESP32_1CH_RUPPV2.ino` (872 lines) | Main: it loops `handleWiFi → bypass/exit → server.handleClient → tickBuzzer → processRFID → tickDoorTimer → tickEvents → tickMqtt → tickAllowlistSync → tickStatusLed` (`loop()` at `.ino:845`) |
| `configuration.h` | Pins, `ROOM_ID`, topic defines, server/backend constants, `DOOR_API_KEY` |
| `secrets.h` | WiFi + MQTT login + API keys (gitignored) |
| `EM4100.h` | UART EMID reader → 10-digit decimal UID (`processRFID()`) |
| `TagManager.h` | Allowlist on LittleFS — `addMapping(tag, name, id[, active, expiry])`, `deleteMapping`, `clearAll`, `beginBulkLoad/bulkAdd/commitBulkLoad/abortBulkLoad` (used by the HTTP allowlist poll today) |

Key in-firmware functions we keep and re-trigger from MQTT:
`openDoor()` (`.ino:184`), `lockDoor()` (`.ino:134`), `startShortBuzz()/startAlarmBuzz()`
(`.ino:147-149`), `queueEvent()` (`.ino:220`) → publish instead of HTTP POST,
`processRFID()` (`.ino:460`) grant/deny decision, `TagManager` ops.

## 4. MQTT topic contract (new listen)

Replace the topic defines in `configuration.h:48-52` to the edge contract:

| Edge → ESP (subscribe) | Design payload | ESP action |
|------------------------|----------------|------------|
| `door/cmd/unlock` | `{door_id, person_id, name, similarity, method, timestamp}` | `openDoor()` (relay GP6 + short buzz) |
| `door/cmd/lock` (kept) | `{...}` | `lockDoor()` |
| `door/sync/card_uid` | `{type: "full_sync"\|"add"\|"delete", cards:[...]}` / single | full_sync: `clearAll()` + `addMapping()` per card; add/delete: one TagManager call |
| `door/alert/warning` | `{type:"warning"\|"alert_cleared", door_id, reason, ...}` | warning → `startAlarmBuzz()`; cleared → LED/buzz off |

ESP → edge:

| Topic | Payload |
|-------|---------|
| `door/access/log` (publish) | `{granted, method, name, person_id, card_uid, timestamp}` from `processRFID()` decisions (replaces `queueEvent` + HTTP POST `/api/edge/events`) |
| `door/status` (retained + LWT) | `"online"` / `"offline"` (replaces `tee/door/<room>/status`) |

**Payload note:** `door/sync/card_uid` `full_sync` embeds an array — `jsonField()`
(`.ino:398-406`) is a flat string extractor and cannot recurse. The handler must add
a small `"cards"` array walker, and `MQTT_BUFFER_BYTES` (`.ino:827`) may need to
grow (or the full list is chunk-published by the edge).

## 5. Cut the HTTP route

Removed from the firmware:

| Item | Location | note |
|------|----------|------|
| `WebServer server(80)` + `server.begin()` | `.ino:73` `.ino:832` | whole HTTP layer goes |
| `setupRoutes()` with all `server.on(...)` | `.ino:523-691` | `/doorStatus`, `/doorOpen`, `/doorLock`, `/lastScanTag`, `/tags`, `/addTag`, `/deleteTag`, `/clearTags`, `/enroll/start`, `/enroll/cancel`, `/enroll/status`, `/restart`, `/update` |
| `authorized()` / `hasValidKey()` + `collectHeaders` | `.ino:496-508,524` | no more `X-API-Key` / `?key=` — broker session is the trust boundary |
| `tickAllowlistSync()` and `HTTPClient` server pull | `.ino:327-391` | `GET /api/edge/allowlist` backstop replaced by MQTT `full_sync` push |
| `tickEvents()` → `HTTPClient` POST `/api/edge/events` (`postEvent` queue) | `.ino:236-313, 303` | replaced by MQTT `door/access/log` publish |
| `POST /enroll/*`, `enrollActive()/tickEnroll()/captureEnrollScan()` | `.ino:284-317, 601-633` | enrollment goes through the edge/Cloud now; cut these HTTP endpoints (`/enroll/start`, `/enroll/cancel`, `/enroll/status`) — enroll locally **cut by default**, can stay for the registration edge later |
| UDP discovery (`handleDiscovery()`, `TEE_DISCOVER`) | `.ino:715-...` (call at `.ino:863`) | only the registration app needs it; **cut** |
| `Update.h` HTTP `/update` handler | `.ino:648-...` | keep `ArduinoOTA` (IDE network upload) — it is independent of the WebServer; cut only the multipart handler |

**Importantly removed**: `WebServer.h` and `HTTPClient.h` includes (`.ino:62-63`).

**Kept:** `WiFi.h`, `ESPmDNS.h` (optional), `PubSubClient.h`, `HardwareSerial.h` (RFID
UART1), `time.h` (NTP + tag expiry), `LittleFS/Preferences.h` (TagManager), `ArduinoOTA.h`.

## 6. MQTT handler changes

Extend `handleMqttCommand()` (`capturedAt .ino:408-428`, currently only `action`:
`addTag`/`deleteTag`/`sync`) to route by **topic** first, then parse the design payloads:

```
topic == door/cmd/unlock        → openDoor()            (skip if bypassActive)
topic == door/cmd/lock          → lockDoor()
topic == door/alert/warning     → type=warning ? startAlarmBuzz() : (alert cleared)
topic == door/sync/card_uid/bk:
    type=add            → tagManager.addMapping(card_uid, name, person_id)   // verify 10 digits
    type=delete         → tagManager.deleteMapping(card_uid)
    type=full_sync      → tagManager.clearAll(); for each cards[] → addMapping()
```

`processRFID()` stays the decision authority (read UID, find allowlist, grant/deny,
`open/openDoor`, buzz); its `queueEvent(...)` site now **publishes** to
`door/access/log` instead of the HTTP queue. Broker down → log dropped silently, door
keeps working offline (same reliability stance as today's backend queue).

## 7. Edge-side consistency requirements

- **Card format:** the edge's `card_uid` in the design (sample `A1B2C3D4`) is hex-style;
  this door reads **10-digit decimal EM4100** (`processRFID()` rejects non-10-length
  at `.ino:462`). The edge MUST send 10-digit decimal in `full_sync/add/delete`, or the
  RFID path never grants. (Registration already captures EM4100 UIDs — those match.)
- Timestamps: keep NTP/ICT-7 so `door/access/log` carries a real time.
- Broker creds: `SECRET_MQTT_USER`/`PASSWORD` reused; edge and door share the same broker.

## 8. Verification plan (after approval)

1. `mosquitto_pub`/paho script: `door/cmd/unlock` → relay opens 3 s + short buzz.
2. `full_sync` a known 10-digit tag → scan physical card → `openDoor()` + `door/access/log` shows granted.
3. `delete` a tag → card denied + alarm buzz.
4. `door/alert/warning` warning → alarm pattern; `alert_cleared` → back to normal.
5. Broker down → RFID still works offline (LittleFS); reconnect restores retained status.
6. Build without `WebServer`/`HTTPClient` — `flash size`, debug pass.

## 9. Open inputs / decisions needed

1. Confirm topic names `door/cmd/unlock`, `door/sync/card_uid`, `door/alert/warning`,
   `door/access/log`, `door/status` override the TEE `tee/door/<room>/...` family.
2. Keep or cut enrollment + UDP discovery (`cut by default` above).
3. Confirm the edge-side card_uid will be EM4100 **10-digit decimal** by the time of the
   allowlist push.

---
**Files to be touched on approval:** `configuration.h` (topics), `ESP32_1CH_RUPPV2.ino`
(cut HTTP, extend MQTT handler, publish access log). The `Door-Edge` Python side
(`mqtt_publisher.py`, `sync API`) is unchanged — it already publishes this contract.