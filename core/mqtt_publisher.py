"""
core/mqtt_publisher.py
Publishes door commands and events to the MQTT broker.
Auto-reconnects on disconnect.
"""
import json
import time
import threading
from typing import Optional

import paho.mqtt.client as mqtt

import config as cfg
from core.screen_state import state


class MQTTPublisher:
    def __init__(self):
        self._client = mqtt.Client(client_id=f"edge-{cfg.DOOR_ID}")
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._connected = False
        self._esp_online = None   # None = unknown, True/False from door/status
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reconnect_thread = None
        self._enroll_capture_cb = None
        self._access_denied_cb = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self):
        self._client.connect(cfg.MQTT_BROKER, cfg.MQTT_PORT, keepalive=60)
        # paho's loop_forever (started by loop_start) reconnects on its own —
        # a second manual reconnect thread would fight it and churn the session.
        self._client.loop_start()
        time.sleep(0.5)   # brief wait for on_connect to fire

        # Route incoming ESP32 messages (presence + access logs + enrol captures)
        self._client.on_message = self._on_message
        self._client.subscribe(cfg.MQTT_TOPIC_STATUS)
        self._client.subscribe(cfg.MQTT_TOPIC_ACCESS_LOG)
        self._client.subscribe(cfg.MQTT_TOPIC_ENROLL_CAPTURE)
        print(f"[MQTT] Subscribed to {cfg.MQTT_TOPIC_STATUS} (ESP32 presence)")
        print(f"[MQTT] Subscribed to {cfg.MQTT_TOPIC_ACCESS_LOG} (RFID events)")
        print(f"[MQTT] Subscribed to {cfg.MQTT_TOPIC_ENROLL_CAPTURE} (enrol captures)")

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        return self._connected

    def is_esp_online(self) -> Optional[bool]:
        """True while the ESP32 door controller is connected to the broker.

        Derived from the retained door/status message ("online"/"offline",
        also published by the broker via LWT when the ESP drops). None means
        the ESP has never been seen (no retained message yet).
        """
        return self._esp_online

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            print(f"[MQTT] Connected to {cfg.MQTT_BROKER}:{cfg.MQTT_PORT}")
        else:
            print(f"[MQTT] Connection failed rc={rc}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        print("[MQTT] Disconnected — reconnecting...")

    # ── ESP32 inbound messages ────────────────────────────────────────────────

    def set_enroll_capture_callback(self, callback):
        """Register a handler for `door/enroll/capture` (armed card captured)."""
        self._enroll_capture_cb = callback

    def set_access_denied_callback(self, callback):
        """Register a handler for denied ESP32 access logs (unknown cards).

        Called with the raw message dict, on the MQTT callback thread.
        """
        self._access_denied_cb = callback

    def _on_message(self, client, userdata, msg):
        topic = msg.topic

        # ESP32 presence — plain-text payload ("online"/"offline"), retained
        if topic == cfg.MQTT_TOPIC_STATUS:
            payload = msg.payload.decode("utf-8", "replace").strip().lower()
            online = payload == "online"
            with self._lock:
                changed = online != self._esp_online
                self._esp_online = online
            if changed:
                print(f"[MQTT] ESP32 {'ONLINE' if online else 'OFFLINE'}")
                state.add_event(f"ESP32 {'online' if online else 'offline'}", "info")
            return

        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return

        if topic == cfg.MQTT_TOPIC_ACCESS_LOG:
            self._on_access_log(data)
        elif topic == cfg.MQTT_TOPIC_ENROLL_CAPTURE:
            if self._enroll_capture_cb:
                self._enroll_capture_cb(data)

    def _on_access_log(self, data: dict):
        """ESP32 publishes door/access/log — forward RFID events to display state."""
        granted = bool(data.get("granted"))
        method = data.get("method", "rfid")

        if granted:
            name = data.get("name", "")
            pid = data.get("person_id", "")
            state.set_last_access({
                "name": name or pid, "person_id": pid, "method": method,
                "granted": True, "epoch": time.time(), "ts": _now(),
            })
            state.add_event(f"{name or pid} via {method}", "ok")
        else:
            uid = data.get("card_uid", "?")
            state.set_last_access({
                "method": method, "granted": False, "reason": "unknown_card",
                "epoch": time.time(), "ts": _now(),
            })
            state.add_event(f"Unknown card {uid}", "deny")
            # Hand off to the edge (Cloud event + alert) off this callback
            if self._access_denied_cb:
                self._access_denied_cb(data)

    # ── Publish helpers ───────────────────────────────────────────────────────

    def publish_unlock(self, person_id: str, name: str, similarity: float):
        payload = {
            "door_id":    cfg.DOOR_ID,
            "person_id":  person_id,
            "name":       name,
            "similarity": round(similarity, 4),
            "method":     "face",
            "timestamp":  _now(),
        }
        self._publish(cfg.MQTT_TOPIC_UNLOCK, payload)
        print(f"[MQTT] UNLOCK  → {name} ({person_id})  sim={similarity:.3f}")

    def publish_denied(self, reason: str, extra: Optional[dict] = None):
        payload = {
            "door_id":   cfg.DOOR_ID,
            "reason":    reason,
            "timestamp": _now(),
        }
        if extra:
            payload.update(extra)
        self._publish(cfg.MQTT_TOPIC_DENIED, payload)
        print(f"[MQTT] DENIED  → {reason}")

    def publish_health(self, enrolled_count: int):
        payload = {
            "door_id":        cfg.DOOR_ID,
            "enrolled_faces": enrolled_count,
            "timestamp":      _now(),
        }
        self._publish(cfg.MQTT_TOPIC_HEALTH, payload)

    def publish_card_uid_sync_full(self, cards: list):
        """
        Publish full card_uid sync to ESP32.
        cards: [{"card_uid": "...", "person_id": "...", "name": "..."}, ...]
        """
        payload = {
            "type": "full_sync",
            "cards": cards,
            "timestamp": _now()
        }
        self._publish(cfg.MQTT_TOPIC_SYNC_CARD_UID, payload)
        print(f"[MQTT] CARD_SYNC → {len(cards)} cards sent to ESP32")

    def publish_card_uid_sync_add(self, card_uid: str, person_id: str, name: str):
        """Publish single card_uid add to ESP32."""
        payload = {
            "type": "add",
            "card_uid": card_uid,
            "person_id": person_id,
            "name": name,
            "timestamp": _now()
        }
        self._publish(cfg.MQTT_TOPIC_SYNC_CARD_UID, payload)
        print(f"[MQTT] CARD_ADD → {card_uid} for {name}")

    def publish_card_uid_sync_delete(self, card_uid: str):
        """Publish single card_uid delete to ESP32."""
        payload = {
            "type": "delete",
            "card_uid": card_uid,
            "timestamp": _now()
        }
        self._publish(cfg.MQTT_TOPIC_SYNC_CARD_UID, payload)
        print(f"[MQTT] CARD_DELETE → {card_uid}")

    def publish_warning_alert(self, method: str, fail_count: int,
                              door_id: str = None):
        """Publish warning alert to ESP32 for local alarm."""
        door_id = door_id or cfg.DOOR_ID
        payload = {
            "type": "warning",
            "door_id": door_id,
            "reason": "suspicious_access",
            "method": method,
            "fail_count": fail_count,
            "timestamp": _now()
        }
        self._publish(cfg.MQTT_TOPIC_ALERT_WARNING, payload)
        print(f"[MQTT] WARNING_ALERT → method={method}, count={fail_count}")

    def publish_enroll_cmd(self, arm: bool, timeout_s: int, door_id: str = None):
        """Arm/disarm the ESP32's enrolment mode (door/cmd/enroll)."""
        door_id = door_id or cfg.DOOR_ID
        payload = {
            "enroll": bool(arm),
            "timeoutS": int(timeout_s),
            "door_id": door_id,
            "timestamp": _now(),
        }
        self._publish(cfg.MQTT_TOPIC_ENROLL_CMD, payload)
        print(f"[MQTT] ENROLL_CMD → arm={arm}, timeoutS={timeout_s}")

    def publish_alert_cleared(self, reason: str = "access_granted",
                              door_id: str = None):
        """Publish alert cleared to ESP32."""
        door_id = door_id or cfg.DOOR_ID
        payload = {
            "type": "alert_cleared",
            "door_id": door_id,
            "reason": reason,
            "timestamp": _now()
        }
        self._publish(cfg.MQTT_TOPIC_ALERT_WARNING, payload)
        print(f"[MQTT] ALERT_CLEARED → reason={reason}")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _publish(self, topic: str, payload: dict):
        with self._lock:
            self._client.publish(topic, json.dumps(payload), qos=1)


def _now() -> str:
    """Local wall-clock at EDGE_UTC_OFFSET, labeled with the same offset."""
    offset = int(getattr(cfg, "EDGE_UTC_OFFSET", 7))
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.gmtime(time.time() + offset * 3600))
    sign = "+" if offset >= 0 else "-"
    return f"{stamp}{sign}{abs(offset):02d}:00"
