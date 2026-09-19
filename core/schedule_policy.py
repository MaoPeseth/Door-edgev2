"""
core/schedule_policy.py
Local evaluator of the Cloud's per-room schedule bundle.

Mirrors the Cloud backend `services/schedule.py` priority so the door enforces
the same policy locally, at the moment of unlock, without the Cloud being on
the critical path. The bundle is fetched by SyncAgent and pushed here via
`update()`; a copy is cached to disk (`schedule_bundle.json`, atomic write) so
the door keeps enforcing while the Cloud is down.

Time rule (CRITICAL): all bundle dates/times are in door-local time
(EDGE_UTC_OFFSET, +07:00). Evaluate .date()/.time()/.weekday() on `now()`
(UTC + offset, naive) — never on naive-UTC. The bundle itself is naive.

StateType enum (priority): LOCKDOWN > OPEN > [scheduled states] >
RESTRICTED > SCHEDULED_LOCKDOWN > HOLIDAY > WEEKEND > WEEKDAY.
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import config as cfg


# State outcomes — order in this enum is informational; the decision logic in
# `state()` is what enforces priority.
class StateType:
    WEEKDAY            = "WEEKDAY"
    WEEKEND            = "WEEKEND"
    HOLIDAY            = "HOLIDAY"
    RESTRICTED         = "RESTRICTED"
    SCHEDULED_LOCKDOWN = "SCHEDULED_LOCKDOWN"
    OPEN               = "OPEN"
    LOCKDOWN           = "LOCKDOWN"


def _naive_now() -> datetime:
    """Current door-local time (UTC + EDGE_UTC_OFFSET), naive.

    Matches cloud_client._now() / mqtt_publisher._now() — wall clock is always
    the door's local time. Uses an explicit fixed offset (never
    `datetime.fromtimestamp()`, which would apply the machine's own TZ on top
    of EDGE_UTC_OFFSET and double-shift the clock).
    """
    offset = int(getattr(cfg, "EDGE_UTC_OFFSET", 7))
    return datetime.now(timezone(timedelta(hours=offset))).replace(tzinfo=None)


def _hms(value) -> str | None:
    """Normalize an HH:MM:SS (or HH:MM) string -> HH:MM:SS, else None.

    None / "" are treated as "no time configured".
    """
    if value in (None, ""):
        return None
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) == 3:
        return s
    if len(parts) == 2:
        return f"{s}:00"
    return None


def _t(value) -> "object":
    """Return a comparable time for HH:MM:SS (or None)."""
    s = _hms(value)
    if s is None:
        return None
    try:
        h, m, sec = (int(x) for x in s.split(":"))
        return datetime(2000, 1, 1, h, m, sec).time()
    except Exception:
        return None


class SchedulePolicy:
    """Thread-safe wrapper around one schedule bundle + edge run window."""

    def __init__(self, room_id: int):
        self._room_id = room_id
        self._bundle = {}
        self._lock = threading.Lock()
        self._persist_path = getattr(cfg, "SCHEDULE_BUNDLE_PATH", None)

    # ── Bundle state ────────────────────────────────────────────────────────

    def update(self, bundle: dict):
        """Replace the stored bundle, bump the revision, persist to disk."""
        bundle = dict(bundle or {})
        with self._lock:
            self._bundle = bundle
        _persist(bundle)
        print(f"[Schedule] Bundle updated — revision={self.revision()}")

    def revision(self) -> str:
        """Opaque schedule revision string ("" when empty)."""
        with self._lock:
            return self._bundle.get("revision") or ""

    def is_empty(self) -> bool:
        with self._lock:
            return not bool(self._bundle)

    def bundle(self) -> dict:
        with self._lock:
            return dict(self._bundle)

    def room_id(self) -> int:
        return self._room_id

    # ── Time helpers ────────────────────────────────────────────────────────

    def now(self) -> datetime:
        return _naive_now()

    # ── Edge run window (Edge timing cooldown) ──────────────────────────────

    def in_run_window(self, now: datetime) -> bool:
        """True when the edge machine should be running (on hours).

        The bundle ships edge_run_start/edge_run_end (HH:MM:SS or null). Either
        null = 24/7. A zero-length window (start == end) never traps. Windows
        may cross midnight (start > end).
        """
        with self._lock:
            s = self._bundle.get("edge_run_start")
            e = self._bundle.get("edge_run_end")
        st = _t(s)
        et = _t(e)
        if st is None or et is None:
            return True
        if st == et:
            return True
        t = now.time()
        if st < et:
            return st <= t < et
        return t >= st or t < et

    def run_window_label(self) -> str:
        with self._lock:
            s = _hms(self._bundle.get("edge_run_start"))
            e = _hms(self._bundle.get("edge_run_end"))
        if s is None or e is None:
            return "24/7"
        return f"{s}-{e}"

    # ── Policy state ────────────────────────────────────────────────────────

    def state(self, now: datetime) -> str:
        """Return the StateType for `now`, using the Cloud's exact priority."""
        with self._lock:
            b = dict(self._bundle)
        d = now.date()

        # 1. Hard lockdown (administrative) — highest priority
        if b.get("lockdown"):
            return StateType.LOCKDOWN

        # 2. Access override — may UNLOCK any request-gated state (but not a
        #    hard lockdown above).
        if any(_override_active(o, d, now) for o in b.get("access_overrides", []) or []):
            return StateType.OPEN

        # 3. Scheduled mode (room has a weekly_schedule) — holiday / school
        #    break / lockdown windows gate the weekly rows.
        weekly = b.get("weekly_schedule") or []
        if weekly:
            in_valid = _within_validity(b, d)
            if in_valid:
                if _inside_school_break(b.get("school_breaks") or [], d):
                    return StateType.RESTRICTED
                if any(_window_active(w, d, now) for w in b.get("scheduled_lockdowns", []) or []):
                    return StateType.SCHEDULED_LOCKDOWN
                if d in _dates(b.get("holidays") or []):
                    return StateType.HOLIDAY
                if any(
                    r.get("allowed")
                    and r.get("weekday") == now.weekday()
                    and _t(r.get("start")) is not None
                    and _t(r.get("end")) is not None
                    and _t(r.get("start")) <= now.time() <= _t(r.get("end"))
                    for r in weekly
                ):
                    return StateType.WEEKDAY
                return StateType.RESTRICTED
            # outside validity range → fall through to steps 4-8

        # 4. require_request gate
        if b.get("require_request"):
            return StateType.RESTRICTED

        # 5. Scheduled lockdown window (applies even outside weekly mode)
        if any(_window_active(w, d, now) for w in b.get("scheduled_lockdowns", []) or []):
            return StateType.SCHEDULED_LOCKDOWN

        # 6. Holiday
        if d in _dates(b.get("holidays") or []):
            return StateType.HOLIDAY

        # 7. Weekend (Sunday only)
        if now.weekday() == 6:
            return StateType.WEEKEND

        # 8. Default
        return StateType.WEEKDAY

    # ── Decision ────────────────────────────────────────────────────────────

    def is_granted(self, member_id: str, now: datetime) -> bool:
        """True when `member_id` may access the door at `now`.

        A hard lockdown or being outside the edge run window always denies.
        WEEKDAY/OPEN grant. All other states (RESTRICTED / WEEKEND / HOLIDAY /
        SCHEDULED_LOCKDOWN) need an approved exception for this member.
        """
        if not self.in_run_window(now):
            return False
        st = self.state(now)
        if st == StateType.LOCKDOWN:
            return False
        if st in (StateType.WEEKDAY, StateType.OPEN):
            return True
        return any(
            e.get("member_id") == member_id
            and e.get("date") == now.date().isoformat()
            and _t(e.get("start")) is not None
            and _t(e.get("end")) is not None
            and _t(e.get("start")) <= now.time() <= _t(e.get("end"))
            for e in self._bundle.get("exceptions", []) or []
        )

    def deny_reason(self, now: datetime) -> str | None:
        """Human/backend reason for denial, or None if `now` would be granted."""
        if not self.in_run_window(now):
            return "COOLDOWN"
        return self.state(now)


