# Edge Sync Agent Design Document

## Overview

The Sync Agent is responsible for keeping the Edge's local Redis database in sync with the Cloud backend. It handles:

1. **Initial sync** — Download all member data and face embeddings on startup
2. **Real-time sync** — Receive live updates via SSE when data changes in Cloud
3. **Event reporting** — Send door access events back to Cloud

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         EDGE (Mini PC)                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐     │
│  │  Sync Agent  │───▶│    Redis     │◀───│ Face Matcher │     │
│  │              │    │  HNSW Index  │    │              │     │
│  └──────┬───────┘    └──────────────┘    └──────────────┘     │
│         │                                                       │
│         │  ┌──────────────┐                                     │
│         ├──│  SSE Client  │◀── Cloud pushes via SSE             │
│         │  └──────────────┘                                     │
│         │                                                       │
│         │  ┌──────────────┐                                     │
│         └──│ HTTP Client  │──▶ Cloud API (REST)                 │
│            └──────────────┘                                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP/SSE
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        CLOUD (Server)                           │
├─────────────────────────────────────────────────────────────────┤
│  GET  /api/students              — Member list                  │
│  GET  /api/embeddings/all        — Face vectors                 │
│  GET  /api/edge/sync-status      — Revision markers             │
│  GET  /api/edge/sync-stream      — SSE real-time updates        │
│  POST /api/edge/events           — Door event reporting         │
└─────────────────────────────────────────────────────────────────┘
```

---

## Cloud API Endpoints Reference

### Authentication

All requests require the `X-API-Key` header:

```
X-API-Key: <EDGE_API_KEY>
```

---

### 1. GET /api/students

Download the complete list of people (students and lecturers) including card numbers and room access.

**Request:**
```http
GET /api/students
X-API-Key: <EDGE_API_KEY>
```

**Response (200 OK):**
```json
{
  "students": [
    {
      "student_id": "STU-001",
      "member_type": "student",
      "name_en": "Sok Dara",
      "name_kh": "សុខ ដារា",
      "year": "3",
      "class_group": "A",
      "contact": "012345678",
      "generation": "5",
      "card_uid": "A1B2C3D4",
      "picture": "https://cloud.example.com/pictures/STU-001.jpg",
      "rooms": ["room-101", "room-102"]
    },
    {
      "student_id": "LEC-001",
      "member_type": "lecturer",
      "name_en": "Dr. Chanthy",
      "name_kh": "បណ្ឌិត ចន្រ្តី",
      "year": "",
      "class_group": "",
      "contact": "098765432",
      "generation": "",
      "card_uid": "E5F6G7H8",
      "picture": "https://cloud.example.com/pictures/LEC-001.jpg",
      "rooms": ["room-101", "room-201"]
    }
  ]
}
```

**Purpose:**
- Build local card_uid → person mapping for ESP32 sync
- Display enrolled count in Edge UI
- Reference data for face matching results

---

### 2. GET /api/embeddings/all

Download every face vector (embedding) stored in Cloud.

**Request:**
```http
GET /api/embeddings/all
X-API-Key: <EDGE_API_KEY>
```

**Response (200 OK):**
```json
{
  "embeddings": [
    {
      "student_id": "STU-001",
      "name_en": "Sok Dara",
      "embedding": [0.123, -0.456, 0.789, ...]  // 512 floats
    },
    {
      "student_id": "LEC-001",
      "name_en": "Dr. Chanthy",
      "embedding": [0.456, -0.789, 0.012, ...]  // 512 floats
    }
  ],
  "total": 150
}
```

**Purpose:**
- Load face vectors into Redis HNSW index
- Used for face similarity search during inference

---

### 3. GET /api/edge/sync-status

Check connection status and current revision markers.

**Request:**
```http
GET /api/edge/sync-status
X-API-Key: <EDGE_API_KEY>
```

**Response (200 OK):**
```json
{
  "status": "connected",
  "revision": 42,
  "roster_revision": 15,
  "total_embeddings": 150,
  "total_students": 148,
  "last_updated": "2026-08-01T10:30:00Z"
}
```

**Revision Markers:**
- `revision` — Increments when face embeddings change (add/update/delete)
- `roster_revision` — Increments when student data changes (add/update/delete)

**Purpose:**
- Check if Edge is connected to Cloud
- Compare local revision with Cloud revision to detect drift
- Detect if full re-sync is needed

---

### 4. GET /api/edge/sync-stream (SSE)

Server-Sent Events stream for real-time push notifications.

**Request:**
```http
GET /api/edge/sync-stream
X-API-Key: <EDGE_API_KEY>
Accept: text/event-stream
```

**SSE Event Types:**

#### Member Added/Updated
```
event: member_updated
data: {"student_id": "STU-002", "name_en": "Kim Leng", "card_uid": "I9J0K1L2", "revision": 43}

