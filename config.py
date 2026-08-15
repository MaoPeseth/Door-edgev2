"""
config.py  —  All Door-Edge settings in one place.
Edit this file before running edge_app.py.
"""
import os

# ── Camera ────────────────────────────────────────────────────────────────────
CAMERA_INDEX  = 0
CAMERA_WIDTH  = 320
CAMERA_HEIGHT = 240
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
DET_THRESH             = 0.6
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
LIVENESS_FAKE_STREAK = 10    # consecutive FAKE frames before the alert chain fires
# ── Hand Verification (second confirmation step) ───────────────────────────
HAND_VERIFICATION_ENABLED = True
HAND_MODEL_PATH = os.path.join(BASE_DIR, "hand_detection_openvino_model")
HAND_CONF = 0.6
HAND_IOU = 0.5
HAND_IMGSZ = 416  # must match exported OpenVINO model input shape (fixed at 416x416)
HAND_SIDE_FACTOR = 2.0  # max hand-centre distance from face centre, in face widths
HAND_MIN_CLEARANCE = 0.3  # min gap between hand centre and face box edge, in face widths
HAND_STREAK_FRAMES       = 6   # consecutive frames (~0.6s @10fps) the hand must be HELD near the face
HAND_MAX_MOVE_PER_FRAME  = 0.5 # hand may move at most this many face-widths/frame, else it's a swipe
# ── Distance / closeness gate ───────────────────────────────────────
# Face bbox width relative to frame width. Rejects if outside range —
# also catches spoof screens/photos shoved right up to the lens.
DISTANCE_CHECK_ENABLED = True
MAX_FACE_WIDTH_RATIO    = 0.5   # reject if face wider than 20% of frame → too close
MIN_FACE_WIDTH_RATIO    = 0.08   # reject if face narrower than 6% of frame → too far
# ── Redis (native systemd on this machine) ────────────────────────────────────
REDIS_HOST       = "localhost"
REDIS_PORT       = 6379
REDIS_INDEX_NAME = "door_face_index"
VECTOR_DIM       = 512

# ── Face Matching ─────────────────────────────────────────────────────────────
SIMILARITY_THRESHOLD = 0.5   # raise if too many false positives
DENIED_COOLDOWN      = 15     # seconds between denied MQTT messages
DENIED_CLOUD_COOLDOWN = 15    # seconds between denied cloud event POSTs (non-face reasons)
INFERENCE_FPS        = 10     # max inference frames/sec (throttles CPU burn)
INFERENCE_THREADS    = 4      # cap OpenVINO/OpenMP CPU threads (don't span all cores)
# INFERENCE_EVERY_N  = 3      # LEGACY/unused — throttling is now time-based (INFERENCE_FPS)

# ── Frame Confirmation ────────────────────────────────────────────────────────
# Same person must be detected consistently for CONFIRM_FRAMES before unlock.
CONFIRM_FRAMES       = 3     # consecutive inference frames required (~5s at 10fps inference)
NO_FACE_RESET_FRAMES = 10     # frames with no face before resetting confirmation state

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
DISPLAY_ACCESS_CLEAR_MS = 5000        # auto-clear "last access" panel (ms)

# ── Sponsor & Credits (bottom bar of the display) ─────────────────────────────
DISPLAY_SPONSORS   = ["Dr.Thap Tharoeun ", "Prof. kuong Samnang"]  # empty list = hide the line
DISPLAY_DEVELOPERS = ["Mao Peseth", "Kouch Mengsrun", "Chay Chhunlong", "Tim Pannak"]  # placeholders
DISPLAY_VERSION    = "v1.0"

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