# ── Pure helpers (no self) ───────────────────────────────────────────────────

def _within_validity(b: dict, d) -> bool:
    """True when `d` is within [schedule_start, schedule_end] (both-null → True)."""
    s = b.get("schedule_start")
    e = b.get("schedule_end")
    if not s and not e:
        return True
    if s and d < _date(s):
        return False
    if e and d > _date(e):
        return False
    return True


def _inside_school_break(breaks: list, d) -> bool:
    for br in breaks or []:
        sd = _date(br.get("start_date"))
        ed = _date(br.get("end_date"))
        if sd is None or ed is None:
            continue
        if sd <= d <= ed:
            return True
    return False


def _override_active(o: dict, d, now) -> bool:
    if _date(o.get("date")) != d:
        return False
    return _time_in_range(o, now)


def _window_active(w: dict, d, now) -> bool:
    if _date(w.get("date")) != d:
        return False
    return _time_in_range(w, now)


def _time_in_range(rec: dict, now) -> bool:
    st = _t(rec.get("start"))
    et = _t(rec.get("end"))
    if st is None or et is None:
        return False
    return st <= now.time() <= et


def _date(value):
    if value in (None, "") or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _dates(values):
    out = set()
    for v in values or []:
        d = _date(v)
        if d is not None:
            out.add(d)
    return out


# ── Persistence (offline rule) ──────────────────────────────────────────────

def _persist(bundle: dict):
    """Write the bundle to disk atomically (write-then-rename)."""
    path = getattr(cfg, "SCHEDULE_BUNDLE_PATH", None)
    if not path:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[Schedule] Cannot persist bundle: {e}")


def load_cached_bundle(room_id: int) -> SchedulePolicy:
    """Return a policy seeded from the cached bundle on disk (may be empty)."""
    policy = SchedulePolicy(room_id)
    path = getattr(cfg, "SCHEDULE_BUNDLE_PATH", None)
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                policy._bundle = data
                print(f"[Schedule] Loaded cached bundle from disk "
                      f"(revision={policy.revision()})")
        except Exception as e:
            print(f"[Schedule] Cannot read cached bundle: {e}")
    return policy