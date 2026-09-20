"""
config.py  —  All Door-Edge settings in one place.
Edit this file before running edge_app.py.
"""
import os

# ── Camera ────────────────────────────────────────────────────────────────────
CAMERA_INDEX  = 0
CAMERA_WIDTH  = 640
CAMERA_HEIGHT = 480
CAMERA_RETRY_DELAY = 5.0   # seconds between reopen attempts while the camera is offline
CAMERA_FAIL_LIMIT  = 30    # consecutive read failures before the device is declared dead (reopen)

# ── ROI Zone (detection area restriction) ──────────────────────────────────────
# Only detections whose bbox center falls inside the zone are processed; the
# rest of the frame is ignored. The zone is a single normalized rectangle
# (x1,y1,x2,y2, 0-1) set in calibration mode on the display (press 'c' on the
# door screen, click two corners) and stored in roi_zone.json.
ROI_ENABLED = True

# ── InsightFace (native OpenVINO IR — det_500m + rec_mbf from buffalo_sc) ─────
# Folder containing det_500m_ov.xml/.bin and rec_mbf_ov.xml/.bin (from convert.py).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSIGHTFACE_MODEL_DIR = os.path.join(BASE_DIR, "baffolo_sc_openvino_model")
INSIGHTFACE_OV_DEVICE  = "CPU"   # "CPU" or "GPU" (if Intel iGPU + drivers set up)
DET_THRESH             = 0.4
# NOTE: DET_SIZE is no longer used — det_500m_ov.xml was reshaped to a fixed
# 640x640 input in convert.py, so that's the real detector resolution now.

# ── Liveness Anti-Spoof (MiniFASNet v2 + v1se ensemble, OpenVINO IR) ──────────
# Replaces the old YOLO screen/printed_photo detector. Classifies the largest
# face crop as REAL (live person) or FAKE (printed photo / screen replay).
# Models: minifasnet_v2 (crop scale 2.7) + minifasnet_v1se (crop scale 4.0),
# converted to OpenVINO IR from the garciafido ONNX exports (80x80 BGR, /255).
LIVENESS_ENABLED    = True
LIVENESS_MODEL_DIR  = os.path.join(BASE_DIR, "liveness_models")
LIVENESS_MODELS     = ["minifasnet_v2", "minifasnet_v1se"]   # IR base names (no ext)
LIVENESS_SCALES     = {"minifasnet_v2": 2.7, "minifasnet_v1se": 4.0}  # crop margin per model
LIVENESS_INPUT_SIZE = 80    # model input (80x80x3 BGR)
LIVENESS_OV_DEVICE  = "CPU" # "CPU" or "GPU" (if Intel iGPU + drivers set up)
LIVENESS_THRESHOLD  = 0.90  # smoothed liveness score cutoff for REAL
LIVENESS_SMOOTHING  = 10    # temporal averaging window (frames, matches webcam test)
LIVENESS_FAKE_STREAK = 40    # consecutive FAKE frames before the alert chain fires
# Liveness-degraded warning: if inference errors while LIVENESS_ENABLED is
# True, the pipeline keeps running normally (fail-open) but raises a throttled
# Telegram warning so a silent model crash can't quietly kill anti-spoof.
LIVENESS_ERROR_ALERT_AFTER  = 30   # consecutive error frames before the first warning
LIVENESS_ERROR_MIN_INTERVAL = 300  # min seconds between liveness-degraded Telegram warnings
# ── Hand Verification (second confirmation step) ───────────────────────────
HAND_VERIFICATION_ENABLED = True
HAND_MODEL_PATH = os.path.join(BASE_DIR, "hand_detection_openvino_model")
HAND_CONF = 0.4
HAND_IOU = 0.5
HAND_IMGSZ = 416  # must match exported OpenVINO model input shape (fixed at 416x416)
HAND_SIDE_FACTOR = 2.0  # max hand-centre distance from face centre, in face widths
HAND_MIN_CLEARANCE = 0.3  # min gap between hand centre and face box edge, in face widths
# Relative-depth gate: min hand-box width / face-box width. Objects farther
# from the camera appear smaller — a hand well behind the face is much narrower
# than the face box, so ratio < HAND_DEPTH_RATIO rejects it. Raise to be stricter
# (0.6-0.7: only hands held close to the face plane accepted), lower to allow
# hands slightly further back.
HAND_DEPTH_RATIO = 0.4
HAND_STREAK_FRAMES       = 6   # consecutive frames (~0.6s @10fps) the hand must be HELD near the face
HAND_MAX_MOVE_PER_FRAME  = 0.5 # hand may move at most this many face-widths/frame, else it's a swipe
# ── Distance / closeness gate ───────────────────────────────────────
# Face bbox width relative to frame width. Rejects if outside range —
# also catches spoof screens/photos shoved right up to the lens.
DISTANCE_CHECK_ENABLED = True
MAX_FACE_WIDTH_RATIO    = 0.5   # reject if face wider than 20% of frame → too close
MIN_FACE_WIDTH_RATIO    = 0.1   # reject if face narrower than 6% of frame → too far

