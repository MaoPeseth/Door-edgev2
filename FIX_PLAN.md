# Fix Plan — Edge ↔ Cloud integration (scheduled access)

Status date: 2026-08-30. Compiled from a full audit of the Cloud backend and the
Door-edgev2 repo. Anything marked **blocker** must be fixed before the edge and
Cloud can work together at all; the rest is needed for the scheduled-access
feature to function end-to-end.

---

## A. Make it talk (blocker)

1. **Align the API key** — **DONE & VERIFIED 2026-08-30**
   - Backend `.env` was `EDGE_API_KEY=edge-secret-key-2024` (default); edge
     `config.py` uses
     `921987fbda42ac3d3318fac699f9d6134d8503c87481a1a9e2085d90309ec3e5`.
   - Fixed: backend `.env` now carries the 64-hex key. Backend rebuilt.
   - Verified: `/api/edge/*` returns 200 with the key; a wrong key → 401.
2. **Rotate the API key (after the test)** — still open (see note below).

---

## B. Backend contract (so the edge can actually use the schedule API)

3. **`GET /api/edge/rooms` — add room `id`** — **DONE & VERIFIED 2026-08-30**
   - File: `Python Backend/routes/events.py` (`edge_rooms`)
   - Now returns `{"id": <int pk>, "name": ..., "online": ...}`. Verified live:
     `{"id":1,"name":"207","online":false}`.
4. **Add `schedule_revision` to `sync_revisions()`** — **DONE & VERIFIED 2026-08-30**
   - File: `Python Backend/postgreSQL/embedding_store.py`
   - `sync_revisions()` now returns all three fingerprints, so the SSE
     **on-connect** `sync` includes `schedule_revision` too.
   - Verified in-container: keys `[revision, roster_revision, schedule_revision]`.
5. **Fix sync-status docstring drift** — **DONE** — `routes/events.py`
   `edge_sync_status` + `edge_sync_stream` docstrings now document
   `schedule_revision`; removed the redundant explicit kwarg (now via
   `sync_revisions()`).

---

## C. Edge implementation (scheduled-access feature) — DONE 2026-08-31 (verified E2E vs live Cloud)

Full task brief: `docs/SCHEDULED_ACCESS_EDGE_TASK.md`
Session record: `docs/SESSION_LOG_2026-08-31.md`

6. **`core/cloud_client.py`** — add `get_schedule_bundle(room_id)` and
   `get_room_ids()`. Do NOT route schedule reads through the offline event queue.
   — **DONE**
7. **New `core/schedule_policy.py`** — local evaluator mirroring the Cloud's
   `compute_state`/`is_granted` priority:
   LOCKDOWN > OPEN > [scheduled: HOLIDAY / SCHEDULED_LOCKDOWN / RESTRICTED
   (school break) / WEEKDAY / RESTRICTED] > RESTRICTED (require_request) >
   SCHEDULED_LOCKDOWN > HOLIDAY > WEEKEND (Sunday only) > WEEKDAY.
   PLUS a persistent JSON cache (`schedule_bundle.json`, atomic write).
   — **DONE**
8. **`core/sync_agent.py`** — track `_local_schedule_revision`; react to
   `schedule_revision` in the boot sync, the connectivity check loop, and SSE
   `_on_sync`; re-fetch the bundle only when it changes. — **DONE**
9. **`core/camera_worker.py`** — gate before `publish_unlock` (`:521-527`):
   `is_granted(person_id, now)`; on deny show `SCHEDULE BLOCKED (<state>)`,
   reset, and report an `access_denied` event with detail
   `schedule_blocked_<state>`. — **DONE** (also added cooldown early-out + a
   LOCKDOWN short-circuit after a successful match)
10. **`edge_app.py` + `config.py`** — wire the policy into the camera worker;
    add `SCHEDULE_BUNDLE_PATH` and `SCHEDULE_FALLBACK_OPEN`; log clearly when no
    bundle is available (first offline boot). — **DONE**

---

## D. Edge bugs found during the full audit

11. **Allowlist parsing bug** — `core/sync_agent.py:219-241`
    `_sync_card_uid_to_esp32` treats every non-empty allowlist line as a
    `card_uid`, but the endpoint returns `revision …`/`count n` header lines
    plus tab-separated records `card_uid\tid\tactive\texpiry\tname`
    (`routes/events.py:502-513`). The ESP32 therefore gets garbage UIDs.
    Fix: skip the two header lines, split records on `\t`, take `[0]`.
