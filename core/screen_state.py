"""
core/screen_state.py

Thread-safe shared state bridging the capture/inference threads
(camera_worker), the MQTT callback thread (mqtt_publisher) and the
screen display thread (screen_display).

  Inference thread  → writes detections, access results, events
  MQTT callback     → writes RFID access events (door/access/log)
  Display thread    → reads everything and renders
"""
import threading
import time
from collections import deque


class ScreenState:
    def __init__(self, event_limit: int = 20):
        self._lock = threading.Lock()

        # Camera frame (BGR) + sequence number for change detection
        self.frame = None
        self.frame_seq = 0

        # Inference results for overlay
        self.draw_cmds = []
        self.yolo_dets = []
        self.state = "idle"     # idle | confirming | unlocked
        self.confirm = 0

        # Latest hand detections (updated whenever the hand model runs)
        self.hand_dets = []

        # Access / event info
        self.last_access = None
        self.alert_active = False
        self.alert_text = ""
        self.events = deque(maxlen=event_limit)

        # Status flags
        self.mqtt_connected = False
        self.cloud_connected = False
        self.enrolled = 0
        self.ir_mode = False

    # ── Frame ────────────────────────────────────────────────────────────────

    def set_frame(self, frame):
        with self._lock:
            self.frame = frame
            self.frame_seq += 1

    def get_frame(self):
        with self._lock:
            frame = None if self.frame is None else self.frame.copy()
            return self.frame_seq, frame

    def get_frame_ref(self):
        """Read-only frame reference (NO copy) for consumers that only read
        the frame and never mutate it — e.g. the display, which copies before
        drawing. Saves one full-frame copy per consumer per tick."""
        with self._lock:
            return self.frame_seq, self.frame

    # ── Results ──────────────────────────────────────────────────────────────

    def set_results(self, draw_cmds, yolo_dets, state, confirm):
        with self._lock:
            self.draw_cmds = draw_cmds
            self.yolo_dets = yolo_dets
            self.state = state
            self.confirm = confirm

    def get_results(self):
        with self._lock:
            return list(self.draw_cmds), list(self.yolo_dets), self.state, self.confirm

    # ── Hand detections ────────────────────────────────────────────────────────

    def set_hand_dets(self, dets):
        with self._lock:
            self.hand_dets = dets

    def get_hand_dets(self):
        with self._lock:
            return list(self.hand_dets)

    # ── Access / alerts / events ─────────────────────────────────────────────

    def set_last_access(self, access: dict):
        with self._lock:
            self.last_access = access

    def set_alert(self, active: bool, text: str = ""):
        with self._lock:
            self.alert_active = active
            self.alert_text = text

    def add_event(self, text: str, kind: str = "ok"):
        with self._lock:
            ts = time.strftime("%H:%M:%S")
            self.events.appendleft((ts, text, kind))

    def get_panel(self):
        with self._lock:
            last = dict(self.last_access) if self.last_access else None
            return last, self.alert_active, self.alert_text, list(self.events)

    # ── Status flags ─────────────────────────────────────────────────────────

    def set_status(self, mqtt=None, cloud=None, enrolled=None):
        with self._lock:
            if mqtt is not None:
                self.mqtt_connected = mqtt
            if cloud is not None:
                self.cloud_connected = cloud
            if enrolled is not None:
                self.enrolled = enrolled

    def get_status(self):
        with self._lock:
            return self.mqtt_connected, self.cloud_connected, self.enrolled

    # ── IR Mode ────────────────────────────────────────────────────────────────

    def set_ir_mode(self, ir: bool):
        with self._lock:
            self.ir_mode = ir

    def get_ir_mode(self):
        with self._lock:
            return self.ir_mode


state = ScreenState()
