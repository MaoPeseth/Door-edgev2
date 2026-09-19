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
from core.schedule_policy import SchedulePolicy, load_cached_bundle


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
        self._local_schedule_revision = 0    # schedule bundle revision

        # Per-room card allowlist cache: {room_name: [card_uid, ...]}
        self._allowlist_cache = {}

        # Schedule policy (seeded from any cached bundle so the door enforces
        # while the Cloud is down; empty on a true first boot).
        self._policy = load_cached_bundle(int(getattr(cfg, "ROOM_ID", 0)) or 0)

        # When the ESP32 comes online, push the current schedule so it has the
        # latest data for offline enforcement.
        self._mqtt.set_esp_online_callback(self._on_esp_online)

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

    def policy(self) -> SchedulePolicy:
        """The thread-safe schedule policy (may be empty on first offline boot)."""
        return self._policy

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
              f"roster_revision={status.get('roster_revision')}, "
              f"schedule_revision={status.get('schedule_revision')}")

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

        # Step 3b: Sync schedule bundle (only when its revision moved)
        self._sync_schedule(status.get("schedule_revision"))

        # Step 4: Sync card allowlist to ESP32 (uses roster_revision as `since`)
        self._sync_card_uid_to_esp32()

        try:
            total = FaceMatcher.count()
        except Exception as e:
            # Redis down at first sync — don't let one outage kill the whole
            # sync thread (embeddings were already loaded; count is cosmetically 0).
            total = 0
            print(f"[Sync] Redis unavailable during initial sync: {e}")
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

    def _resolve_room_id(self) -> Optional[int]:
        """Map config.py ROOM_ID (room name) → integer PK via /api/edge/rooms.

        Returns None when the backend is missing ids or the name is unknown —
        schedule sync is then skipped (door keeps its previous behaviour).
        """
        mapping = self._cloud.get_room_ids()
        if not mapping:
            print("[Sync] Schedule sync disabled — no room id map from Cloud")
            return None
        rid = mapping.get(cfg.ROOM_ID)
        if rid is None:
            print(f"[Sync] Room {cfg.ROOM_ID!r} not found in /api/edge/rooms — "
                  f"schedule sync disabled")
            return None
        try:
            return int(rid)
        except (TypeError, ValueError):
            print(f"[Sync] Non-integer room id for {cfg.ROOM_ID!r}: {rid!r}")
            return None

    def _sync_schedule(self, cloud_schedule_revision):
        """Re-fetch the schedule bundle only when its revision moved.

        Compares the cloud's schedule_revision against the local one, resolves
        the room id, fetches the bundle, seeds the policy and persists it.
        """
        cloud_rev = cloud_schedule_revision if cloud_schedule_revision not in (None, 0) else None
        local_rev = self._local_schedule_revision or None

        if cloud_rev is not None and cloud_rev == local_rev:
            return

        room_id = self._resolve_room_id()
        if room_id is None:
            return

        bundle = self._cloud.get_schedule_bundle(room_id)
        if not bundle:
            print(f"[Sync] Failed to fetch schedule bundle for room={room_id} "
                  f"(keeping previous policy)")
            return

        self._policy.update(bundle)
        if cloud_rev is not None:
            self._local_schedule_revision = cloud_rev
        else:
            # Backend didn't report a schedule revision — track the bundle's own
            # revision so future comparisons are stable.
            self._local_schedule_revision = bundle.get("revision") or 0
        print(f"[Sync] Schedule bundle loaded (room={room_id}, "
              f"revision={self._local_schedule_revision})")

        # Push the updated schedule to ESP32 so it has the latest for offline enforcement.
        if self._mqtt.is_esp_online():
            self._mqtt.publish_schedule_sync(self._policy.bundle())

    def _on_esp_online(self):
        """Called when the ESP32 transitions to online — push current schedule
        and re-send the card allowlist (a late-connecting ESP would otherwise
        miss the one-shot startup push and keep an empty/stale tag table)."""
        bundle = self._policy.bundle()
        if bundle:
            self._mqtt.publish_schedule_sync(bundle)
            print("[Sync] Pushed schedule to ESP32 on reconnect")

        # Allowlist fetch is a Cloud call that can block — never stall the MQTT
        # loop thread on it.
        threading.Thread(target=self._sync_card_uid_to_esp32, daemon=True,
                         name="AllowlistOnReconnect").start()

    def _sync_card_uid_to_esp32(self):
        """
        Pull GET /api/edge/allowlist?room=<name> and push that room's card
        list to the ESP32 over MQTT (replaces the old {"action":"sync"}).
        `?since=<allowlist revision>` answers 304 when unchanged.
        """
        if not cfg.ALLOWLIST_SYNC_ENABLED:
            return
        room = cfg.ROOM_ID
        since = self._local_allowlist_revision or None

        text = self._cloud.get_allowlist(room, since=since)
        if text is None:
            if since:
                print(f"[Sync] Allowlist unchanged for room={room} (304)")
                # A previous sync already cached this room's cards — re-push
                # them to the ESP32 anyway. This function is also called on
                # every esp-online (reconnect); without this, a 304 would
                # early-return and the late/reconnecting ESP would keep an
                # empty/stale tag table until the next Cloud allowlist edit.
                if self._allowlist_cache.get(room):
                    self._publish_uids_to_esp(room)
                    print(f"[Sync] Re-pushed {len(self._allowlist_cache[room])} "
                          f"card(s) to ESP32 from cache")
            else:
                print(f"[Sync] Failed to fetch allowlist for room={room}")
            return

        # The Cloud returns a header block followed by one tab-separated row
        # per card:
        #   revision <revision>
        #   count <n>
        #   <card_uid>\t<student_id>\t<member_type>\t<flag>\t<name>
        # (an older backend serving bare card_uid lines parses the same way).
        # Take the first tab-delimited field as the UID and drop metadata
        # header lines (revision/count/empty) so only real UIDs reach the ESP.
        # Track the response's own `revision` so `since` compares against the
        # Cloud's allowlist revision namespace (not roster_revision) — a
        # card-UID-only edit (which may not bump roster_revision) changes the
        # 304 answer. Fall back to roster_revision when no revision header is
        # present (older backends / the mock server use that namespace).
        uids, seen = [], set()
        new_revision = self._local_roster_revision
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            token = line.split("\t", 1)[0].strip()
            first = token.split(" ", 1)[0].lower()
            if first in ("revision", "count"):
                if first == "revision":
                    new_revision = token.split(" ", 1)[1].strip() or new_revision
                continue
            if token and token not in seen:
                seen.add(token)
                uids.append(token)
        self._allowlist_cache[room] = uids
        self._local_allowlist_revision = new_revision
        print(f"[Sync] Allowlist room={room}: {len(uids)} card(s)")

        # Enrich UIDs with names from the student roster; unknown UIDs still
        # unlock the door (the ESP32 matches on the UID itself).
        self._publish_uids_to_esp(room)

    def _publish_uids_to_esp(self, room):
        """Push the current allowlist cache for `room` to the ESP32 (names
        enriched from the roster). Used after a fresh fetch AND on a 304
        (allowlist unchanged) to re-send the last known list to a
        late/reconnecting ESP32 that missed the one-shot startup push."""
        uids = self._allowlist_cache.get(room, [])
        cards = []
        for uid in uids:
            info = self._card_uid_map.get(uid)
            if info:
                cards.append({
                    "card_uid": uid,
                    "person_id": info["student_id"],
                    "name": info["name_en"],
                })
            else:
                cards.append({"card_uid": uid, "person_id": "", "name": ""})
        self._mqtt.publish_card_uid_sync_full(cards)

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
            cloud_schedule = status.get("schedule_revision", 0)

            if cloud_revision != self._local_revision:
                print(f"[Sync] Revision mismatch: local={self._local_revision}, cloud={cloud_revision}")
                self._resync_embeddings()

            if cloud_roster != self._local_roster_revision:
                print(f"[Sync] Roster mismatch: local={self._local_roster_revision}, cloud={cloud_roster}")
                self._resync_students()
            elif self._mqtt.is_esp_online():
                # A card-UID-only edit on the Cloud may not bump roster_revision
                # (the allowlist carries its own `revision`). Refresh periodically
                # so the ESP32 stays current without a roster change; the
                # allowlist revision as `since` keeps the refetch cheap (304).
                self._sync_card_uid_to_esp32()

            if cloud_schedule not in (None, 0) and cloud_schedule != self._local_schedule_revision:
                print(f"[Sync] Schedule mismatch: local={self._local_schedule_revision}, "
                      f"cloud={cloud_schedule}")
                self._sync_schedule(cloud_schedule)

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
        schedule_revision = data.get("schedule_revision")

        print(f"[Sync] SSE sync — revision={revision}, roster_revision={roster_revision}, "
              f"schedule_revision={schedule_revision}")

        if revision is not None and revision != self._local_revision:
            self._resync_embeddings()

        if roster_revision is not None and roster_revision != self._local_roster_revision:
            self._resync_students()

        if schedule_revision is not None and schedule_revision != self._local_schedule_revision:
            self._sync_schedule(schedule_revision)


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
