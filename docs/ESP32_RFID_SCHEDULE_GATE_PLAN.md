# ESP32 / RFID Side — Schedule Gate Plan (Draft for review)

**Date:** 2026-09-01
**Status:** PLAN — no firmware changed yet.
**Target firmware:** `~/Desktop/my_project/ESP_mqttV3/` (`ESP_mqtt.ino` + `EM4100.h`,
`TagManager.h`, `configuration.h`) — confirmed.

## Decisions locked (your review)

- **D1 — Clock/schedule unknown:** **deny** the card until time + schedule are known.
- **D2 — Per-member exceptions:** **skip in v1** — faces still honor exceptions;
  cards enforce weekly/holiday/lockdown/window only.
- **Board:** EM4100 10-digit-UID door controller (`ESP_mqttV3`).

## Goal

Cards are currently decided locally in `processRFID()` (`ESP_mqtt.ino:444`) as
`tag.found && tag.active && !tag.expired` and open the door 24/7 — the schedule
gates only the **face** path on the Edge PC. This makes RFID follow the same
schedule, while keeping the door **offline-first** (card works with no broker,
but only inside the last-known schedule window).

---

## 1. What "by our schedule" means for a card (ESP check chain)

Inserted in `processRFID()` between allowlist lookup and `openDoor()`:

| # | Rule | Source | Deny reason (if not met) |
|---|---|---|---|
| 1 | Clock known (NTP once, or cached `bootTime`) | existing `TagManager::currentTime()` | `schedule_blocked_UNKNOWN_TIME` |
| 2 | Not hard lockdown | gate `lockdown` | `schedule_blocked_LOCKDOWN` |
| 3 | Inside Edge run window | `edge_run_start/end` (null → always) | `schedule_blocked_COOLDOWN` |
| 4 | Today is not a holiday | gate `holidays[]` | `schedule_blocked_HOLIDAY` |
| 5 | Inside today's weekly window | gate `week[7]` | `schedule_blocked_<state>` (WEEKEND/RESTRICTED) |
| 6 | Allowlisted + active + unexpired | TagManager (unchanged) | existing `unknown_card` / `expired` / `deactivated` |

Deny path keeps the current UX: `startAlarmBuzz()`, red LED via door stays
locked, and `publishAccessLog(false, ..., reason)` so the Edge/Cloud sees the
same reason codes the face path already uses. **Bypass & exit switches are
physical and untouched** (they still open the door; schedule never overrides a
hardware bypass).

## 2. Clock — already solved, no new time channel needed

`ESP_mqtt.ino:525` already does what section 2 of the earlier draft wanted:
`configTime` + `setenv("TZ","ICT-7")`, and `TagManager::currentTime()`
(`TagManager.h:192`) falls back to **persisted `bootTime` + uptime** for
offline operation. Since `bootTime` is stored at every successful NTP sync,
the ESP knows wall-clock time even with no network — **offline scheduling
works with zero new code**. SNTP keeps correcting in the background.

Only "brand-new device, never synced NTP" hits UNKNOWN_TIME (D1 → deny).

## 3. Schedule distribution: Edge → ESP (new MQTT topic)

**`door/sync/schedule`** (Edge → ESP), compact JSON, published **retained**
(QoS 1). The ESP's `cleanSession=false` + retained subscription means a
new/reconnecting door receives the current gate automatically on subscribe —
no special publish-on-connect logic needed on the Edge.

```json
{
  "revision": "r1:2026-09-01 12:27:17.022612",
  "edge": [480, 1380],             // run window in minutes-of-day, [null,null]=24/7
  "lockdown": false,
  "week": [null, [480,1380], ...], // 7 entries, weekday index 0..6, null=closed
  "holidays": [20260924, 20260930] // YYYYMMDD ints
}
```

- Exceptions **omitted** (D2).
- Payload is tiny — fits one packet, no MQTT buffer growth (buffer = 4096 B).
- **ESP side:** parse once, store as compact structs + `revision` in **NVS**
  (`Preferences`, namespace `sched`), ignore identical `revision` re-push.
  Survives reboot → gate still enforced while Cloud is down.

## 4. New module: `ScheduleGate.h` (mirror of `schedule_policy.py`)

