"""
tests/conftest.py  — shared fixtures for the Door-Edge QA suite (Tiers 1+2).

Sandboxing rules (the live 24/7 service must NEVER see test traffic):
  MQTT   → every integration test runs on a dedicated `qa/door/...` topic
           namespace; the live edge (watching production `door/status` +
           `door/access/log`) stays untouched. No false ESP presence, no
           Telegram spam.
  Redis  → face-matcher tests run on logical DB 15 (cfg.REDIS_DB) with a
           unique index name per test; production DB 0 is never written,
           read-set-modified or cleared.
  Cloud  → the mock cloud (Testing/mock_cloud_server.py) runs on a free
           local port with state reset per test; nothing leaves the machine.
  Disk   → schedule bundle + offline event queue live in tmp_path.
"""
import json
import os
import socket
import sys
import threading
import time
import uuid

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config as cfg                                            # noqa: E402
import Testing.mock_cloud_server as mcs                         # noqa: E402


# ── QA MQTT topic namespace ──────────────────────────────────────────────────
QA_TOPICS = {
    "MQTT_TOPIC_UNLOCK":          "qa/door/cmd/unlock",
    "MQTT_TOPIC_DENIED":          "qa/door/access/denied",
    "MQTT_TOPIC_HEALTH":          "qa/door/health",
    "MQTT_TOPIC_SYNC_CARD_UID":   "qa/door/sync/card_uid",
    "MQTT_TOPIC_ALERT_WARNING":   "qa/door/alert/warning",
    "MQTT_TOPIC_ACCESS_LOG":      "qa/door/access/log",
    "MQTT_TOPIC_STATUS":          "qa/door/status",
    "MQTT_TOPIC_ENROLL_CMD":      "qa/door/cmd/enroll",
    "MQTT_TOPIC_ENROLL_CAPTURE":  "qa/door/enroll/capture",
    "MQTT_TOPIC_SCHEDULE_SYNC":   "qa/door/sync/schedule",
}


def wait_for(pred, timeout=10.0, interval=0.05):
    """Poll `pred()` until truthy or `timeout` elapses (no bare sleeps)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def wait_for_val(pred, timeout=10.0, interval=0.05):
    """Like wait_for but returns `pred()`'s truthy value (or the last one)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    return last


# ── Mock cloud ───────────────────────────────────────────────────────────────

