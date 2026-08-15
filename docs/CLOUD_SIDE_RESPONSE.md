# Cloud Side Response — Embedding Sync Issue

**Date:** 2026-08-03
**From:** Cloud side
**To:** Door-Edge team
**Re:** `CLOUD_SIDE_FIXES_REQUEST.md` and `sync_agent_design.md`

---

## 1. The 2732 is not a dimension

The stored vectors are, and always have been, **512 × float32**. `2732` is the
length of the **base64 string** that carries them:

```
512 floats × 4 bytes = 2048 bytes
base64 encodes 3 bytes → 4 chars
2048 ÷ 3 = 682 groups (2046 bytes) + 2 bytes over
682 × 4 = 2728, the trailing 2 bytes add 4 more (3 chars + one "=" pad)
        = 2732 characters
```

Verified against the live server on 2026-08-03 — both records returned
`b64chars=2732 → bytes=2048 → float32 dim=512`.

The Edge was measuring the encoded string instead of decoding it, so
`np.array(...)` saw 2732 characters and reported `(2732,)`. Re-generating the
embeddings would have changed nothing.

A quick tell for next time: the string ends in exactly one `=`. That single pad
character means 2048 bytes. A 1024-d vector would be 5464 chars ending in `==`.

**The cloud never computes embeddings.** It stores whatever the registration app
sends. So "can the Cloud re-generate them with `w600k_mbf`" is a no — if the
registration app runs a different model, re-enrolment is the only fix. See §4.

---

## 2. Shipped on the cloud side

### `GET /api/embeddings/all` — now sends plain floats

`embedding` is a JSON array of 512 numbers by default. No decoding needed.

```json
{
  "success": true,
  "count": 2,
  "dim": 512,
  "dtype": "float32",
  "encoding": "floats",
  "embeddings": [
    { "key": "T002:normal", "student_id": "T002",
      "name": "Mengsrun", "name_en": "Mengsrun",
      "embedding": [0.0123, -0.0456, "...512 total..."] }
  ]
}
```

- `?format=base64` still returns the old encoding. Anything else → 400.
- `name_en` added alongside `name` — same value, both keys.
- The response now names its own `encoding`, `dtype` and `dim`, so a client
  never has to guess what the field holds. Read `encoding` rather than
  assuming; that makes the Edge immune to this class of bug permanently.

### `POST /api/edge/events` — accepts `name_en`

The person's name may be sent as `name_en` or `name`; `name_en` wins when both
are present. Every other field name is unchanged — see §3.1.

### `GET /api/edge/sync-status` — added the totals

Now returns `total_embeddings` and `total_students`. `count` is kept as an alias
of `total_embeddings`.

Not added: `status` and `last_updated`. A 200 already means connected, and the
revisions already carry the change marker.

### `POST /api/edge/alert` — new, was 404

```json
{ "method": "face" | "card" | "both",
  "fail_count": 5,
  "room": "207",
  "card_uid": "0000012345",
  "timestamp": "2026-08-03T10:15:00Z" }
```

- `"rfid"` is accepted and read as `"card"`. `door_id` is accepted as `room`.
- Logged as `unknown_face` / `unknown_card`, so it appears in the same activity
  log and Telegram alerts as everything else the door refuses.
- **`face_image` is accepted and discarded.** `access_logs` has no image column;
  storing captures is a schema change, not something to slip into this endpoint.
  Say the word if you need it and we'll do it properly.
- Returns **201**, not 200: `{success, status, alert_id, alert_sent, message}`.
  Check `response.ok`, not `== 200`.

**`alert_type` is dropped.** The event type is derived from `method` instead:
`face` → `unknown_face`, `card` → `unknown_card`, `both` → `unknown_face`.
`AccessLog.event` only holds the four values the dashboard understands, and
`/api/v1/alerts/latest` maps exactly those to severities — a fifth value would
need dashboard work before it rendered anywhere.

### How the two endpoints divide up

Both fire Telegram: `/api/edge/events` already alerts on any non-granted event,
so `/api/edge/alert` is not what makes notification work. To avoid one incident
producing several messages, the agreed split is:

- **every failed attempt** → `POST /api/edge/events` with `unknown_face` or
  `unknown_card`. This is the per-attempt audit record.
- **the threshold crossing only** → `POST /api/edge/alert` with `fail_count`.
  Once per incident, not once per attempt.

So five failed attempts is five `/events` calls and one `/alert` call. Do not
send `/alert` on every attempt, and do not skip `/events` — the log would then
be missing the individual attempts.

### `GET /api/edge/sync-stream` — named heartbeat

The 15-second keep-alive is now `event: heartbeat` with a `timestamp`, instead
of a bare `: ping` comment.

---

## 3. What the Edge still needs to change

### 3.1 `POST /api/edge/events` — currently fails on every call

The backend reads `event`. The design doc sends `event_type`, so the request is
rejected with 400 before anything else is read.

