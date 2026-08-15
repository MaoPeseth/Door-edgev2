# Door-Edge System Implementation Log & Guide

**Date:** August 1, 2026  
**Project:** Door-Edge Face Recognition Access Control System

---

## Table of Contents

1. [Today's Work Summary](#todays-work-summary)
2. [System Architecture Overview](#system-architecture-overview)
3. [Edge Implementation Guide](#edge-implementation-guide)
4. [ESP32 Firmware Guide](#esp32-firmware-guide)
5. [Testing Procedures](#testing-procedures)
6. [Troubleshooting](#troubleshooting)
7. [Next Steps](#next-steps)

---

## Today's Work Summary

### What Was Completed

| Component | Status | Files Modified/Created |
|-----------|--------|------------------------|
| **Design Document** | ✅ Done | `docs/sync_agent_design.md` |
| **Cloud API Integration** | ✅ Done | `core/cloud_client.py` |
| **SSE Real-Time Sync** | ✅ Done | `core/sse_client.py` |
| **Sync Agent (Cloud-based)** | ✅ Done | `core/sync_agent.py` |
| **MQTT Publisher (Extended)** | ✅ Done | `core/mqtt_publisher.py` |
| **Warning Alert System** | ✅ Done | `core/alert_tracker.py` |
| **Configuration** | ✅ Done | `config.py` |
| **Edge App Integration** | ✅ Done | `edge_app.py` |
| **Camera Worker (Updated)** | ✅ Done | `core/camera_worker.py` |
| **Face Matcher (Extended)** | ✅ Done | `core/face_matcher.py` |
| **ESP32 Config** | ✅ Done | `esp_test/config.h` |
| **ESP32 RFID Reader** | ✅ Done | `esp_test/rfid_reader.h` |
| **ESP32 NVS Storage** | ✅ Done | `esp_test/nvs_storage.h` |
| **ESP32 MQTT Handler** | ✅ Done | `esp_test/mqtt_handler.h` |
| **ESP32 Door Lock** | ✅ Done | `esp_test/door_lock.h` |
| **ESP32 Main Firmware** | ✅ Done | `esp_test/esp_door_control.ino` |

### Key Features Implemented

1. **Cloud-Based Sync** — Replaced SQLite with Cloud API
2. **Real-Time Updates** — SSE for instant member/embedding changes
3. **Warning Alert System** — Detects suspicious access attempts
4. **ESP32 Local RFID Match** — OR gate logic (RFID or Face)
5. **NVS Card Storage** — Persistent card mapping across reboots
6. **Event Reporting** — All access events logged to Cloud

---

## System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         SYSTEM ARCHITECTURE                             │
└─────────────────────────────────────────────────────────────────────────┘

┌──────────────┐      ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│   REGISTER   │      │    CLOUD     │      │    EDGE      │      │    ESP32     │
│   SOFTWARE   │─────▶│   (Server)   │─────▶│  (Mini PC)   │─────▶│   (Door)    │
│              │ POST │              │  SSE │              │ MQTT │              │
│ - Register   │      │ - Dashboard  │      │ - InsightFace│      │ - RFID      │
│   student    │      │ - Store data │      │ - Redis      │      │ - MQTT      │
│ - Capture    │      │ - SSE push   │      │ - Camera     │      │ - Relay     │
│   face       │      │              │      │ - YOLO       │      │ - NVS       │
│ - Assign     │      │              │      │              │      │              │
│   card_uid   │      │              │      │              │      │              │
└──────────────┘      └──────────────┘      └──────────────┘      └──────────────┘
```

### Data Flow

```
REGISTRATION:
Register Software ──POST──▶ Cloud API

SYNC (Cloud → Edge):
Cloud ──SSE──▶ Edge (real-time push)
Cloud ──REST──▶ Edge (initial/recovery sync)

SYNC (Edge → ESP32):
Edge ──MQTT──▶ ESP32 (card_uid mappings)

UNLOCK (Face):
Camera ──▶ Edge (Inference) ──MQTT──▶ ESP32 ──▶ Door

UNLOCK (RFID):
RFID Reader ──▶ ESP32 (local match) ──▶ Door

EVENTS:
Edge ──POST──▶ Cloud (access logging)
Edge ──POST──▶ Cloud (warning alerts)
```

---

## Edge Implementation Guide

### Prerequisites

1. **Hardware:**
   - Mini PC (Intel N100 or better recommended)
   - USB Camera
   - Ethernet/WiFi connection to Cloud

2. **Software:**
   - Python 3.10+
   - Redis (Docker or native)
   - Mosquitto MQTT Broker (Docker)

3. **Cloud Access:**
   - Cloud API URL
   - API Key

### Step 1: Install Dependencies

```bash
cd /home/tee/Desktop/my_project/Door-Edge_new_machine

# Create virtual environment (if not exists)
python3 -m venv venv
source venv/bin/activate

# Install requirements
pip install -r requirements.txt
```

### Step 2: Configure Settings

Edit `config.py`:

```python
# Cloud API Settings
CLOUD_API_URL = "http://YOUR_CLOUD_IP:5000"
CLOUD_API_KEY = "YOUR_API_KEY"

# MQTT Settings (if using Docker Mosquitto)
MQTT_BROKER = "localhost"
```

### Step 3: Start Redis

```bash
# Using Docker
docker run -d --name redis -p 6380:6379 redis:alpine

# Or native
sudo systemctl start redis
```

### Step 4: Start MQTT Broker

```bash
# Using Docker Compose
cd /home/tee/Desktop/my_project/Door-Edge_new_machine
docker-compose up -d
```

### Step 5: Run Edge System

```bash
cd /home/tee/Desktop/my_project/Door-Edge_new_machine
source venv/bin/activate
python edge_app.py
```

### Expected Output

```
==================================================
  Door-Edge Face Recognition System
==================================================

[Init] Loading models...
[OpenVINO] DLL path added: ...
[ModelHub] InsightFace (native OpenVINO) ready (device=CPU)
[ModelHub] YOLO anti-spoof ready
[ModelHub] Hand verification ready

[Init] Connecting to MQTT broker...
[MQTT] Connected to localhost:1883

[Init] Connecting to Cloud API...
[Sync] Cloud connected — revision=42

[Init] Running first embedding sync from Cloud...
[Sync] Loaded 150 students from Cloud
[Sync] Loaded 150 embeddings to Redis
[MQTT] CARD_SYNC → 150 cards sent to ESP32
[Sync] Initial sync complete — 150 faces in Redis

[Init] Starting camera worker...
       Press  q  in the preview window to quit.
```

---

## ESP32 Firmware Guide

### Prerequisites

1. **Hardware:**
   - ESP32 DevKit-C (or similar)
   - MFRC522 RFID Reader
   - Door Lock Relay Module
   - LEDs (Green, Red)
   - Buzzer (optional)
   - Jumper wires

2. **Software:**
   - Arduino IDE 2.0+
   - ESP32 Board Package

### Step 1: Install Arduino Libraries

In Arduino IDE:
1. Go to **Sketch → Include Library → Manage Libraries**
2. Install:
   - `MFRC522` (by Miguel Balboa)
   - `ArduinoJson` (by Benoit Blanchon)
   - `PubSubClient` (by Nick O'Leary)

### Step 2: Install ESP32 Board

1. Go to **File → Preferences**
2. Add to "Additional Board Manager URLs":
   ```
   https://dl.espressif.com/dl/package_esp32_index.json
   ```
3. Go to **Tools → Board → Boards Manager**
4. Search "esp32" and install

### Step 3: Configure Firmware

Edit `esp_test/config.h`:

```cpp
// WiFi Settings
#define WIFI_SSID           "YOUR_WIFI_SSID"
#define WIFI_PASSWORD       "YOUR_WIFI_PASSWORD"

// MQTT Settings
#define MQTT_BROKER         "YOUR_EDGE_PC_IP"
```

### Step 4: Wire Connections

```
ESP32 Pin    →    Component
─────────────────────────────
GPIO 5       →    MFRC522 SDA
GPIO 4       →    MFRC522 RST
GPIO 23      →    MFRC522 MOSI
GPIO 19      →    MFRC522 MISO
GPIO 18      →    MFRC522 SCK
GPIO 26      →    Relay IN
GPIO 12      →    Green LED (+)
GPIO 14      →    Red LED (+)
GPIO 25      →    Buzzer (+)
GND          →    All GND pins
3.3V         →    MFRC522 VCC
5V           →    Relay VCC, Buzzer VCC
```

### Step 5: Upload Firmware

1. Connect ESP32 via USB
2. In Arduino IDE:
   - **Tools → Board → ESP32 Dev Module**
   - **Tools → Port → (select your port)**
3. Click **Upload** (or Ctrl+U)

### Step 6: Monitor Output

1. Open **Tools → Serial Monitor**
2. Set baud rate to **115200**
3. Expected output:

```
╔════════════════════════════════════════════════════╗
║  ESP32 Door Control System                        ║
║  Edge-Cloud Integrated                            ║
╚════════════════════════════════════════════════════╝

[Init] Starting initialization...
[Init] Initializing door lock...
[Door] Lock initialized — locked
[Init] Initializing NVS storage...
[NVS] Loaded 0 cards from storage
[Init] Initializing RFID reader...
[RFID] Reader initialized — UID: 92
[Init] Connecting to WiFi...
[WiFi] Connected!
[WiFi] IP Address: 192.168.1.105
[Init] Initializing MQTT...
[MQTT] Connecting to 10.4.70.127:1883
[MQTT] Connected!
[MQTT] Subscribed to topics:
  - door/cmd/unlock
  - door/access/denied
  - door/sync/card_uid
  - door/alert/warning
[Init] ====================
[Init] System ready!
[Init] Waiting for RFID cards or MQTT commands...
```

---

## Testing Procedures

### Test 1: Edge → Cloud Connection

```bash
# On Edge PC
cd /home/tee/Desktop/my_project/Door-Edge_new_machine
source venv/bin/activate
python -c "from core.cloud_client import CloudClient; c = CloudClient(); print(c.get_sync_status())"
```

Expected: Cloud status JSON with revision numbers.

### Test 2: Redis Face Index

```bash
# Check Redis
redis-cli -p 6380 FT.INFO door_face_index
```

Expected: Index info with `num_docs` showing enrolled faces.

### Test 3: MQTT Communication

```bash
# Subscribe to topics (terminal 1)
mosquitto_sub -h localhost -t "door/#" -v

# Publish test unlock (terminal 2)
mosquitto_pub -h localhost -t "door/cmd/unlock" -m '{"door_id":"door-01","person_id":"TEST","name":"Test User","method":"face","similarity":0.85}'
```

Expected: ESP32 receives and unlocks door.

### Test 4: RFID Card

1. Present RFID card to reader
2. Check serial monitor for:
   - `RFID Card detected: XXXXXXXX`
   - `RFID match: Student Name (STU-001)` (if card registered)
   - OR `Unknown card: XXXXXXXX` (if not registered)

### Test 5: Warning Alert

1. Present unknown card 3 times rapidly
2. Check Edge logs for:
   - `Alert] Unknown card XXXXXXXX attempt #3`
   - `[Alert] TRIGGERED! method=rfid, fail_count=3`
3. Check ESP32 for buzzer activation

---

## Troubleshooting

### Edge Issues

| Problem | Solution |
|---------|----------|
| Cannot connect to Cloud | Check `CLOUD_API_URL` and network |
| Redis connection failed | Ensure Redis is running on port 6380 |
| MQTT connection failed | Check Mosquitto is running |
| Camera not found | Check `CAMERA_INDEX` in config |
| No faces detected | Check lighting and camera position |

### ESP32 Issues

| Problem | Solution |
|---------|----------|
| WiFi won't connect | Check SSID/password in config.h |
| MQTT won't connect | Check `MQTT_BROKER` IP address |
| RFID not reading | Check wiring, try different card |
| Door won't unlock | Check relay wiring and power |
| Cards not syncing | Check MQTT connection to Edge |

### Common Fixes

```bash
# Restart Redis
docker restart redis

# Restart MQTT
docker restart door-mosquitto

# Clear Redis data
redis-cli -p 6380 FLUSHALL

# View ESP32 logs
# Open Arduino Serial Monitor at 115200 baud
```

---

## Next Steps

### Phase 1: Testing (Tomorrow)

- [ ] Test Edge → Cloud connection
- [ ] Test initial sync (students + embeddings)
- [ ] Test SSE real-time updates
- [ ] Test ESP32 WiFi + MQTT connection
- [ ] Test RFID card reading
- [ ] Test door unlock via RFID
- [ ] Test door unlock via MQTT (face match)

### Phase 2: Integration

- [ ] Test full flow: Register → Cloud → Edge → ESP32
- [ ] Test warning alert system
- [ ] Test event reporting to Cloud
- [ ] Test card_uid sync from Edge to ESP32

### Phase 3: Production

- [ ] Set up systemd service for Edge (auto-start)
- [ ] Configure static IP for ESP32
- [ ] Add HTTPS for Cloud API (if needed)
- [ ] Set up monitoring/alerting

---

## File Reference

### Edge Files

```
Door-Edge_new_machine/
├── edge_app.py                 ← Main entry point
├── config.py                   ← Configuration
├── requirements.txt            ← Python dependencies
├── docker-compose.yml          ← Docker services
├── docs/
│   └── sync_agent_design.md    ← Design document
└── core/
    ├── cloud_client.py         ← HTTP client for Cloud API
    ├── sse_client.py           ← SSE client for real-time sync
    ├── sync_agent.py           ← Cloud-based sync agent
    ├── mqtt_publisher.py       ← MQTT publish helper
    ├── alert_tracker.py        ← Warning alert system
    ├── face_matcher.py         ← Redis face search
    ├── camera_worker.py        ← Camera + inference
    ├── models.py               ← InsightFace + YOLO
    ├── insightface_ov.py       ← OpenVINO face model
    └── anti_spoof.py           ← YOLO anti-spoof
```

### ESP32 Files

```
esp_test/
├── esp_door_control.ino        ← Main firmware
├── config.h                    ← All settings
├── rfid_reader.h               ← MFRC522 module
├── nvs_storage.h               ← NVS card storage
├── mqtt_handler.h              ← MQTT communication
└── door_lock.h                 ← Door lock control
```

---

## Cloud API Reference

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/api/students` | Download member list |
| `GET` | `/api/embeddings/all` | Download face vectors |
| `GET` | `/api/edge/sync-status` | Check revision markers |
| `GET` | `/api/edge/sync-stream` | SSE real-time updates |
| `POST` | `/api/edge/events` | Report door events |
| `POST` | `/api/edge/alert` | Report suspicious activity |

---

## MQTT Topics Reference

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `door/cmd/unlock` | Edge → ESP32 | Face match unlock |
| `door/access/denied` | Edge → ESP32 | Access denied |
| `door/sync/card_uid` | Edge → ESP32 | Card mapping sync |
| `door/alert/warning` | Edge → ESP32 | Suspicious activity |
| `door/health` | ESP32 → Edge | Heartbeat status |
| `door/access/log` | ESP32 → Edge | RFID access logging |

---

**Document Version:** 1.0  
**Last Updated:** August 1, 2026