```

#### Member Deleted
```
event: member_deleted
data: {"student_id": "STU-002", "revision": 44}

```

#### Embedding Updated
```
event: embedding_updated
data: {"student_id": "STU-001", "revision": 45}

```

#### Embedding Deleted
```
event: embedding_deleted
data: {"student_id": "STU-001", "revision": 46}

```

#### Heartbeat (keep-alive)
```
event: heartbeat
data: {"timestamp": "2026-08-01T10:35:00Z"}

```

**Purpose:**
- Real-time sync without polling
- Instant update when new member registered
- Instant update when member deleted

---

### 5. POST /api/edge/events

Report door access events to Cloud for logging and analytics.

**Request:**
```http
POST /api/edge/events
X-API-Key: <EDGE_API_KEY>
Content-Type: application/json
```

**Payload:**
```json
{
  "door_id": "door-01",
  "event_type": "access_granted",
  "method": "face",
  "student_id": "STU-001",
  "name_en": "Sok Dara",
  "similarity": 0.85,
  "timestamp": "2026-08-01T10:30:00Z"
}
```

**Event Types:**
- `access_granted` — Door unlocked (face or RFID)
- `access_denied` — Access denied (unknown face, spoof, etc.)

**Methods:**
- `face` — Face recognition
- `rfid` — RFID card

**Response (200 OK):**
```json
{
  "status": "ok",
  "event_id": "EVT-20260801-001"
}
```

**Purpose:**
- Log all access attempts to Cloud
- Analytics and reporting on dashboard
- Audit trail for security

---

### 6. POST /api/edge/alert (NEW)

Report suspicious activity alerts to Cloud when multiple failed access attempts occur.

**Request:**
```http
POST /api/edge/alert
X-API-Key: <EDGE_API_KEY>
Content-Type: application/json
```

**Payload (Face-based alert):**
```json
{
  "door_id": "door-01",
  "alert_type": "suspicious_access",
  "method": "face",
  "fail_count": 5,
  "face_image": "data:image/jpeg;base64,/9j/4AAQSkZJRg...",
  "timestamp": "2026-08-01T10:15:00Z"
}
```

**Payload (RFID-based alert):**
```json
{
  "door_id": "door-01",
  "alert_type": "suspicious_access",
  "method": "rfid",
  "card_uid": "UNKNOWN_CARD_123",
  "fail_count": 4,
  "timestamp": "2026-08-01T10:15:00Z"
}
```

**Alert Types:**
- `suspicious_access` — Multiple failed access attempts

**Methods:**
- `face` — Unknown face repeatedly detected
- `rfid` — Unknown RFID card repeatedly used
- `both` — Both face and RFID attempts

**Response (200 OK):**
```json
{
  "status": "ok",
  "alert_id": "ALT-20260801-001",
  "message": "Alert recorded"
}
```

**Purpose:**
- Security monitoring for suspicious activity
- Alert admin via dashboard notification
- Capture face image of unknown person for identification
- Audit trail for security incidents

---

## Edge Sync Flow

### Startup Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                        STARTUP SEQUENCE                         │
└─────────────────────────────────────────────────────────────────┘

Step 1: Check Cloud Connection
        │
        ▼
        GET /api/edge/sync-status
        │
        ├── Success → Continue to Step 2
        └── Failure → Retry with backoff

Step 2: Download All Students
        │
        ▼
        GET /api/students
        │
        ├── Store locally for card_uid mapping
        └── Continue to Step 3

Step 3: Download All Embeddings
        │
        ▼
        GET /api/embeddings/all
        │
        ├── Load into Redis HNSW index
        └── Continue to Step 4

Step 4: Start SSE Listener
        │
        ▼
        GET /api/edge/sync-stream (SSE)
        │
        └── Listen for real-time updates

Step 5: Ready
        │
        ▼
        System operational, listening for face recognition
```

### Real-Time Sync Flow (SSE)

