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
from core.cloud_client import CloudPermanentError

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

        # Drop record is copied back to _pending below; at the end the whole
        # still-failing tail is requeued behind the first failure — the OLD
        # code `break`ed after the first failure, silently LOSING every entry
        # that came after it in the batch.
        kept = []
        for i, entry in enumerate(batch):
            try:
                resp = self._cloud._request(
                    "POST", entry["endpoint"], entry["payload"],
                    retries=[1, 2], raise_on_4xx=True,
                )
            except CloudPermanentError as e:
                # Server definitively rejected this entry (4xx) — drop it, it
                # can never succeed. No head/tail ambiguity: 4xx is confirmed
                # rejection, unlike a plain network None below.
                print(f"[OfflineQueue] Dropping rejected {entry['endpoint']} "
                      f"(server 4xx: {e})")
                continue
            if resp is not None:
                print(f"[OfflineQueue] Delivered {entry['endpoint']} "
                      f"(kind={entry['kind']}, ts={entry['ts']})")
                continue

            # Retryable failure (network error / timeout / 5xx / 429 after
            # [1,2] retries) — requeue this entry AND every following one so
            # no queued event is lost mid-batch.
            entry["tries"] += 1
            if entry["tries"] >= cfg.QUEUE_MAX_TRIES:
                print(f"[OfflineQueue] Dropping {entry['endpoint']} after "
                      f"{entry['tries']} failed cycles")
                continue
            kept.append(entry)
            kept.extend(batch[i + 1:])
            break   # stop after the first failure — everything after is kept

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
