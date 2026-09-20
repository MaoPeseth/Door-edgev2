"""
tests/test_schedule_policy.py  — Tier 1: schedule policy unit tests.

Covers time normalization, edge run window (incl. midnight crossing),
the exact Cloud state priority chain, grant/deny decisions, and the
disk-cached bundle persistence path.
"""
import datetime as dt
import pytest

import config as cfg
import core.schedule_policy as sp


def _base_bundle(**kw):
    b = {
        "revision": "qa:1",
        "lockdown": False,
        "require_request": False,
        "edge_run_start": None,
        "edge_run_end": None,
        "schedule_start": None,
        "schedule_end": None,
        "holidays": [],
        "scheduled_lockdowns": [],
        "access_overrides": [],
        "weekly_schedule": [],
        "school_breaks": [],
        "exceptions": [],
    }
    b.update(kw)
    return b


# 2026-09-21 = Monday (weekday 0); 2026-09-20 = Sunday (weekday 6).
MONDAY = dt.datetime(2026, 9, 21, 10, 0, 0)
SUNDAY = dt.datetime(2026, 9, 20, 10, 0, 0)


# ── Time helpers ─────────────────────────────────────────────────────────────

def test_hms_normalizes_common_forms():
    assert sp._hms(None) is None
    assert sp._hms("") is None
    assert sp._hms("   ") is None
    assert sp._hms("08:30") == "08:30:00"
    assert sp._hms(" 09:15 ") == "09:15:00"
    assert sp._hms("08:30:00") == "08:30:00"


def test_hms_rejects_garbage():
    assert sp._hms("garbage") is None
    assert sp._hms("08:30:00:45") is None   # 4 colon-parts
    assert sp._hms("8:3") == "8:3:00"       # permissive: pads a 2-part form


def test_t_returns_comparable_time():
    assert sp._t("08:30:00") == dt.time(8, 30)
    assert sp._t("08:30") == dt.time(8, 30)
    assert sp._t(None) is None
    assert sp._t("99:00:00") is None        # invalid hour → None


# ── Edge run window ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("start,end,target,expected", [
    (None, None, "10:00:00", True),             # 24/7
    ("08:00:00", "08:00:00", "10:00:00", True), # zero-length never traps
    ("08:00:00", "17:00:00", "10:00:00", True),
    ("08:00:00", "17:00:00", "08:00:00", True),  # inclusive start
    ("08:00:00", "17:00:00", "07:59:59", False),
    ("08:00:00", "17:00:00", "17:00:00", False), # exclusive end
    ("17:00:00", "08:00:00", "20:00:00", True),  # crosses midnight
    ("17:00:00", "08:00:00", "02:00:00", True),
    ("17:00:00", "08:00:00", "12:00:00", False),
])
def test_in_run_window(start, end, target, expected):
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(edge_run_start=start, edge_run_end=end))
    h, m, s = (int(x) for x in target.split(":"))
    assert policy.in_run_window(dt.datetime(2026, 9, 21, h, m, s)) is expected


def test_run_window_label():
    policy = sp.SchedulePolicy(1)
    assert policy.run_window_label() == "24/7"
    policy.update(_base_bundle(edge_run_start="08:00", edge_run_end="17:00"))
    assert policy.run_window_label() == "08:00:00-17:00:00"


# ── State priority chain ─────────────────────────────────────────────────────

def test_lockdown_outranks_everything():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(lockdown=True,
                               access_overrides=[{"date": "2026-09-21",
                                                  "start": "09:00",
                                                  "end": "11:00"}]))
    assert policy.state(MONDAY) == sp.StateType.LOCKDOWN


def test_access_override_opens_request_gated_door():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(
        require_request=True,
        access_overrides=[{"date": "2026-09-21",
                           "start": "09:00", "end": "11:00"}],
    ))
    assert policy.state(MONDAY) == sp.StateType.OPEN       # override active
    assert policy.state(MONDAY.replace(hour=8)) == sp.StateType.RESTRICTED


def test_weekly_schedule_row_grants_weekday():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(weekly_schedule=[
        {"allowed": True, "weekday": 0, "start": "09:00:00", "end": "12:00:00"},
    ]))
    assert policy.state(MONDAY) == sp.StateType.WEEKDAY
    assert policy.state(MONDAY.replace(hour=13)) == sp.StateType.RESTRICTED


def test_holiday_overrides_weekly_row():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(
        holidays=["2026-09-21"],
        weekly_schedule=[
            {"allowed": True, "weekday": 0, "start": "09:00:00", "end": "12:00:00"},
        ],
    ))
    assert policy.state(MONDAY) == sp.StateType.HOLIDAY


def test_school_break_restricts_weekly_door():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(
        school_breaks=[{"start_date": "2026-09-20", "end_date": "2026-09-25"}],
        weekly_schedule=[
            {"allowed": True, "weekday": 0, "start": "09:00:00", "end": "12:00:00"},
        ],
    ))
    assert policy.state(MONDAY) == sp.StateType.RESTRICTED