```
┌─────────────────────────────────────────────────────────────────┐
│                     SSE EVENT HANDLING                          │
└─────────────────────────────────────────────────────────────────┘

Cloud SSE Event Received
        │
        ├── event: member_updated
        │       │
        │       ▼
        │   Update local student cache
        │   Update card_uid mapping
        │   (Embedding update may follow)
        │
        ├── event: member_deleted
        │       │
        │       ▼
        │   Remove from local student cache
        │   Remove from Redis
        │   Remove card_uid mapping
        │
        ├── event: embedding_updated
        │       │
        │       ▼
        │   Fetch updated embedding from GET /api/embeddings/all
        │   Update Redis HNSW index
        │
        ├── event: embedding_deleted
        │       │
        │       ▼
        │   Remove from Redis HNSW index
        │
        └── event: heartbeat
                │
                ▼
            No action (keep-alive)
```

### Event Reporting Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    DOOR EVENT REPORTING                         │
└─────────────────────────────────────────────────────────────────┘

Door Access Occurs
        │
        ├── Face Match Success
        │       │
        │       ▼
        │   POST /api/edge/events
        │   {
        │     "door_id": "door-01",
        │     "event_type": "access_granted",
        │     "method": "face",
        │     "student_id": "...",
        │     "name_en": "...",
        │     "similarity": 0.85,
        │     "timestamp": "..."
        │   }
        │
        ├── RFID Match Success
        │       │
        │       ▼
        │   POST /api/edge/events
        │   {
        │     "door_id": "door-01",
        │     "event_type": "access_granted",
        │     "method": "rfid",
        │     "student_id": "...",
        │     "name_en": "...",
        │     "card_uid": "...",
        │     "timestamp": "..."
        │   }
        │
        └── Access Denied (unknown, spoof, etc.)
                │
                ▼
            POST /api/edge/events
            {
              "door_id": "door-01",
              "event_type": "access_denied",
              "method": "face",
              "reason": "unknown_face",
              "timestamp": "..."
            }
```

---

### Warning Alert Flow (Suspicious Activity)

When multiple failed access attempts occur, Edge captures the person's face and sends a warning alert to Cloud.

```
┌─────────────────────────────────────────────────────────────────┐
│                    WARNING ALERT SYSTEM                         │
└─────────────────────────────────────────────────────────────────┘

FAILED ATTEMPT TRACKING:
        │
        ├── Unknown face detected (not in Redis)
        │       │
        │       ▼
        │   Increment fail counter for this face
        │   (track by face embedding similarity cluster)
        │
        ├── Unknown RFID card (not in card_uid_map)
        │       │
        │       ▼
        │   Increment fail counter for this card_uid
        │
        └── Spoof detected
                │
                ▼
            Increment fail counter

THRESHOLD CHECK:
        │
        ├── fail_count >= WARNING_THRESHOLD (e.g., 3 attempts)
        │       │
        │       ▼
        │   TRIGGER WARNING ALERT
        │
        └── fail_count < WARNING_THRESHOLD
                │
                ▼
            Continue monitoring

WARNING ALERT TRIGGERED:
        │
        ├── Face available?
        │       │
        │       YES
        │       ▼
        │   Capture face image from camera
        │   POST /api/edge/alert
        │   {
        │     "door_id": "door-01",
        │     "alert_type": "suspicious_access",
        │     "method": "face",
        │     "fail_count": 5,
        │     "face_image": "base64_encoded_image",
        │     "timestamp": "..."
        │   }
        │
        ├── RFID card_uid available?
        │       │
        │       YES
        │       ▼
        │   POST /api/edge/alert
        │   {
        │     "door_id": "door-01",
        │     "alert_type": "suspicious_access",
        │     "method": "rfid",
        │     "card_uid": "UNKNOWN_CARD_123",
        │     "fail_count": 4,
        │     "timestamp": "..."
        │   }
        │
        └── Both face and RFID?
                │
                YES
                ▼
            POST /api/edge/alert
            {
              "door_id": "door-01",
              "alert_type": "suspicious_access",
              "method": "both",
              "card_uid": "...",
              "fail_count": 6,
              "face_image": "base64_encoded_image",
              "timestamp": "..."
            }

CLOUD RESPONSE:
        │
        └── Cloud receives alert
                │
                ├── Store in database
                ├── Show on dashboard (warning notification)
                ├── Optional: Send notification to admin
                └── Optional: Lock door temporarily

COUNTER RESET:
        │
        ├── Successful access by ANY person
        │       │
        │       ▼
        │   Reset all fail counters
        │   (person with access = not suspicious)
        │
        └── Timeout (e.g., 5 minutes no attempts)
                │
                ▼
            Reset fail counters
