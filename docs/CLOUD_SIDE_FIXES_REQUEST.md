# Cloud Side Fixes Request — Embedding Sync Issue

**Date:** 2026-08-03
**From:** Door-Edge (Edge) side
**To:** Cloud API team
**Status:** Edge ↔ Cloud connected, but face data is NOT syncing

---

## 1. Problem summary

The Edge connects to the Cloud successfully (`GET /api/edge/sync-status` returns OK),
and the Cloud has **2 registered people**, but **0 faces** land in the Edge's Redis
index. Root cause found: the Cloud stores **2732-dim embeddings**, while the Edge
expects exactly **512-dim**.

Consequence: the Edge silently skips every embedding
(`[Sync] Skipping T002: unexpected dim (2732,)`) — no faces, no card allowlist,
no recognition.

---

## 2. Questions for the Cloud team

1. **Which face model generates the embeddings?**
   The Edge runs InsightFace **buffalo_sc → `w600k_mbf`** (ONNX, 112×112 input,
   512-dim output). If the Cloud uses any other model (DeepFace, FaceNet, a
   different InsightFace pack, etc.), the embeddings are mathematically
   incompatible and recognition will never work — even after fixing the
   dimension. **Both sides must use the same face model.**

2. **Why are the stored embeddings 2732-dim?**
   2732 is not a standard face-embedding size (512/1024/2048 are typical). It
   looks like a wrong tensor was stored (e.g. raw model output, concatenated
   vector, or a mis-shaped array). Each person's stored vector must be exactly
   **512 floats**.

3. **Can the Cloud re-generate and re-store all embeddings** with InsightFace
   `w600k_mbf` (512-dim), then bump `revision` so the Edge re-syncs?

4. **When will `card_uid` be filled in `/api/students`?**
   Both students currently return `card_uid: null`. Not blocking for face
   testing, but the ESP32 card allowlist needs them later — in **10-digit
   decimal EM4100** format (the ESP rejects hex-style UIDs).

5. **Does `revision` / `roster_revision` increment on every data change?**
   The Edge compares these values to detect drift and trigger re-sync. Current
   values are strings (`"2:2026-08-01T06:37:51.898183"`) — that's acceptable,
   but they must change whenever students or embeddings change.

---

## 3. What the Edge expects (API contract)

All requests require header: `X-API-Key: <api_key>`

| Endpoint | Expected response shape |
|----------|------------------------|
| `GET /api/students` | `{"students": [{"student_id", "name_en", "card_uid", ...}]}` |
| `GET /api/embeddings/all` | `{"embeddings": [{"student_id", "name_en", "embedding": [512 floats]}]}` |
| `GET /api/edge/sync-status` | `{revision, roster_revision, total_embeddings, total_students}` |
| `GET /api/edge/sync-stream` | SSE events: `member_updated`, `member_deleted`, `embedding_updated`, `embedding_deleted`, `heartbeat` |
| `POST /api/edge/events` | 200 + JSON (door access logging) |
| `POST /api/edge/alert` | 200 + JSON (suspicious activity) |

---

## 4. Observed mismatch (verified 2026-08-03)

| Item | Cloud currently returns | Edge requires |
|------|------------------------|---------------|
| embedding dimension | **2732** | **512** (`config.py` → `VECTOR_DIM = 512`) |
| `card_uid` | `null` for all students | 10-digit decimal EM4100 (e.g. `0000012345`) |
| `revision` | string `"2:2026-08-01T06:37:51.898183"` | any value that changes on update |
| `embedding` array key | `embedding` | `embedding` (matching already) |
| `student_id` key | `student_id` | `student_id` (matching already) |

Example of a valid Cloud embedding record the Edge accepts:

```json
{
  "student_id": "T002",
  "name_en": "Mengsrun",
  "embedding": [0.1234, -0.5678, 0.9012, "...512 values total..."]
}
```

---

## 5. API endpoints the Edge will call (exact)

Base URL the Edge is configured with: `http://10.4.70.189:5000`
All requests include header `X-API-Key: 921987fb...` (the key provided).

| # | Method | Full URL | Purpose |
|---|--------|----------|---------|
| 1 | `GET` | `http://10.4.70.189:5000/api/edge/sync-status` | Connection + revision check |
| 2 | `GET` | `http://10.4.70.189:5000/api/students` | Download member roster |
| 3 | `GET` | `http://10.4.70.189:5000/api/embeddings/all` | Download all face vectors |
| 4 | `GET` | `http://10.4.70.189:5000/api/edge/sync-stream` | SSE real-time push stream |
| 5 | `POST` | `http://10.4.70.189:5000/api/edge/events` | Door access event logging |
| 6 | `POST` | `http://10.4.70.189:5000/api/edge/alert` | Suspicious activity alerts |

The Edge never exposes HTTP endpoints itself — it only consumes these from the
Cloud. (The only local server is the temporary mock, port 5005, which is deleted
once the real Cloud works.)

---

## 6. Edge side reference (handover facts)

- **Face model:** InsightFace `buffalo_sc` pack → recognition model `w600k_mbf.onnx`
  (see `convert.py` — the same ONNX files converted to OpenVINO run on the Edge).
- **Vector dimension:** `config.py:50` → `VECTOR_DIM = 512`
- **Similarity threshold:** `config.py:53` → `SIMILARITY_THRESHOLD = 0.5`
  (cosine; Redis search uses distance threshold 0.30)
- **Skip behavior:** `core/sync_agent.py:162` — embeddings with dim ≠ 512 are
  skipped and logged, sync continues without them.
- **Consistency check:** Edge polls `sync-status` every `SYNC_CONSISTENCY_CHECK`
  seconds (300 s default); a revision change triggers re-sync of embeddings /
  students automatically. No Edge restart needed.
- **Card sync:** card_uid mappings are pushed to the ESP32 over MQTT
  (`door/sync/card_uid`, `full_sync` / `add` / `delete`).

---

## 7. After the Cloud fix, the Edge-side verification is

1. `python -c "from core.cloud_client import CloudClient; print(CloudClient().get_sync_status())"`
2. `redis-cli -p 6380 FT.INFO door_face_index | grep num_docs` → should equal the
   Cloud's `total_embeddings` (e.g. 2)
3. Run `edge_app.py` → `[Sync] Loaded 2 embeddings to Redis`
4. Register/update a student in Cloud → Edge picks it up via SSE **without restart**
5. Real face at camera → match with similarity ≥ 0.5 → door unlock