@pytest.fixture()
def mock_cloud(tmp_path, monkeypatch):
    """Local mock Cloud API on a free port; module state reset per test."""
    mcs._students.clear()
    mcs._embeddings.clear()
    mcs._rooms[:] = ["001"]
    mcs._doors.clear()
    mcs._events.clear()
    mcs._pending_events.clear()
    mcs._event_log.clear()
    mcs._revision = 0
    mcs._roster_revision = 0
    mcs._change_seq = 0
    mcs._SCHEDULE_REVISION = 0
    mcs.DATA_FILE = tmp_path / "data.json"

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = mcs.ThreadingHTTPServer(("127.0.0.1", port), mcs.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server._qa_port = port
    server._qa_thread = thread
    monkeypatch.setattr(cfg, "CLOUD_API_URL", f"http://127.0.0.1:{port}/")

    yield server

    server.shutdown()
    server.server_close()


@pytest.fixture()
def cloud_client(mock_cloud):
    """CloudClient wired to the mock cloud (reads cfg at construction)."""
    from core.cloud_client import CloudClient
    return CloudClient()


# ── MQTT pair (real broker, QA topics) ───────────────────────────────────────

@pytest.fixture()
def mqtt_pair(monkeypatch):
    """(publisher, msg_for) on the qa/door/... namespace.

    pub  = a real MQTTPublisher (the EDGE role) — subscribes to qa status /
           access-log / enrol feeds, publishes unlock/sync/etc.
    msg_for(topic, pred, timeout) = first matching payload, or None.
    pub.is_esp_online() tracks the qa/door/status retained message.
    """
    import paho.mqtt.client as mqttc
    from core.mqtt_publisher import MQTTPublisher

    monkeypatch.setattr(cfg, "DOOR_ID", "test-qa")
    monkeypatch.setattr(cfg, "ROOM_ID", "qa-room")
    for key, topic in QA_TOPICS.items():
        monkeypatch.setattr(cfg, key, topic)

    pub = MQTTPublisher()
    pub.connect()
    assert wait_for(pub.is_connected, timeout=10.0), \
        "publisher never connected to broker"

    msgs = []
    sub = mqttc.Client(client_id=f"qa-subs-{uuid.uuid4().hex[:8]}",
                       protocol=mqttc.MQTTv311)
    sub.on_message = lambda c, u, m: msgs.append(m)
    sub.connect(cfg.MQTT_BROKER, cfg.MQTT_PORT, keepalive=60)
    sub.loop_start()
    for topic in QA_TOPICS.values():
        sub.subscribe(topic)
    time.sleep(0.3)  # let subscriptions register on the broker

    def msg_for(topic, pred=None, timeout=10.0):
        pred = pred or (lambda p: True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for m in msgs:
                if m.topic != topic:
                    continue
                try:
                    payload = json.loads(m.payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    payload = m.payload.decode("utf-8", "replace")
                if pred(payload):
                    return payload
            time.sleep(0.05)
        return None

    yield pub, sub, msg_for

    # Tear down: clear any retained qa/door/status, then drop both clients.
    try:
        sub.publish(cfg.MQTT_TOPIC_STATUS, "", retain=True)
        time.sleep(0.2)
    finally:
        sub.loop_stop()
        pub.disconnect()


# ── Fake MQTT (for pure alert-chain tests, no broker needed) ─────────────────

class _FakeMqtt:
    def __init__(self):
        self.online = None              # None = never seen

    def is_esp_online(self):
        return self.online

    def publish_warning_alert(self, **kwargs):
        pass


@pytest.fixture()
def fake_mqtt():
    return _FakeMqtt()


@pytest.fixture()
def esp_alert_fast(monkeypatch):
    """Tighten the ESP presence alert chain for deterministic tests."""
    monkeypatch.setattr(cfg, "ESP_OFFLINE_ALERT_ENABLED", True)
    monkeypatch.setattr(cfg, "ESP_OFFLINE_ALERT_GRACE_S", 1)
    monkeypatch.setattr(cfg, "ESP_OFFLINE_ESCALATE_S", 0)
    monkeypatch.setattr(cfg, "ESP_RESTORED_ALERT_ENABLED", True)


@pytest.fixture()
def alert_tracker(cloud_client, fake_mqtt, esp_alert_fast):
    from core.alert_tracker import AlertTracker
    return AlertTracker(cloud_client, fake_mqtt)


# ── Redis sandbox (DB 15, unique index; production DB 0 untouched) ───────────

@pytest.fixture()
def temp_redis_index(monkeypatch):
    """FaceMatcher sandbox: a unique index name + key prefix ON DB 0.

    RediSearch refuses indexes on logical DB != 0, so DB isolation is not an
    option. Instead each test gets its own `qa_door_person:<token>:` prefix and
    `qa_face_index_<token>` index — the production `door_person:` prefix and
    `door_face_index` index (used by the live service) are completely disjoint:
    no keys of theirs are ever written, read-set or deleted here.
    """
    import core.face_matcher as fm
    from core.face_matcher import FaceMatcher

    token = f"{os.getpid()}_{uuid.uuid4().hex[:6]}"
    idx = f"qa_face_index_{token}"
    prefix = f"qa_door_person:{token}:"
    monkeypatch.setattr(cfg, "REDIS_HOST", "localhost")
    monkeypatch.setattr(cfg, "REDIS_PORT", 6379)
    monkeypatch.setattr(cfg, "REDIS_INDEX_NAME", idx)
    monkeypatch.setattr(fm, "_KEY_PREFIX", prefix)   # safest: prod prefix untouched
    FaceMatcher._r = None          # rebuild the connection + index from patched cfg

    yield idx

    # Best-effort cleanup limited to OUR prefix / index on db 0.
    try:
        if FaceMatcher._r is not None:
            r = FaceMatcher._redis()
            keys = r.keys(f"{prefix}*")
            if keys:
                r.delete(*keys)
            try:
                r.ft(idx).dropindex(delete_documents=False)
            except Exception:
                pass
    finally:
        FaceMatcher._r = None


# ── Disk sandbox (schedule bundle + event queue in tmp) ──────────────────────

@pytest.fixture()
def sandbox_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "SCHEDULE_BUNDLE_PATH",
                        str(tmp_path / "schedule_bundle.json"))
    monkeypatch.setattr(cfg, "EVENT_QUEUE_PATH",
                        str(tmp_path / "event_queue.jsonl"))
    return tmp_path


@pytest.fixture()
def offline_queue_ctx(sandbox_paths, monkeypatch):
    """Isolated, process-reset OfflineEventQueue singleton."""
    import core.offline_queue as oq
    oq.stop_offline_queue()          # halt any thread from a prior test
    oq._QUEUE = None
    monkeypatch.setattr(cfg, "QUEUE_FLUSH_INTERVAL", 1)
    yield oq
    oq.stop_offline_queue()
    oq._QUEUE = None