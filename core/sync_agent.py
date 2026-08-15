"""
core/sync_agent.py
Cloud-based sync agent for face embeddings and member data.

Replaces the old SQLite-based sync with Cloud API communication.

Sync Flow:
  1. On startup: GET /api/students + GET /api/embeddings/all
  2. Running: SSE via GET /api/edge/sync-stream — the stream emits a single
     `sync` event {revision, roster_revision} on connect and on any change;
     re-fetch whichever revision moved.
  3. On roster change: pull GET /api/edge/allowlist?room=<name> and push the
     card list to this room's ESP32 over MQTT.
  4. Events: POST /api/edge/events for door access logging
"""
import base64
import threading
import time
from typing import Optional

import numpy as np

import config as cfg
from core.cloud_client import CloudClient
from core.sse_client import SSEClient
from core.face_matcher import FaceMatcher
from core.mqtt_publisher import MQTTPublisher


class SyncAgent:
    """Cloud-based sync agent for Edge."""

    def __init__(self, mqtt_publisher: MQTTPublisher,
                 on_enroll=None, on_enroll_cancel=None):
        self._cloud = CloudClient()
        self._mqtt = mqtt_publisher
        self._on_enroll = on_enroll
        self._on_enroll_cancel = on_enroll_cancel
        self._sse = None
        self._stop = threading.Event()
        self._thread = None

        # Local caches
        self._students_cache = {}   # {student_id: student_data}
        self._card_uid_map = {}     # {card_uid: {student_id, name_en}}

        # Revision tracking (opaque strings — compare with !=, never >)
        self._local_revision = 0
        self._local_roster_revision = 0
        self._local_allowlist_revision = 0   # allowlist follows roster_revision

        # Per-room card allowlist cache: {room_name: [card_uid, ...]}
        self._allowlist_cache = {}

    def start(self):
        """Start sync agent with background threads."""
        self._stop.clear()

        # Start SSE client for real-time updates
        self._sse = SSEClient(
            on_sync=self._on_sync,
            on_enroll=self._on_enroll,
            on_enroll_cancel=self._on_enroll_cancel,
        )
        self._sse.start()

        # Start periodic consistency check thread
        self._thread = threading.Thread(target=self._consistency_loop, daemon=True, name="SyncAgent")
        self._thread.start()
        print("[Sync] Agent started")

    def stop(self):
        """Stop sync agent."""
        self._stop.set()
        if self._sse:
            self._sse.stop()
        if self._thread:
            self._thread.join(timeout=5)
        print("[Sync] Agent stopped")

    def sync_now(self):
        """Force an immediate sync (called from edge_app on startup)."""
        self._do_initial_sync()

    def get_students_cache(self) -> dict:
        """Get cached student data."""
        return self._students_cache.copy()

    def get_card_uid_map(self) -> dict:
        """Get card_uid to student mapping."""
        return self._card_uid_map.copy()

    def get_student_by_card(self, card_uid: str) -> Optional[dict]:
        """Look up student by card_uid."""
        return self._card_uid_map.get(card_uid)

    def is_connected(self) -> bool:
        """True while the SSE stream to Cloud is live."""
        return self._sse.is_connected() if self._sse else False

    # ── Initial Sync ─────────────────────────────────────────────────────────

    def _do_initial_sync(self):
        """Perform initial sync from Cloud API."""
        print("[Sync] Starting initial sync from Cloud...")

        # Step 1: Check connection
        status = self._cloud.get_sync_status()
        if not status:
            print("[Sync] Cannot connect to Cloud! Check network and Cloud server.")
            return

        print(f"[Sync] Cloud connected — revision={status.get('revision')}, "
              f"roster_revision={status.get('roster_revision')}")

        # Step 2: Download students
        students = self._cloud.get_students()
        if students:
            self._update_students_cache(students)
            print(f"[Sync] Loaded {len(students)} students from Cloud")
        else:
            print("[Sync] Failed to load students from Cloud")

        # Step 3: Download embeddings
        resp = self._cloud.get_embeddings()
        if resp:
            embeddings = resp.get("embeddings", [])
            encoding = resp.get("encoding", "floats")
            # The Cloud response is authoritative: clear the previous index
            # first so deletions on the Cloud are mirrored locally. An empty
            # Cloud empties the index too — without this, people removed on
            # the Cloud kept unlocking the door from a stale startup sync.
            FaceMatcher.clear()
            self._load_embeddings_to_redis(embeddings, encoding)
            print(f"[Sync] Loaded {len(embeddings)} embeddings to Redis")
        else:
            print("[Sync] Failed to load embeddings from Cloud")

        # Update local revisions
        self._local_revision = status.get("revision", 0)
        self._local_roster_revision = status.get("roster_revision", 0)

        # Step 4: Sync card allowlist to ESP32 (uses roster_revision as `since`)
        self._sync_card_uid_to_esp32()

        total = FaceMatcher.count()
        print(f"[Sync] Initial sync complete — {total} faces in Redis")

    def _update_students_cache(self, students: list):
        """Update local students cache and card_uid mapping.

        NOTE: `all_rooms: true` means access everywhere and `rooms` comes back
        `[]` — reading that as "no access" denies the wrong people. Keep the
        flag and the room list both.
        """
        self._students_cache.clear()
        self._card_uid_map.clear()

        for student in students:
            sid = student.get("student_id")
            if sid:
                self._students_cache[sid] = student

                # Build card_uid mapping
                card_uid = student.get("card_uid")
                if card_uid:
                    self._card_uid_map[card_uid] = {
                        "student_id": sid,
                        "name_en": student.get("name_en", ""),
                        "all_rooms": bool(student.get("all_rooms", False)),
                        "rooms": student.get("rooms") or [],
                    }

    def _load_embeddings_to_redis(self, embeddings: list, encoding: str = "floats"):
        """Load face embeddings into Redis HNSW index.

        One student may have several variants; each variant is indexed under
        its own `key` (e.g. "T002:normal") but stores the student's person_id,
        so a search hit maps back to the right person.
        """
        loaded = 0
        for item in embeddings:
            student_id = item.get("student_id")
            name = item.get("name_en") or item.get("name", "")
            key = item.get("key") or student_id
            raw = item.get("embedding")

            if not student_id or raw is None:
                continue

            try:
                embedding = _to_vector(raw, encoding)
            except ValueError as e:
                print(f"[Sync] Skipping {student_id}: {e}")
                continue

            FaceMatcher.upsert(key, student_id, name, embedding)
            loaded += 1

    def _sync_card_uid_to_esp32(self):
        """
        Pull GET /api/edge/allowlist?room=<name> and push that room's card
        list to the ESP32 over MQTT (replaces the old {"action":"sync"}).
        `?since=<roster_revision>` answers 304 when unchanged.
        """
        if not cfg.ALLOWLIST_SYNC_ENABLED:
            return
        room = cfg.ROOM_ID
        since = self._local_allowlist_revision or None

        text = self._cloud.get_allowlist(room, since=since)
        if text is None:
            if since:
                print(f"[Sync] Allowlist unchanged for room={room} (304)")
            else:
                print(f"[Sync] Failed to fetch allowlist for room={room}")
            return

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        self._allowlist_cache[room] = lines
        self._local_allowlist_revision = self._local_roster_revision
        print(f"[Sync] Allowlist room={room}: {len(lines)} card(s)")

        # Enrich UIDs with names from the student roster; unknown UIDs still
        # unlock the door (the ESP32 matches on the UID itself).
        cards = []
        for uid in lines:
            info = self._card_uid_map.get(uid)
            if info:
                cards.append({
                    "card_uid": uid,
                    "person_id": info["student_id"],
                    "name": info["name_en"],
                })
            else:
                cards.append({"card_uid": uid, "person_id": "", "name": ""})

        if cards:
            self._mqtt.publish_card_uid_sync_full(cards)
        else:
            self._mqtt.publish_card_uid_sync_full([])

    # ── Background Consistency Check ─────────────────────────────────────────

    def _consistency_loop(self):
        """Periodically check for revision drift."""
        while not self._stop.wait(cfg.SYNC_CONSISTENCY_CHECK):
            self._check_consistency()

    def _check_consistency(self):
        """Check if local revisions match Cloud revisions."""
        try:
            status = self._cloud.get_sync_status()
            if not status:
                print("[Sync] Cannot check consistency — Cloud unreachable")
                return

            cloud_revision = status.get("revision", 0)
            cloud_roster = status.get("roster_revision", 0)

            if cloud_revision != self._local_revision:
                print(f"[Sync] Revision mismatch: local={self._local_revision}, cloud={cloud_revision}")
                self._resync_embeddings()

            if cloud_roster != self._local_roster_revision:
                print(f"[Sync] Roster mismatch: local={self._local_roster_revision}, cloud={cloud_roster}")
                self._resync_students()

        except Exception as e:
            print(f"[Sync] Consistency check error: {e}")

    def _resync_embeddings(self):
        """Re-sync all embeddings from Cloud."""
        resp = self._cloud.get_embeddings()
        if resp:
            embeddings = resp.get("embeddings", [])
            encoding = resp.get("encoding", "floats")
            FaceMatcher.clear()
            self._load_embeddings_to_redis(embeddings, encoding)
            status = self._cloud.get_sync_status()
            self._local_revision = status.get("revision", 0)
            print(f"[Sync] Re-synced {len(embeddings)} embeddings")

    def _resync_students(self):
        """Re-sync student data from Cloud."""
        students = self._cloud.get_students()
        if students:
            self._update_students_cache(students)
            status = self._cloud.get_sync_status()
            self._local_roster_revision = status.get("roster_revision", 0)
            self._sync_card_uid_to_esp32()
            print(f"[Sync] Re-synced {len(students)} students")

    # ── SSE Event Handlers ───────────────────────────────────────────────────

    def _on_sync(self, data: dict):
        """Handle `sync` SSE event: revisions changed → re-fetch whichever moved."""
        revision = data.get("revision")
        roster_revision = data.get("roster_revision")

        print(f"[Sync] SSE sync — revision={revision}, roster_revision={roster_revision}")

        if revision is not None and revision != self._local_revision:
            self._resync_embeddings()

        if roster_revision is not None and roster_revision != self._local_roster_revision:
            self._resync_students()


def _to_vector(value, encoding: str = "floats") -> np.ndarray:
    """Decode an embedding field from the Cloud into a 512-dim float32 vector.

    - encoding == "base64" (or the value is a str): base64-decoded bytes
    - otherwise: JSON array of floats
    Shape is validated AFTER decoding, never on the raw field.
    """
    if encoding == "base64" or isinstance(value, str):
        v = np.frombuffer(base64.b64decode(value), dtype=np.float32).copy()
    else:
        v = np.asarray(value, dtype=np.float32)

    if v.shape != (cfg.VECTOR_DIM,):
        raise ValueError(f"unexpected dim {v.shape}")
    return v