# ── Minimum detection box size (px) ─────────────────────────────────────────
# Detections narrower than these are dropped entirely — too small to be a real
# face/hand at the camera resolution — so they never reach recognition, the
# distance gates or the display. Keep MIN_FACE_BOX_SIZE below the distance
# gate's too-far cutoff (0.1 x frame width = 64px at 640px) so moderately far
# faces still get the friendly "Move closer" prompt.
MIN_FACE_BOX_SIZE = 48    # min face box width (px) accepted for recognition
MIN_HAND_BOX_SIZE = 40    # min hand box width (px) accepted
# ── Redis (native systemd on this machine) ────────────────────────────────────
REDIS_HOST       = "localhost"
REDIS_PORT       = 6379
REDIS_DB         = 0      # logical DB — 0 is production; QA tests sandbox on 15
REDIS_INDEX_NAME = "door_face_index"
VECTOR_DIM       = 512

# ── Face Matching ─────────────────────────────────────────────────────────────
SIMILARITY_THRESHOLD = 0.6  # RGB mode: strict matching (raised from 0.4)
DENIED_COOLDOWN      = 15     # seconds between denied MQTT messages
DENIED_CLOUD_COOLDOWN = 10   # seconds between denied cloud event POSTs (non-face reasons)
INFERENCE_FPS        = 15     # max inference frames/sec (throttles CPU burn)
INFERENCE_THREADS    = 4      # cap OpenVINO/OpenMP CPU threads (don't span all cores)
# INFERENCE_EVERY_N  = 3      # LEGACY/unused — throttling is now time-based (INFERENCE_FPS)

# ── Frame Confirmation ────────────────────────────────────────────────────────
# Same person must be detected consistently for CONFIRM_FRAMES before unlock.
CONFIRM_FRAMES       = 5     # consecutive inference frames required (~5s at 10fps inference)
NO_FACE_RESET_FRAMES = 10     # frames with no face before resetting confirmation state

# ── IR Mode Adaptive Thresholds ────────────────────────────────────────────────
# When the camera switches to IR/night-vision mode (low ambient light),
# all models degrade because they were trained on RGB images.
# These relaxed thresholds prevent false denies and noisy alerts in IR mode.
# Detection: mean HSV saturation < IR_SATURATION_THRESHOLD → IR mode.
IR_MODE_ENABLED          = True
IR_SATURATION_THRESHOLD  = 15     # mean HSV saturation below this = IR mode (RGB ~15-20, IR ~0-5)
IR_SIMILARITY_THRESHOLD  = 0.4     # face matching (RGB: 0.6) — ArcFace less discriminative in IR
IR_LIVENESS_THRESHOLD    = 0.50    # liveness score (RGB: 0.90) — MiniFASNet untrained on IR
IR_LIVENESS_FAKE_STREAK  = 40     # consecutive FAKE before alert (RGB: 30) — alert sooner, IR is noisier
IR_HAND_CONF             = 0.25    # hand detection confidence (RGB: 0.4) — hand model trained on RGB
IR_HAND_STREAK_FRAMES    = 4       # frames to confirm hand (RGB: 6) — less strict hold
IR_HAND_DEPTH_RATIO      = 0.4     # hand depth gate (RGB: 0.5) — accept hands slightly further back
IR_CONFIRM_FRAMES        = 3       # frames to confirm identity (RGB: 4) — faster confirmation
IR_MIN_FACE_BOX_SIZE     = 40      # min face box px (RGB: 48) — accept slightly smaller faces
IR_MIN_FACE_WIDTH_RATIO  = 0.08    # min face width ratio (RGB: 0.1) — accept faces slightly further away
IR_WARNING_THRESHOLD     = 2       # failed attempts before alert (RGB: 3) — alert sooner

