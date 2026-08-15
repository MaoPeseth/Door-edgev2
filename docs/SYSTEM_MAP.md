# Door-Edge V2 — System Map (Flowcharts)

Rendered with Mermaid — view in GitHub, GitLab, or VSCode (Markdown preview).

---

## 1. Architecture map (3 tiers)

```mermaid
flowchart LR
    subgraph CLOUD["☁️ CLOUD — tee-doorlock-dashboard.local"]
        DB[(Members<br/>Embeddings<br/>Allowlist<br/>Access logs)]
        API["API  /api/edge/*"]
        SSE["SSE  /api/edge/sync-stream"]
        TG["Telegram alerts"]
        DB --- API --- SSE --- TG
    end

    subgraph EDGE["💻 EDGE PC — 10.4.70.114 (Ubuntu, door-edge.service)"]
        APP["edge_app.py<br/>(entry point)"]
        CAM["📷 Camera worker<br/>capture @ full fps → inference @ 10 fps"]
        INFER["Inference pipeline<br/>face → liveness (Real/Fake) → distance → match → hand → confirm"]
        REDIS[(Redis-Stack HNSW<br/>door_face_index 512-dim)]
        KIOSK["🖥️ Kiosk display (Tkinter)"]
        SYNC["sync_agent + SSE client<br/>revision-based full mirror"]
        QUEUE[(event_queue.jsonl<br/>offline buffer)]
        MQTT_EDGE["MQTT publisher<br/>(Mosquitto 10.4.70.114:1883)"]
        HB["heartbeat (60 s, TTL 180 s)"]
    end

    subgraph DOOR["🔌 ESP32-S3 DOOR CONTROLLER"]
        RELAY["Relay — lock (3 s)"]
        RFID["EM4100 RFID reader"]
        ALLOW[(LittleFS allowlist<br/>max 500 tags)]
        BUZZ["Buzzer / LEDs"]
        SW["Bypass / exit switches"]
    end

    CLOUD <-->|"HTTPS + X-API-Key<br/>/api/edge/*"| EDGE
    CLOUD <-->|"SSE sync-stream"| EDGE
    EDGE <-->|"MQTT topics<br/>door/cmd/*, door/access/*, door/status"| DOOR

    CAM --> INFER
    INFER --> REDIS
    INFER --> KIOSK
    SYNC --> REDIS
    SYNC --> MQTT_EDGE
    MQTT_EDGE <--> DOOR
    INFER --> QUEUE
    QUEUE --> CLOUD
    HB --> CLOUD
```

---

## 2. Face unlock decision flow (per inference frame)

```mermaid
flowchart TD
    START["New camera frame"] --> EMPTY{"Enrolled faces<br/>in Redis?"}
    EMPTY -->|"no"| STOP["Skip frame"]
    EMPTY -->|"yes"| FACE["InsightFace detect<br/>largest face only"]
    FACE --> NF{"Face found?"}
    NF -->|"no"| NOFACE["idle; reset after NO_FACE_RESET_FRAMES<br/>(also re-arms spoof episode detector)"]
    NF -->|"yes"| LIVE["MiniFASNet liveness<br/>80×80 crop, v2+v1se ensemble,<br/>15-frame smoothing"]
    LIVE --> S{"Score ≥ 0.99?<br/>(REAL)"}
    S -->|"no (FAKE)"| SDENY["🚫 FAKE — block ALL unlocks<br/>deny MQTT + Cloud event + photo /alert<br/>(1 per episode, 15 s min interval)"]
    S -->|"yes"| DIST{"Distance gate"}
    DIST -->|"too close (>50% width)"| CDENY["🚫 TOO CLOSE — deny + event"]
    DIST -->|"too far (<8% width)"| FAR["'Move closer' prompt"]
    DIST -->|"ok"| MATCH{"Redis HNSW match<br/>sim ≥ 0.5"}
    MATCH -->|"no match"| UK{"Hand raised<br/>near face (stable)?"}
    UK -->|"no (just passing by)"| UKSHOW["Show 'Unknown — raise hand'<br/>NO event / NO alert"]
    UK -->|"yes (access attempt)"| UKDENY["🚫 ACCESS DENIED<br/>event 1/s → /alert at 3 attempts<br/>+ photo → Telegram, then 15 s silent"]
    MATCH -->|"match"| SPOOF2{"Face still FAKE?"}
    SPOOF2 -->|"yes"| SDENY
    SPOOF2 -->|"no"| HAND{"Hand held still<br/>beside/above face<br/>(HAND_STREAK_FRAMES)"}
    HAND -->|"no"| WAIT["Show 'Raise hand to unlock'"]
    HAND -->|"yes"| CONF{"CONFIRM_FRAMES (3)<br/>consecutive frames"}
    CONF -->|"no"| SHOW["Show progress [n/3]"]
    CONF -->|"yes"| UNLOCK["🔓 door/cmd/unlock (MQTT)<br/>ESP32 relay 3 s + buzz<br/>+ Cloud access_granted (off-thread,<br/>offline → event_queue.jsonl)<br/>+ reset alert counters + alert_cleared"]
```

