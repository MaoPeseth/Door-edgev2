"""
core/enroll_relay.py
Enrolment relay between the Cloud and the ESP32.

SSE `enroll` → arm the door: publish `door/cmd/enroll` {"enroll": true,
"timeoutS": N} so the ESP32 captures the next card instead of running its
normal access logic. `enroll_cancel` → disarm.

While armed, the ESP32 publishes the captured card on `door/enroll/capture`
(MQTT_TOPIC_ENROLL_CAPTURE) → we relay it with POST
/api/edge/doors/<room>/enroll/capture {"tagID": "..."} and disarm on success.
"""
import threading
import time

import config as cfg
from core.cloud_client import CloudClient
from core.mqtt_publisher import MQTTPublisher


class EnrollRelay:
    def __init__(self, cloud_client: CloudClient, mqtt_publisher: MQTTPublisher):
        self._cloud = cloud_client
        self._mqtt = mqtt_publisher
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._armed = False
        self._deadline = 0.0
        self._watchdog = None

        self._mqtt.set_enroll_capture_callback(self.on_capture)

    # ── Public API ──────────────────────────────────────────────────────────

    def on_enroll(self, data: dict):
        """SSE `enroll` event: {"room": "001", "timeoutS": 60} — arm this door."""
        room = data.get("room")
        if room is not None and str(room) != cfg.ROOM_ID:
            print(f"[Enroll] Enrolment for room '{room}' — not this door (this={cfg.ROOM_ID})")
            return
        timeout_s = int(data.get("timeoutS") or cfg.ENROLL_DEFAULT_TIMEOUT)
        self.arm(timeout_s)

    def on_enroll_cancel(self, data: dict = None):
        """SSE `enroll_cancel` event — disarm this door."""
        room = (data or {}).get("room")
        if room is not None and str(room) != cfg.ROOM_ID:
            print(f"[Enroll] Cancel for room '{room}' — not this door (this={cfg.ROOM_ID})")
            return
        self.disarm()

    def arm(self, timeout_s: int):
        with self._lock:
            self._armed = True
            self._deadline = time.time() + timeout_s
        self._mqtt.publish_enroll_cmd(True, timeout_s)
        print(f"[Enroll] Door armed for {timeout_s}s — waiting for card")
        self._start_watchdog(timeout_s)

    def disarm(self, reason: str = ""):
        with self._lock:
            was_armed = self._armed
            self._armed = False
            self._deadline = 0.0
        if was_armed:
            self._mqtt.publish_enroll_cmd(False, 0)
            print(f"[Enroll] Door disarmed{(' — ' + reason) if reason else ''}")

    def is_armed(self) -> bool:
        return self._armed

    # ── ESP32 capture (MQTT door/enroll/capture) ────────────────────────────

    def on_capture(self, data: dict):
        """Card captured while armed — relay to the Cloud, then disarm."""
        if not self._armed:
            print(f"[Enroll] Capture {data} ignored — door not armed")
            return
        tag_id = data.get("tagID") or data.get("card_uid") or ""
        if not tag_id:
            print(f"[Enroll] Capture ignored — no tagID in {data}")
            return
        print(f"[Enroll] Card captured: {tag_id} — relaying to Cloud")
        ok = self._cloud.enroll_capture(tag_id)
        if ok:
            self.disarm(reason="capture relayed")
        else:
            # Cloud unreachable — re-arm for another try
            print(f"[Enroll] Relay failed — card {tag_id} not accepted")

    # ── Watchdog: auto-disarm after timeout ─────────────────────────────────

    def _start_watchdog(self, timeout_s: int):
        if self._watchdog and self._watchdog.is_alive():
            return
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="EnrollWatchdog"
        )
        self._watchdog.start()

    def _watchdog_loop(self):
        while not self._stop.is_set():
            with self._lock:
                armed = self._armed
                remaining = self._deadline - time.time()
            if not armed:
                return
            if remaining <= 0:
                self.disarm(reason="timeout")
                return
            self._stop.wait(min(remaining, 5))

    def stop(self):
        self._stop.set()
        self.disarm(reason="shutdown")