# ── MQTT (Docker Mosquitto) ───────────────────────────────────────────────────
MQTT_BROKER          = "localhost"  # Docker broker on local PC
MQTT_PORT            = 1883
MQTT_TOPIC_UNLOCK    = "door/cmd/unlock"
MQTT_TOPIC_DENIED    = "door/access/denied"
MQTT_TOPIC_HEALTH    = "door/health"
MQTT_TOPIC_SYNC_CARD_UID = "door/sync/card_uid"    # Card UID sync to ESP32 (allowlist)
MQTT_TOPIC_ALERT_WARNING = "door/alert/warning"    # Warning alert to ESP32
MQTT_TOPIC_ACCESS_LOG    = "door/access/log"       # ESP32 → Edge (RFID events)
MQTT_TOPIC_STATUS        = "door/status"           # ESP32 → Edge (retained "online"/"offline", also LWT)
MQTT_TOPIC_ENROLL_CMD    = "door/cmd/enroll"       # Edge → ESP32 (arm/disarm enrolment)
MQTT_TOPIC_ENROLL_CAPTURE = "door/enroll/capture"  # ESP32 → Edge (card captured while armed)
MQTT_TOPIC_SCHEDULE_SYNC = "door/sync/schedule"   # Edge → ESP32 (weekly schedule + lockdown)
DOOR_ID              = "door-01"
ROOM_ID              = "001"    # Cloud room id used in /api/edge/events + /alert payloads
DEVICE_ID            = "esp32-001"   # device name reported in POST /api/edge/doors
                                    # (must match the room's ESP32, e.g. "esp32-001")

# ── Screen Display (Tkinter + Pillow, kiosk-style) ────────────────────────────
# Renders the fullscreen door UI (feed + status + events + credits).
# DISPLAY_ENABLED=False → headless server mode, zero rendering cost.
DISPLAY_ENABLED         = True
DISPLAY_FULLSCREEN      = True
DISPLAY_FPS             = 15          # max redraws per second
DISPLAY_FEED_SCALE      = 2           # scale the 320x240 feed for the display
DISPLAY_ACCESS_CLEAR_MS = 500        # auto-clear "last access" panel (ms)

# ── Sponsor & Credits (bottom bar of the display) ─────────────────────────────
DISPLAY_SPONSORS   = ["Dr.Thap Tharoeun ", "Prof. kuong Samnang"]  # empty list = hide the line
DISPLAY_DEVELOPERS = ["Mao Peseth", "Kouch Mengsrun", "Chay Chhunlong", "Tim Pannak"]  # placeholders
DISPLAY_VERSION    = "v1.0"
LOGO_PATH          = os.path.join(BASE_DIR, "assets", "logo.png")  # path to brand logo for kiosk header

# ── Sync: Registration_edge SQLite ────────────────────────────────────────────
# Points to the registration.db produced by your existing Registration_edge app.
# DEPRECATED: Now using Cloud API instead of local SQLite
# REGISTRATION_DB_PATH = os.path.join(
#     os.path.dirname(BASE_DIR),
#     "Registration_edge",
#     "registration.db"
# )
SYNC_INTERVAL_SECONDS = 60    # re-sync embeddings every 60 s

# ── Cloud API ──────────────────────────────────────────────────────────────────
CLOUD_API_URL        = "http://tee-doorlock-dashboard.local/"       # port 80 through Caddy
CLOUD_API_KEY        = "921987fbda42ac3d3318fac699f9d6134d8503c87481a1a9e2085d90309ec3e5"
CLOUD_SSE_URL        = f"{CLOUD_API_URL}/api/edge/sync-stream"

# ── Door Presence / Timestamps ─────────────────────────────────────────────────
# Local timezone offset sent with every event/alert/heartbeat timestamp
# (e.g. "+07:00" → 2026-08-04T09:15:00+07:00). Server reads no-offset
# timestamps as EDGE_UTC_OFFSET local time.
EDGE_UTC_OFFSET      = 7
HEARTBEAT_INTERVAL   = 60     # seconds between POST /api/edge/doors/status
PRESENCE_TTL         = 180    # server-side TTL; 3 missed beats = room offline

