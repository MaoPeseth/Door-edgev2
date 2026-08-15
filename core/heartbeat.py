"""
core/heartbeat.py
Door presence reporting for the Cloud.

- On startup: POST /api/edge/doors (register this door's device) and verify
  cfg.ROOM_ID appears in GET /api/edge/rooms — a wrong room name makes the
  allowlist fail closed to `all_rooms` members only.
- Every HEARTBEAT_INTERVAL (60 s): POST /api/edge/doors/status.
  The server's presence TTL is 180 s, so three missed beats = room offline.
"""
import threading
import time

import config as cfg
from core.cloud_client import CloudClient


class HeartbeatReporter:
    """Reports door presence and verifies the room name at startup."""

    def __init__(self, cloud_client: CloudClient = None):
        self._cloud = cloud_client or CloudClient()
        self._stop = threading.Event()
        self._thread = None

    def register(self) -> bool:
        """
        Startup registration: POST /api/edge/doors for this room's device.
        Verifies cfg.ROOM_ID against GET /api/edge/rooms first and warns
        (does not auto-change config) if it is missing.
        Returns True if the registration POST succeeded.
        """
        self._verify_room_name()

        ok = self._cloud.register_door(cfg.ROOM_ID, cfg.DEVICE_ID)
        if ok:
            print(f"[Heartbeat] Registered door room={cfg.ROOM_ID} device={cfg.DEVICE_ID}")
        else:
            print(f"[Heartbeat] WARNING: door registration failed for room={cfg.ROOM_ID}")
        return bool(ok)

    def _verify_room_name(self):
        """Check cfg.ROOM_ID against the room names the server knows."""
        rooms = self._cloud.get_rooms()
        if rooms is None:
            print("[Heartbeat] WARNING: cannot fetch /api/edge/rooms — "
                  f"cannot verify ROOM_ID='{cfg.ROOM_ID}'")
            return

        names = [r.get("name") for r in rooms if isinstance(r, dict)]
        if cfg.ROOM_ID not in names:
            print(f"[Heartbeat] WARNING: ROOM_ID='{cfg.ROOM_ID}' is NOT in "
                  f"the room list {names} — allowlist will fail closed to "
                  "all_rooms members only. Fix config.py ROOM_ID to match "
                  "the server's room name exactly.")
        else:
            print(f"[Heartbeat] Room '{cfg.ROOM_ID}' verified against /api/edge/rooms")

    def start(self):
        """Start the periodic presence thread (first beat goes out at once)."""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="Heartbeat"
        )
        self._thread.start()
        print(f"[Heartbeat] Started — status every {cfg.HEARTBEAT_INTERVAL}s")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        self._beat()   # immediate first beat
        while not self._stop.wait(cfg.HEARTBEAT_INTERVAL):
            self._beat()

    def _beat(self):
        ok = self._cloud.report_heartbeat([
            {"room": cfg.ROOM_ID, "online": True},
        ])
        if not ok:
            print(f"[Heartbeat] WARNING: status POST failed for room={cfg.ROOM_ID}")
