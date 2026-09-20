"""
tests/test_integration.py  — Tier 2: sandboxed integration.

Real MQTT broker (qa/door/... namespace), real Redis (DB 15), mock Cloud API
on a free port. Nothing here touches the production topics, production Redis
DB 0, or the real Cloud.
"""
import json
import time

import numpy as np
import pytest

import config as cfg
import Testing.mock_cloud_server as mcs

from conftest import wait_for, wait_for_val


# ── mock-cloud log helpers ───────────────────────────────────────────────────

def _log_payloads(kind, **fields):
    with mcs._lock:
        entries = list(mcs._event_log)
    out = []
    for k, p in entries:
        if k != kind:
            continue
        if all(p.get(fk) == fv for fk, fv in fields.items()):
            out.append(p)
    return out


def _alert_count(method):
    return len(_log_payloads("alert", method=method))


# ── ESP presence alert chain (real broker) ───────────────────────────────────

def test_esp_state_alert_chain_via_broker(mqtt_pair, cloud_client, esp_alert_fast):
    pub, sub, msg_for = mqtt_pair
    from core.alert_tracker import AlertTracker
    tracker = AlertTracker(cloud_client, pub)
    pub.set_esp_offline_callback(tracker.record_esp_offline)
    pub.set_esp_online_callback(tracker.record_esp_restored)

    # ESP comes online (retained door/status) → edge sees it
    sub.publish(cfg.MQTT_TOPIC_STATUS, "online", retain=True)
    assert wait_for(lambda: pub.is_esp_online() is True, timeout=8)

    # ESP drops → edge starts the offline chain
    sub.publish(cfg.MQTT_TOPIC_STATUS, "offline", retain=True)
    assert wait_for(lambda: pub.is_esp_online() is False, timeout=8)

    # grace (1s) still-offline → esp_offline /alert to Cloud
    assert wait_for(lambda: _alert_count("esp_offline") == 1, timeout=8)

    # ESP back → esp_online "restored" /alert (only because one had fired)
    sub.publish(cfg.MQTT_TOPIC_STATUS, "online", retain=True)
    assert wait_for(lambda: _alert_count("esp_online") == 1, timeout=8)


def test_watchdog_never_seen_alerts_but_dedupes_chain(alert_tracker):
    tracker = alert_tracker
    assert tracker._mqtt.is_esp_online() is None   # never seen → watchdog path

    tracker.record_esp_offline()
    tracker.record_esp_offline()                   # duplicate within grace → ignored
    assert wait_for(lambda: _alert_count("esp_offline") == 1, timeout=8)

    tracker.record_esp_offline()                   # new chain after the fire → re-arms
    assert wait_for(lambda: _alert_count("esp_offline") == 2, timeout=8)


def test_offline_chain_cancelled_by_reconnect(alert_tracker):
    tracker = alert_tracker
    tracker.record_esp_offline()
    tracker._mqtt.online = True    # device back before the 1s grace elapses
    assert wait_for(lambda: _alert_count("esp_offline") == 1, timeout=3) is False
    time.sleep(1.5)                # total > grace: timer ran, saw online, cancelled
    assert _alert_count("esp_offline") == 0


# ── ESP32 sync payloads (edge → ESP) ─────────────────────────────────────────

def test_card_sync_payloads(mqtt_pair):
    pub, sub, msg_for = mqtt_pair
    pub.publish_card_uid_sync_full([
        {"card_uid": "AA11", "person_id": "S1", "name": "One"},
    ])
    got = msg_for(cfg.MQTT_TOPIC_SYNC_CARD_UID,
                  lambda p: p.get("type") == "full_sync")
    assert got and got["cards"] == \
        [{"card_uid": "AA11", "person_id": "S1", "name": "One"}]

    pub.publish_card_uid_sync_add("BB22", "S2", "Two")
    got = msg_for(cfg.MQTT_TOPIC_SYNC_CARD_UID,
                  lambda p: p.get("type") == "add")
    assert got and got["card_uid"] == "BB22" and got["person_id"] == "S2"

    pub.publish_card_uid_sync_delete("CC33")
    got = msg_for(cfg.MQTT_TOPIC_SYNC_CARD_UID,
                  lambda p: p.get("type") == "delete")
    assert got and got["card_uid"] == "CC33"


def test_schedule_sync_payload(mqtt_pair):
    pub, sub, msg_for = mqtt_pair
    bundle = {
        "revision": "qa:v7",
        "lockdown": True,
        "weekly_schedule": [
            {"weekday": 0, "start": "08:00", "end": "16:00", "allowed": True},
        ],
        "edge_run_start": "22:00:00",
        "edge_run_end": "06:00:00",
        "holidays": ["2026-09-25"],
    }
    pub.publish_schedule_sync(bundle)
    got = msg_for(cfg.MQTT_TOPIC_SCHEDULE_SYNC,
                  lambda p: p.get("type") == "full_sync")
    assert got is not None
    assert got["revision"] == "qa:v7"
    assert got["lockdown"] is True
    assert got["weekly_schedule"] == bundle["weekly_schedule"]
    assert got["edge_run_start"] == "22:00:00"
    assert got["edge_run_end"] == "06:00:00"
    assert got["holidays"] == ["2026-09-25"]


# ── ESP → edge access log forwarding (→ Cloud events + alerts) ───────────────

