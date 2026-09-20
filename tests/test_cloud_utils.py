"""
tests/test_cloud_utils.py  — Tier 1: Cloud API surface + contract against the
mock cloud.

Pure helpers (_MAP_METHOD, _now) plus the CloudClient read/write contract
verified end-to-end against Testing/mock_cloud_server.py on a free port.
"""
import re

import pytest

import config as cfg
import Testing.mock_cloud_server as mcs
from core.cloud_client import _MAP_METHOD, _now


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_map_method_translates_rfid_to_card():
    assert _MAP_METHOD("rfid") == "card"
    assert _MAP_METHOD("face") == "face"
    assert _MAP_METHOD("spoof") == "spoof"
    assert _MAP_METHOD("hologram") == "hologram"   # unknown passes through


@pytest.mark.parametrize("offset,stamp", [
    (7, "+07:00"),
    (0, "+00:00"),
    (-5, "-05:00"),
])
def test_now_uses_fixed_utc_offset(monkeypatch, offset, stamp):
    import time as _time
    monkeypatch.setattr(cfg, "EDGE_UTC_OFFSET", offset)
    t = _now()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", t)
    assert t[-6:] == stamp
    expected = _time.strftime(
        "%Y-%m-%dT%H:%M:%S", _time.gmtime(_time.time() + offset * 3600))
    assert t[:19] == expected


# ── CloudClient contract vs mock ─────────────────────────────────────────────

def _enroll(student):
    ok, _ = mcs.enroll(student)
    return ok


def test_rooms_and_sync_status(cloud_client):
    assert cloud_client.get_rooms() == [{"id": 1, "name": "001", "online": False}]
    assert _enroll({"student_id": "S1", "name_en": "Ann",
                    "embedding": [0.1] * 512})
    status = cloud_client.get_sync_status()
    assert status["status"] == "ok"
    assert status["total_students"] == 1
    assert status["total_embeddings"] == 1
    assert status["roster_revision"] == 1


def test_register_door_and_heartbeat_show_online(cloud_client):
    _enroll({"student_id": "S1", "name_en": "Ann"})
    assert cloud_client.register_door("001", device="qa-device")["success"] is True
    cloud_client.report_heartbeat([{"room": "001", "online": True}])
    rooms = cloud_client.get_rooms()
    assert rooms[0]["online"] is True


def test_pagination_and_allowlist(cloud_client):
    for sid in ("S1", "S2", "S3", "S4", "S5"):
        assert _enroll({"student_id": sid, "name_en": sid,
                        "card_uid": f"card-{sid}", "rooms": ["001"]})
    students = cloud_client.get_students(per_page=2)
    assert students is not None and len(students) == 5

    allow = cloud_client.get_allowlist("001")
    assert allow is not None
    assert len(allow.strip().splitlines()) == 5

    # same roster revision (5 after 5 enrolls) → backend answers 304 → None
    assert cloud_client.get_allowlist("001", since="5") is None


def test_schedule_bundle_revision(cloud_client):
    bundle = cloud_client.get_schedule_bundle(1)
    assert bundle is not None
    assert bundle["revision"] == "0h1:mock"
    assert bundle["lockdown"] is False


def test_events_and_alerts_land_in_mock_log(cloud_client):
    resp = cloud_client.report_face_match("S1", "Ann", 0.91, room_id="001")
    assert resp is not None and resp.get("success") is True

    with mcs._lock:
        events = [p for (k, p) in mcs._event_log if k == "event"]
    assert events[0]["event"] == "access_granted"
    assert events[0]["method"] == "face"
    assert events[0]["student_id"] == "S1"
    assert events[0]["room"] == "001"
    assert events[0]["similarity"] == pytest.approx(0.91, abs=1e-3)

    resp = cloud_client.report_suspicious_alert(method="rfid", fail_count=3,
                                                card_uid="C99", room_id="001")
    assert resp is not None and resp.get("success") is True

    with mcs._lock:
        alerts = [p for (k, p) in mcs._event_log if k == "alert"]
    assert alerts[0]["method"] == "card"      # rfid → card mapping
    assert alerts[0]["fail_count"] == 3
    assert alerts[0]["card_uid"] == "C99"


def test_unknown_endpoint_is_404(cloud_client):
    import requests
    url = cfg.CLOUD_API_URL.rstrip("/") + "/api/edge/nope"
    assert requests.get(url, timeout=5).status_code == 404