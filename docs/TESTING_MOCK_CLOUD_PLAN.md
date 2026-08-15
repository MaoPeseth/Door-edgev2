# Plan: Local Mock Cloud API for Testing (Testing/ folder)

**Date:** 2026-08-02
**Status:** IMPLEMENTED & VERIFIED — see section 5 and `docs/MAINTENANCE_LOG.md`
**Related:** `docs/DISPLAY_DESIGN.md` (screen display), `config.py` (Cloud API URL)

---

## 1. Goal

The Cloud backend endpoints are still pending, but we want to test the edge
system now (camera preview + screen display + face enrollment). The
registration software normally sends enrollments to the Cloud, and the
edge's `SyncAgent` pulls them back via Cloud API + SSE.

**This plan:** a temporary, local stand-in for the Cloud API so the
registration software can POST enrollments directly to the edge. The edge
code is **not modified** — it still talks to "a cloud", just a local one.
When the real Cloud is done: delete the `Testing/` folder and revert one
config line.

```
Registration software ──POST──► mock server (on edge PC, Testing/)
                                     │ SSE push (door/edge/sync-stream)
edge_app.py ◄──SyncAgent pulls───────┘
     │
     ▼
Redis face index → screen display "ENROLLED: N"
```

## 2. Deliverables

| File | Purpose |
|------|---------|
| `Testing/mock_cloud_server.py` | Mock Cloud API. Stdlib only (no new pip deps). Threading HTTP server on port 5005. Endpoints mirror `core/cloud_client.py` + `core/sse_client.py`. Persists to `Testing/data.json`. |
| `Testing/send_enroll.py` | CLI test client: enroll / delete a student (with optional real embedding JSON, else random 512-dim placeholder). |
| `Testing/README.md` | Usage, endpoint table, caveats, elimination steps. |
| `docs/TESTING_MOCK_CLOUD_PLAN.md` | This document. |
| `config.py` | **Temporary** change: `CLOUD_API_URL = "http://localhost:5005"`. Reverted when Cloud is ready. |

## 3. Mock server endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/edge/sync-status` | revisions + counts (edge consistency check) |
| GET | `/api/students` | member roster (`{"students": [...]}`) |
| GET | `/api/embeddings/all` | face vectors (`{"embeddings": [...]}`) |
| GET | `/api/edge/sync-stream` | SSE: heartbeat every 10 s; pushes `member_updated` + `embedding_updated` on new enrollment → edge picks it up live, no restart |
| POST | `/api/enroll` | add/update enrollment (also accepts `/api/students`, `/api/embeddings`, unknown paths — permissive, raw payload printed) |
| POST | `/api/delete` | remove by `student_id` |

Field-name tolerance: `student_id`/`id`/`studentId`, `name_en`/`name`/`full_name`,
`card_uid`/`card_id`/`card`, `embedding`/`face_embedding`/`vector`. Embedding
must be 512 dims (`config.VECTOR_DIM`) or the edge's SyncAgent skips it.

## 4. Implementation steps (after approval)

1. Write `Testing/mock_cloud_server.py`, `Testing/send_enroll.py`, `Testing/README.md` (files are drafted already).
2. **Temporary** `config.py`: `CLOUD_API_URL = "http://localhost:5005"` (and `CLOUD_API_KEY = "anything"`).
3. Verify (see checklist). Fix anything that fails.
4. User tests: start server → `edge_app.py` → send enrollments → check display "Enrolled faces" + camera preview.

## 5. Verification checklist

- [x] `venv/bin/python -m py_compile` on both Testing files
- [x] Start server; `curl` sync-status / students / embeddings/all
- [x] POST enroll via `send_enroll.py` → data appears in `data.json` + SSE pushes event
- [x] `edge_app.py` full run with `CLOUD_API_URL` pointed at mock: initial sync loads students, `[Sync] Loaded N embeddings to Redis`, display shows enrolled count
- [x] Delete flow removes from Redis (SSE `member_deleted` path works)
- [x] Revert test: restore real `CLOUD_API_URL`, confirm no `Testing/` files referenced anywhere in the main code

### Additional fixes found & applied during verification (see MAINTENANCE_LOG.md)
- `edge_app.py` missing `import config as cfg` → crash at display start (fixed)
- SSE: HTTP/1.0 buffering → requests clients got nothing (fixed with HTTP/1.1 + chunked)
- SSE: count-based change detection missed upserts (fixed with change counter); deletes now push `member_deleted`/`embedding_deleted`

