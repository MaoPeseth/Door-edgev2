# Data Sent to Cloud via API

Every piece of data this registration app pushes to the cloud backend.
There are exactly two endpoints the app writes to; the other calls only read.

Auth on every request: `X-API-Key: <EDGE_API_KEY>`

---

## 1. Profile — `POST /api/students`

If the server answers `409` because the `student_id` already exists, the app
retries the same payload as `PATCH /api/students/<id>`.

| Field          | Source                | Value                                              |
|----------------|-----------------------|----------------------------------------------------|
| `student_id`   | ID field              | e.g. `STU-12345` / `LEC-2200`                      |
| `member_type`  | Role toggle           | `"student"` or `"lecturer"`                        |
| `name_en`      | English name          | text                                               |
| `name_kh`      | Khmer name            | text (empty string if none)                        |
| `year`         | Year field            | string (students) / `""` (lecturers)               |
| `class_group`  | Group combo           | string (students) / `""` (lecturers)               |
| `contact`      | Contact field         | text                                               |
| `generation`   | Generation combo      | string (students) / `""` (lecturers)               |
| `department`   | Dept combo (lecturers)| string (lecturers) / `""` (students) — *discarded server-side, no column yet* |
| `card_uid`     | RFID reader           | only sent when a card is set                       |
| `picture`      | photo file            | multipart file upload, sent if a photo exists      |

Server-side these land in the `members_info` table columns
(`student_id`, `member_type`, `name_en`, `name_kh`, `generation`,
`class_group`, `contact`, `card_uid`, `picture`).

---

## 2. Face — `POST /api/embeddings/register`

| Field       | Value                            |
|-------------|----------------------------------|
| `student_id`| same ID as the profile above     |
| `name_en`   | English name                     |
| `embedding` | 512-number float array (face vector) |

This endpoint upserts and never returns `409`; re-registering a face
overwrites the stored vector. Server-side it lands in `face_embeddings`
(`embedding`, keyed by `key_suffix`).

---

## 3. Read-only calls (no personal data sent)

| Call                                   | What it does                                   |
|----------------------------------------|------------------------------------------------|
| `GET /api/edge/sync-status`            | Connection test — reachability, key and DB     |
| `GET /api/edge/rooms`                  | Pulls room names for the enrolment picker      |

---

## Notes

- `all_rooms` and `rooms` (room access grants) are **not** sent — handled on the
  cloud side.
- `photo_path` is local-only; only the actual photo bytes are uploaded.
- All 15 face-capture poses collapse into a single `embedding` per person.