```

#### Warning Alert Data Structure

```python
# Track failed attempts per door
failed_attempts = {
    "door-01": {
        # Face-based tracking (by embedding cluster)
        "face_clusters": {
            "cluster_001": {
                "embedding": [...],  # Unknown face embedding
                "fail_count": 5,
                "first_attempt": "2026-08-01T10:00:00Z",
                "last_attempt": "2026-08-01T10:15:00Z",
                "face_image": "base64..."
            }
        },
        # RFID-based tracking
        "card_attempts": {
            "UNKNOWN_CARD_123": {
                "fail_count": 4,
                "first_attempt": "2026-08-01T10:05:00Z",
                "last_attempt": "2026-08-01T10:20:00Z"
            }
        }
    }
}
```

#### Warning Thresholds (Configurable)

```python
# Add to config.py
WARNING_THRESHOLD = 3          # Number of failed attempts to trigger alert
WARNING_RESET_TIMEOUT = 300    # Reset counters after 5 minutes (seconds)
WARNING_CAPTURE_IMAGE = True   # Capture face image when alert triggered
```

#### API Endpoint: POST /api/edge/alert

**Request:**
```http
POST /api/edge/alert
X-API-Key: <EDGE_API_KEY>
Content-Type: application/json
```

**Payload:**
```json
{
  "door_id": "door-01",
  "alert_type": "suspicious_access",
  "method": "face",
  "fail_count": 5,
  "face_image": "data:image/jpeg;base64,/9j/4AAQSkZJRg...",
  "timestamp": "2026-08-01T10:15:00Z"
}
```

**Response (200 OK):**
```json
{
  "status": "ok",
  "alert_id": "ALT-20260801-001",
  "message": "Alert recorded"
}
```

---

## Data Structures

### Local Student Cache

```python
# In-memory cache (or SQLite for persistence)
students_cache = {
    "STU-001": {
        "student_id": "STU-001",
        "name_en": "Sok Dara",
        "member_type": "student",
        "card_uid": "A1B2C3D4",
        "rooms": ["room-101", "room-102"]
    },
    "LEC-001": {
        "student_id": "LEC-001",
        "name_en": "Dr. Chanthy",
        "member_type": "lecturer",
        "card_uid": "E5F6G7H8",
        "rooms": ["room-101", "room-201"]
    }
}

# Card UID lookup (for ESP32 sync)
card_uid_map = {
    "A1B2C3D4": {"student_id": "STU-001", "name_en": "Sok Dara"},
    "E5F6G7H8": {"student_id": "LEC-001", "name_en": "Dr. Chanthy"}
}
```

### Redis Storage

```python
# Face embedding (HNSW index)
Key:    door_person:{student_id}
Fields:
  - person_id: "STU-001"
  - name: "Sok Dara"
  - embedding: <512 float32 as bytes>
```

### Revision Tracking

```python
# Track Cloud revisions locally
local_revisions = {
    "revision": 42,           # Face embedding revision
    "roster_revision": 15     # Student data revision
}
```

---

## MQTT Topics (Edge → ESP32)

After sync, Edge sends card_uid mappings to ESP32:

### Topic: door/sync/card_uid

**Full Sync (batch):**
```json
{
  "type": "full_sync",
  "cards": [
    {"card_uid": "A1B2C3D4", "person_id": "STU-001", "name": "Sok Dara"},
    {"card_uid": "E5F6G7H8", "person_id": "LEC-001", "name": "Dr. Chanthy"}
  ]
}
```

**Incremental Update:**
```json
{
  "type": "add",
  "card_uid": "I9J0K1L2",
  "person_id": "STU-002",
  "name": "Kim Leng"
}
```

**Delete:**
```json
{
  "type": "delete",
  "card_uid": "I9J0K1L2"
}
```

### Topic: door/alert/warning (NEW)

Edge notifies ESP32 when warning alert is triggered (for local feedback like buzzer/LED).

**Warning Alert:**
```json
{
  "type": "warning",
  "door_id": "door-01",
  "reason": "suspicious_access",
  "method": "face",
  "fail_count": 5,
  "timestamp": "2026-08-01T10:15:00Z"
}
```

**Alert Cleared:**
```json
{
  "type": "alert_cleared",
  "door_id": "door-01",
  "reason": "access_granted",
  "timestamp": "2026-08-01T10:20:00Z"
}
```

**Purpose:**
- ESP32 can activate local alarm (buzzer, LED, etc.)
- Provide visual/audio feedback for suspicious activity
- Clear alert when legitimate access occurs

---

## Error Handling

### Connection Failures

```python
# Retry with exponential backoff
retry_attempts = [1, 2, 4, 8, 16, 30]  # seconds

