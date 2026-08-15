# Testing/ — TEMPORARY local mock of the Cloud API

The Cloud backend endpoints are still pending. This folder lets you test the
full pipeline **on the edge only**:

```
Registration software ──POST──► mock_cloud_server.py (on edge PC)
                                     │  SSE push (door/edge/sync-stream)
edge_app.py ◄──SyncAgent pulls───────┘
     │
     ▼
Redis face index → screen display "ENROLLED: N"
```

**No changes to edge code** — the edge still talks to a "cloud"; it's just
this local one. When the real Cloud is ready: delete this folder and revert
`CLOUD_API_URL` in `config.py`.

---

## 1. Start the mock server (on the edge PC)

```bash
cd Door-Edge_new_machine
venv/bin/python Testing/mock_cloud_server.py
# listening on 0.0.0.0:5005
```

## 2. Point the edge at it (temporary)

In `config.py`:

```python
CLOUD_API_URL = "http://localhost:5005"     # TEMPORARY — revert when Cloud is up
CLOUD_API_KEY = "anything"                  # mock accepts any key
```

## 3. Run the edge

```bash
venv/bin/python edge_app.py
```

The first sync loads everything stored in `Testing/data.json` into Redis —
the display's "Enrolled faces" count updates (polled every 5 s).

## 4. Enroll from the registration software

Make it POST a JSON object to:

```
http://<edge-ip>:5005/api/enroll
```

Accepted keys (several spellings are tolerated, unknown keys are preserved):

```json
{
  "student_id": "STU-001",
  "name_en": "Sok Dara",
  "card_uid": "A1B2C3",
  "member_type": "student",
  "embedding": [0.123, ... 512 floats ...]
}
```

Delete: `POST /api/delete` with `{"student_id": "STU-001"}`.

Each enrollment triggers an SSE `embedding_updated` + `member_updated` push,
so the edge picks it up in real time (no restart needed).

### Quick manual test (before wiring your registration software)

```bash
venv/bin/python Testing/send_enroll.py --student-id STU-001 --name "Sok Dara" --card A1B2C3
venv/bin/python Testing/send_enroll.py --student-id STU-002 --name "Chan Virak"   # random embedding
venv/bin/python Testing/send_enroll.py --delete --student-id STU-001
```

---

## Endpoints implemented

| Method | Path                      | Purpose                                      |
|--------|---------------------------|----------------------------------------------|
| GET    | `/api/edge/sync-status`   | revisions + counts (edge consistency check) |
| GET    | `/api/students`           | member roster                               |
| GET    | `/api/embeddings/all`     | face vectors                                |
| GET    | `/api/edge/sync-stream`   | SSE stream (heartbeat + change push)        |
| POST   | `/api/enroll`             | add/update enrollment                       |
| POST   | `/api/delete`             | remove enrollment                           |

Data persists in `Testing/data.json`.

## Caveats

- If your registration software uses different endpoint paths or field
  names, tell me — the mock is intentionally permissive (unknown POST paths
  are treated as enrollments and the raw payload is printed to the server
  console), so it will likely still work.
- Face vectors must be 512 dims (`config.VECTOR_DIM`); wrong dims are
  skipped by the edge's SyncAgent with a log line.
- The real Cloud is untouched by all of this.