12. **Similarity threshold mismatch** — `core/face_matcher.py:5-18` docstring
    says sim ≥ 0.70, but `config.py SIMILARITY_THRESHOLD = 0.5`. Decide the
    intended threshold and align doc + value.
13. **Delete stale `models.py` at the edge repo root** — it references
    `YOLO_ENABLED`/`YOLO_MODEL_PATH` that no longer exist in `config.py`; the
    live module is `core/models.py`.

---

## E. Backend hygiene (optional, not blocking)

14. **Prune expired `access_exceptions`** — `services/retention.py` today only
    trims `access_logs`. Add periodic deletion of
    `AccessException.exception_date < today`.

---

## F. NEW FEATURE — Edge timing (edge PC run window / cooldown)

Sets a per-room daily window (e.g. 06:00–22:00) from the Cloud; outside it the
edge **cools down** (stops processing access). Either `start`/`end` null = 24/7.
Cross-midnight windows allowed (`start > end`).

**Backend — DONE & VERIFIED 2026-08-30 (containers rebuilt):**
- `postgreSQL/models.py` — Room `edge_run_start`/`edge_run_end` (`db.Time`,
  nullable) + `to_dict` fields.
- `app.py:upgrade_schema` — idempotent `ALTER TABLE rooms ADD COLUMN IF NOT
  EXISTS edge_run_start/edge_run_end TIME`.
- `services/schedule.py:schedule_bundle` — ships `edge_run_start`/`edge_run_end`
  (HH:MM:SS or null) to the edge.
- `routes/schedule_routes.py` — `GET /admin/rooms/<id>/policy` returns
  `edgeRunStart`/`edgeRunEnd`; new `POST /admin/rooms/<id>/edge-timing`
  `{start?, end?}` → sets, audits `room_edge_timing`, `notify_schedule_changed()`
  (bumps schedule_revision via `Room.updated_at`), Telegram alert.
- `routes/v1.py:_room_dict` — dashboard rooms list carries `edgeRunStart`/
  `edgeRunEnd`.
- Verified live: POST 06:00→22:00 → policy + schedule bundle both reflect it;
  reset to null → bundle back to 24/7.

**Dashboard — DONE & VERIFIED 2026-08-30 (rebuilt, no new lint warnings):**
- `lib/schemas.ts` — `roomSchema` + `lockdownPolicySchema` carry
  `edgeRunStart`/`edgeRunEnd` (nullable string).
- NEW `lib/hooks/use-edge-timing.ts` — `useSetEdgeTiming(roomId)` POST
  `/admin/rooms/<id>/edge-timing`, invalidates `["room-policy", id]` + `["rooms"]`.
- `components/rooms/room-schedule-dialog.tsx` — "Edge timing" section: 24/7 ↔
  timer toggle + start→end time fields + save button.
- Verified: `npx tsc --noEmit` + container build clean.

**Edge — DONE & VERIFIED 2026-08-31** (`docs/SESSION_LOG_2026-08-31.md`):
- `schedule_policy.in_run_window(now)` + cooldown early-out in `camera_worker`.
- No access events during cooldown; one log line per window transition.
- Verified live: 24/7 open, OFF-HOURS cooldown, lockdown, holiday, and both the
  SSE + consistency re-fetch paths all behave correctly.

---

## Suggested order — COMPLETE 2026-08-31

A1 → B3, B4 → C6-C10 → D11 → then test. B5/D12/D13/E14 are cleanups that don't
block the test. F (Edge timing) is a parallel feature: backend shipped now,
dashboard UI next, edge cooldown rides along with the C bundle work.
- C6–C10 **done**, F-edge **done**, live E2E **passed** (see
  `docs/SESSION_LOG_2026-08-31.md`).
- D11 (allowlist line parsing), D12 (similarity threshold), D13 (stale
  `models.py`) remain open — non-blocking cleanups.

## Verification after fixes — PASSED 2026-08-31

- Backend: `cd "Python Backend"; python -m py_compile <files>`; rebuild
  containers.
- Dashboard: `cd "TEE Door Lock Dashboard"; npx tsc --noEmit`.
- Edge: `python -m py_compile core/cloud_client.py core/schedule_policy.py core/sync_agent.py core/camera_worker.py`. — ✅ clean
- Live test: edge boots → sync fetches the schedule bundle → recognized face
  denied outside the allowed window (`schedule_blocked_<state>` event), granted
  inside it; offline kill → door keeps enforcing the last bundle. — ✅ verified
  against live Cloud