# ── ESP32 status alerts (offline / restored → Telegram) ────────────────────────
# The ESP32 publishes a retained door/status ("online"/"offline", also LWT).
# On an offline transition the edge waits ESP_OFFLINE_ALERT_GRACE_S to confirm
# the device is really down (it usually reconnects within seconds), then POSTs
# an /alert (method=esp_offline) that lands in Telegram. ESP_OFFLINE_ESCALATE_S
# re-alerts every N s while it stays down (0 = no escalation); returning online
# sends an esp_online alert when ESP_RESTORED_ALERT_ENABLED is set.
ESP_OFFLINE_ALERT_ENABLED  = True
ESP_OFFLINE_ALERT_GRACE_S  = 20    # confirm still offline after N s before alerting
ESP_OFFLINE_ESCALATE_S     = 1800  # re-alert every N s while still offline (0 = off)
ESP_RESTORED_ALERT_ENABLED = True

# ── Scheduled Access / Edge timing ────────────────────────────────────────────
# SCHEDULE_BUNDLE_PATH: local cache of the per-room schedule bundle so the
# door keeps enforcing while the Cloud is down (atomic write-then-rename).
# SCHEDULE_FALLBACK_OPEN: no bundle available and never synced → allow access
# (pre-schedule behaviour) + log, instead of denying everyone.
SCHEDULE_BUNDLE_PATH   = os.path.join(BASE_DIR, "schedule_bundle.json")
SCHEDULE_FALLBACK_OPEN = True

# ── Offline Event Queue (local buffer when Cloud is unreachable) ───────────────
# Events and alerts that fail to POST are written to EVENT_QUEUE_PATH (JSONL)
# and flushed to the Cloud in order once it is reachable again.
EVENT_QUEUE_PATH     = os.path.join(BASE_DIR, "event_queue.jsonl")
QUEUE_FLUSH_INTERVAL = 10     # seconds between flush attempts to Cloud
QUEUE_MAX_ENTRIES    = 10000  # max buffered entries (oldest dropped beyond this)
QUEUE_MAX_TRIES      = 30     # drop an entry after this many failed flush cycles (~5 min)

# ── Sync Settings ──────────────────────────────────────────────────────────────
SYNC_RETRY_DELAY     = 5         # seconds between retry attempts
SYNC_RETRY_MAX       = 30        # max seconds between retries
SYNC_CONSISTENCY_CHECK = 300     # check consistency every 5 minutes
ALLOWLIST_SYNC_ENABLED = True    # push GET /api/edge/allowlist cards to ESP32 on roster change

# ── Enrolment Relay ────────────────────────────────────────────────────────────
ENROLL_DEFAULT_TIMEOUT = 60      # seconds the door stays armed if SSE omits timeoutS

# ── Warning Alert Settings ────────────────────────────────────────────────────
WARNING_THRESHOLD        = 3     # failed attempts to trigger alert (faces)
WARNING_RESET_TIMEOUT    = 60    # reset counters after 1 minute (seconds)
WARNING_CAPTURE_IMAGE    = True  # capture face image on alert
ATTEMPT_MIN_INTERVAL     = 1.0   # seconds between counts of the same attempt
CARD_ALERT_EVERY_ATTEMPT = True  # True: EVERY unknown card tap alerts Telegram
                                 # False: use WARNING_THRESHOLD like faces do
# Minimum gap between unknown-face /alert triggers (per door). After a face
# alert fires, unknown-face attempts are neither counted nor logged until the
# interval elapses — alert once, then stay silent through the cooldown (no
# re-fire when it expires and no audit/Telegram records during the gap).
FACE_ALERT_MIN_INTERVAL  = 15

# ── Spoof Alert Settings ───────────────────────────────────────────────────────
# Anti-spoof detections are their own alert channel (event + /alert), fully
# decoupled from the unknown-face threshold flow. One /alert per spoof episode.
SPOOF_ALERT_ENABLED = True
SPOOF_ALERT_MIN_INTERVAL = 15  # min seconds between spoof /alerts (flicker suppression)

# ── Debug Log (diagnostics for door access decisions) ─────────────────────────
# Writes timestamped decisions (liveness scores, match similarity, schedule
# state, hand geometry) to logs/debug.log. Disable once diagnosis is done.
DEBUG_LOG_ENABLED = True
DEBUG_LOG_MAX_BYTES = 500 * 1024   # debug.log rotates (keeps newest ~500KB)
