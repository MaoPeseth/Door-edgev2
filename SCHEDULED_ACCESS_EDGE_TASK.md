# Scheduled Access — Edge PC Task Brief

> **Audience:** the Door-edgev2 agent / next developer working on the edge PC.
> **Goal:** make the edge PC consume the Cloud's per-room **schedule bundle** and
> enforce it locally before every unlock. The Cloud (backend + dashboard) is fully
> deployed and live; only this edge repo needs work.
>
> **Cloud status (verified 2026-08-31):** all endpoints live, API key aligned,
> containers rebuilt. The Edge timing dashboard feature (dedicated "Edge timing"
> menu item + 24-hour duty rail dialog) is live. Bundle ships `edge_run_start`/
> `edge_run_end`. The edge must consume them.
>
> Everything in this file was verified against the live Cloud code. File/line
> references are exact as of the commit this repo was cloned at (`83112cd`).

---

## 1. Why (context)

Today the door opens on a successful face match + frame confirmation alone
(`core/camera_worker.py:521-527`). The Cloud has a full scheduled-access engine
(holidays, weekly class windows, school breaks, lockdowns, per-member
exceptions) and exposes everything the edge needs, but **the edge never asks for
it**. Result: doors are open even when the schedule says restricted.

**The Edge timing dashboard UI is now live.** Administrators have a dedicated
"Edge timing" menu item (Timer icon) in each room's `⋯` dropdown. It opens a
dialog with a 24-hour duty rail, quick-fill presets (Standard 08–17, Extended
07–19, Night 20–06), and a live ON DUTY / COOLING NOW status readout. The
admin sets `edge_run_start` and `edge_run_end` (or clears them for 24/7 mode),
and the bundle ships these fields. The edge must consume them — that is the
missing piece.

Per the planning doc the door is expected to re-evaluate the room's schedule
state **on the edge, at the moment of unlock** — the Cloud is not on the
critical path.

---

## 2. Cloud-side dependencies — CLOSED 2026-08-30 (verified live)

Both were resolved on the Cloud side; rely on them, no defensive fallback needed
beyond the normal degradation rules (§8).

1. **Room identity — CLOSED.** `GET /api/edge/schedule` requires the room's
   **integer primary-key id** (`?room=<id>`), and `GET /api/edge/rooms` now
   returns `{"id": <int>, "name": ..., "online": ...}`. Map `config.py ROOM_ID`
   (the room name) → `id` from that response. Verified: `{"id":1,"name":"207","online":false}`.
2. **Revisions — CLOSED.** `sync_revisions()` now includes `schedule_revision`,
   so the SSE **on-connect** `sync` carries
   `{revision, roster_revision, schedule_revision}`. A later `sync` with just
   `{"schedule_revision": …}` is still pushed on schedule changes. Keep the
   boot + periodic consistency poll of `GET /api/edge/sync-status` as written —
   it is still the authoritative health/sync probe.

---

## 3. Cloud contract (verbatim)

### `GET /api/edge/sync-status`
```json
{
  "success": true,
  "count": 12, "total_embeddings": 12, "total_students": 10,
  "revision": "e12:2026-08-29T10:00:00|...",
  "roster_revision": "...",
  "schedule_revision": "h3:2026-08-29T10:00:00|e0:...|r6:..."
}
```
Revisions are **opaque strings — compare with `!=`, never order them**.

### `GET /api/edge/schedule?room=<int id>`
```json
{
  "success": true,
  "room_id": 1,
  "revision": "<schedule_revision>",
  "lockdown": false,
  "require_request": false,
  "edge_run_start": "06:00:00",             // null = edge runs 24/7
  "edge_run_end": "22:00:00",               // null = edge runs 24/7
  "schedule_start": "2026-09-07",           // null = always active
  "schedule_end": null,                     // null = always active
  "holidays": ["2026-08-17", "2026-11-18"],
  "scheduled_lockdowns": [
    {"room_id": 1, "date": "2026-09-10", "start": "08:00:00", "end": "10:00:00", "reason": "exam"}
  ],
  "access_overrides": [
    {"room_id": 1, "date": "2026-09-12", "start": "13:00:00", "end": "17:00:00", "reason": "event"}
  ],
  "weekly_schedule": [
    {"room_id": 1, "weekday": 0, "allowed": true, "start": "07:30:00", "end": "20:30:00"}
  ],
  "school_breaks": [
    {"start_date": "2026-08-01", "end_date": "2026-08-31", "name": "Summer"}
  ],
  "exceptions": [
    {"member_id": "T002", "date": "2026-09-11", "start": "08:00:00", "end": "12:00:00"}
  ]
}
```
- Dates are local `YYYY-MM-DD`, times local `HH:MM:SS`, all in
  `EDGE_UTC_OFFSET` time (Kampong Speu / +07:00). **Never naive-UTC.**
