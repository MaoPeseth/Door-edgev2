"""
core/alert_tracker.py
Warning Alert System for suspicious access attempts.

Tracks failed access attempts (unknown face, unknown RFID card)
and triggers alerts when threshold is reached.
"""
import time
import threading
from typing import Optional

import numpy as np

import config as cfg
from core.cloud_client import CloudClient
from core.mqtt_publisher import MQTTPublisher
from core.face_image import encode_face_image
from core.screen_state import state


class AlertTracker:
    """Track failed access attempts and trigger warning alerts."""

    def __init__(self, cloud_client: CloudClient, mqtt_publisher: MQTTPublisher):
        self._cloud = cloud_client
        self._mqtt = mqtt_publisher
        self._lock = threading.Lock()

        # Failed attempts tracking: {door_id: {key: attempt_data}}
        self._face_attempts = {}   # Track unknown faces
        self._card_attempts = {}   # Track unknown RFID cards

        # Doors with an active (alerted, not yet reset) incident
        self._alerted_doors = set()

        # Last unknown-face /alert time per door — enforces FACE_ALERT_MIN_INTERVAL
        # so a face incident that reset (success/timeout) can't immediately re-alert.
        self._last_face_alert = {}

        # Doors currently showing a spoof object — re-arms once the frame clears
        self._spoof_active = {}

        # Last spoof /alert time per door — enforces SPOOF_ALERT_MIN_INTERVAL
        # so a flickering spoof detection (frame drops and reappears) can't
        # re-fire Telegram alerts every episode.
        self._last_spoof_alert = {}

        # ESP32 status-alert chain (offline → grace → alert → escalate):
        # _esp_offline_timer holds the pending grace/escalation timer (None when
        # idle); _esp_offline_alerted means an esp_offline /alert has actually
        # fired, so a later restore can send the esp_online "back up" alert;
        # _esp_alert_count is how many offline alerts have fired (escalation #).
        self._esp_offline_timer = None
        self._esp_offline_alerted = False
        self._esp_alert_count = 0

        # Timestamps for timeout reset
        self._last_activity = time.time()

    def is_incident_active(self, door_id: str = None) -> bool:
        """True while an alert for this door is still pending (no reset yet)."""
        door_id = door_id or cfg.DOOR_ID
        with self._lock:
            return door_id in self._alerted_doors

    def is_face_alert_cooldown(self, door_id: str = None) -> bool:
        """True while the unknown-face alert is within FACE_ALERT_MIN_INTERVAL
        of the last fire — attempts are being ignored, so the door is silent."""
        door_id = door_id or cfg.DOOR_ID
        last = self._last_face_alert.get(door_id, 0.0)
        return time.time() - last < cfg.FACE_ALERT_MIN_INTERVAL

    def record_unknown_face(self, face_embedding: np.ndarray, frame=None,
                            face_crop: tuple = None, door_id: str = None,
                            warning_threshold: int = None) -> int:
        """
        Count consecutive hand-raised unknown-face attempts.

        One counter per door (not per embedding — embeddings drift frame to
        frame, which previously kept every counter stuck at 1). Each counted
        attempt returns its fail_count so the caller can post the /event with
        the attempt number. At WARNING_THRESHOLD the /alert fires and
        FACE_ALERT_MIN_INTERVAL applies: attempts are neither counted nor
        logged until it elapses, then the cycle starts fresh (the stale
        counter can never re-fire the alert). Returns 0 while silenced
        (cooldown) or when the attempt count did not change (still inside
        ATTEMPT_MIN_INTERVAL) — callers must not POST a /event for it.

        warning_threshold: override the default cfg.WARNING_THRESHOLD
        (e.g. use a lower threshold in IR mode so alerts fire sooner).
        """
        door_id = door_id or cfg.DOOR_ID
        warn_thr = warning_threshold if warning_threshold is not None else cfg.WARNING_THRESHOLD
        now = time.time()

        # Cooldown window: fully silent — attempts are not counted or logged.
        if self.is_face_alert_cooldown(door_id):
            return 0

        # Reset counters if timeout exceeded
        if now - self._last_activity > cfg.WARNING_RESET_TIMEOUT:
            self.reset_counters(door_id)

        self._last_activity = now

        with self._lock:
            # Cooldown expired since the last fire → fresh cycle, no refire
            # from a stale counter.
            if door_id in self._last_face_alert:
                self._face_attempts.pop(door_id, None)
                self._alerted_doors.discard(door_id)
                del self._last_face_alert[door_id]

            entry = self._face_attempts.get(door_id)
            if entry is None or now - entry["last_attempt"] >= cfg.ATTEMPT_MIN_INTERVAL:
                fail_count = (entry["fail_count"] + 1) if entry else 1
                self._face_attempts[door_id] = {
                    "fail_count": fail_count,
                    "first_attempt": entry["first_attempt"] if entry else now,
                    "last_attempt": now,
                }
                print(f"[Alert] Unknown face attempt #{fail_count} for {door_id}")
            else:
                return 0

            if fail_count >= warn_thr and door_id not in self._alerted_doors:
                self._alerted_doors.add(door_id)
                self._last_face_alert[door_id] = now
                self._trigger_alert(
                    door_id=door_id,
                    method="face",
                    fail_count=fail_count,
                    face_frame=frame,
                    face_crop=face_crop,
                )
            return fail_count

    def record_unknown_card(self, card_uid: str, door_id: str = None):
        """
        Record an unknown RFID card attempt.

        With CARD_ALERT_EVERY_ATTEMPT (default), EVERY tap triggers the alert
        immediately. Otherwise it accumulates to WARNING_THRESHOLD like faces.
        """
        door_id = door_id or cfg.DOOR_ID
        now = time.time()

        # Reset counters if timeout exceeded
        if now - self._last_activity > cfg.WARNING_RESET_TIMEOUT:
            self.reset_counters(door_id)

        self._last_activity = now

        with self._lock:
            if door_id not in self._card_attempts:
                self._card_attempts[door_id] = {}

            if card_uid not in self._card_attempts[door_id]:
                self._card_attempts[door_id][card_uid] = {
                    "fail_count": 1,
                    "first_attempt": now,
                    "last_attempt": now
                }
            else:
                if now - self._card_attempts[door_id][card_uid]["last_attempt"] >= cfg.ATTEMPT_MIN_INTERVAL:
                    self._card_attempts[door_id][card_uid]["fail_count"] += 1
                    self._card_attempts[door_id][card_uid]["last_attempt"] = now

            fail_count = self._card_attempts[door_id][card_uid]["fail_count"]
            print(f"[Alert] Unknown card {card_uid} attempt #{fail_count} for {door_id}")

            if cfg.CARD_ALERT_EVERY_ATTEMPT:
                # One Telegram alert per tap, no threshold, no incident lock
                self._trigger_alert(
                    door_id=door_id,
                    method="rfid",
                    fail_count=1,
                    card_uid=card_uid
                )
            elif fail_count >= cfg.WARNING_THRESHOLD and door_id not in self._alerted_doors:
                self._alerted_doors.add(door_id)
                self._trigger_alert(
                    door_id=door_id,
                    method="rfid",
                    fail_count=fail_count,
                    card_uid=card_uid
                )

    def record_spoof(self, door_id: str = None, face_frame=None):
        """Anti-spoof incident → its own /alert channel.

        Fires once per spoof episode; re-arms via spoof_clear() when the
        object leaves the frame. Fully decoupled from the unknown-face
        threshold/counters.
        """
        door_id = door_id or cfg.DOOR_ID
        now = time.time()
        with self._lock:
            if self._spoof_active.get(door_id):
                return
            if now - self._last_spoof_alert.get(door_id, 0.0) < cfg.SPOOF_ALERT_MIN_INTERVAL:
                return
            self._last_spoof_alert[door_id] = now
            self._spoof_active[door_id] = True
            self._trigger_alert(
                door_id=door_id,
                method="spoof",
                fail_count=1,
                face_frame=face_frame,
            )

    def spoof_clear(self, door_id: str = None):
        """Re-arm the spoof incident detector (no spoof object this frame)."""
        door_id = door_id or cfg.DOOR_ID
        with self._lock:
            self._spoof_active[door_id] = False

    def reset_counters(self, door_id: str = None):
        """Reset all failed attempt counters (called on successful access or timeout)."""
        with self._lock:
            if door_id:
                self._face_attempts.pop(door_id, None)
                self._card_attempts.pop(door_id, None)
                self._alerted_doors.discard(door_id)
                self._spoof_active.pop(door_id, None)
            else:
                self._face_attempts.clear()
                self._card_attempts.clear()
                self._alerted_doors.clear()
                self._spoof_active.clear()
            print(f"[Alert] Counters reset for {door_id or 'all doors'}")
            state.set_alert(False, "")

    def get_fail_count(self, door_id: str = None) -> dict:
        """Get current fail counts for a door."""
        door_id = door_id or cfg.DOOR_ID
        with self._lock:
            face = self._face_attempts.get(door_id)
            face_count = face.get("fail_count", 0) if face else 0
            card_count = len(self._card_attempts.get(door_id, {}))
            return {
                "face_attempts": face_count,
                "card_attempts": card_count,
                "total": face_count + card_count
            }

    # ── ESP32 status alerts (offline / restored → Telegram) ──────────────────

    def record_esp_offline(self):
        """ESP32 dropped (door/status → offline).

        Starts the grace timer: the /alert fires only if the device is STILL
        offline when it elapses (an ESP mid-reconnect reconnects within
        seconds and is not an incident), then re-fires every
        ESP_OFFLINE_ESCALATE_S while it stays down. A reconnect during the
        grace/escalation cancels the chain silently — no alert spam on flaps.
        """
        if not cfg.ESP_OFFLINE_ALERT_ENABLED:
            return
        with self._lock:
            if self._esp_offline_timer is not None:
                return  # a grace/escalation chain is already pending
            self._esp_alert_count = 0
            timer = threading.Timer(cfg.ESP_OFFLINE_ALERT_GRACE_S,
                                    self._esp_offline_check)
            timer.daemon = True
            self._esp_offline_timer = timer
        timer.start()

    def _esp_offline_check(self):
        """Timer fire: re-verify the ESP is still offline before alerting, then
        chain the next escalation if configured. Runs on a daemon timer thread."""
        if self._mqtt.is_esp_online():
            with self._lock:
                self._esp_offline_timer = None
            return  # came back during grace / since the last escalation

        escalate = cfg.ESP_OFFLINE_ESCALATE_S > 0
        with self._lock:
            self._esp_alert_count += 1
            self._esp_offline_alerted = True
            if escalate:
                timer = threading.Timer(cfg.ESP_OFFLINE_ESCALATE_S,
                                        self._esp_offline_check)
                timer.daemon = True
                self._esp_offline_timer = timer
            else:
                self._esp_offline_timer = None
            count = self._esp_alert_count

        self._post_status_alert("esp_offline", count)
        if escalate:
            timer.start()

    def record_esp_restored(self):
        """ESP32 came back (door/status → online). Cancel any pending offline
        alert/escalation chain and, only if an esp_offline alert had actually
        fired, notify Telegram that the device is back up."""
        with self._lock:
            timer = self._esp_offline_timer
            self._esp_offline_timer = None
            alerted = self._esp_offline_alerted
            self._esp_offline_alerted = False
            self._esp_alert_count = 0
        if timer is not None:
            timer.cancel()
        if cfg.ESP_RESTORED_ALERT_ENABLED and alerted:
            self._post_status_alert("esp_online")

    def _post_status_alert(self, method: str, fail_count: int = 0):
        """POST a device-status /alert to the Cloud (→ Telegram). No-throw,
        posted on its own daemon thread so it never blocks the caller."""

        def _send():
            try:
                self._cloud.report_suspicious_alert(method=method,
                                                    fail_count=fail_count)
            except Exception as e:
                print(f"[Cloud] {method} alert error: {e}")

        threading.Thread(target=_send, daemon=True, name="StatusAlert").start()

    def _trigger_alert(self, door_id: str, method: str, fail_count: int,
                       card_uid: str = None, face_frame=None, face_crop: tuple = None):
        """Trigger warning alert to Cloud and ESP32."""
        print(f"[Alert] TRIGGERED! method={method}, fail_count={fail_count}")

        # Send to Cloud (threshold crossing only — individual attempts go via
        # POST /api/edge/events from camera_worker). The face_image encode runs
        # off this thread — /alert is not on the door's critical path, but
        # encoding should never stall it.
        def _send_with_image():
            face_image = encode_face_image(face_frame, crop=face_crop) if face_frame is not None else None
            self._cloud.report_suspicious_alert(
                method=method,
                fail_count=fail_count,
                card_uid=card_uid,
                face_image=face_image,
            )

        threading.Thread(target=_send_with_image, daemon=True, name="AlertEvent").start()

        # Send to ESP32 (for local alarm/buzzer)
        self._mqtt.publish_warning_alert(
            method=method,
            fail_count=fail_count,
            door_id=door_id
        )

        # Show on screen display (banner)
        state.set_alert(True, f"{fail_count} unknown {method} attempt(s)")
