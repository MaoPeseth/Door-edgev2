"""
Testing/mock_cloud_server.py  —  TEMPORARY local stand-in for the Cloud API.

Mimics the real backend contract (see docs/CLOUD_SIDE_RESPONSE.md +
docs/EDGE_PC_TODO.md) so the edge can be verified end-to-end on the LAN:

    GET  /api/edge/sync-status    → {status, revision, roster_revision, ...}
    GET  /api/students            → {"students": [...], "page", "pages", ...}
    GET  /api/embeddings/all      → {"embeddings": [...], "encoding", "dim", ...}
    GET  /api/edge/rooms          → [{"name", "online"}]  (online = beat within 180 s)
    GET  /api/edge/allowlist      → plain text, one card_uid per line; ?since= → 304
    GET  /api/edge/sync-stream    → SSE: `sync` on connect + changes, `enroll` /
                                     `enroll_cancel` via /api/test/enroll[-cancel],
                                     `heartbeat` every 2 s
    POST /api/edge/doors          → register door device (sets presence)
    POST /api/edge/doors/status   → presence heartbeat (TTL 180 s)
    POST /api/edge/events         → log the event (checks the payload fields)
    POST /api/edge/alert          → log the alert (threshold crossings)
    POST /api/edge/doors/<room>/enroll/capture → log the relayed tagID
    POST /api/enroll, /api/delete → registration software interface
    POST /api/test/enroll         → push an SSE `enroll` event (for the relay)

Data persists in Testing/data.json. Events/alerts are printed to console and
appended to data.json ("events").

ELIMINATION: when the real Cloud is ready, delete this whole Testing/
folder and revert CLOUD_API_URL in config.py.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
PORT = 5005

PRESENCE_TTL = 180   # seconds; mirror of the real backend

# Scheduled-access mock: a synthetic schedule bundle + revision served to the
# edge. Edit _SCHEDULE_BUNDLE to exercise different policy states / edge timing.
_SCHEDULE_REVISION = 0
_SCHEDULE_BUNDLE = {
    "success": True,
    "room_id": None,
    "revision": "h1:mock",
    "lockdown": False,
    "require_request": False,
    "edge_run_start": None,   # null = 24/7
    "edge_run_end": None,
    "schedule_start": None,
    "schedule_end": None,
    "holidays": [],
    "scheduled_lockdowns": [],
    "access_overrides": [],
    "weekly_schedule": [],
    "school_breaks": [],
    "exceptions": [],
}

# Accept several common key spellings so unknown registration-software
# payloads still work. Unknown fields are preserved and echoed back.
ID_KEYS   = ("student_id", "id", "studentId", "person_id")
NAME_KEYS = ("name_en", "name", "full_name", "name_en_us")
CARD_KEYS = ("card_uid", "card_id", "card", "card_uid_hex")
EMB_KEYS  = ("embedding", "face_embedding", "vector", "embedding_vector")

_lock = threading.Lock()
_students = []       # list of dicts (student metadata)
_embeddings = []     # list of dicts {student_id, name_en, embedding}
_rooms = ["001"]     # room names this mock serves
_doors = {}          # {room: {"device", "last_beat"}}
_events = []         # log of events/alerts (bounded)
_revision = 0        # bump on embedding change
_roster_revision = 0  # bump on student change
_change_seq = 0      # bump on any change (SSE `sync` trigger)
_pending_events = [] # (event_name, data) pushed to SSE clients on next tick
_event_log = []      # event name + payload logged by the edge


def _load():
    global _students, _embeddings, _revision, _roster_revision, _rooms, _doors
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text())
        _students = data.get("students", [])
        _embeddings = data.get("embeddings", [])
        _revision = data.get("revision", 0)
        _roster_revision = data.get("roster_revision", 0)
        _rooms = data.get("rooms") or ["207"]
        _doors = data.get("doors", {})
    print(f"[MockCloud] Loaded {len(_students)} students, "
          f"{len(_embeddings)} embeddings from {DATA_FILE.name}")


def _save():
    DATA_FILE.write_text(json.dumps({
        "students": _students,
        "embeddings": _embeddings,
        "revision": _revision,
        "roster_revision": _roster_revision,
        "rooms": _rooms,
        "doors": _doors,
        "events": _events[-200:],
    }, indent=2))


def _status_dict():
    return {
        "status": "ok",
        "revision": _revision,
        "roster_revision": _roster_revision,
        "schedule_revision": _SCHEDULE_REVISION,
        "total_embeddings": len(_embeddings),
        "total_students": len(_students),
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _pick(d, keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _parse_enroll(body: dict):
    """Normalize an incoming enrollment payload into (student, embedding)."""
    sid = str(_pick(body, ID_KEYS))
    name = _pick(body, NAME_KEYS)
    card = _pick(body, CARD_KEYS)
    emb = body.get("embedding") or body.get("face_embedding") \
        or body.get("vector") or body.get("embedding_vector")

    student = {k: v for k, v in body.items() if k not in EMB_KEYS}
    student["student_id"] = sid
    student["name_en"] = name
    if card:
        student["card_uid"] = card
    student.setdefault("all_rooms", False)
    student.setdefault("rooms", [])
    return sid, name, card, emb, student


def enroll(body: dict):
    """Add/update one enrollment; returns (ok, message)."""
    global _revision, _roster_revision, _change_seq
    sid, name, card, emb, student = _parse_enroll(body)
    if not sid:
        return False, "no student_id found in payload"

    with _lock:
        # Upsert student metadata
        for i, s in enumerate(_students):
            if s.get("student_id") == sid:
                _students[i] = student
                break
        else:
            _students.append(student)
        _roster_revision += 1

        # Upsert embedding (if present and valid)
        if emb is not None:
            if isinstance(emb, list):
                ok_emb = emb
            elif isinstance(emb, dict) and "values" in emb:
                ok_emb = emb["values"]
            else:
                ok_emb = list(emb)
            ok_emb = [float(x) for x in ok_emb]
            for i, e in enumerate(_embeddings):
                if e.get("student_id") == sid:
                    _embeddings[i] = {"student_id": sid, "name_en": name,
                                      "key": f"{sid}:normal", "embedding": ok_emb}
                    break
            else:
                _embeddings.append({"student_id": sid, "name_en": name,
                                    "key": f"{sid}:normal", "embedding": ok_emb})
            _revision += 1
        _change_seq += 1
        _save()

    print(f"[MockCloud] Enrolled: {sid} ({name}) card={card} "
          f"embedding_len={len(ok_emb) if emb is not None else 'none'}")
    return True, "enrolled"


def delete(body: dict):
    """Remove an enrollment by student_id; returns (ok, message)."""
    global _revision, _roster_revision, _change_seq
    sid = str(_pick(body, ID_KEYS))
    if not sid:
        return False, "no student_id found in payload"

    with _lock:
        _students[:] = [s for s in _students if s.get("student_id") != sid]
        _embeddings[:] = [e for e in _embeddings if e.get("student_id") != sid]
        _roster_revision += 1
        _revision += 1
        _change_seq += 1
        _save()

    print(f"[MockCloud] Deleted: {sid}")
    return True, "deleted"


def _allowlist_for(room: str) -> list:
    """card_uids of students who can access `room` (all_rooms or in rooms)."""
    uids = []
    for s in _students:
        if s.get("all_rooms") or room in (s.get("rooms") or []):
            uid = s.get("card_uid")
            if uid:
                uids.append(str(uid))
    return uids


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 + chunked encoding so streaming clients (requests/urllib3)
    # receive body data as it is written instead of buffering until close.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # quiet default logging

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, status=200):
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def _get_branch(self):
        return self.path.split("?", 1)[0].rstrip("/")

    def _log_event(self, kind, payload):
        with _lock:
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind,
                   "payload": payload}
            _events.append(rec)
            _event_log.append((kind, payload))
            if len(_events) > 200:
                _events[:] = _events[-200:]

    # ── GET ────────────────────────────────────────────────────────────

    def do_GET(self):
        path = self._get_branch()
        query = dict(p.split("=", 1) for p in self.path.split("?", 1)[-1].split("&") if "=" in p)

        if path == "/api/edge/sync-status":
            self._send_json(_status_dict())

        elif path == "/api/students":
            n = len(_students)
            page = int(query.get("page", 1))
            per_page = int(query.get("per_page", 200))
            start = (page - 1) * per_page
            pages = max(1, -(-n // per_page)) if n else 1
            self._send_json({
                "students": _students[start:start + per_page],
                "total": n, "page": page, "pages": pages,
            })

        elif path == "/api/embeddings/all":
            now = time.time()
            with _lock:
                emb = [dict(e) for e in _embeddings]
                for e in emb:
                    e.setdefault("key", f"{e.get('student_id')}:normal")
                    e.setdefault("name_en", e.get("name", ""))
            self._send_json({
                "success": True, "count": len(emb), "dim": 512,
                "dtype": "float32", "encoding": "floats",
                "embeddings": emb, "_server_time": now,
            })

        elif path == "/api/edge/rooms":
            now = time.time()
            rooms = []
            for i, name in enumerate(_rooms, start=1):
                d = _doors.get(name)
                online = bool(d) and (now - d.get("last_beat", 0)) <= PRESENCE_TTL
                rooms.append({"id": i, "name": name, "online": online})
            self._send_json(rooms)

        elif path == "/api/edge/schedule":
            room_id = int(query.get("room", 0) or 0)
            bundle = dict(_SCHEDULE_BUNDLE)
            bundle["room_id"] = room_id
            bundle["revision"] = str(_SCHEDULE_REVISION) + _SCHEDULE_BUNDLE.get("revision", "")
            self._send_json(bundle)

        elif path == "/api/edge/allowlist":
            room = query.get("room", "")
            since = query.get("since")
            if str(since) == str(_roster_revision):
                self._send_text("", 304)
            else:
                cards = _allowlist_for(room)
                print(f"[MockCloud] allowlist room={room} "
                      f"since={since} → {len(cards)} card(s)")
                self._send_text("\n".join(cards))

        elif path == "/api/edge/sync-stream":
            self._sse_loop()

        else:
            self._send_json({"error": "not found", "path": path}, 404)

    # ── POST ───────────────────────────────────────────────────────────

    def do_POST(self):
        path = self._get_branch()
        body = self._read_body()
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, 400)
            return

        if path in ("/api/enroll", "/api/students", "/api/embeddings",
                    "/api/edge/enroll"):
            ok, msg = enroll(body)
            self._send_json({"status": msg, "success": ok}, 200 if ok else 400)

        elif path in ("/api/delete", "/api/edge/delete"):
            ok, msg = delete(body)
            self._send_json({"status": msg, "success": ok}, 200 if ok else 400)

        elif path == "/api/edge/doors":
            room = str(body.get("room", ""))
            device = body.get("device", "")
            with _lock:
                _doors[room] = {"device": device, "last_beat": time.time()}
            print(f"[MockCloud] Door registered: room={room} device={device}")
            self._send_json({"success": True, "room": room}, 201)

        elif path == "/api/edge/doors/status":
            for d in body.get("doors", []):
                room = str(d.get("room", ""))
                if d.get("online"):
                    with _lock:
                        _doors.setdefault(room, {})["last_beat"] = time.time()
            self._send_json({"success": True}, 201)

        elif path.startswith("/api/edge/doors/") and path.endswith("/enroll/capture"):
            room = path.split("/")[4]
            tag = body.get("tagID", "")
            self._log_event("enroll_capture", {"room": room, "tagID": tag})
            print(f"[MockCloud] Enrol capture room={room} tagID={tag}")
            self._send_json({"success": True, "room": room, "tagID": tag}, 201)

        elif path == "/api/edge/events":
            self._log_event("event", body)
            img = body.get("face_image", "")
            print(f"[MockCloud] EVENT event={body.get('event')} room={body.get('room')} "
                  f"method={body.get('method')} name_en={body.get('name_en')} "
                  f"detail={body.get('detail')} ts={body.get('timestamp')} "
                  f"face_image={len(img)} chars")
            self._send_json({"success": True, "log_id": len(_events),
                             "alert_sent": False}, 201)

        elif path == "/api/edge/alert":
            self._log_event("alert", body)
            img = body.get("face_image", "")
            print(f"[MockCloud] ALERT method={body.get('method')} "
                  f"fail_count={body.get('fail_count')} room={body.get('room')} "
                  f"card_uid={body.get('card_uid')} ts={body.get('timestamp')} "
                  f"face_image={len(img)} chars")
            self._send_json({"success": True, "alert_id": len(_events),
                             "alert_sent": False}, 201)

        elif path == "/api/test/enroll":
            with _lock:
                _pending_events.append(("enroll", {
                    "room": body.get("room", "207"),
                    "timeoutS": body.get("timeoutS", 30),
                }))
            print(f"[MockCloud] Test: queued SSE enroll event "
                  f"room={body.get('room')} timeoutS={body.get('timeoutS')}")
            self._send_json({"success": True, "queued": "enroll"}, 201)

        elif path == "/api/test/enroll-cancel":
            with _lock:
                _pending_events.append(("enroll_cancel", {
                    "room": body.get("room", "207"),
                }))
            print("[MockCloud] Test: queued SSE enroll_cancel event")
            self._send_json({"success": True, "queued": "enroll_cancel"}, 201)

        else:
            # Unknown endpoint: be permissive — treat as enrollment so the
            # registration software's real payload shape still works.
            print(f"[MockCloud] POST {path} treated as enrollment; "
                  f"raw payload keys: {list(body.keys())}")
            ok, msg = enroll(body)
            self._send_json({"status": msg, "success": ok}, 200 if ok else 400)

    # ── SSE stream (for core/sse_client.py) ────────────────────────────

    def _sse_send(self, event, data):
        payload = (f"event:{event}\n"
                   f"data:{json.dumps(data)}\n\n").encode()
        self.wfile.write(f"{len(payload):X}\r\n".encode())
        self.wfile.write(payload + b"\r\n")
        self.wfile.flush()

    def _sse_loop(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        print("[MockCloud] SSE client connected")

        with _lock:
            seen_seq = _change_seq
        try:
            while True:
                with _lock:
                    changed = _change_seq != seen_seq
                    seen_seq = _change_seq
                    pending = list(_pending_events)
                    _pending_events.clear()
                if changed:
                    self._sse_send("sync", _status_dict())
                for name, data in pending:
                    self._sse_send(name, data)
                    print(f"[MockCloud] SSE pushed {name} {data}")
                self._sse_send("heartbeat", {"timestamp": time.time()})
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            print("[MockCloud] SSE client disconnected")
        except Exception as e:
            print(f"[MockCloud] SSE loop error: {e}")


def main():
    _load()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[MockCloud] Listening on 0.0.0.0:{PORT}  (data file: {DATA_FILE.name})")
    print("[MockCloud] Point config.py CLOUD_API_URL here, then run edge_app.py")
    print("[MockCloud] Registration software should POST to:")
    print(f"            http://<edge-ip>:{PORT}/api/enroll")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[MockCloud] Stopped.")


if __name__ == "__main__":
    main()
