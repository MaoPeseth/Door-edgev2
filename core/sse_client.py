"""
core/sse_client.py
SSE (Server-Sent Events) Client for real-time sync from Cloud.

The Cloud stream emits:
  - `sync`           → {revision, roster_revision, schedule_revision} on connect
                       (the on-connect sync carries all three), and later
                       `sync` events may carry only the key(s) that changed —
                       e.g. {"schedule_revision": …} on a policy change.
  - `enroll`         → {room, timeoutS} — arm a door for card enrolment
  - `enroll_cancel`  → {room} — disarm it
  - `heartbeat`      → keep-alive every 15 s of quiet

The sync contract is revision-based: on `sync`, the SyncAgent compares the
revision values against its cached ones and re-fetches whichever moved. There
are no per-record member/embedding events.
"""
import json
import threading
import time
from typing import Callable, Optional

import requests

import config as cfg


class SSEClient:
    """SSE client for real-time Cloud push notifications."""

    def __init__(self, on_sync: Callable = None,
                 on_enroll: Callable = None,
                 on_enroll_cancel: Callable = None):
        """
        Initialize SSE client.

        Args:
            on_sync: Called with one or more of
                     {"revision", "roster_revision", "schedule_revision"}
                     on every sync event (connect + changes). Note that a
                     change-only `sync` may carry just {"schedule_revision"}.
            on_enroll: Called with {"room", "timeoutS"} on `enroll` events.
            on_enroll_cancel: Called with {"room"} on `enroll_cancel` events.
        """
        self._base_url = cfg.CLOUD_API_URL
        self._api_key = cfg.CLOUD_API_KEY
        self._sse_url = cfg.CLOUD_SSE_URL

        self._on_sync = on_sync
        self._on_enroll = on_enroll
        self._on_enroll_cancel = on_enroll_cancel

        # Threading
        self._stop = threading.Event()
        self._thread = None
        self._connected = False

    def start(self):
        """Start SSE listener in background thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="SSEClient")
        self._thread.start()
        print("[SSE] Client started")

    def stop(self):
        """Stop SSE listener."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        print("[SSE] Client stopped")

    def is_connected(self) -> bool:
        """Check if SSE connection is active."""
        return self._connected

    def _run(self):
        """Main SSE loop with auto-reconnect."""
        while not self._stop.is_set():
            try:
                self._connect_and_listen()
            except Exception as e:
                print(f"[SSE] Connection error: {e}")

            if not self._stop.is_set():
                print(f"[SSE] Reconnecting in {cfg.SYNC_RETRY_DELAY}s...")
                self._stop.wait(cfg.SYNC_RETRY_DELAY)

    def _connect_and_listen(self):
        """Connect to SSE endpoint and listen for events."""
        headers = {"X-API-Key": self._api_key}

        try:
            response = requests.get(
                self._sse_url,
                headers=headers,
                stream=True,
                timeout=60
            )

            if response.status_code != 200:
                print(f"[SSE] HTTP {response.status_code}")
                return

            self._connected = True
            print(f"[SSE] Connected to {self._sse_url}")

            current_event = None
            current_data = None

            for line in response.iter_lines():
                if self._stop.is_set():
                    break

                if not line:
                    # Empty line = end of event
                    if current_event and current_data:
                        self._handle_event(current_event, current_data)
                    current_event = None
                    current_data = None
                    continue

                line = line.decode("utf-8")

                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    current_data = line[5:].strip()

            self._connected = False
            print("[SSE] Connection closed")

        except requests.exceptions.ConnectionError:
            self._connected = False
            print("[SSE] Connection failed")
        except Exception as e:
            self._connected = False
            print(f"[SSE] Error: {e}")

    def _handle_event(self, event_type: str, data_str: str):
        """Handle received SSE event."""
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            print(f"[SSE] Invalid JSON: {data_str}")
            return

        print(f"[SSE] Event: {event_type}")

        if event_type == "sync":
            if self._on_sync:
                self._on_sync(data)

        elif event_type == "enroll":
            if self._on_enroll:
                self._on_enroll(data)

        elif event_type == "enroll_cancel":
            if self._on_enroll_cancel:
                self._on_enroll_cancel(data)

        elif event_type == "heartbeat":
            # Keep-alive, no action needed
            pass

        else:
            print(f"[SSE] Unknown event type: {event_type}")