- `weekday` uses Python weekday(): **Monday = 0 … Sunday = 6**.
- **Edge run window (new, Cloud is live):** outside `[edge_run_start,
  edge_run_end)` the edge **cools down** — it stops processing access entirely.
  Either value `null` = no restriction (the edge runs 24/7). Windows may cross
  midnight (`start > end`). The window is independent of the access policy: it
  is the *machine's* on/off hours, not a per-member decision (see §4.8).

### SSE `GET /api/edge/sync-stream`
- On connect: `sync` `{revision, roster_revision, schedule_revision}`
- On policy change: `sync` `{"schedule_revision": "…"}` (only that key)
- Also: `enroll` `{"room","timeoutS"}`, `enroll_cancel` `{"room"}`,
  `heartbeat` keep-alive every 15 s.

---

## 4. Work items (implementation order)

### 4.1 `core/cloud_client.py` — add two methods
- `get_schedule_bundle(room_id: int) -> Optional[dict]` → `GET /api/edge/schedule?room=<id>`.
  Use the existing `_request` retry pattern.
- `get_room_ids() -> Optional[dict]` → `GET /api/edge/rooms`, return
  `{name: id}` while skipping entries without an `"id"` field. Log a one-line
  warning when every entry lacks `id` (backend dependency #1 not deployed yet).
- **Do NOT enqueue `/edge/schedule` in the offline queue** — it is a read, not
  an audit event. Buffered endpoints stay `/events` + `/alert`.

### 4.2 New module `core/schedule_policy.py`
Implement a self-contained policy evaluator (mirror of the Cloud's
`services/schedule.py`, reproducible here for tests):

```python
class SchedulePolicy:
    def __init__(self, room_id: int)         # room_id = integer PK
    def update(self, bundle: dict)           # replace stored bundle, bump own revision
    def revision(self) -> str                # bundle["revision"] or ""
    def now(self) -> datetime                # UTC + EDGE_UTC_OFFSET (naive), see §6
    def state(self, now: datetime) -> str    # one of the StateType values below
    def in_run_window(self, now: datetime) -> bool   # edge on/off hours (Edge timing)
    def is_granted(self, member_id: str, now: datetime) -> bool
    def deny_reason(self, now: datetime) -> str | None
```

StateType: `WEEKDAY | WEEKEND | HOLIDAY | RESTRICTED | SCHEDULED_LOCKDOWN |
OPEN | LOCKDOWN`.

**Evaluation algorithm — copy this priority exactly** (from Cloud `schedule.py`):
```
state(room, now):
   1. if bundle.lockdown:                                return LOCKDOWN
   2. if override active on now.date & [start,end] now:  return OPEN
   3. if room is "scheduled" (bundle.weekly_schedule non-empty):
        if today within [schedule_start, schedule_end] (skip check when both null):
            if now.date in holidays:                     return HOLIDAY
            if lockdown window on now.date & now in it:  return SCHEDULED_LOCKDOWN
            if today inside any school_break range:      return RESTRICTED
            if weekly row (weekday=now.weekday(), allowed=True)
               and row.start <= now.time <= row.end:     return WEEKDAY
            else:                                        return RESTRICTED
        # outside validity range → fall through to steps 4-8
   4. if bundle.require_request:                         return RESTRICTED
   5. if lockdown window on now.date & now in it:        return SCHEDULED_LOCKDOWN
   6. if now.date in holidays:                           return HOLIDAY
   7. if now.weekday() == 6 (Sunday):                    return WEEKEND
   8. return WEEKDAY
```
State priority note (matches Cloud docstring): LOCKDOWN > RESTRICTED > HOLIDAY
> WEEKEND > WEEKDAY, with OPEN sitting above every request-gated state but below
a hard lockdown.

**`is_granted(member_id, now)`** (mirror of Cloud `is_granted`):
```
if not in_run_window(now):           return False        # Edge timing cooldown
state = state(now)
if state == LOCKDOWN:                return False
if state in (WEEKDAY, OPEN):         return True
# RESTRICTED / WEEKEND / HOLIDAY / SCHEDULED_LOCKDOWN need an approved exception:
return any e in bundle.exceptions where
    e.member_id == member_id and e.date == now.date
    and e.start <= now.time <= e.end
```

**`in_run_window(now)`** (Edge timing, from Cloud `edge_run_start/_end`):
```
s, e = bundle.edge_run_start, bundle.edge_run_end
if s is None or e is None:   return True          # 24/7
if s == e:                   return True          # zero-length window never traps
if s < e:                    return s <= now.time() < e
return now.time() >= s or now.time() < e          # overnight window (start > end)
```

**Persistence + offline rule:** after a successful fetch, write the bundle to
`<BASE_DIR>/schedule_bundle.json` (atomic write-then-rename, same pattern as
`offline_queue.py:_persist_locked`). On boot: load the cached bundle first so
the door keeps enforcing while the Cloud is down. If a fetch fails, keep the old
bundle and log.

### 4.3 `core/sync_agent.py` — track `schedule_revision`
- Add `self._local_schedule_revision = 0` (next to `:48-50`) and
  `self._policy` (a `SchedulePolicy`).
- `_do_initial_sync` (`:103-144`): after `get_sync_status()` compare
  `status.get("schedule_revision")`; if it differs from local (or local is 0),
  resolve the room id via `get_room_ids()` and call `get_schedule_bundle(id)`,
  then `_policy.update(bundle)` + save. Store the revision.
- `_check_consistency` (`:250-270`): add a third branch — on
  `schedule_revision` mismatch, re-fetch the bundle (same steps).
- `_on_sync` (`:296-307`): handle the `schedule_revision` key when present (the
  SSE `sync` that carries only `schedule_revision` — today the function ignores
  it because that key is never looked at).
- Expose `policy()` so `edge_app` can wire it to the camera.

### 4.4 `core/sse_client.py` — documentation only
No code change required — it forwards any JSON payload on `sync` to `_on_sync`.
Update the docstring (§6-13) to mention that `sync` may carry
`schedule_revision` (alone) and that `schedule_revision` is absent on connect.

### 4.5 `edge_app.py` — wire it up
- After `sync.sync_now()` (post line 148) the policy is populated.
- Pass the policy into `CameraWorker` (see 4.6). If the policy is empty
  (first boot offline, no cache, Cloud unreachable), log:
  `"[Schedule] No bundle yet — door behaves as before (allow + log)"`.

### 4.6 `core/camera_worker.py` — the unlock gate
- Accept an optional `schedule_policy=None` in `__init__` (`:72-75`).
- **Primary gate at `:521-527`**, immediately before `publish_unlock`:

  ```python
  if self._confirm_count >= cfg.CONFIRM_FRAMES:
      if self._schedule is not None and \
             not self._schedule.is_granted(person_id, self._schedule.now()):
          reason = self._schedule.deny_reason(self._schedule.now())
          self._reset()
          state.set_results(
              [{"bbox": bbox, "label": f"{name} SCHEDULE BLOCKED ({reason})",
                "color": (0, 0, 255)}], [], _IDLE, 0)
          # one denied event per episode (cooldown-style guard is fine)
          if self._send_denied(f"access_denied", frame):
              state.add_event(f"Schedule blocked: {reason}", "deny")
              state.set_last_access({"method": "face", "granted": False,
                  "reason": f"schedule_blocked_{reason}", "epoch": time.time(),
                  "ts": _ts()})
          print(f"[Inference] {name} denied by schedule ({reason})")
          return
      # ... existing unlock code unchanged ...
  ```
  (`_send_denied` exists at `:565` and already respects cooldowns; `detail` will
  read `schedule_blocked_<STATE>` server-side.)
- **Recommended (nice-to-have) pruning:** after the hand is accepted / match
  succeeds (`:504-508`), if the state is `LOCKDOWN`, short-circuit to the denied
  path immediately instead of waiting for CONFIRM_FRAMES. Optional — the primary
  gate above is the correctness requirement. **An already-unlocked door must not
  auto-continue granting**: re-checking per-frame at the gate covers a lockdown
  landing mid-confirmation.

### 4.7 (optional) `core/screen_display.py` — visible state
Add the schedule state to the STATUS panel (e.g. `"Schedule: HOLIDAY"`) using
`policy.state(policy.now())`, updated on the same 5 s cadence as
`_enrolled` (`:378-394`). Displays `"no bundle"` when the policy is empty.
Small, high-value for the live test.

### 4.8 Edge timing — "cool down" outside the run window (Cloud + dashboard live, 2026-08-31)

**Dashboard UI is live:** the admin has a dedicated "Edge timing" menu item
(Timer icon) for each room. The dialog shows:
- **24-hour duty rail** — a midnight→midnight track with active hours filled teal
  and a live "now" cursor.
- **ON DUTY / COOLING NOW** status — big card at top, judged in `Asia/Phnom_Penh`
  (+07:00).
- **Mode toggle** — "Set daily hours" ↔ "Always on (24/7)".
- **Time fields** — start → end, with presets: Standard 08–17, Extended 07–19,
  Night 20–06.
- **"Run 24/7" quick action** — one-click clear in footer.
- Auto-refreshes "now" every 15s while open.

**What the edge must do:** the bundle ships `edge_run_start` / `edge_run_end`
(null = 24/7, may cross midnight). Outside the window the edge must **cool down**:
stop running the face pipeline, keep the door locked, and show an OFF HOURS
state.

- **Early-out in `core/camera_worker.py`** — at the top of the per-frame work
  (before the detection/recognition pipeline, i.e. `:521-527` and above), when
  the policy exists and `not policy.in_run_window(policy.now())`:
  - skip inference entirely, keep the last frame / show
    `"Edge cooling — runs <start>–<end>"`;
  - never unlock; do **not** emit access events for a cooling window (a cold
    overnight stream of denials is noise);
  - one log line when the window transitions (entering/leaving cooldown).
- Re-check the window continuously (per frame or on the 5 s cadence): the
  machine must come out of cooldown the moment the window opens without a
  restart.
- The RFID/card path stays as §5 — cards are ESP32-side and unaffected. Only the
  local face pipeline cools down.
- `deny_reason()` returns `"COOLDOWN"` for an out-of-window moment (used by the
  screen/log line).

---

## 5. What NOT to change
- Do **not** enqueue schedule reads in `offline_queue` (see 4.1).
- Do **not** alter MQTT topics, allowlist handling, liveness, or the
  confirm-frame logic outside the gate.
- Do **not** gate the RFID/card path: cards are decided by the ESP32 against
  the allowlist it already holds. Edge timing (run window) is face-only — the
  card reader stays active regardless of the window. If the team wants cards
  schedule-gated too, that is a **separate decision** (either push an empty/
  delayed allowlist while closed, or a firmware change). Flag it, don't implement
  it here.
- Do **not** write dashboard UI code — that is already shipped. This brief is
  edge-only.

---

## 6. Timezone rule (critical)
All bundle dates/times are in door-local time. Reuse the established pattern from
`mqtt_publisher.py:_now()` (`:255-261`) / `cloud_client.py:_now()`:

```python
offset = int(getattr(cfg, "EDGE_UTC_OFFSET", 7))   # 7
local = datetime.fromtimestamp(time.time() + offset * 3600)
```
Evaluate `.date()` / `.time()` / `.weekday()` on **that** value, nowhere else.
The bundle itself is naive — do not attach timezones to its fields.

---

## 7. Config
Add to `config.py`:
```python
SCHEDULE_BUNDLE_PATH = os.path.join(BASE_DIR, "schedule_bundle.json")
SCHEDULE_FALLBACK_OPEN = True   # no bundle → allow (pre-schedule behaviour) + log
```

---

## 8. Degradation rules
| Situation | Behaviour |
|---|---|
| Fetch fails, cache exists | Keep enforcing with the cached bundle; log |
| Fetch fails, no cache, never synced | `SCHEDULE_FALLBACK_OPEN=True` → allow + one log line at boot; `False` → deny |
| `schedule_revision` key absent (old backend) | Work as today; bundle fetched once at boot; log a warning |
| `/api/edge/rooms` has no `id` field | Log "backend missing room id — schedule sync disabled"; allow + log |
| Lockdown lifted / window changes mid-frame | Gate runs per unlock decision, so the new state applies on the next attempt |

---

## 9. Verification checklist
1. `python -m py_compile core/schedule_policy.py core/cloud_client.py core/sync_agent.py core/camera_worker.py`.
2. Reproduce the priority order with a small local unit test over fixed
   `datetime` values (holiday, Sunday, school-break, lockdown window, open
   override, `LOCKDOWN`) — the algorithm in §4.2 must match the Cloud exactly.
3. With `Testing/mock_cloud_server.py`: add `/api/edge/schedule` +
   `schedule_revision` in `/api/edge/sync-status`, then confirm:
   - boot fetches and **persists** `schedule_bundle.json`;
   - an SSE `sync` carrying only `schedule_revision` triggers a re-fetch;
   - killing the mock mid-run → door keeps enforcing the last bundle;
   - camera denies a recognized face outside the allowed window with a
     `schedule_blocked_*` denied event, and grants inside it.
4. **Edge timing:** set `edge_run_start/end` in the mock; outside the window
   the camera shows OFF HOURS and never unlocks and logs one line at the
   window transition; inside it, normal behaviour resumes without a restart.
5. Check daily log line at shutdown: `[Schedule] revision=… rooms=…`.
6. `git status` clean of anything outside the intended files.

---

## 10. Files touched (summary)
```
core/cloud_client.py      +2 methods (get_schedule_bundle, get_room_ids)
core/schedule_policy.py   new module (evaluator + JSONL-style persistence + run window)
core/sync_agent.py        +schedule_revision tracking, policy wiring
core/sse_client.py        docstring only
core/camera_worker.py     +schedule gate + cooldown early-out (Edge timing)
core/screen_display.py    (optional) schedule state row / OFF HOURS
config.py                 +SCHEDULE_BUNDLE_PATH, +SCHEDULE_FALLBACK_OPEN
schedule_bundle.json      (generated, gitignore it)
```