def test_scheduled_lockdown_overrides_weekly_door():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(
        scheduled_lockdowns=[{"date": "2026-09-21",
                              "start": "09:00", "end": "12:00"}],
        weekly_schedule=[
            {"allowed": True, "weekday": 0, "start": "09:00:00", "end": "12:00:00"},
        ],
    ))
    assert policy.state(MONDAY) == sp.StateType.SCHEDULED_LOCKDOWN


def test_outside_validity_falls_through_to_default():
    """Weekly mode outside [schedule_start, schedule_end] = plain WEEKDAY,
    NOT RESTRICTED (the schedule is not in force yet)."""
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(
        schedule_start="2026-12-01",
        schedule_end=None,
        weekly_schedule=[
            {"allowed": True, "weekday": 0, "start": "09:00:00", "end": "12:00:00"},
        ],
    ))
    assert policy.state(MONDAY) == sp.StateType.WEEKDAY


def test_require_request_gates_default_door():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(require_request=True))
    assert policy.state(MONDAY) == sp.StateType.RESTRICTED


def test_scheduled_lockdown_without_weekly_mode():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(scheduled_lockdowns=[
        {"date": "2026-09-21", "start": "09:00", "end": "12:00"},
    ]))
    assert policy.state(MONDAY) == sp.StateType.SCHEDULED_LOCKDOWN
    assert policy.state(MONDAY.replace(hour=13)) == sp.StateType.WEEKDAY


def test_holiday_without_weekly_mode():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(holidays=["2026-09-21"]))
    assert policy.state(MONDAY) == sp.StateType.HOLIDAY


def test_sunday_is_weekend():
    policy = sp.SchedulePolicy(1)
    assert policy.state(SUNDAY) == sp.StateType.WEEKEND
    assert SUNDAY.weekday() == 6


def test_default_is_weekday():
    policy = sp.SchedulePolicy(1)
    assert policy.state(MONDAY) == sp.StateType.WEEKDAY


# ── Grant / deny ─────────────────────────────────────────────────────────────

def test_weekday_grants_member():
    policy = sp.SchedulePolicy(1)
    assert policy.is_granted("S1", MONDAY) is True


def test_open_override_grants():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(access_overrides=[
        {"date": "2026-09-21", "start": "09:00", "end": "11:00"}],
    ))
    assert policy.is_granted("S1", MONDAY) is True


def test_lockdown_never_grants():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(lockdown=True))
    assert policy.is_granted("S1", MONDAY) is False


def test_outside_run_window_denies():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(edge_run_start="08:00:00",
                               edge_run_end="17:00:00"))
    assert policy.is_granted("S1", MONDAY.replace(hour=20)) is False
    assert policy.deny_reason(MONDAY.replace(hour=20)) == "COOLDOWN"


def test_exception_grants_only_matching_member_and_window():
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle(
        require_request=True,
        exceptions=[{"member_id": "S1", "date": "2026-09-21",
                     "start": "09:00", "end": "12:00"}],
    ))
    assert policy.is_granted("S1", MONDAY) is True
    assert policy.is_granted("S2", MONDAY) is False
    assert policy.is_granted("S1", MONDAY.replace(hour=13)) is False
    assert policy.is_granted("S1", dt.datetime(2026, 9, 22, 10, 0)) is False


def test_deny_reason_matches_state():
    policy = sp.SchedulePolicy(1)
    # Real semantics: COOLDOWN outside the run window, otherwise the state
    # string. It is only ever called when a deny is already in progress (see
    # camera_worker), so it never needs to return None for grant states.
    assert policy.deny_reason(MONDAY) == "WEEKDAY"
    policy.update(_base_bundle(require_request=True))
    assert policy.deny_reason(MONDAY) == "RESTRICTED"
    policy.update(_base_bundle(lockdown=True))
    assert policy.deny_reason(MONDAY) == "LOCKDOWN"


# ── Persistence ──────────────────────────────────────────────────────────────

def test_update_persists_and_reloads(sandbox_paths):
    policy = sp.SchedulePolicy(1)
    bundle = _base_bundle(revision="qa:9")
    policy.update(bundle)
    assert (sandbox_paths / "schedule_bundle.json").exists()

    reloaded = sp.load_cached_bundle(1)
    assert reloaded.revision() == "qa:9"
    assert reloaded.is_empty() is False
    assert reloaded.bundle()["require_request"] is False


def test_corrupt_cache_falls_back_to_empty(sandbox_paths):
    (sandbox_paths / "schedule_bundle.json").write_text("{not-json!!")
    policy = sp.load_cached_bundle(1)
    assert policy.is_empty() is True
    assert policy.revision() == ""


def test_update_none_empties_bundle(sandbox_paths):
    policy = sp.SchedulePolicy(1)
    policy.update(_base_bundle())
    policy.update(None)
    assert policy.is_empty() is True