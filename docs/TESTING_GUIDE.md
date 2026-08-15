# Door-Edge System Testing Guide

**Date:** August 1, 2026  
**Purpose:** Step-by-step testing procedures for all components

---

## Table of Contents

1. [Pre-Test Checklist](#pre-test-checklist)
2. [Phase 1: Component Testing](#phase-1-component-testing)
3. [Phase 2: Integration Testing](#phase-2-integration-testing)
4. [Phase 3: System Testing](#phase-3-system-testing)
5. [Phase 4: Stress Testing](#phase-4-stress-testing)
6. [Test Results Log](#test-results-log)

---

## Pre-Test Checklist

### Hardware Checklist

- [ ] Mini PC powered on and connected to network
- [ ] Camera connected and working
- [ ] Redis running (port 6380)
- [ ] MQTT broker running (port 1883)
- [ ] ESP32 powered on
- [ ] MFRC522 RFID reader connected
- [ ] Door lock relay connected
- [ ] LEDs connected (Green, Red)
- [ ] Buzzer connected (optional)

### Software Checklist

- [ ] Edge Python environment activated
- [ ] `config.py` updated with Cloud URL and API key
- [ ] ESP32 `config.h` updated with WiFi and MQTT settings
- [ ] All required Python packages installed
- [ ] All required Arduino libraries installed

### Network Checklist

- [ ] Edge PC has IP address
- [ ] Cloud server reachable from Edge
- [ ] ESP32 connected to WiFi
- [ ] ESP32 can reach Edge MQTT broker

---

## Phase 1: Component Testing

### Test 1.1: Cloud API Connection

**Objective:** Verify Edge can connect to Cloud API

**Steps:**

```bash
# On Edge PC
cd /home/tee/Desktop/my_project/Door-Edge_new_machine
source venv/bin/activate

# Test sync status endpoint
python -c "
from core.cloud_client import CloudClient
c = CloudClient()
result = c.get_sync_status()
if result:
    print('SUCCESS: Cloud connected!')
    print(f'Revision: {result.get(\"revision\")}')
    print(f'Roster Revision: {result.get(\"roster_revision\")}')
    print(f'Total Students: {result.get(\"total_students\")}')
else:
    print('FAILED: Cannot connect to Cloud')
"
```

**Expected Result:**
```
SUCCESS: Cloud connected!
Revision: 42
Roster Revision: 15
Total Students: 148
```

**Pass/Fail:** _______

---

### Test 1.2: Cloud Students Download

**Objective:** Verify Edge can download student list from Cloud

**Steps:**

```bash
python -c "
from core.cloud_client import CloudClient
c = CloudClient()
students = c.get_students()
if students:
    print(f'SUCCESS: Downloaded {len(students)} students')
    print('First 3 students:')
    for s in students[:3]:
        print(f'  - {s.get(\"student_id\")}: {s.get(\"name_en\")} (card: {s.get(\"card_uid\")})')
else:
    print('FAILED: Cannot download students')
"
```

**Expected Result:**
```
SUCCESS: Downloaded 150 students
First 3 students:
  - STU-001: Sok Dara (card: A1B2C3D4)
  - STU-002: Kim Leng (card: I9J0K1L2)
  - LEC-001: Dr. Chanthy (card: E5F6G7H8)
```

**Pass/Fail:** _______

---

### Test 1.3: Cloud Embeddings Download

**Objective:** Verify Edge can download face embeddings from Cloud

**Steps:**

```bash
python -c "
from core.cloud_client import CloudClient
c = CloudClient()
embeddings = c.get_embeddings()
if embeddings:
    print(f'SUCCESS: Downloaded {len(embeddings)} embeddings')
    if embeddings:
        e = embeddings[0]
        print(f'First embedding: {e.get(\"student_id\")} - dim={len(e.get(\"embedding\", []))}')
else:
    print('FAILED: Cannot download embeddings')
"
```

**Expected Result:**
```
SUCCESS: Downloaded 150 embeddings
First embedding: STU-001 - dim=512
```

**Pass/Fail:** _______

---

### Test 1.4: Redis Connection

**Objective:** Verify Redis is running and accessible

**Steps:**

```bash
# Check Redis status
redis-cli -p 6380 ping

# Check index exists
redis-cli -p 6380 FT.INFO door_face_index

# Count documents
redis-cli -p 6380 FT.INFO door_face_index | grep num_docs
```

**Expected Result:**
```
PONG
# Index info output
num_docs: 150
```

**Pass/Fail:** _______

---

### Test 1.5: MQTT Broker

**Objective:** Verify MQTT broker is running

**Steps:**

```bash
# Check if Mosquitto is running
docker ps | grep mosquitto

# Test publish/subscribe (Terminal 1)
mosquitto_sub -h localhost -t "test/#" -v

# Test publish (Terminal 2)
mosquitto_pub -h localhost -t "test/message" -m "Hello"
```

**Expected Result:**
```
test/message Hello
```

**Pass/Fail:** _______

---

### Test 1.6: ESP32 WiFi Connection

**Objective:** Verify ESP32 connects to WiFi

**Steps:**

1. Upload `esp_door_control.ino` to ESP32
2. Open Serial Monitor (115200 baud)
3. Check WiFi connection output

**Expected Result:**
```
[WiFi] Connecting to seth
[WiFi] Connected!
[WiFi] IP Address: 192.168.1.105
```

**Pass/Fail:** _______

---

### Test 1.7: ESP32 MQTT Connection

**Objective:** Verify ESP32 connects to MQTT broker

**Steps:**

1. Check Serial Monitor output after WiFi connects
2. Verify MQTT subscription

**Expected Result:**
```
[MQTT] Connecting to 10.4.70.127:1883
[MQTT] Connected!
[MQTT] Subscribed to topics:
  - door/cmd/unlock
  - door/access/denied
  - door/sync/card_uid
  - door/alert/warning
```

**Pass/Fail:** _______

---

### Test 1.8: ESP32 RFID Reader

**Objective:** Verify RFID reader detects cards

**Steps:**

1. Present RFID card to reader
2. Check Serial Monitor

**Expected Result:**
```
[RFID] Card detected: A1B2C3D4
```

**Pass/Fail:** _______

---

### Test 1.9: ESP32 Door Lock

**Objective:** Verify door lock relay works

**Steps:**

1. Trigger unlock via MQTT:
```bash
mosquitto_pub -h localhost -t "door/cmd/unlock" \
  -m '{"door_id":"door-01","person_id":"TEST","name":"Test User","method":"face","similarity":0.85}'
```

2. Check ESP32 Serial Monitor
3. Verify relay activates (click sound)

**Expected Result:**
```
[MQTT] UNLOCK: Test User (TEST) via face
[Door] UNLOCK: Test User (TEST)
```

**Pass/Fail:** _______

---

## Phase 2: Integration Testing

### Test 2.1: Edge Full Sync

**Objective:** Verify Edge performs complete initial sync

**Steps:**

```bash
cd /home/tee/Desktop/my_project/Door-Edge_new_machine
source venv/bin/activate
python edge_app.py
```

**Expected Result:**
```
[Init] Running first embedding sync from Cloud...
[Sync] Loaded 150 students from Cloud
[Sync] Loaded 150 embeddings to Redis
[MQTT] CARD_SYNC → 150 cards sent to ESP32
[Sync] Initial sync complete — 150 faces in Redis
```

**Pass/Fail:** _______

---

### Test 2.2: Card UID Sync to ESP32

**Objective:** Verify card mappings sent to ESP32

**Steps:**

1. Start Edge system
2. Subscribe to MQTT topics:
```bash
mosquitto_sub -h localhost -t "door/sync/card_uid" -v
```

3. Check ESP32 Serial Monitor for card count

**Expected Result:**
```
[MQTT] CARD_SYNC → 150 cards sent to ESP32
[NVS] Loaded 150 cards from storage
```

**Pass/Fail:** _______

---

### Test 2.3: RFID Local Match

**Objective:** Verify RFID card matches locally on ESP32

**Steps:**

1. Ensure card is synced to ESP32 (from Test 2.2)
2. Present registered card to RFID reader
3. Check ESP32 Serial Monitor

**Expected Result:**
```
[RFID] Card detected: A1B2C3D4
[Main] RFID match: Sok Dara (STU-001)
[Door] UNLOCK: Sok Dara (STU-001)
```

**Pass/Fail:** _______

---

### Test 2.4: RFID Unknown Card

**Objective:** Verify unknown RFID card is denied

**Steps:**

1. Present unregistered card to RFID reader
2. Check ESP32 Serial Monitor
3. Check Edge MQTT logs

**Expected Result:**
```
[RFID] Card detected: FFFFFFFF
[Main] Unknown card: FFFFFF
[Door] Access denied
```

**Pass/Fail:** _______

---

### Test 2.5: Face Match Unlock

**Objective:** Verify face recognition triggers unlock

**Steps:**

1. Start Edge system with camera
2. Look at camera with registered face
3. Wait for face match (3 consecutive frames)
4. Check ESP32 unlocks

**Expected Result:**
```
# Edge output:
[Inference] Recognized Sok Dara (STU-001) sim=0.852
[Inference] Unlock was sent: Sok Dara (STU-001) sim=0.852

# ESP32 output:
[MQTT] UNLOCK: Sok Dara (STU-001) via face
[Door] UNLOCK: Sok Dara (STU-001)
```

**Pass/Fail:** _______

---

### Test 2.6: Face Unknown

**Objective:** Verify unknown face is denied

**Steps:**

1. Look at camera with unregistered face
2. Check Edge output
3. Check ESP32 output

**Expected Result:**
```
# Edge output:
[Inference] Unknown face detected
[MQTT] DENIED  → unknown_face

# ESP32 output:
[MQTT] DENIED  → unknown_face
```

**Pass/Fail:** _______

---

### Test 2.7: Event Reporting to Cloud

**Objective:** Verify access events sent to Cloud

**Steps:**

1. Trigger a face match or RFID read
2. Check Cloud for event log

**Expected Result:**
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

**Pass/Fail:** _______

---

### Test 2.8: SSE Real-Time Update

**Objective:** Verify SSE pushes member changes instantly

**Steps:**

1. Start Edge system
2. Register new student on Cloud (via Registration Software)
3. Check Edge logs for SSE event

**Expected Result:**
```
[SSE] Event: member_updated
[Sync] Member updated: STU-NEW
[Sync] Loaded 151 students from Cloud
[MQTT] CARD_SYNC → 151 cards sent to ESP32
```

**Pass/Fail:** _______

---

## Phase 3: System Testing

### Test 3.1: Full Flow - Registration to Access

**Objective:** Verify complete flow from registration to door unlock

**Steps:**

1. Register new student on Registration Software
2. Capture face and assign card
3. Wait for Cloud sync
4. Verify Edge receives data via SSE
5. Test RFID card on ESP32
6. Test face recognition on Edge

**Expected Result:**
- Student registered in Cloud
- Edge syncs new student
- ESP32 receives card mapping
- RFID unlocks door
- Face recognition unlocks door

**Pass/Fail:** _______

---

### Test 3.2: Member Deletion

**Objective:** Verify deleted member can no longer access

**Steps:**

1. Delete student from Cloud
2. Wait for SSE event
3. Test old RFID card
4. Test face recognition

**Expected Result:**
```
[SSE] Event: member_deleted
[Sync] Member deleted: STU-OLD
[FaceMatcher] Deleted STU-OLD
```

RFID card: Unknown card, access denied  
Face: Unknown face, access denied

**Pass/Fail:** _______

---

### Test 3.3: Warning Alert System

**Objective:** Verify warning alerts trigger after threshold

**Steps:**

1. Present unknown card 3 times rapidly
2. Check Edge logs
3. Check ESP32 for buzzer
4. Check Cloud for alert

**Expected Result:**
```
# Edge output:
[Alert] Unknown card attempt #1 for door-01
[Alert] Unknown card attempt #2 for door-01
[Alert] Unknown card attempt #3 for door-01
[Alert] TRIGGERED! method=rfid, fail_count=3

# ESP32 output:
[MQTT] ALERT: warning - suspicious_access (count: 3)
```

**Pass/Fail:** _______

---

### Test 3.4: Alert Counter Reset

**Objective:** Verify alert counters reset on successful access

**Steps:**

1. Present unknown card 2 times (below threshold)
2. Present known card (successful access)
3. Present unknown card 2 more times
4. Check no alert triggered

**Expected Result:**
```
[Alert] Unknown card attempt #1
[Alert] Unknown card attempt #2
[Door] UNLOCK: Sok Dara (STU-001)  ← Successful access
[Alert] Counters reset for door-01
[Alert] Unknown card attempt #1  ← Counter reset
[Alert] Unknown card attempt #2
```

No alert triggered (counter was reset)

**Pass/Fail:** _______

---

## Phase 4: Stress Testing

### Test 4.1: Rapid Card Swipes

**Objective:** Test system under rapid RFID reads

**Steps:**

1. Present card to reader rapidly (10+ times)
2. Check system stability
3. Verify no crashes or missed reads

**Expected Result:**
- All reads processed correctly
- No duplicate unlocks
- System remains stable

**Pass/Fail:** _______

---

### Test 4.2: Multiple Face Attempts

**Objective:** Test face recognition under varying conditions

**Steps:**

1. Test with good lighting
2. Test with low lighting
3. Test with face at angle
4. Test with multiple people in frame

**Expected Result:**
- Good lighting: High accuracy
- Low lighting: Graceful degradation
- Angled face: Reasonable detection
- Multiple faces: Largest face processed

**Pass/Fail:** _______

---

### Test 4.3: Network Disconnection

**Objective:** Test system behavior during network issues

**Steps:**

1. Disconnect Edge from network
2. Verify ESP32 still works for RFID (local match)
3. Reconnect Edge
4. Verify sync resumes

**Expected Result:**
- ESP32 RFID still works (local storage)
- Edge logs connection errors
- Auto-reconnect when network restored
- Sync resumes automatically

**Pass/Fail:** _______

---

### Test 4.4: ESP32 Reboot

**Objective:** Verify ESP32 recovers after reboot

**Steps:**

1. Power cycle ESP32
2. Check WiFi reconnects
3. Check MQTT reconnects
4. Check NVS cards loaded
5. Test RFID card

**Expected Result:**
```
[Init] Loaded 150 cards from storage  ← NVS persisted
[WiFi] Connected!
[MQTT] Connected!
```

RFID card works immediately

**Pass/Fail:** _______

---

## Test Results Log

### Date: _______________

| Test | Result | Notes |
|------|--------|-------|
| 1.1 Cloud API | ☐ Pass ☐ Fail | |
| 1.2 Students Download | ☐ Pass ☐ Fail | |
| 1.3 Embeddings Download | ☐ Pass ☐ Fail | |
| 1.4 Redis Connection | ☐ Pass ☐ Fail | |
| 1.5 MQTT Broker | ☐ Pass ☐ Fail | |
| 1.6 ESP32 WiFi | ☐ Pass ☐ Fail | |
| 1.7 ESP32 MQTT | ☐ Pass ☐ Fail | |
| 1.8 ESP32 RFID | ☐ Pass ☐ Fail | |
| 1.9 ESP32 Door Lock | ☐ Pass ☐ Fail | |
| 2.1 Edge Full Sync | ☐ Pass ☐ Fail | |
| 2.2 Card Sync | ☐ Pass ☐ Fail | |
| 2.3 RFID Local Match | ☐ Pass ☐ Fail | |
| 2.4 RFID Unknown | ☐ Pass ☐ Fail | |
| 2.5 Face Match | ☐ Pass ☐ Fail | |
| 2.6 Face Unknown | ☐ Pass ☐ Fail | |
| 2.7 Event Reporting | ☐ Pass ☐ Fail | |
| 2.8 SSE Update | ☐ Pass ☐ Fail | |
| 3.1 Full Flow | ☐ Pass ☐ Fail | |
| 3.2 Member Deletion | ☐ Pass ☐ Fail | |
| 3.3 Warning Alert | ☐ Pass ☐ Fail | |
| 3.4 Alert Reset | ☐ Pass ☐ Fail | |
| 4.1 Rapid Card Swipes | ☐ Pass ☐ Fail | |
| 4.2 Face Attempts | ☐ Pass ☐ Fail | |
| 4.3 Network Disconnect | ☐ Pass ☐ Fail | |
| 4.4 ESP32 Reboot | ☐ Pass ☐ Fail | |

### Issues Found

| Test | Issue | Severity | Status |
|------|-------|----------|--------|
| | | | |
| | | | |
| | | | |

### Sign-off

- **Tester:** _______________
- **Date:** _______________
- **Overall Result:** ☐ Pass ☐ Fail ☐ Partial

---

## Quick Reference Commands

### Edge Commands

```bash
# Start Edge system
cd /home/tee/Desktop/my_project/Door-Edge_new_machine
source venv/bin/activate
python edge_app.py

# Test Cloud connection
python -c "from core.cloud_client import CloudClient; print(CloudClient().get_sync_status())"

# Check Redis
redis-cli -p 6380 FT.INFO door_face_index

# Monitor MQTT
mosquitto_sub -h localhost -t "door/#" -v
```

### ESP32 Commands

```bash
# Upload firmware
# Arduino IDE → Upload button

# Monitor serial
# Arduino IDE → Serial Monitor (115200 baud)
```

### MQTT Test Commands

```bash
# Subscribe to all door topics
mosquitto_sub -h localhost -t "door/#" -v

# Send unlock command
mosquitto_pub -h localhost -t "door/cmd/unlock" \
  -m '{"door_id":"door-01","person_id":"TEST","name":"Test","method":"face","similarity":0.85}'

# Send denied command
mosquitto_pub -h localhost -t "door/access/denied" \
  -m '{"door_id":"door-01","reason":"test","timestamp":"2026-08-01T10:00:00Z"}'

# Send card sync
mosquitto_pub -h localhost -t "door/sync/card_uid" \
  -m '{"type":"add","card_uid":"TEST123","person_id":"STU-TEST","name":"Test Student"}'
```

---

**Document Version:** 1.0  
**Created:** August 1, 2026