## 6. Elimination when Cloud is done

1. Delete the whole `Testing/` folder (or move it to an archive).
2. Revert `CLOUD_API_URL` / `CLOUD_API_KEY` in `config.py`.
3. Grep confirms nothing in main code imports or references `Testing/`.

## 7. Open questions / risks — RESOLVED during implementation

- **Registration software payload**: unknown — mock is permissive (accepts
  several key spellings, prints the raw payload to its console). The user
  confirmed the registration software has an "edge testing" mode where it
  only needs a URL to POST to — so pointing it at `http://<edge-ip>:5005`
  works. If the actual body keys differ, the mock prints them for us to adapt.
- **Port 5005**: the "already responding" process was a leftover instance of
  the mock itself from an earlier aborted test run — not a conflict. 5005 is
  free and in use by the mock now.
- **venv path anomaly**: `venv/bin/python` failing once was a shell quirk
  (`cd X && cmd &` backgrounds the whole chain, so the `cd` never applied to
  the foreground shell). Not a code issue; use absolute paths or run with the
  project as the working directory.
- Mock is **unauthenticated** — only for local dev network, never exposed
  outside the lab LAN.

---

## 8. As-built — what was actually done (2026-08-02)

### 8.1 Files created

| File | Notes |
|------|-------|
| `Testing/mock_cloud_server.py` | Final version includes: HTTP/1.1 + chunked SSE (fix below), change-counter change detection, `member_deleted`/`embedding_deleted` pushes on delete. |
| `Testing/send_enroll.py` | CLI client; verified working. |
| `Testing/README.md` | Usage + endpoint table + elimination steps. |
| `docs/TESTING_MOCK_CLOUD_PLAN.md` | This document. |

### 8.2 Config changed (TEMPORARY — revert when Cloud is ready)

```python
CLOUD_API_URL = "http://localhost:5005"   # TEMP: Testing/mock_cloud_server.py
CLOUD_API_KEY = "mock-key"                # TEMP: mock accepts any key
```

### 8.3 Main-code bug found & fixed (would have crashed on the real machine)

`edge_app.py` used `cfg.DISPLAY_ENABLED` (screen-display start) but never
imported `config`. With `DISPLAY_ENABLED=True` the app crashed right after
starting the camera worker. Fixed: added `import config as cfg` to `edge_app.py`.

### 8.4 Mock-server bugs found & fixed during testing

1. **SSE buffering**: `http.server` defaults to HTTP/1.0; without
   `Content-Length` the edge's `requests`/urllib3 client buffers the whole
   body until connection close → received nothing. Fixed with
   `protocol_version = "HTTP/1.1"` + `Transfer-Encoding: chunked` in the SSE
   handler. Verified against the real `core.sse_client.SSEClient`.
2. **Change detection**: was count-based, so re-enrolling (upserting) the
   same student produced no SSE push. Fixed with a global change counter +
   last-changed student id. Deletes now push `member_deleted` +
   `embedding_deleted` (instead of `*_updated`).

### 8.5 Verification results (this dev machine)

| Test | Result |
|------|--------|
| `py_compile` on Testing files | PASS |
| GET endpoints (sync-status/students/embeddings) | PASS |
| POST enroll/delete + `data.json` persistence | PASS |
| SSE via real `SSEClient`: heartbeat + new enroll push | PASS |
| SSE upsert push (same student re-enrolled) | PASS |
| SSE delete push (`member_deleted`/`embedding_deleted`) | PASS |
| Full `edge_app.py` run vs mock: models load, MQTT connected, initial sync `3 face(s) enrolled in Redis`, SSE stream connected | PASS |
| **Live enroll while running**: `send_enroll.py --student-id STU-LIVE` → SSE push → `Member updated`/`Embedding updated` → Redis `num_docs` 3→4, no restart | PASS |
| Graceful shutdown (SIGINT): SSE / Sync / MQTT stop cleanly | PASS |

Test data was cleaned afterwards: `Testing/data.json` reset to empty, Redis
`door_person:*` keys deleted.

### 8.6 Current state

- Mock server is **running** on port 5005 (started with `setsid nohup`, log
  at `/tmp/mock_cloud.log`). Restart if needed:
  ```bash
  venv/bin/python Testing/mock_cloud_server.py
  ```
- Docker containers `door-redis` + mosquitto started for the tests.
- Remaining: hardware test on the Mini PC (real camera + display +
  registration software pointed at `http://<edge-ip>:5005`).
