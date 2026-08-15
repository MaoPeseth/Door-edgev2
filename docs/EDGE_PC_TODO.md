# Edge PC — work list

Base URL `http://10.4.70.189` (port 80, through Caddy).
Every call needs `X-API-Key: <EDGE_API_KEY>`.

Each room's edge PC runs its own MQTT broker. The backend has no MQTT client and
cannot reach a door — anything touching an ESP32 goes through its edge PC.

---

**P0 — heartbeat, or every room reads offline**
- [x] `POST /api/edge/doors` per door at startup: `{"room":"001","device":"esp32-001","online":true}`
- [x] `POST /api/edge/doors/status` every 60s: `{"doors":[{"room":"001","online":true}]}`
- [x] Presence TTL is 180s. Three missed beats = room marked offline.

**P0 — room names**
- [x] Take them from `GET /api/edge/rooms` → `[{"name","online"}]`. `ROOM_ID` must match exactly.
- [x] Today the Rooms page says `001`, the edge reports `207`. A wrong name makes
      `/api/edge/allowlist` fail closed to `all_rooms` members only.

**P0 — event payload** (currently 400s on every call)
- [x] `event` not `event_type` — one of `access_granted`, `access_denied`, `unknown_face`, `unknown_card`
- [x] `room` not `door_id` · `name_en` not `name` (both work) · `detail` not `reason`
- [x] `method` is `"face"` or `"card"` only — send it explicitly, never `"rfid"`
- [x] Response is **201**, `{success, log_id, alert_sent}`

**P0 — timestamps**
- [x] Send an offset: `2026-08-04T09:15:00+07:00`, or UTC with `Z`.
      No offset is read as local time (`EDGE_UTC_OFFSET`, default 7).

**P1 — embeddings** (`GET /api/embeddings/all`)
- [x] `embedding` is now a plain array of 512 floats — no base64 decode
- [x] Read the `encoding` field rather than assuming; `dtype` and `dim` come too
- [x] Check shape **after** decoding. 512 float32 base64-encodes to 2732 chars —
      that was the phantom "2732-dim"
- [x] Vectors are already L2-normalized; normalize the probe vector
- [x] `key` is `T002:normal` — several variants per person. Do not key Redis on `student_id`
- [ ] The face model must match the registration app's (`buffalo_sc` / `w600k_mbf`, 512-d),
      or every similarity sits near zero however clean the sync looks

**P1 — roster** (`GET /api/students`)
- [x] Paginated, 200/page — follow `pages`
- [x] `all_rooms: true` means access everywhere and `rooms` comes back `[]`.
      Reading that as "no access" denies the wrong people
- [ ] `generation`, not `year`

**P1 — SSE** (`GET /api/edge/sync-stream`)
- [x] Only two events exist: `sync` (`{revision, roster_revision}`) and `heartbeat`.
      There is no `member_updated` / `embedding_updated`
- [x] On `sync`, re-fetch whichever revision moved: `revision` → embeddings,
      `roster_revision` → students **and** allowlist
- [x] Revisions are opaque strings — `!=`, never `>`. `Last-Event-ID` is inert

**P1 — MQTT (yours now)**
- [x] On `roster_revision` change, pull `GET /api/edge/allowlist?room=<name>` and
      push to that room's ESP32s. Replaces the old `{"action":"sync"}`
- [x] Response is plain text, one line per card; `?since=<revision>` answers 304
- [x] Topics under `door/...` are yours to define

**P2 — enrolment relay**
- [x] SSE `enroll` → `{"room","timeoutS"}`: arm that door. `enroll_cancel`: disarm
- [x] `POST /api/edge/doors/<room>/enroll/capture` with `{"tagID":"0000012345"}`

**P2 — alerts**
- [x] Every attempt → `/api/edge/events`. Threshold crossing only → `/api/edge/alert`
      (`{"method","fail_count","room","card_uid","timestamp"}`)
- [x] Both fire Telegram, so never call `/alert` per attempt

**P1 — photo on an unrecognized attempt**
- [x] Add `face_image` to the `/api/edge/events` payload on `unknown_face` (and
      `unknown_card` if a frame is available): the **full camera frame**, JPEG,
      base64 — bare or as `data:image/jpeg;base64,...`, both accepted
- [x] It becomes the photo the Telegram alert is captioned on, so the picture and
      the message arrive as one notification
- [x] The same field works on `/api/edge/alert`
- [x] Nothing is stored server-side. A malformed or oversize image (>10 MB) is
      dropped and the alert still sends as text, so a bad frame never costs the alert
- [x] **Cap the frame on the edge.** The backend sets no request size limit, so an
      oversized frame is uploaded in full and only rejected at the 10 MB check —
      wasted bandwidth and a slower event POST. Send a JPEG at ~640×480, quality
      ~75, which is roughly 50 KB (~67 KB base64). Resize and re-encode rather
      than forwarding the raw camera frame
- [x] The POST is on the door's critical path — do the encode off the thread that
      opens the lock, or send the alert asynchronously

**P2 — hand verification is a real gesture (not just proximity)**
- [x] `HAND_VERIFICATION_MARGIN` 80 → 50 px (config.py)
- [x] Positional rule `_hand_raised_near_face()`: hand center must be at/above the
      chin line, not just overlapping the expanded face bbox
- [x] Wave gesture not used at this door — `HAND_WAVE_THRESHOLD`/`HAND_WAVE_REQUIRED`
      config and `_track_hand_wave()` removed
- [ ] On-camera calibration pass (gesture cadence vs. YOLO hand detection flicker)
      before rolling out to other rooms

---

## Verify in order

1. `/api/edge/sync-status` → 200
2. Heartbeat → `/api/edge/rooms` shows the room online
3. A face registered in the app → edge loads it with no dimension warning
4. Real face at camera → 201, entry on the dashboard with the right room and local time
5. Assign a card → `roster_revision` moves → edge re-pulls → the card opens the door
6. Unknown face → Alerts page entry and a Telegram message

Wrong room at step 4 is the room-name item; wrong time is the timestamp item.