for attempt in retry_attempts:
    try:
        response = requests.get(url, headers=headers, timeout=10)
        break
    except ConnectionError:
        time.sleep(attempt)
else:
    # All retries failed
    log_error("Cannot connect to Cloud")
```

### SSE Reconnection

```python
# SSE auto-reconnect with Last-Event-ID
def listen_sse():
    last_event_id = None
    
    while True:
        try:
            headers = {"X-API-Key": API_KEY}
            if last_event_id:
                headers["Last-Event-ID"] = last_event_id
            
            with requests.get(SSE_URL, headers=headers, stream=True) as r:
                for line in r.iter_lines():
                    if line.startswith(b"id:"):
                        last_event_id = line.split(b":")[1].decode()
                    # Process event...
                    
        except ConnectionError:
            time.sleep(5)  # Reconnect after 5 seconds
```

### Data Inconsistency

```python
# Periodic consistency check (every 5 minutes)
def check_consistency():
    cloud_status = get_sync_status()
    
    if local_revisions["revision"] != cloud_status["revision"]:
        # Revision mismatch — full re-sync needed
        full_sync()
    
    if local_revisions["roster_revision"] != cloud_status["roster_revision"]:
        # Roster mismatch — re-sync students
        sync_students()
```

---

## Implementation Plan

### Phase 1: HTTP Client (Initial Sync)
- [ ] Create `core/cloud_client.py`
- [ ] Implement `get_students()` — GET /api/students
- [ ] Implement `get_embeddings()` — GET /api/embeddings/all
- [ ] Implement `get_sync_status()` — GET /api/edge/sync-status
- [ ] Implement `post_event()` — POST /api/edge/events

### Phase 2: SSE Client (Real-Time Sync)
- [ ] Create `core/sse_client.py`
- [ ] Implement SSE connection with auto-reconnect
- [ ] Handle `member_updated` event
- [ ] Handle `member_deleted` event
- [ ] Handle `embedding_updated` event
- [ ] Handle `embedding_deleted` event

### Phase 3: Sync Agent Integration
- [ ] Update `core/sync_agent.py` to use Cloud API
- [ ] Remove SQLite dependency
- [ ] Add revision tracking
- [ ] Add periodic consistency check

### Phase 4: ESP32 Card Sync
- [ ] Add MQTT publish for card_uid sync
- [ ] Send full sync on startup
- [ ] Send incremental updates on change

### Phase 5: Event Reporting
- [ ] Report face match events
- [ ] Report RFID match events
- [ ] Report access denied events

### Phase 6: Warning Alert System
- [ ] Track failed access attempts per door
- [ ] Implement fail counter with threshold
- [ ] Capture face image on warning trigger
- [ ] POST /api/edge/alert to Cloud
- [ ] MQTT alert notification to ESP32
- [ ] Counter reset on successful access or timeout

---

## Configuration

Add to `config.py`:

```python
# ── Cloud API ──────────────────────────────────────────────────────────────────
CLOUD_API_URL        = "http://192.168.1.100:5000"  # Cloud server URL
CLOUD_API_KEY        = "your_api_key_here"
CLOUD_SSE_URL        = f"{CLOUD_API_URL}/api/edge/sync-stream"

# ── Sync Settings ──────────────────────────────────────────────────────────────
SYNC_RETRY_DELAY     = 5         # seconds between retry attempts
SYNC_RETRY_MAX       = 30        # max seconds between retries
SYNC_CONSISTENCY_CHECK = 300     # check consistency every 5 minutes

# ── Warning Alert Settings ────────────────────────────────────────────────────
WARNING_THRESHOLD        = 3     # failed attempts to trigger alert
WARNING_RESET_TIMEOUT    = 300   # reset counters after 5 minutes (seconds)
WARNING_CAPTURE_IMAGE    = True  # capture face image on alert
```

---

## Testing Checklist

- [ ] Initial sync downloads all students
- [ ] Initial sync downloads all embeddings
- [ ] Redis HNSW index created correctly
- [ ] SSE connection established
- [ ] Member added event updates local cache
- [ ] Member deleted event removes from Redis
- [ ] Embedding updated event updates Redis
- [ ] Door event reported to Cloud
- [ ] Auto-reconnect on SSE disconnect
- [ ] Retry on Cloud connection failure
- [ ] Consistency check detects revision mismatch
- [ ] Warning alert triggers after threshold reached
- [ ] Face image captured on warning alert
- [ ] POST /api/edge/alert sent to Cloud
- [ ] MQTT alert notification sent to ESP32
- [ ] Counter resets on successful access
- [ ] Counter resets after timeout