def test_access_log_forwarding(mqtt_pair, cloud_client):
    pub, sub, msg_for = mqtt_pair
    from core.alert_tracker import AlertTracker
    tracker = AlertTracker(cloud_client, pub)

    pub.set_rfid_granted_callback(lambda d: cloud_client.report_rfid_match(
        person_id=d.get("person_id", ""), name=d.get("name", ""),
        card_uid=d.get("card_uid", ""), room_id=cfg.ROOM_ID))
    pub.set_access_denied_callback(lambda d: (
        cloud_client.report_access_denied(reason="unknown_card", method="rfid",
                                          room_id=cfg.ROOM_ID,
                                          card_uid=d.get("card_uid", "")),
        tracker.record_unknown_card(d.get("card_uid", "?"))))

    # granted card → access_granted Cloud event (method rfid → card)
    sub.publish(cfg.MQTT_TOPIC_ACCESS_LOG, json.dumps({
        "granted": True, "person_id": "S001", "name": "Alice",
        "method": "rfid", "card_uid": "C111",
    }))
    assert wait_for(lambda: len(_log_payloads(
        "event", event="access_granted", method="card",
        student_id="S001", card_uid="C111")) == 1, timeout=8)

    # denied card → unknown_card Cloud event + Telegram /alert
    sub.publish(cfg.MQTT_TOPIC_ACCESS_LOG, json.dumps({
        "granted": False, "card_uid": "UNK-9", "method": "rfid",
    }))
    assert wait_for(lambda: len(_log_payloads(
        "event", event="unknown_card", method="card", card_uid="UNK-9")) == 1,
        timeout=8)
    assert wait_for(lambda: len(_log_payloads(
        "alert", method="card", card_uid="UNK-9")) == 1, timeout=8)


# ── Offline event queue ──────────────────────────────────────────────────────

def test_offline_queue_buffers_and_drains(offline_queue_ctx, cloud_client):
    oq = offline_queue_ctx
    queue = oq.start_offline_queue(cloud_client)

    cloud_client._base_url = "http://127.0.0.1:1/"     # dead cloud
    resp = cloud_client.post_event({"event": "access_granted", "room": "001"})
    assert resp is None
    # The flush thread briefly pulls the entry out of `_pending` (pending→0)
    # during its dead-cloud retry, then requeues it — poll until it settles.
    assert wait_for(lambda: queue.pending_count() == 1, timeout=10)

    # "cloud" restored → flush thread delivers the backlog in order
    cloud_client._base_url = cfg.CLOUD_API_URL
    assert wait_for(lambda: queue.pending_count() == 0 and len(
        _log_payloads("event", event="access_granted", room="001")) == 1,
        timeout=15)


def test_offline_queue_only_buffers_audit_endpoints(offline_queue_ctx):
    oq = offline_queue_ctx
    queue = oq.start_offline_queue(type("_C", (), {})())

    queue.enqueue("status", "/api/edge/doors/status",
                  {"room": "001", "online": True})     # NOT buffered
    assert queue.pending_count() == 0


def test_offline_queue_drops_permanent_reject(offline_queue_ctx):
    from core.cloud_client import CloudPermanentError
    oq = offline_queue_ctx

    class _RejectCloud:
        def _request(self, method, endpoint, payload,
                     retries=None, raise_on_4xx=False):
            raise CloudPermanentError(f"HTTP 400 for {endpoint}")

    queue = oq.OfflineEventQueue(_RejectCloud(), cfg.EVENT_QUEUE_PATH)
    queue.start()
    queue.enqueue("event", "/api/edge/events", {"event": "access_denied"})
    assert wait_for(lambda: queue.pending_count() == 0, timeout=8)
    queue.stop()


def test_offline_queue_flushes_in_order(offline_queue_ctx):
    oq = offline_queue_ctx
    delivered = []

    class _OkCloud:
        def _request(self, method, endpoint, payload,
                     retries=None, raise_on_4xx=False):
            delivered.append(endpoint)
            return {"success": True, "log_id": len(delivered)}

    queue = oq.OfflineEventQueue(_OkCloud(), cfg.EVENT_QUEUE_PATH)
    queue.start()
    queue.enqueue("event", "/api/edge/events", {"event": "access_granted"})
    queue.enqueue("alert", "/api/edge/alert", {"method": "face"})
    assert wait_for(lambda: queue.pending_count() == 0, timeout=8)
    queue.stop()
    assert delivered == ["/api/edge/events", "/api/edge/alert"]


# ── FaceMatcher (real Redis, sandboxed DB 15) ────────────────────────────────

def test_face_matcher_roundtrip(temp_redis_index):
    from core.face_matcher import FaceMatcher, FaceMatcherError

    rng = np.random.default_rng(7)
    v1 = rng.standard_normal(512)
    v1 /= np.linalg.norm(v1)
    v2 = rng.standard_normal(512)
    v2 /= np.linalg.norm(v2)

    FaceMatcher.upsert("T001:normal", "T001", "Tee", v1)
    FaceMatcher.upsert("T002:normal", "T002", "Bob", v2)
    assert FaceMatcher.count() == 2

    res = wait_for_val(
        lambda: _safe_search(FaceMatcher.search, v1), timeout=5)
    assert res and res["person_id"] == "T001"
    assert FaceMatcher.search(v2)["person_id"] == "T002"

    assert FaceMatcher.search(-v1) is None                 # far below threshold
    assert FaceMatcher.search(np.zeros(512)) is None       # degenerate probe

    # explicit threshold override
    assert FaceMatcher.search(v1, similarity_threshold=0.9999)["person_id"] == "T001"

    # NaN probe must never be accepted
    try:
        bad = FaceMatcher.search(np.full(512, np.nan))
    except FaceMatcherError:
        bad = None
    assert bad is None

    FaceMatcher.clear()
    assert FaceMatcher.count() == 0


def _safe_search(fn, vec):
    try:
        return fn(vec)
    except Exception:
        return None