| Design doc sends | Backend expects |
|---|---|
| `event_type` | **`event`** — required, 400 without it |
| `door_id` | `room` (or `room_name`) |
| `method: "rfid"` | `method: "face"` or `"card"` |
| `name_en` | `name_en` — **now accepted**, `name` still works |
| `reason: "unknown_face"` | use the event name itself, or `detail` |
| expects 200 `{status, event_id}` | returns **201** `{success, log_id, alert_sent}` |

The complete set of keys read is: `event`, `student_id`, `name_en` (or `name`),
`room`, `similarity`, `method`, `detail`, `timestamp`. Everything else is
silently dropped — there is no schema validation, so unknown keys are not an
error.

`event` must be one of `access_granted`, `access_denied`, `unknown_face`,
`unknown_card`. The last two are missing from the design doc and are the ones
that raise critical alerts.

**Always send `method` explicitly.** When it is absent the server infers it:
event name, then a similarity score, then a granted entry with no score is taken
as a card scan. A face grant sent without a score is mislabelled "card", and a
denial with neither lands unlabelled and matches no dashboard filter.

### 3.2 Decode is no longer needed, but handle both

```python
def to_vector(value, encoding, dtype="float32", dim=512):
    if encoding == "base64":
        v = np.frombuffer(base64.b64decode(value), dtype=dtype).copy()
    else:
        v = np.asarray(value, dtype=dtype)
    if v.shape != (dim,):
        raise ValueError(f"unexpected dim {v.shape}")
    return v
```

Check the shape **after** decoding, never on the raw field. Use
`encoding = payload.get("encoding", "base64")` so an older server still works.

Vectors arrive **already L2-normalized** — cosine is a plain dot product, but
normalize the probe vector from the camera.

### 3.3 `GET /api/students` — four mismatches

- **Paginated**: 200 per page, with `total` / `page` / `pages`. The design doc
  assumes one flat list. Follow `pages` or pass `per_page`.
- **`all_rooms` is missing from the design and this is a correctness bug.**
  `rooms` is `[]` when `all_rooms` is true — the field is moot in that case. An
  Edge reading `rooms: []` as "no access" would deny exactly the people with
  access everywhere. Check `all_rooms` first.
- `rooms` holds room **names**, not ids like `room-101`.
- No `year` field; it is `generation`. `picture` is a relative path
  (`/api/students/<id>/photo`) needing the API key, not a public URL.

### 3.4 Embeddings are per-variant, not per-person

`key` looks like `T002:normal`. One student can have several variants, so
`door_person:{student_id}` in Redis would silently keep only the last one. Key
the index on `key`, or handle multiple vectors per person.

### 3.5 SSE — the event types in the design do not exist

There is no `member_updated`, `member_deleted`, `embedding_updated` or
`embedding_deleted`. The stream emits:

- `sync` — `{revision, roster_revision}`, both sent on connect and on any change
- `heartbeat` — every 15s of quiet

The contract is revision-based: on any `sync`, compare both values against your
cached ones and re-fetch whichever moved. `revision` → `/api/embeddings/all`,
`roster_revision` → `/api/students`.

Revisions are **opaque strings** (`"2:2026-08-01T06:37:51.898183"`), not
incrementing integers. `!=` works; `>` does not.

`Last-Event-ID` is inert — the stream sends no `id:` lines. Reconnect and read
the revisions that arrive on connect.

### 3.6 MQTT — please drop the card path

`door/sync/card_uid` and `door/alert/warning` collide with the backend, which
already owns the ESP32: commands on `tee/door/<room>/cmd`, and the card list
served from `GET /api/edge/allowlist?room=<id>` which each door mirrors itself.
Two writers on one chip will desync it.

Proposed split: **backend owns cards and door commands, Edge owns faces.** Happy
to discuss if the Edge needs something the allowlist does not provide.

### 3.7 `card_uid: null` is expected

No cards have been enrolled yet — it is data, not a bug. Enrolment happens in
the dashboard. Card UIDs are stored as captured by the door firmware; the
allowlist drops any UID longer than 11 characters, so 10-digit decimal EM4100
fits.

---

## 4. Verify the face model before anything else

Matching 512-d does **not** mean the vectors are comparable. If the registration
app runs a different InsightFace pack than the Edge's `buffalo_sc / w600k_mbf`,
both sides produce 512-d vectors that are mutually meaningless — decoding will
succeed, the index will load, and every similarity will sit near zero at any
threshold.

Check the registration app's model before tuning `SIMILARITY_THRESHOLD`. If it
differs, everyone must be re-enrolled; there is no conversion.

---

## 5. Suggested order of work

1. Rebuild the backend (cloud side, required for §2 to take effect).
2. Edge: fix the `event` / `room` / `method` field names — event reporting is
   entirely broken until then.
3. Edge: consume plain floats from `/api/embeddings/all`; confirm 2 vectors land
   in Redis.
4. Confirm the registration app and the Edge run the same face model.
5. Edge: handle `all_rooms` and pagination on `/api/students`.
6. Edge: rewrite the SSE handler around the single `sync` event.
7. Decide the MQTT card ownership question together.

Base URL on the campus LAN is `http://10.4.70.189` — port 80, through Caddy,
which serves both the dashboard and `/api/*`. Port 5000 reaches the backend
directly and bypasses the proxy.
