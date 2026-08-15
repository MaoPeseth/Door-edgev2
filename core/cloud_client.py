"""
core/cloud_client.py
HTTP Client for Cloud API communication.

Handles:
- GET /api/students           → Download member list
- GET /api/embeddings/all     → Download face vectors
- GET /api/edge/sync-status   → Check revision markers
- GET /api/edge/rooms         → Room names (presence verification)
- POST /api/edge/doors        → Register this door's device at startup
- POST /api/edge/doors/status → Periodic presence heartbeat
- GET /api/edge/allowlist     → Card allowlist for this room (plain text)
- POST /api/edge/events       → Report door events
- POST /api/edge/alert        → Report suspicious activity alerts
- POST /api/edge/doors/<room>/enroll/capture → Relay enrolled card
"""
import json
import time
from typing import Optional

import requests

import config as cfg


class CloudClient:
    """HTTP client for Cloud API communication."""

    def __init__(self):
        self._base_url = cfg.CLOUD_API_URL
        self._api_key = cfg.CLOUD_API_KEY
        self._headers = {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json"
        }
        self._timeout = 30  # seconds

    def _request(self, method: str, endpoint: str, data: dict = None,
                 retries: list = None) -> Optional[dict]:
        """
        Make HTTP request to Cloud API with retry logic.
        Returns response JSON or None on failure.
        retries: optional override of the default backoff schedule.
        """
        url = f"{self._base_url}{endpoint}"
        if retries is None:
            retries = [1, 2, 4, 8, 16, cfg.SYNC_RETRY_MAX]

        for attempt in retries:
            try:
                if method == "GET":
                    response = requests.get(
                        url,
                        headers=self._headers,
                        timeout=self._timeout
                    )
                elif method == "POST":
                    response = requests.post(
                        url,
                        headers=self._headers,
                        json=data,
                        timeout=self._timeout
                    )
                else:
                    print(f"[CloudClient] Unsupported method: {method}")
                    return None

                if response.status_code in (200, 201):
                    return response.json()
                else:
                    print(f"[CloudClient] HTTP {response.status_code}: {response.text}")
                    return None

            except requests.exceptions.ConnectionError:
                print(f"[CloudClient] Connection failed, retrying in {attempt}s...")
                time.sleep(attempt)
            except requests.exceptions.Timeout:
                print(f"[CloudClient] Request timed out, retrying in {attempt}s...")
                time.sleep(attempt)
            except Exception as e:
                print(f"[CloudClient] Error: {e}")
                return None

        print(f"[CloudClient] All retries failed for {endpoint}")
        return None

    def _request_raw(self, method: str, endpoint: str) -> Optional[str]:
        """
        Like _request but for plain-text responses (allowlist).
        Returns the body text on 200, None on 304 (unchanged) or failure.
        """
        url = f"{self._base_url}{endpoint}"
        retries = [1, 2, 4, 8, 16, cfg.SYNC_RETRY_MAX]

        for attempt in retries:
            try:
                response = requests.get(
                    url,
                    headers=self._headers,
                    timeout=self._timeout
                )
                if response.status_code == 200:
                    return response.text
                if response.status_code == 304:
                    return None
                print(f"[CloudClient] HTTP {response.status_code}: {response.text}")
                return None
            except requests.exceptions.ConnectionError:
                print(f"[CloudClient] Connection failed, retrying in {attempt}s...")
                time.sleep(attempt)
            except requests.exceptions.Timeout:
                print(f"[CloudClient] Request timed out, retrying in {attempt}s...")
                time.sleep(attempt)
            except Exception as e:
                print(f"[CloudClient] Error: {e}")
                return None

        print(f"[CloudClient] All retries failed for {endpoint}")
        return None

    def get_sync_status(self) -> Optional[dict]:
        """
        GET /api/edge/sync-status
        Returns: {status, revision, roster_revision, total_embeddings, total_students, last_updated}
        """
        return self._request("GET", "/api/edge/sync-status")

    def get_rooms(self) -> Optional[list]:
        """
        GET /api/edge/rooms
        Returns: [{"name": "001", "online": bool}, ...] — room NAMES, not ids.
        """
        resp = self._request("GET", "/api/edge/rooms")
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            rooms = resp.get("rooms")
            if isinstance(rooms, list):
                return rooms
        print(f"[CloudClient] Unexpected /rooms response: {resp}")
        return None

    def register_door(self, room: str, device: str) -> Optional[dict]:
        """
        POST /api/edge/doors
        Registers this door's device at startup so the room shows online.
        """
        return self._request("POST", "/api/edge/doors", {
            "room": room,
            "device": device,
            "online": True,
        })

    def report_heartbeat(self, doors: list) -> Optional[dict]:
        """
        POST /api/edge/doors/status
        doors: [{"room": "001", "online": True}, ...]
        Call every HEARTBEAT_INTERVAL (60s); server presence TTL is 180s.
        """
        return self._request("POST", "/api/edge/doors/status", {
            "doors": doors,
        })

    def get_allowlist(self, room: str, since: str = None) -> Optional[str]:
        """
        GET /api/edge/allowlist?room=<name>[&since=<revision>]
        Returns plain text, one line per card. 304 when unchanged — returns None.
        """
        url = f"/api/edge/allowlist?room={room}"
        if since:
            url += f"&since={since}"
        return self._request_raw("GET", url)

    def get_students(self, per_page: int = 200) -> Optional[list]:
        """
        GET /api/students (follows pagination)
        Returns: list of {student_id, name_en, member_type, card_uid, rooms, ...}
        """
        students = []
        page = 1
        while True:
            response = self._request("GET", f"/api/students?page={page}&per_page={per_page}")
            if not response:
                break
            students.extend(response.get("students", []))
            pages = response.get("pages")
            if pages is None or page >= int(pages):
                break
            page += 1
        return students or None

    def get_embeddings(self) -> Optional[dict]:
        """
        GET /api/embeddings/all
        Returns: {embeddings: [{student_id, name_en, key, embedding}], encoding, dim, ...}
        """
        return self._request("GET", "/api/embeddings/all")

    def post_event(self, event_data: dict) -> Optional[dict]:
        """
        POST /api/edge/events
        Report door access event to Cloud. Returns 201 {success, log_id, alert_sent}.
        Tries immediately; on failure the event is buffered locally and
        flushed automatically once the Cloud is reachable again.
        """
        resp = self._request("POST", "/api/edge/events", event_data,
                             retries=[1, 2])
        if resp is not None:
            return resp
        from core.offline_queue import get_offline_queue
        get_offline_queue(self).enqueue("event", "/api/edge/events", event_data)
        return None

    def post_alert(self, alert_data: dict) -> Optional[dict]:
        """
        POST /api/edge/alert
        Report suspicious activity alert to Cloud (threshold crossing only).
        Tries immediately; on failure the alert is buffered locally and
        flushed automatically once the Cloud is reachable again.
        """
        resp = self._request("POST", "/api/edge/alert", alert_data,
                             retries=[1, 2])
        if resp is not None:
            return resp
        from core.offline_queue import get_offline_queue
        get_offline_queue(self).enqueue("alert", "/api/edge/alert", alert_data)
        return None

    def enroll_capture(self, tag_id: str, room: str = None) -> Optional[dict]:
        """
        POST /api/edge/doors/<room>/enroll/capture
        Relay a card captured while the door was armed for enrolment.
        """
        room = room or cfg.ROOM_ID
        return self._request(
            "POST",
            f"/api/edge/doors/{room}/enroll/capture",
            {"tagID": tag_id},
        )

    def report_face_match(self, person_id: str, name: str, similarity: float,
                          room_id: str = None) -> Optional[dict]:
        """Helper: Report successful face match event."""
        room_id = room_id or cfg.ROOM_ID
        event = {
            "event": "access_granted",
            "room": room_id,
            "method": "face",
            "student_id": person_id,
            "name_en": name,
            "similarity": round(similarity, 4),
            "timestamp": _now()
        }
        return self.post_event(event)

    def report_rfid_match(self, person_id: str, name: str, card_uid: str,
                          room_id: str = None) -> Optional[dict]:
        """Helper: Report successful RFID match event."""
        room_id = room_id or cfg.ROOM_ID
        event = {
            "event": "access_granted",
            "room": room_id,
            "method": "card",
            "student_id": person_id,
            "name_en": name,
            "card_uid": card_uid,
            "timestamp": _now()
        }
        return self.post_event(event)

    def report_access_denied(self, reason: str, method: str = "face",
                             room_id: str = None, face_image: str = None,
                             card_uid: str = None,
                             fail_count: int = None) -> Optional[dict]:
        """Helper: Report access denied event.

        reason may be a backend event name (unknown_face / unknown_card /
        access_denied) or a free-form detail; anything unrecognized goes into
        `detail` with event = access_denied.

        face_image: full camera frame, JPEG, base64 (bare or data: URI). It is
        the photo the Telegram alert is captioned on — encode off the
        lock-opening thread; oversized frames are dropped server-side but the
        alert still sends as text.

        fail_count: unknown-face attempt number, included as the event's
        `detail` (e.g. "attempt 3") plus a numeric `fail_count` field.
        """
        room_id = room_id or cfg.ROOM_ID
        event_name = reason if reason in _EVENT_NAMES else "access_denied"
        method = _MAP_METHOD(method)
        event = {
            "event": event_name,
            "room": room_id,
            "method": method,
            "timestamp": _now()
        }
        if event_name != reason:
            event["detail"] = reason
        if face_image:
            event["face_image"] = face_image
        if card_uid:
            event["card_uid"] = card_uid
        if fail_count:
            event["fail_count"] = fail_count
            event["detail"] = f"attempt {fail_count}"
        return self.post_event(event)

    def report_suspicious_alert(self, method: str, fail_count: int,
                                card_uid: str = None,
                                room_id: str = None,
                                face_image: str = None) -> Optional[dict]:
        """Helper: Report suspicious activity alert (threshold crossing only)."""
        room_id = room_id or cfg.ROOM_ID
        alert = {
            "method": _MAP_METHOD(method),
            "fail_count": fail_count,
            "room": room_id,
            "timestamp": _now()
        }
        if card_uid:
            alert["card_uid"] = card_uid
        if face_image:
            alert["face_image"] = face_image
        return self.post_alert(alert)


_EVENT_NAMES = ("access_granted", "access_denied", "unknown_face", "unknown_card", "spoof_detected")


def _MAP_METHOD(method: str) -> str:
    """Backend accepts face / card; 'rfid' is read as 'card'. Everything else —
    incl. 'spoof' — goes through unchanged so the Cloud/Telegram sees the
    original reason."""
    return {"rfid": "card"}.get(method, method)


def _now() -> str:
    """Local time with explicit offset, e.g. 2026-08-04T20:15:00+07:00.

    Computed as UTC + EDGE_UTC_OFFSET so the wall-clock is always the door's
    local time, even if the machine itself is configured to a UTC clock
    (otherwise the timestamp would read N hours behind).
    """
    offset = int(getattr(cfg, "EDGE_UTC_OFFSET", 7))
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.gmtime(time.time() + offset * 3600))
    sign = "+" if offset >= 0 else "-"
    return f"{stamp}{sign}{abs(offset):02d}:00"
