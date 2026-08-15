"""
core/offline_queue.py
Local event buffer for when the Cloud is unreachable.

Every event/alert that fails to POST to the Cloud is appended to a JSONL file
(EVENT_QUEUE_PATH) so it survives restarts, then a background thread re-tries
the pending entries in order every QUEUE_FLUSH_INTERVAL seconds. Once the
Cloud answers, the entry is removed; when the Cloud comes back the whole
backlog is delivered automatically.

Endpoints buffered: POST /api/edge/events and POST /api/edge/alert (the audit
trail). Heartbeats and roster/sync reads are NOT buffered — they are
transient by design.
"""
import json
import os
import threading
import time
from collections import deque
from typing import Optional

import config as cfg

_BUFFERED_ENDPOINTS = ("/api/edge/events", "/api/edge/alert")

_QUEUE = None
_QUEUE_LOCK = threading.Lock()


def get_offline_queue(cloud) -> "OfflineEventQueue":
    """Process-wide singleton queue (one flush thread, one backing file)."""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = OfflineEventQueue(cloud, cfg.EVENT_QUEUE_PATH)
        return _QUEUE


def start_offline_queue(cloud) -> "OfflineEventQueue":
    """Start the background flush thread (call once from edge_app)."""
    queue = get_offline_queue(cloud)
    queue.start()
    return queue


def stop_offline_queue():
    """Stop the flush thread, flushing any backlog one last time."""
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is not None:
            _QUEUE.stop()


class OfflineEventQueue:
    """Append-only JSONL queue with an in-order background flusher."""

    def __init__(self, cloud, path: str):
        self._cloud = cloud
        self._path = path
        self._pending = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._load()

    # ── Public API ──────────────────────────────────────────────────────────

    def enqueue(self, kind: str, endpoint: str, payload: dict):
        """Append an entry to the in-memory queue and the backing file."""
        if endpoint not in _BUFFERED_ENDPOINTS:
            return
        entry = {
            "kind": kind,
            "endpoint": endpoint,
            "payload": payload,
            "tries": 0,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with self._lock:
            if len(self._pending) >= cfg.QUEUE_MAX_ENTRIES:
                dropped = self._pending.popleft()
                print(f"[OfflineQueue] Queue full — dropped oldest "
                      f"{dropped.get('endpoint')} from {dropped.get('ts')}")
            self._pending.append(entry)
            self._persist_locked()
        if self._thread and self._thread.is_alive():
            self._wake.set()
        print(f"[OfflineQueue] Queued {kind} → {endpoint} "
              f"(pending={self.pending_count()})")

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="OfflineQueue"
        )
        self._thread.start()
        print(f"[OfflineQueue] Started — flush every "
              f"{cfg.QUEUE_FLUSH_INTERVAL}s, {self.pending_count()} pending")

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=cfg.QUEUE_FLUSH_INTERVAL + 5)

    # ── Flush loop ──────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop.is_set():
            self._wake.wait(cfg.QUEUE_FLUSH_INTERVAL)
            self._wake.clear()
            self._flush_once()
        self._flush_once()   # one last attempt on shutdown

    def _flush_once(self):
        with self._lock:
            if not self._pending:
                return
            batch = list(self._pending)
            self._pending.clear()

        kept = []
        sent_any = False
        for entry in batch:
            resp = self._cloud._request(
                "POST", entry["endpoint"], entry["payload"], retries=[1, 2]
            )
            if resp is not None:
                sent_any = True
                print(f"[OfflineQueue] Delivered {entry['endpoint']} "
                      f"(kind={entry['kind']}, ts={entry['ts']})")
                continue

            entry["tries"] += 1
            if sent_any:
                # Cloud is up (earlier entries went through) but rejects this
                # one — a permanent payload error; drop it, don't stall the queue.
                print(f"[OfflineQueue] Dropping rejected {entry['endpoint']} "
                      f"(server up, permanent error)")
                continue
            if entry["tries"] >= cfg.QUEUE_MAX_TRIES:
                print(f"[OfflineQueue] Dropping {entry['endpoint']} after "
                      f"{entry['tries']} failed cycles")
                continue
            kept.append(entry)
            break   # first failure in a dead-cloud cycle — stop here

        with self._lock:
            # Re-queue failures in front of anything newly enqueued (FIFO)
            self._pending.extendleft(reversed(kept))
            self._persist_locked()

    # ── Persistence ─────────────────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("endpoint") not in _BUFFERED_ENDPOINTS:
                        continue
                    if not isinstance(entry.get("payload"), dict):
                        continue
                    entry.setdefault("tries", 0)
                    self._pending.append(entry)
        except OSError as e:
            print(f"[OfflineQueue] Cannot read {self._path}: {e}")
            return
        print(f"[OfflineQueue] Loaded {len(self._pending)} pending "
              f"event(s) from {self._path}")

    def _persist_locked(self):
        """Rewrite the whole file atomically (write-then-rename)."""
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for entry in self._pending:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp, self._path)
        except OSError as e:
            print(f"[OfflineQueue] Cannot persist queue: {e}")
