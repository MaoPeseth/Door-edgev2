# Door-Edge Session Log — 2026-08-31

**Date:** August 31, 2026
**Scope:** Implement the scheduled-access feature on the edge PC (planned in
`SCHEDULED_ACCESS_EDGE_TASK.md` / `FIX_PLAN.md`) and verify it end-to-end
against the live Cloud dashboard.

---

## Summary

Implemented the edge-side of **scheduled access** — the door now consumes the
Cloud's per-room schedule bundle and enforces it locally before every unlock.
All Cloud/dashboard work (rooms `id`, `schedule_revision`, `/api/edge/schedule`,
Edge-timing dashboard UI) was already live; this session added the missing edge
piece. Live E2E verification passed: bundle fetch + persistence, cache-on-boot,
SSE + consistency re-fetch, run-window cooldown, and the schedule gate all work
against the real Cloud.

---

## What was done

### 1. New `core/schedule_policy.py` — policy evaluator

A self-contained evaluator mirroring the Cloud's `services/schedule.py` priority
(LOCKDOWN > OPEN > HOLIDAY > SCHEDULED_LOCKDOWN > RESTRICTED > WEEKEND >
WEEKDAY), plus the **edge run window** (Edge timing) cooldown.

- `state(now)` / `is_granted(member_id, now)` / `in_run_window(now)` /
  `deny_reason(now)` / `revision()`
- Opaque revision handling (`!=` only, never ordered)
- Edge timing: `edge_run_start`/`edge_run_end`, null = 24/7, cross-midnight
  windows allowed, zero-length window never traps
- Persistent JSON cache `schedule_bundle.json` (atomic write-then-rename) so the
  door keeps enforcing while the Cloud is down; loads the cache on boot
- Door-local time only (`EDGE_UTC_OFFSET`, never naive-UTC)

### 2. `core/cloud_client.py` — two new methods

- `get_schedule_bundle(room_id)` → `GET /api/edge/schedule?room=<id>` (read only,
  **not** routed through the offline event queue)
- `get_room_ids()` → `GET /api/edge/rooms` as `{name: id}`, skipping entries
  without an `id` and logging "backend missing room id" when none exist

### 3. `core/sync_agent.py` — track `schedule_revision`

- Seed a `SchedulePolicy` from the cached bundle at boot
- Fetch/update the bundle when `schedule_revision` moves in the boot sync,
  the 5-min consistency loop, and the SSE `_on_sync` handler
- Expose `policy()` for the camera worker

### 4. `core/camera_worker.py` — the unlock gate

- **Cooldown early-out:** outside the edge run window the whole face pipeline
  cools down (OFF HOURS label, no access events, one log line per window
  transition, auto-resumes without restart)
- **Schedule gate** before `publish_unlock`: `is_granted(person_id, now)`;
  on deny shows `SCHEDULE BLOCKED (<state>)`, resets, and reports an
  `access_denied` event with `detail = schedule_blocked_<state>`
- **LOCKDOWN short-circuit** right after a successful face match (no need to
  wait out CONFIRM_FRAMES); the per-frame gate still catches a lockdown landing
  mid-confirmation

### 5. `edge_app.py` + `config.py` + display + docs

- `edge_app.py` passes `sync.policy()` into the `CameraWorker`; logs when no
  bundle is available (first offline boot → behaves as before, allow + log)
- `config.py` adds `SCHEDULE_BUNDLE_PATH` and `SCHEDULE_FALLBACK_OPEN`
- `core/screen_display.py` shows a **Schedule** status row (state / OFF HOURS /
  no bundle) on the 5 s status cadence
- `core/sse_client.py` — docstring only (documents `schedule_revision`-only sync)
- `.gitignore` — ignore generated `schedule_bundle.json`
- `Testing/mock_cloud_server.py` — added `/api/edge/schedule`, `schedule_revision`
  in sync-status, and room `id` fields so the mock mirrors the live contract

---

## E2E test results (live Cloud)

| Check | Result |
|-------|--------|
| `GET /api/edge/sync-status` includes `schedule_revision` | ✅ |
| `GET /api/edge/rooms` returns `id` (`001` → `1`) | ✅ |
| `GET /api/edge/schedule?room=1` returns the bundle | ✅ |
| Initial sync fetches + parses the bundle | ✅ |
| Bundle persisted to `schedule_bundle.json` | ✅ |
| Cache-on-boot (loads policy while Cloud is down) | ✅ |
| SSE `_on_sync` re-fetches on `schedule_revision` change | ✅ |
| Consistency loop re-fetches on change, skips when unchanged | ✅ |
| 24/7 run window (`null`/`null`) → running | ✅ |
| OFF-HOURS cooldown (`deny_reason=COOLDOWN`) blocks known members | ✅ |
| Hard lockdown blocks all | ✅ |
| Holiday blocks (unless exception) | ✅ |
| Weekday grants; exception grants on weekend; out-of-window denies | ✅ |
| Policy priority matches the Cloud exactly (unit checks) | ✅ |
| Full `edge_app.py` boot: MQTT + camera + heartbeat + sync + SSE | ✅ |

Current live policy is **WEEKDAY / 24/7** (open) — `edge_run_start/end` null, no
lockdown, no weekly windows — so the door currently grants everyone. Enforcement
(deny) paths were verified both with synthetic bundles and the user's own
restrictive-schedule test on the dashboard.

---

## Files touched

```
core/cloud_client.py      +get_schedule_bundle, +get_room_ids
core/schedule_policy.py   NEW (evaluator + persistence + run window)
core/sync_agent.py        +schedule_revision tracking, policy wiring
core/sse_client.py        docstring only
core/camera_worker.py     +cooldown early-out + schedule gate + lockdown shortcut
core/screen_display.py    +Schedule status row
edge_app.py               wire policy into CameraWorker
config.py                 +SCHEDULE_BUNDLE_PATH, +SCHEDULE_FALLBACK_OPEN
.gitignore                ignore schedule_bundle.json
Testing/mock_cloud_server.py  +schedule endpoint, schedule_revision, room ids
schedule_bundle.json      generated (gitignored)
```

---

## Notes / follow-ups

- The **card/RFID path is intentionally NOT schedule-gated** (cards are decided
  by the ESP32 against its own allowlist). Gating cards too is a separate
  decision (push an empty/delayed allowlist, or a firmware change).
- The user plans to **rotate the Cloud API key** and update it in both
  `config.py` (`CLOUD_API_KEY`) and the Cloud `.env` (`EDGE_API_KEY`); they match
  today. Several secrets were exposed in chat and should be rotated (Telegram
  bot tokens, `SECRET_KEY`, `SUPERADMIN_PASSWORD`, `POSTGRES_PASSWORD`).
- Housekeeping still open from the FIX_PLAN audit: allowlist line-parsing bug
  (D11), similarity-threshold doc/value mismatch (D12), stale `models.py` (D13).
