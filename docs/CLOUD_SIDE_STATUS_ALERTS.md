# Cloud Side — Device Status Alerts (ESP32 offline/online → Telegram)

**Date:** 2026-09-13
**From:** Door-Edge (Edge) side
**To:** Cloud API team
**Status:** Edge sends the alert, Cloud rejects it → no Telegram for a device going offline

---

## 1. Problem

When an ESP32 door controller goes offline, the Edge now raises a status alert,
but `POST /api/edge/alert` rejects it because the Cloud whitelists only:

```
['both', 'card', 'face', 'spoof']
```

Observation (live, 2026-09-13):

```
POST /api/edge/alert
{"method": "esp_offline", "fail_count": 1, "room": "001", "timestamp": "..."}

HTTP 400
{"message": "method must be one of ['both', 'card', 'face', 'spoof']", "success": false}
```

The Edge retries the alert through its offline buffer, but a permanent 400 means
it can never reach Telegram.

## 2. What the Edge already sends (no Edge change needed)

The Edge fires these only on device transition events, throttled (boot-grace 20 s,
re-alert every 30 min while still offline) — one alert channel, no spam:

```json
POST /api/edge/alert
{
  "method": "esp_offline",
  "fail_count": 1,
  "room": "001",
  "timestamp": "2026-09-13T17:50:44+07:00"
}
```

```json
POST /api/edge/alert
{
  "method": "esp_online",
  "fail_count": 0,
  "room": "001",
  "timestamp": "2026-09-13T18:20:10+07:00"
}
```

Field semantics:

| Field        | Meaning                                                       |
|--------------|---------------------------------------------------------------|
| `method`     | `esp_offline` (device went down) / `esp_online` (device back) |
| `fail_count` | `esp_offline` → escalation number (1, 2, 3, … each ~30 min down); `esp_online` → 0 |
| `room`       | door room id, same as every other alert (`001`)               |
| `timestamp`  | local time with UTC offset, same format as all alerts         |
| `face_image` | not sent for status alerts (no photo needed)                  |

## 3. Requested changes (Cloud side)

1. **Accept** two new `method` values on `POST /api/edge/alert` — `"esp_offline"`
   and `"esp_online"` — alongside `both` / `card` / `face` / `spoof`
   (do not drop the existing four).

   Suggested validation (`method` can otherwise stay free-form):

   ```python
   ALLOWED_ALERT_METHODS = {"both", "card", "face", "spoof",
                            "esp_offline", "esp_online"}
   ```

2. **Derive the Telegram caption** from `method` (same pattern the cloud already
   uses for the other methods), keeping `fail_count` / `room` / `timestamp`:

   - `esp_offline`  → "Door device OFFLINE"
   - `esp_online`   → "Door device back online"

3. **Season the two separately** from the access/spoof channels (they are
   device-status notices, not security incidents) — normal/banner priority, no
   camera photo required, stored in the same alert log as every other alert.

## 4. Verify after the change

From the Edge PC (`Door-Edge V2`):

```bash
KEY=$(venv/bin/python -c 'import config as c; print(c.CLOUD_API_KEY)')
curl -X POST "http://tee-doorlock-dashboard.local/api/edge/alert" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"method":"esp_offline","fail_count":2,"room":"001","timestamp":"2026-09-13T18:00:00+07:00"}'
```

Expected: success response (no more 400). Then Telegram receives the
device-offline alert. Any alert the Edge buffered during the outage is delivered
automatically by its offline queue on the next flush cycle (10 s), and the device
status continues to re-alert every 30 min until the ESP reconnects.