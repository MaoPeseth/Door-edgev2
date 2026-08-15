# Cloud Side — Spoof Alert Channel (Telegram shows "Unknown face" instead of "Spoof")

**Date:** 2026-08-08
**From:** Door-Edge (edge) side
**To:** Cloud API team
**Status:** Telegram alert for anti-spoof fires with the wrong reason

---

## 1. Problem

When the YOLO anti-spoof detector blocks access, the Cloud's Telegram alert message
says **"Unknown face"** instead of a spoof/photo-attack reason. The door's own
screen and MQTT show `spoof_detected` correctly — only the Cloud alert loses the
reason.

## 2. Why (measured on the edge)

The edge fires the spoof incident as two Cloud calls:

1. `POST /api/edge/events` with `event: "spoof_detected"`, `method: "face"`
   (`core/camera_worker.py:279` → `core/cloud_client.py:271 report_access_denied`).
2. `POST /api/edge/alert` with `method: "face"`, `fail_count: 1`
   (`core/alert_tracker.py:175-196` → `core/cloud_client.py:309-325`;
   `_MAP_METHOD` rewrites `"spoof"` → `"face"` at `core/cloud_client.py:334`).

The rewrite exists because the backend rejects any other `method` value — the
backend then derives the alert type from `method`, so both calls land as
**face → unknown_face** and the Telegram caption says "Unknown face".

The `event: "spoof_detected"` value is **outside the backend's whitelist**
(`access_granted` / `access_denied` / `unknown_face` / `unknown_card`,
per CLOUD_SIDE_RESPONSE.md §2 and §3.1), so the edge cannot tell the backend
"this is a spoof" through any field the backend renders.

## 3. Requested changes (Cloud side)

Mirror the `rfid` → `card` treatment you already done:

1. **`POST /api/edge/alert`** — accept `method: "spoof"` (like `"rfid"` is read
   as `"card"`). Derived type: `spoof_detected`, severity critical, Telegram
   caption **"Spoof attempt detected"**. `face_image`/`fail_count`/`room` behave
   as today.

2. **`POST /api/edge/events`** — accept `event: "spoof_detected"` (a fifth
   value). It must raise the same critical alert as `unknown_face`, with the
   caption **"Spoof attempt detected"**. Keep storing the `face_image` photo on
   the alert, as for unknown faces / unknown cards.

Edge payloads are already final — no edge change needed for the caption once
the backend accepts these. Example alert the edge will send:

```json
POST /api/edge/alert
{
  "method": "spoof",
  "fail_count": 1,
  "room": "001",
  "timestamp": "2026-08-08T15:00:00+07:00",
  "face_image": "<jpeg base64>"
}
```

After the backend change lands, the edge will flip `core/cloud_client.py:334`
to stop rewriting `"spoof"` → `"face"` (one-line change, guarded by a config
flag so it can be rolled back without the backend).

## 4. Verification (after the backend change)

1. Hold a photo/screen in front of the camera inside the ROI.
2. Door shows `SPOOF - NO ACCESS`, MQTT `door/access/denied` = `spoof_detected`.
3. Dashboard logs `spoof_detected` for room `001`.
4. Telegram receives one "Spoof attempt" alert with the captured photo,
   no "Unknown face" text.