```cpp
class ScheduleGate {
public:
  void load();                                  // NVS → RAM, called once in setup()
  bool hasSchedule() const;                     // any bundle stored yet?
  bool clockKnown() const;                      // currentTime() != 0
  bool apply(const String& json);               // parse+persist, skips same revision
  bool isGranted(time_t now) const;
  const char* denyReason(time_t now) const;     // "" if granted
private:
  // NVS keys: revision, lockdown, edgeS/edgeE, w0S..w6S/w0E..w6E, holiday blob
};
```

Evaluator (in order):
1. `now == 0` → deny `schedule_blocked_UNKNOWN_TIME`
2. `lockdown` → `schedule_blocked_LOCKDOWN`
3. edge window set && outside → `schedule_blocked_COOLDOWN`
4. today (`now`) in `holidays` → `schedule_blocked_HOLIDAY`
5. today's `week[weekday]` covers `HHMM(now)` → **grant**
6. else → `schedule_blocked_WEEKEND` (Sat/Sun) / `schedule_blocked_RESTRICTED`

## 5. File change map

### ESP (`ESP_mqttV3/`)
| File | Change |
|---|---|
| `configuration.h` | `#define MQTT_TOPIC_SCHEDULE "door/sync/schedule"`; maybe bump `FIRMWARE_VERSION` → `3.4.0-mqtt` |
| `ScheduleGate.h` | **NEW** — compact evaluator + NVS persistence (above) |
| `ESP_mqtt.ino` | `#include "ScheduleGate.h"`; global `ScheduleGate scheduleGate;`; `setup()` → `scheduleGate.load()`; `tickMqtt()` → `mqtt.subscribe(MQTT_TOPIC_SCHEDULE, 1)`; `handleMqttCommand()` → new branch calling `scheduleGate.apply(body)`; `processRFID()` → after allowlist `granted` check, add `scheduleGate.isGranted(now)` before `openDoor()`, else deny with reason |
| `TagManager.h` | no change (`currentTime()` reused) |

### Edge (Python)
| File | Change |
|---|---|
| `core/mqtt_publisher.py` | +`publish_schedule_gate(gate)`; serialize from `SchedulePolicy`: `revision`, `edge`, `week[7]`, `holidays`, `lockdown`, minute-of-day ints; publish **retained** QoS 1 |
| `core/sync_agent.py` | after each `sync_now()` with a `schedule_revision` change → `publish_schedule_gate(...)` |

## 6. Degradation / edge cases

| Situation | Behaviour |
|---|---|
| Broker/Edge down | Gate from NVS keeps enforcing; clock from NTP/`bootTime` — fully offline |
| No schedule ever received (brand-new door) | `!hasSchedule()` → deny (D1); faces unaffected |
| Clock unknown (never NTP'd, no `bootTime`) | deny `UNKNOWN_TIME` (D1) |
| Corrupt / same-revision schedule push | ignore non-JSON; skip identical `revision` |
| Weekly/holiday/run-window change mid-day | next tap re-evaluates; next push updates immediately |
| Face path | unchanged — Edge is still the authoritative schedule check |
| Lockdown | ESP short-circuits at step 2 — both paths denied |
| Bypass switch / exit switch | physical, unaffected (schedule can't override) |

## 7. Parity test (before flashing)

Host script replays **one JSON gate object** through:
- Python `SchedulePolicy` (real, repurposed from `schedule_policy.py`), and
- a Python port of the C++ `ScheduleGate` logic

across a grid of datetimes (weekdays, Sat/Sun, holidays, lockdown on/off, edge
window edges, minutes around midnight). Assert identical grant/deny/reason on
both sides — catches C++/Python drift before it reaches the door.

## 8. Delivery order

1. **P0** — `ScheduleGate.h` + NVS storage; load in `setup()`; `processRFID()`
   gate with deny reasons; subscribe + `apply()` in `handleMqttCommand`.
2. **P0** — Edge: `publish_schedule_gate()` (retained, QoS 1) wired into
   `sync_agent` on revision change; verify retained delivery to reconnect.
3. **P1** — parity test script (§7) against the live bundle.
4. **P2** — optional: exceptions enforcement (D2 later), off-hours LED/buzzer
   cue, kiosk display line "cards schedule-gated".

No open decisions remain for this firmware — proceed to P0 when you're ready,
with the ESP changes under `ESP_mqttV3/` and the Edge changes in this repo.