---

## 3. RFID unlock flow (ESP32, offline-first)

```mermaid
flowchart LR
    TAP["Card tap<br/>(EM4100 10-digit UID)"] --> LOOKUP{"UID in LittleFS<br/>allowlist?"}
    LOOKUP -->|"yes"| GRANT["🔓 Relay 3 s + short buzz<br/>publish door/access/log granted"]
    LOOKUP -->|"no"| DGRANT["🚫 Alarm buzz<br/>publish door/access/log denied"]
    GRANT --> EDGE2["Edge: display update +<br/>POST access_granted to Cloud"]
    DGRANT --> EDGE3["Edge: POST unknown_card<br/>→ Telegram (every tap)"]
    LOOKUP -.->|"works with zero network"| OK["✅ RFID works offline"]
```

---

## 4. Sync flow (Cloud → edge → ESP32)

```mermaid
flowchart TD
    C["Cloud bumps revision<br/>on any member/embedding change"] --> SSE2["SSE sync event"]
    POLL["5-minute consistency poll"] --> CMP{"revision changed?"}
    SSE2 --> CMP
    CMP -->|"no"| NOTHING["do nothing"]
    CMP -->|"yes"| FULL["Edge full-mirrors (clear + reload)"]
    FULL --> E2R["embeddings → Redis HNSW index"]
    FULL --> A2E["students + card UIDs →<br/>door/sync/card_uid full_sync (MQTT)"]
    A2E --> ESP["ESP32 atomic replace of LittleFS allowlist"]
    E2R --> RD[(door_face_index)]
```

---

## 5. Card enrolment flow

```mermaid
flowchart LR
    DASH["Dashboard starts enrolment"] -->|"SSE enroll"| EDGE["Edge arms ESP32<br/>door/cmd/enroll (60 s timeout + watchdog)"]
    EDGE -->|"armed"| ESP2["ESP32 captures card<br/>door/enroll/capture {tagID}"]
    ESP2 -->|"relay"| CLOUD2["Cloud stores UID<br/>→ disarms"]
```

---

## 6. Security / alert channels

```mermaid
flowchart LR
    subgraph CH["Alert channels"]
        UF["Unknown face —<br/>attempt 1/s, warning at 3<br/>(hand-raised only)"]
        UC["Unknown card —<br/>every denied tap"]
        SP["Fake face —<br/>score < 0.90, 5 consecutive<br/>FAKE frames, 1 per episode,<br/>15 s min interval"]
        DC["Distance gate —<br/>too close / too far"]
    end
    UF --> TG2["Telegram + photo<br/>ESP32 alarm + banner"]
    UC --> TG2
    SP --> TG2
    DC --> SCR["On-screen instruction only"]
```