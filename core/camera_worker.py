"""
core/camera_worker.py

Two-thread architecture for smooth, continuous operation:

  Capture thread  — reads camera at full fps, publishes frames to screen_state
  Inference thread — runs InsightFace + YOLO + matching as fast as possible

Both threads communicate through the thread-safe shared state in
core/screen_state.py, which the screen display thread also reads.
Rendering is done by core/screen_display.py — no OpenCV window here.

Unknown-face events carry a JPEG `face_image` (the Telegram photo); the encode
+ HTTP POST happen on a background thread so the door's critical path is never
blocked by image work.
"""
import threading
import time
import queue
from collections import deque

import cv2
import numpy as np

import config as cfg
from core.models import ModelHub
from core.face_matcher import FaceMatcher, FaceMatcherError
from core.schedule_policy import StateType
from core.face_image import encode_face_image
from core.screen_state import state
from core.debug_log import log as dbglog


_IDLE       = "idle"
_CONFIRMING = "confirming"
_UNLOCKED   = "unlocked"

# Max denied events buffered for the background denied-event poster. Each entry
# can hold a raw camera frame; an unbounded queue stalls the worker (Cloud POST
# with retries can take seconds) while unknown-face events arrive at ~1/s → the
# queue grows ~1 MB/s, i.e. tens of GB during an hour-long outage → OOM. Bounded,
# and on overflow the photo payload is dropped (text denial still queued).
_DENIED_QUEUE_MAX = 64


def _hand_center(bbox):
    x1, y1, x2, y2 = bbox
    return (x1 + x2) / 2, (y1 + y2) / 2


def _is_ir_mode(frame: np.ndarray) -> bool:
    """Detect IR/night-vision mode by mean HSV saturation.

    The KM1065's CDS sensor swaps in the IR-cut filter + IR LEDs in low light;
    the sensor then sees mostly 850nm IR, so all three BGR channels respond
    ~equally → near-zero saturation. RGB frames have mean saturation typically
    > 15-20; IR frames sit at 0-5.
    """
    if not getattr(cfg, "IR_MODE_ENABLED", True):
        return False
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    return float(hsv[:, :, 1].mean()) < getattr(cfg, "IR_SATURATION_THRESHOLD", 15)


def _hand_raised_near_face(face_bbox, hand_bbox, depth_ratio=None) -> bool:
    """The hand counts as raised when its centre sits at head height (at or
    above the chin line) within a horizontal band that extends HAND_SIDE_FACTOR
    face-widths from the face centre — so a hand raised beside the face, even
    slightly turned away or angled, works, while hands at chest level or far
    out to the side do not.

    Two relative-depth checks keep hands that are NOT at the face plane from
    counting:
      - #1 IN FRONT: a hand whose centre falls INSIDE the face box is either
        resting on / covering the face (palm occlusion) or in front of it —
        rejected. (A real raised hand beside the face never overlaps it.)
      - #2 BEHIND: by perspective, a hand further from the camera than the
        face appears narrower than the face box. If
        hand_width < HAND_DEPTH_RATIO * face_width the hand is judged to be
        behind the face and rejected.
    """
    x1, y1, x2, y2 = face_bbox
    hx1, hy1, hx2, hy2 = hand_bbox
    hx, hy = _hand_center(hand_bbox)
    face_h = y2 - y1
    if hy > y2 + 0.15 * face_h:
        return False
    face_w = x2 - x1

    # #1 — hand centre inside the face box → in front of / covering the face.
    if x1 <= hx <= x2 and y1 <= hy <= y2:
        return False

    # #2 — hand too narrow relative to the face → behind the face plane.
    depth_ratio = depth_ratio if depth_ratio is not None else cfg.HAND_DEPTH_RATIO
    hand_w = hx2 - hx1
    if hand_w < face_w * depth_ratio:
        return False

    # Hand must be BESIDE or ABOVE the face with at least HAND_MIN_CLEARANCE
    # face-widths of gap — a hand over the face (palm on forehead, facepalm)
    # or brushing the cheek/face edge does not count. The gap is measured from
    # the hand box's INNER edge (the side facing the face), not its centre —
    # hand boxes are often bigger than the hand itself, so a centre-based gap
    # lets a hand that is actually touching the face pass.
    clearance = face_w * cfg.HAND_MIN_CLEARANCE
    beside = (x1 - hx2) > clearance or (hx1 - x2) > clearance
    above = (y1 - hy2) > clearance
    if not (beside or above):
        return False
    face_cx = (x1 + x2) / 2
    band = face_w * cfg.HAND_SIDE_FACTOR
    return abs(hx - face_cx) <= band


class CameraWorker:
    def __init__(self, mqtt_publisher, cloud_client=None, alert_tracker=None,
                 schedule_policy=None):
        self._mqtt = mqtt_publisher
        self._cloud = cloud_client
        self._alert_tracker = alert_tracker
        self._schedule = schedule_policy
        self._stop = threading.Event()

        # IR/night-vision mode flag — updated in _process() from the frame's
        # HSV saturation. Drives the relaxed thresholds below.
        self._in_ir_mode = False

        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True, name="Capture")
        self._infer_thread   = threading.Thread(target=self._infer_loop,   daemon=True, name="Inference")

        # ── Inference-thread-only state (no lock needed) ───────────────────────
        self._fstate          = _IDLE
        self._last_frame_seq  = -1
        self._confirm_id      = None
        self._confirm_name    = None
        self._confirm_count   = 0
        self._no_face_count   = 0
        self._hand_streak     = 0
        self._hand_pos        = None
        self._hand_bbox       = None
        self._hand_latched    = False
        self._hand_off        = 0
        self._denied_ts       = 0.0
        self._denied_cloud_ts = {}   # reason -> last cloud POST time (15s cap)
        # Temporal liveness-score buffer (MiniFASNet REAL/FAKE smoothing) —
        # decision is mean(score) >= LIVENESS_THRESHOLD, same as the webcam test.
        self._liveness_buffer = deque(maxlen=cfg.LIVENESS_SMOOTHING)
        # Consecutive FAKE frames — the alert chain (deny + event + /alert)
        # fires only after LIVENESS_FAKE_STREAK frames, so a single bad frame
        # (blur/glare) can never raise an alert. Unlock is still blocked
        # immediately on the first FAKE frame.
        self._fake_streak = 0
        # Consecutive frames where liveness inference errored (enabled but
        # model returned None). Faces still unlock normally (fail-open), but
        # a throttled Telegram warning fires after LIVENESS_ERROR_ALERT_AFTER
        # frames so anti-spoof degradation never goes unnoticed.
        self._liveness_error_streak = 0
        self._liveness_error_ts = 0.0
        # FIFO of (reason, frame, fail_count) — a single worker POSTs them one
        # at a time so attempts reach the Cloud/Telegram in order (concurrent
        # threads could deliver #3 before #2 when HTTP latency varies).
        self._denied_queue = queue.Queue(maxsize=_DENIED_QUEUE_MAX)
        self._denied_worker = None
        # Per-key last print time — per-frame status lines are throttled so
        # the console doesn't flood at inference rate.
        self._last_logs = {}
        # Edge-timing cooldown tracking — one log line per window transition,
        # and no access events at all while cooling down.
        self._cooldown_active = None   # None = schedule not yet evaluated

    def _dbg(self, msg: str):
        """One timestamped line to logs/debug.log (never raises)."""
        dbglog(msg)

    def _schedule_snapshot(self) -> str:
        if self._schedule is not None and not self._schedule.is_empty():
            now = self._schedule.now()
            return (f"state={self._schedule.state(now)} "
                    f"granted={self._schedule.is_granted('__probe__', now)} "
                    f"in_win={self._schedule.in_run_window(now)} "
                    f"rev={self._schedule.revision()}")
        return "state=(no policy)"

    def _log(self, key: str, msg: str, interval: float = 2.0):
        """Print at most once per `interval` seconds per key."""
        now = time.time()
        if now - self._last_logs.get(key, 0.0) >= interval:
            self._last_logs[key] = now
            print(msg)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._capture_thread.start()
        self._infer_thread.start()
        self._start_denied_worker()
        print(f"[Camera] Started — capture + inference threads running "
              f"(confirm={cfg.CONFIRM_FRAMES} frames)")
        self._dbg(f"[Camera] Started — confirm={cfg.CONFIRM_FRAMES} "
                  f"liveness_thr={cfg.LIVENESS_THRESHOLD} "
                  f"schedule={self._schedule_snapshot()}")

    def stop(self):
        self._stop.set()
        self._capture_thread.join(timeout=5)
        self._infer_thread.join(timeout=5)

    # ── Denied-event delivery (ordered single worker) ─────────────────────────

    def _start_denied_worker(self):
        """One consumer thread drains the denied-event queue in order."""
        if self._denied_worker is not None:
            return
        self._denied_worker = threading.Thread(
            target=self._denied_worker_loop, daemon=True, name="DeniedEventQueue"
        )
        self._denied_worker.start()

    def _denied_worker_loop(self):
        while not self._stop.is_set():
            try:
                reason, frame, fail_count, crop = self._denied_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._report_denied_async(reason, frame, fail_count, crop)
            finally:
                self._denied_queue.task_done()

    # ── Capture thread — full camera fps, publishes to shared state ───────────

    def _open_camera(self, index: int):
        """Try the V4L2 backend first, fall back to the default backend.

        Some OpenCV builds fail to open a device with an explicit CAP_V4L2
        flag ("can't open camera by index") while the default backend works.
        """
        cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if cap.isOpened():
            return cap, "V4L2"
        cap.release()
        cap = cv2.VideoCapture(index)
        if cap.isOpened():
            return cap, "default"
        return None, None

    def _find_working_camera(self):
        """Auto-detect a working camera by trying indices 0-5.

        First tries the configured CAMERA_INDEX, then scans other indices.
        Returns (cap, backend, working_index) or (None, None, None).
        """
        # Try configured index first
        cap, backend = self._open_camera(cfg.CAMERA_INDEX)
        if cap is not None:
            return cap, backend, cfg.CAMERA_INDEX

        # Try other indices 0-5
        for idx in range(6):
            if idx == cfg.CAMERA_INDEX:
                continue
            cap, backend = self._open_camera(idx)
            if cap is not None:
                print(f"[Camera] Found working camera at index {idx}")
                return cap, backend, idx

        return None, None, None

    def _capture_loop(self):
        """Capture thread with automatic reconnect.

        The camera is opened in an outer retry loop: a failed open, or a run
        of CAMERA_FAIL_LIMIT consecutive read failures (USB reset, unplug,
        UVC glitch), releases the dead handle and retries every
        CAMERA_RETRY_DELAY seconds forever — the door resumes on its own
        once the device is back, no restart needed.
        """
        was_online = False
        cap = None
        active_index = cfg.CAMERA_INDEX
        while not self._stop.is_set():
            cap, backend, active_index = self._find_working_camera()
            if cap is None:
                if was_online:
                    was_online = False
                    state.set_frame(None)   # display shows "Waiting for camera…"
                    state.add_event("Camera offline — reconnecting", "info")
                self._log("cam_open_fail",
                          f"[Camera] No working camera found (tried 0-5) — "
                          f"retrying in {cfg.CAMERA_RETRY_DELAY:.0f}s",
                          interval=cfg.CAMERA_RETRY_DELAY)
                self._stop.wait(cfg.CAMERA_RETRY_DELAY)
                continue

            if not was_online:
                was_online = True
                state.add_event("Camera online", "info")
            print(f"[Camera] Opened index {active_index} ({backend} backend)")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  cfg.CAMERA_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.CAMERA_HEIGHT)
            print(f"[Camera] Requested {cfg.CAMERA_WIDTH}x{cfg.CAMERA_HEIGHT}")

            read_fails = 0
            while not self._stop.is_set():
                ret, frame = cap.read()
                if not ret:
                    read_fails += 1
                    if read_fails >= cfg.CAMERA_FAIL_LIMIT:
                        print("[Camera] Camera appears offline — releasing and reconnecting...")
                        cap.release()
                        cap = None
                        self._stop.wait(cfg.CAMERA_RETRY_DELAY)
                        break
                    self._stop.wait(0.05)
                    continue
                frame = cv2.flip(frame,1)
                state.set_frame(frame.copy())
                read_fails = 0

        if cap is not None:
            cap.release()

    # ── Inference thread — capped at cfg.INFERENCE_FPS ─────────────────────────

    def _infer_loop(self):
        # Cap inference to ~INFERENCE_FPS frames/sec (time-based, so it works
        # regardless of camera fps). Recognition quality is unaffected — the
        # models already bottleneck at ~10-16 fps; this just stops burning CPU.
        interval = 1.0 / max(float(getattr(cfg, "INFERENCE_FPS", 10)), 1)
        while not self._stop.is_set():
            t0 = time.time()
            seq, frame = state.get_frame()
            # Only run inference on NEW frames — while the camera is offline
            # the frame stops advancing and re-inferring the same stale frame
            # at 10 fps would burn CPU for nothing.
            if frame is not None and seq != self._last_frame_seq:
                self._last_frame_seq = seq
                try:
                    self._process(frame)
                except Exception as e:
                    # A single unexpected error must never kill recognition for
                    # the rest of the session — log and keep the loop alive.
                    self._dbg(f"[Inference] _process error: {e!r}")
                    self._log("infer_error",
                              f"[Inference] _process error (continuing): {e!r}")
            delay = interval - (time.time() - t0)
            if delay > 0:
                self._stop.wait(delay)

    # ── Core inference logic ──────────────────────────────────────────────────

    def _hand_present(self, bbox, frame, other_face_boxes=(),
                      hand_conf=None, hand_streak=None, hand_depth=None) -> bool:
        """True only when a hand has been HELD still near the face for
        HAND_STREAK_FRAMES consecutive inference frames. A hand that moves
        faster than HAND_MAX_MOVE_PER_FRAME face-widths between frames is a
        swipe (hair-brush, wave, scratch) and resets the streak, so
        pass-through motions never count.

        hand_conf: hand-detection confidence override (lower in IR mode).
        hand_streak / hand_depth: streak-length and depth-ratio overrides
        (relaxed in IR mode). None → cfg defaults.

        Once accepted the result latches True until the hand has been absent
        for HAND_STREAK_FRAMES consecutive frames (a dropped detection for
        1-2 frames no longer flips the label back and forth). The latch is
        cleared immediately when no face is in frame.

        `other_face_boxes`: bboxes of the OTHER people in frame. Hand
        detections overlapping one of those boxes are dropped — the hand
        model misreads a face as a hand, and a real raised hand never
        overlaps a face anyway."""
        hand_conf   = hand_conf   or cfg.HAND_CONF
        hand_streak = hand_streak or cfg.HAND_STREAK_FRAMES
        hand_depth  = hand_depth  or cfg.HAND_DEPTH_RATIO
        detections = ModelHub.get_hand_detections(frame, conf=hand_conf)
        if other_face_boxes:
            detections = [
                d for d in detections
                if not any(_boxes_overlap(d["bbox"], fb) for fb in other_face_boxes)
            ]
        state.set_hand_dets(detections)
        near_hands = [
            det for det in detections
            if _hand_raised_near_face(bbox, det["bbox"], depth_ratio=hand_depth)
        ]
        if detections:
            dbg_msg = (f"[HAND-DEBUG] face={tuple(bbox)} "
                       f"clearance={int((bbox[2]-bbox[0])*cfg.HAND_MIN_CLEARANCE)}px "
                       f"depth_ratio_min={hand_depth:.2f} "
                       f"hands={[tuple(map(int, d['bbox'])) for d in detections]} "
                       f"ratios={[round((d['bbox'][2]-d['bbox'][0])/(bbox[2]-bbox[0]),2) for d in detections]} "
                       f"near={[tuple(map(int, d['bbox'])) for d in near_hands]}")
            self._log("hand_debug", dbg_msg)
            self._dbg(dbg_msg)

        if near_hands:
            face_cx = (bbox[0] + bbox[2]) / 2
            hand = min(near_hands, key=lambda d: abs(_hand_center(d["bbox"])[0] - face_cx))
            hx, hy = _hand_center(hand["bbox"])
            face_w = bbox[2] - bbox[0]
            if self._hand_pos is not None:
                dx, dy = hx - self._hand_pos[0], hy - self._hand_pos[1]
                if (dx * dx + dy * dy) ** 0.5 > face_w * cfg.HAND_MAX_MOVE_PER_FRAME:
                    self._hand_streak = 0
            self._hand_pos = (hx, hy)
            self._hand_bbox = tuple(map(int, hand["bbox"]))
            self._hand_streak += 1
            if self._hand_streak >= hand_streak:
                self._hand_latched = True
            self._hand_off = 0
        else:
            self._hand_streak = 0
            self._hand_pos = None
            if self._hand_latched:
                self._hand_off += 1
                if self._hand_off >= hand_streak:
                    self._hand_latched = False
                    self._hand_off = 0
                    self._hand_bbox = None
        return self._hand_latched

    # ── Edge timing cooldown early-out ────────────────────────────────────────
    # Outside the edge run window the machine cools down: skip the whole face
    # pipeline, keep the door locked and emit NO access events (a cold overnight
    # stream of denials would be noise). One log line per window transition.
    # Re-evaluated every frame so the machine resumes the moment the window
    # opens, no restart needed.
    def _cooldown_active_state(self) -> bool:
        """True when the edge is currently cooling down (outside run window)."""
        if self._schedule is None or self._schedule.is_empty():
            return False
        return not self._schedule.in_run_window(self._schedule.now())

    def _maybe_log_cooldown_transition(self):
        if self._schedule is None or self._schedule.is_empty():
            self._cooldown_active = False
            return
        active = self._cooldown_active_state()
        if active is not self._cooldown_active:
            self._cooldown_active = active
            if active:
                print(f"[Schedule] Edge cooling — runs "
                      f"{self._schedule.run_window_label()} ({self._schedule.now():%H:%M:%S})")
                self._dbg(f"[Schedule] Edge cooling — {self._schedule.run_window_label()}")
            else:
                print(f"[Schedule] Edge resumed — run window active "
                      f"({self._schedule.now():%H:%M:%S})")
                self._dbg(f"[Schedule] Edge resumed — {self._schedule_snapshot()}")

    def _process(self, frame: np.ndarray):
        # ── IR mode detection + per-mode thresholds ────────────────────────────
        # In IR/night-vision mode every model degrades (all trained on RGB), so
        # all thresholds are relaxed to avoid false denies and noisy alerts.
        # The mode is re-detected every frame; on transition the stale liveness
        # buffer + fake streak are cleared so scores never mix across modes.
        ir_mode = _is_ir_mode(frame)
        if ir_mode != self._in_ir_mode:
            self._in_ir_mode = ir_mode
            self._liveness_buffer.clear()
            self._fake_streak = 0
            state.set_ir_mode(ir_mode)
            self._dbg(f"[Process] Mode → {'IR' if ir_mode else 'RGB'}")

        if ir_mode:
            sim_thr = cfg.IR_SIMILARITY_THRESHOLD
            live_thr  = cfg.IR_LIVENESS_THRESHOLD
            live_fk   = cfg.IR_LIVENESS_FAKE_STREAK
            hand_conf = cfg.IR_HAND_CONF
            hand_str  = cfg.IR_HAND_STREAK_FRAMES
            hand_dep  = cfg.IR_HAND_DEPTH_RATIO
            confirm_f = cfg.IR_CONFIRM_FRAMES
            min_face  = cfg.IR_MIN_FACE_BOX_SIZE
            min_ratio = cfg.IR_MIN_FACE_WIDTH_RATIO
            warn_thr  = cfg.IR_WARNING_THRESHOLD
        else:
            sim_thr = cfg.SIMILARITY_THRESHOLD
            live_thr  = cfg.LIVENESS_THRESHOLD
            live_fk   = cfg.LIVENESS_FAKE_STREAK
            hand_conf = cfg.HAND_CONF
            hand_str  = cfg.HAND_STREAK_FRAMES
            hand_dep  = cfg.HAND_DEPTH_RATIO
            confirm_f = cfg.CONFIRM_FRAMES
            min_face  = cfg.MIN_FACE_BOX_SIZE
            min_ratio = cfg.MIN_FACE_WIDTH_RATIO
            warn_thr  = cfg.WARNING_THRESHOLD

        self._maybe_log_cooldown_transition()
        if self._cooldown_active:
            state.set_results(
                [{"bbox": (0, 0, 0, 0),
                  "label": f"Edge cooling — runs {self._schedule.run_window_label()}",
                  "color": (60, 60, 60)}],
                [], _IDLE, 0
            )
            self._reset()
            return

        try:
            enrolled = FaceMatcher.count()
        except FaceMatcherError as e:
            # Redis is down — don't misread the outage as "no enrolled faces"
            # (that turned a transient blip into a dead, silent door);
            # skip recognition this frame and retry next frame.
            self._log("redis_down",
                      f"[Inference] Redis unavailable — skipping recognition ({e})")
            state.set_results([], [], _IDLE, 0)
            return
        if enrolled == 0:
            self._log("no_enrolled", "[Inference] No enrolled faces available")
            state.set_results([], [], _IDLE, 0)
            return

        faces = ModelHub.get_faces(frame, min_face_box_size=min_face)

        # ── No face ───────────────────────────────────────────────────────────
        if not faces:
            self._no_face_count += 1
            self._hand_latched = False
            self._hand_off = 0
            self._hand_bbox = None
            state.set_hand_dets([])
            self._liveness_buffer.clear()
            self._fake_streak = 0
            # No face in frame → nothing fake present → re-arm the spoof
            # episode detector (same as the old YOLO spoof_clear).
            if self._alert_tracker:
                self._alert_tracker.spoof_clear()
            if self._no_face_count >= cfg.NO_FACE_RESET_FRAMES:
                print("[Inference] Resetting due to no face for too long")
                self._reset()
            state.set_results([], [], self._fstate, self._confirm_count)
            return

        self._no_face_count = 0

        # Largest face only
        face      = max(faces, key=lambda f: _bbox_area(f.bbox))
        bbox      = tuple(map(int, face.bbox))
        embedding = face.embedding
        # Hand detections that overlap ANOTHER person's face are almost always
        # faces misread as hands (a real raised hand clears the face box by
        # HAND_MIN_CLEARANCE, so it never overlaps a face). Filtering them out
        # stops a bystander's face behind the subject from counting as a hand.
        other_face_boxes = [f.bbox for f in faces if f is not face]

        # ── Already-unlocked short-circuit ──
        # Liveness (2 MiniFASNet models) and the hand model are the two most
        # expensive steps of the pipeline. While the door is already unlocked
        # for this same person we skip distance gates, liveness and hand —
        # the door was already verified seconds ago. Only a cheap Redis match
        # runs, so the "ACCESS GRANTED" window stays up without burning CPU.
        if self._fstate == _UNLOCKED and self._confirm_id is not None:
            try:
                res = FaceMatcher.search(embedding, similarity_threshold=sim_thr)
            except FaceMatcherError:
                # Redis hiccup during the unlocked window — keep the current
                # ACCESS GRANTED label rather than collapsing it.
                res = None
            if res is not None and res["person_id"] == self._confirm_id:
                # Hard-lockdown re-check: the primary gate below is skipped by
                # this short-circuit, so a lockdown landing mid access-granted
                # window must still revoke the grant (not keep showing
                # "ACCESS GRANTED" for up to CONFIRM_FRAMES more).
                if self._schedule is not None and not self._schedule.is_empty() \
                        and self._schedule.state(self._schedule.now()) == StateType.LOCKDOWN:
                    reason = self._schedule.deny_reason(self._schedule.now())
                    self._dbg(f"[Process] SCHEDULE BLOCKED ({reason}) during unlocked window")
                    self._reset()
                    state.set_results(
                        [{"bbox": bbox, "label": f"{res['name']} SCHEDULE BLOCKED ({reason})",
                          "color": (0, 0, 255)}], [], _IDLE, 0
                    )
                    if self._send_denied(f"schedule_blocked_{reason}", frame):
                        state.add_event(f"Schedule blocked: {reason}", "deny")
                        state.set_last_access({"method": "face", "granted": False,
                            "reason": f"schedule_blocked_{reason}", "epoch": time.time(),
                            "ts": _ts()})
                    return
                state.set_results(
                    [{"bbox": bbox, "label": f"{res['name']}  ACCESS GRANTED",
                      "color": (0, 230, 0)}],
                    [], _UNLOCKED, confirm_f
                )
                return

        # ── Distance gate (far) FIRST — a far-away face is not a spoof ───────
        # The liveness crop is only 80x80; a face too far away is mostly
        # upscaled background inside that crop, which the model can misjudge
        # as FAKE → false spoof alert. Too-far faces only get a "Move closer"
        # prompt — no deny, no event, no /alert.
        dist_flag = _check_distance(bbox, frame.shape, min_ratio=min_ratio) if cfg.DISTANCE_CHECK_ENABLED else None
        if dist_flag == "too_far":
            fw = bbox[2] - bbox[0]
            self._dbg(f"[Process] too_far — face_w={fw} frame_w={frame.shape[1]} "
                      f"ratio={fw / frame.shape[1]:.3f} (min {min_ratio})")
            state.set_results(
                [{"bbox": bbox, "label": "Move closer", "color": (0, 165, 255)}],
                [], self._fstate, self._confirm_count
            )
            return

        # ── Liveness anti-spoof (REAL/FAKE) on the largest face ──────────────
        # MiniFASNet ensemble, 80x80 face crop, LIVENESS_SMOOTHING-frame score
        # averaging. A FAKE score blocks unlock immediately (spoof_present),
        # but the alert chain (MQTT deny + Cloud event + /alert photo) fires
        # only after LIVENESS_FAKE_STREAK consecutive FAKE frames — one
        # blur/glare frame can never raise an alert. FAKE is its own alert
        # channel, decoupled from the unknown-face flow below.
        spoof_present = False
        liveness = ModelHub.get_liveness(frame, bbox)
        if liveness is not None:
            self._liveness_error_streak = 0
            self._liveness_buffer.append(liveness["liveness_score"])
            avg_score = sum(self._liveness_buffer) / len(self._liveness_buffer)
            if avg_score < live_thr:
                self._fake_streak += 1
                spoof_present = True
                self._dbg(f"[Process] liveness=FAKE avg={avg_score:.3f} "
                          f"latest={liveness['liveness_score']:.3f} "
                          f"streak={self._fake_streak} thr={live_thr}")
                if self._fake_streak >= live_fk:
                    self._log("spoof", f"[Inference] FAKE face — blocking face unlock "
                                        f"(score={avg_score:.3f} < {live_thr}, "
                                        f"streak={self._fake_streak})")
                    self._reset()
                    state.set_results(
                        [{"bbox": bbox, "label": "FAKE - NO ACCESS", "color": (0, 0, 255)}],
                        [], _IDLE, 0
                    )
                    if self._send_denied("spoof_detected", frame)[0]:
                        state.add_event("Spoof object blocked", "deny")
                        state.set_last_access({
                            "method": "face", "granted": False, "reason": "spoof_detected",
                            "epoch": time.time(), "ts": _ts(),
                        })
                    # Dedicated /alert channel (once per spoof episode) — independent
                    # of the unknown-face threshold/counters.
                    if self._alert_tracker and cfg.SPOOF_ALERT_ENABLED:
                        self._alert_tracker.record_spoof(face_frame=frame)
            else:
                self._fake_streak = 0
                if self._alert_tracker:
                    # Real face this frame — re-arm the spoof episode detector.
                    self._alert_tracker.spoof_clear()
        elif getattr(ModelHub, "liveness_enabled", False):
            # Enabled but inference failed this frame — run face processing
            # normally (fail-open), but warn Telegram if it keeps failing so
            # a silent model crash can't quietly disable anti-spoof.
            self._liveness_error_streak += 1
            self._dbg(f"[Process] liveness inference error — "
                      f"streak={self._liveness_error_streak}")
            now = time.time()
            if (self._liveness_error_streak >= cfg.LIVENESS_ERROR_ALERT_AFTER
                    and now - self._liveness_error_ts >= cfg.LIVENESS_ERROR_MIN_INTERVAL):
                self._liveness_error_ts = now
                self._liveness_error_streak = 0
                self._log("liveness_error",
                          "[Inference] Liveness failing — anti-spoof degraded, "
                          "faces still processed (fail-open)")
                state.add_event("Liveness inference failing — anti-spoof degraded",
                                "deny")
                if self._cloud:
                    def _alert():
                        try:
                            self._cloud.report_suspicious_alert(
                                method="liveness_error",
                                fail_count=cfg.LIVENESS_ERROR_ALERT_AFTER)
                        except Exception as e:
                            print(f"[Cloud] Liveness-degraded alert error: {e}")
                    threading.Thread(target=_alert, daemon=True,
                                     name="LivenessErrorAlert").start()

        # ── Distance gate (close) — after liveness so a close-up photo attack
        # still triggers the FAKE channel instead of just "too close".
        if dist_flag == "too_close":
            self._log("too_close", f"[Inference] Face/object too close bbox={bbox} — blocking")
            self._dbg(f"[Process] too_close — bbox={tuple(bbox)}")
            self._reset()
            state.set_results(
                [{"bbox": bbox, "label": "TOO CLOSE - Step back", "color": (0, 165, 255)}],
                [], _IDLE, 0
            )
            if self._send_denied("too_close")[0]:
                state.add_event("Person too close", "deny")
                state.set_last_access({
                    "method": "face", "granted": False, "reason": "too_close",
                    "epoch": time.time(), "ts": _ts(),
                })
            return

        # Note: Anti-spoof was already checked above (per-face liveness).
        # The unknown-face flow runs even while the face is FAKE.

        # ── Face match ────────────────────────────────────────────────────────
        try:
            result = FaceMatcher.search(embedding, similarity_threshold=sim_thr)
        except FaceMatcherError as e:
            # Redis search error (NOT "no match"): a known user must not be
            # routed into the unknown-face deny/alert flow during an outage.
            self._log("redis_down",
                      f"[Inference] Redis search unavailable ({e})")
            state.set_results([], [], _IDLE, 0)
            return
        if result is None:
            if spoof_present:
                # Face is FAKE — the spoof event/alert already fired above;
                # don't also run the unknown-face flow (no duplicate
                # "Unknown Face" events or Telegram messages).
                return
            self._log("unknown_face", f"[Inference] Unknown face detected for bbox={bbox}")
            self._dbg(f"[Process] UNKNOWN face bbox={tuple(bbox)} "
                      f"liveness_avg={sum(self._liveness_buffer) / max(len(self._liveness_buffer), 1):.3f} "
                      f"spoof={spoof_present}")

            # Only treat it as an access attempt when the person signals intent
            # (hand near face, stable across consecutive frames). A stranger
            # merely walking past is shown as "Unknown" but is NOT reported to
            # Cloud / alerted / MQTT-denied.
            # A recognized user may hold hand-state (streak/latch) from their
            # confirmation flow. A different (unknown) subject must not inherit
            # it, so drop it here — but do NOT reset it across *consecutive*
            # unknown-face frames, or this stranger's held hand could never
            # accumulate enough streak frames to register as a raised hand.
            if self._confirm_id is not None:
                self._hand_latched = False
                self._hand_streak  = 0
                self._hand_pos     = None
                self._hand_bbox    = None

            hand_near_face = False
            if cfg.HAND_VERIFICATION_ENABLED:
                hand_near_face = self._hand_present(
                    bbox, frame, other_face_boxes,
                    hand_conf=hand_conf, hand_streak=hand_str, hand_depth=hand_dep)
            is_access_attempt = (not cfg.HAND_VERIFICATION_ENABLED) or hand_near_face

            self._reset_fstate()
            if not is_access_attempt:
                state.set_results(
                    [{"bbox": bbox, "label": "Unknown  Raise hand to access", "color": (0, 140, 255)}],
                    [], _IDLE, 0
                )
                return

            state.set_results(
                [{"bbox": bbox, "label": "Unknown  ACCESS DENIED", "color": (0, 0, 255)}],
                [], _IDLE, 0
            )
            # Count EVERY hand-raised unknown-face attempt toward the warning
            # alert (returns the attempt number for the /event detail; 0 while
            # the face alert is silenced by cooldown). Each counted attempt
            # posts its own /event with the count; /alert fires at threshold.
            fail_count = 0
            crop = _crop_region(bbox, self._hand_bbox, frame.shape)
            if self._alert_tracker:
                fail_count = self._alert_tracker.record_unknown_face(
                    embedding, frame, face_crop=crop, warning_threshold=warn_thr)
            mqtt_sent, cloud_posted = self._send_denied(
                "unknown_face", frame, fail_count=fail_count, crop=crop)
            if mqtt_sent:
                state.add_event("Unknown face", "deny")
                state.set_last_access({
                    "method": "face", "granted": False, "reason": "unknown_face",
                    "epoch": time.time(), "ts": _ts(),
                })
            return

        person_id  = result["person_id"]
        name       = result["name"]
        similarity = result["similarity"]
        self._log("recognized", f"[Inference] Recognized {name} ({person_id}) sim={similarity:.3f}")
        self._dbg(f"[Process] MATCH {name} ({person_id}) sim={similarity:.3f} "
                  f"liveness_avg={sum(self._liveness_buffer) / max(len(self._liveness_buffer), 1):.3f}")

        # Anti-spoof: a recognized face never unlocks while the face is FAKE
        # (the live person may still be reported via the denied flow, but
        # access stays blocked).
        if spoof_present:
            state.set_results(
                [{"bbox": bbox, "label": "FAKE - NO ACCESS", "color": (0, 0, 255)}],
                [], _IDLE, 0
            )
            return

        # ── Hard lockdown short-circuit ──────────────────────────────────────
        # In an active hard lockdown we deny immediately (without waiting out
        # CONFIRM_FRAMES). Re-checked at the primary gate per frame too, so a
        # lockdown landing mid-confirmation is still caught.
        if self._schedule is not None and not self._schedule.is_empty() \
                and self._schedule.state(self._schedule.now()) == StateType.LOCKDOWN:
            reason = self._schedule.deny_reason(self._schedule.now())
            self._dbg(f"[Process] SCHEDULE BLOCKED ({reason}) for {name} — "
                      f"{self._schedule_snapshot()}")
            self._reset()
            state.set_results(
                [{"bbox": bbox, "label": f"{name} SCHEDULE BLOCKED ({reason})",
                  "color": (0, 0, 255)}], [], _IDLE, 0
            )
            if self._send_denied(f"schedule_blocked_{reason}", frame):
                state.add_event(f"Schedule blocked: {reason}", "deny")
                state.set_last_access({"method": "face", "granted": False,
                    "reason": f"schedule_blocked_{reason}", "epoch": time.time(),
                    "ts": _ts()})
            print(f"[Inference] {name} denied by schedule ({reason})")
            return

        # ── Already unlocked ──────────────────────────────────────────────────
        if self._fstate == _UNLOCKED and person_id == self._confirm_id:
            state.set_results(
                [{"bbox": bbox, "label": f"{name}  ACCESS GRANTED", "color": (0, 230, 0)}],
                [], _UNLOCKED, confirm_f
            )
            return

        # ── Hand verification (second confirmation step) ─────────────────────
        # The hand-streak latch belongs to the current confirm subject: if the
        # face changed, the previous person's latch must not carry over (the
        # new person has to raise their own hand).
        if self._confirm_id is not None and person_id != self._confirm_id:
            self._hand_latched = False
            self._hand_streak  = 0
            self._hand_pos     = None
            self._hand_bbox    = None
            state.set_hand_dets([])

        if cfg.HAND_VERIFICATION_ENABLED:
            if self._confirm_id == person_id and self._confirm_count > 0:
                # Mid-confirmation for the same person — the hand was just
                # accepted by the streak and stays latched; skip the (expensive)
                # hand model for the few remaining confirm frames (~0.4s).
                hand_verified = self._hand_latched
            else:
                hand_verified = self._hand_present(
                    bbox, frame, other_face_boxes,
                    hand_conf=hand_conf, hand_streak=hand_str, hand_depth=hand_dep)

            if not hand_verified:
                self._log("wait_hand", f"[Inference] {name} recognized, waiting for hand near face")
                self._dbg(f"[Process] {name} waiting for hand — confirm_id={self._confirm_id} "
                          f"count={self._confirm_count} latched={self._hand_latched}")
                self._confirm_id    = person_id
                self._confirm_name  = name
                self._confirm_count = 0
                self._fstate        = _IDLE
                state.set_results(
                    [{"bbox": bbox, "label": f"{name}  Status: Raise hand to unlock", "color": (0, 200, 255)}],
                    [], _IDLE, 0
                )
                return

            self._log("hand_accepted", f"[Inference] Hand accepted near {name}; proceeding to unlock check")
            state.set_results(
                [{"bbox": bbox, "label": f"{name}  Status: Hand accepted", "color": (0, 200, 255)}],
                [], _IDLE, 0
            )
        # ── Frame confirmation ─────────────────────────────────────────────────
        if person_id == self._confirm_id:
            self._confirm_count += 1
        else:
            self._confirm_id    = person_id
            self._confirm_name  = name
            self._confirm_count = 1
            self._fstate        = _CONFIRMING

        ratio = min(self._confirm_count / confirm_f, 1.0)
        color = (0, int(220 * ratio), int(220 * (1.0 - ratio)))

        if self._confirm_count >= confirm_f:
            # ── Schedule gate ────────────────────────────────────────────────
            # The unlock decision is re-evaluated per frame at this gate, so a
            # lockdown or restriction landing mid-confirmation takes effect on
            # the very next frame. A denied attempt resets the confirmation
            # cycle and reports one schedule_blocked_<STATE> event.
            if self._schedule is not None and not self._schedule.is_empty() \
                    and not self._schedule.is_granted(person_id, self._schedule.now()):
                reason = self._schedule.deny_reason(self._schedule.now())
                self._dbg(f"[Process] SCHEDULE BLOCKED ({reason}) for {name} "
                          f"(sim={similarity:.3f}) — {self._schedule_snapshot()}")
                self._reset()
                state.set_results(
                    [{"bbox": bbox, "label": f"{name} SCHEDULE BLOCKED ({reason})",
                      "color": (0, 0, 255)}], [], _IDLE, 0
                )
                if self._send_denied(f"schedule_blocked_{reason}", frame):
                    state.add_event(f"Schedule blocked: {reason}", "deny")
                    state.set_last_access({"method": "face", "granted": False,
                        "reason": f"schedule_blocked_{reason}", "epoch": time.time(),
                        "ts": _ts()})
                print(f"[Inference] {name} denied by schedule ({reason})")
                return

            self._fstate = _UNLOCKED
            state.set_results(
                [{"bbox": bbox, "label": f"{name}  Status: UNLOCK SENT  {similarity:.2f}", "color": (0, 255, 0)}],
                [], _UNLOCKED, confirm_f
            )
            self._mqtt.publish_unlock(person_id, name, similarity)
            print(f"[Inference] Unlock was sent: {name} ({person_id})  sim={similarity:.3f}")
            self._dbg(f"[Process] UNLOCK SENT {name} ({person_id}) sim={similarity:.3f} "
                      f"confirm={self._confirm_count} — {self._schedule_snapshot()}")
            # Report to Cloud off the inference thread — an unreachable Cloud
            # (30s x 3 timeout cycle) must never freeze the door; failures
            # fall back to the offline queue inside report_face_match.
            if self._cloud:
                threading.Thread(
                    target=self._cloud.report_face_match,
                    args=(person_id, name, similarity),
                    daemon=True, name="AccessEvent",
                ).start()
            # Reset alert counters on successful access
            if self._alert_tracker:
                self._alert_tracker.reset_counters()
                self._mqtt.publish_alert_cleared(reason="access_granted")
            # Update display state
            state.set_last_access({
                "name": name, "person_id": person_id, "method": "face",
                "similarity": round(similarity, 4), "granted": True,
                "epoch": time.time(), "ts": _ts(),
            })
            state.add_event(f"{name} ({person_id}) sim={similarity:.2f}", "ok")
        else:
            label = f"{name}  [{self._confirm_count}/{confirm_f}]  {similarity:.2f}"
            state.set_results(
                [{"bbox": bbox, "label": label, "color": color}],
                [], _CONFIRMING, self._confirm_count
            )

    # ── State reset ───────────────────────────────────────────────────────────

    def _reset_fstate(self):
        """Reset only the confirmation state (person/confirm window).

        Unlike `_reset()`, this leaves the hand-state fields (`_hand_streak`,
        `_hand_latched`, ...) untouched so a stranger's held hand can keep
        accumulating streak frames across consecutive unknown-face frames."""
        if self._fstate != _IDLE:
            self._fstate         = _IDLE
            self._confirm_id     = None
            self._confirm_name   = None
            self._confirm_count  = 0

    def _reset(self):
        if self._fstate != _IDLE:
            self._fstate         = _IDLE
            self._confirm_id     = None
            self._confirm_name   = None
            self._confirm_count  = 0
        self._hand_latched = False
        self._hand_streak  = 0
        self._hand_pos     = None
        self._hand_bbox    = None
        state.set_hand_dets([])

    def _send_denied(self, reason: str, frame: np.ndarray = None,
                     fail_count: int = None, crop: tuple = None) -> tuple:
        """Send a denied MQTT message (respecting cooldown).

        Returns (mqtt_sent, cloud_posted). The Cloud event POST (with the JPEG
        face_image, when a frame is given) runs on a background thread — the
        encode and upload must not sit on the frame's critical path.

        fail_count: for unknown_face each counted attempt posts its own /event
        (with the attempt number as detail). Other reasons keep a 15s
        per-reason cooldown.

        crop: optional (x1, y1, x2, y2) region of `frame` to send — the
        denied photo shows only the person (face + raised hand), not the
        whole camera view.
        """
        now = time.time()
        mqtt_sent = now - self._denied_ts >= cfg.DENIED_COOLDOWN
        if mqtt_sent:
            self._denied_ts = now
            self._mqtt.publish_denied(reason)
        post_cloud = self._cloud is not None
        if reason == "unknown_face" and (
                not fail_count
                or (self._alert_tracker
                    and (self._alert_tracker.is_incident_active()
                         or self._alert_tracker.is_face_alert_cooldown()))):
            # Silenced (cooldown / no counted attempt) — don't POST (no
            # audit-log record and no Telegram photo during the quiet gap).
            post_cloud = False
        elif reason != "unknown_face" and now - self._denied_cloud_ts.get(reason, 0) < cfg.DENIED_CLOUD_COOLDOWN:
            # Counted unknown_face attempts always POST their own event (each
            # carries the attempt number); the 15s cap applies to other reasons.
            post_cloud = False
        if post_cloud:
            self._denied_cloud_ts[reason] = now
            # Pre-crop BEFORE enqueueing so the queue stores only the small
            # region copy, not the full 640×480 frame per event (the unbounded
            # queue holding full frames was what OOM'd the process on a long
            # Cloud outage). The payload crop is then cleared so the poster
            # doesn't crop twice.
            if frame is not None and crop is not None:
                x1, y1, x2, y2 = crop
                fh, fw = frame.shape[:2]
                x1 = min(max(0, x1), fw); x2 = min(max(x1, x2), fw)
                y1 = min(max(0, y1), fh); y2 = min(max(y1, y2), fh)
                if x2 > x1 and y2 > y1:
                    frame = frame[y1:y2, x1:x2].copy()
                    crop  = None
            try:
                self._denied_queue.put_nowait((reason, frame, fail_count, crop))
            except queue.Full:
                # Saturated (Cloud down, poster stalled behind retries). Drop
                # the photo payload but keep the text denial — and if even that
                # is full, drop the event rather than blocking the inference
                # thread or letting memory grow.
                try:
                    self._denied_queue.put_nowait((reason, None, fail_count, None))
                except queue.Full:
                    pass
        return mqtt_sent, post_cloud

    def _report_denied_async(self, reason: str, frame: np.ndarray,
                             fail_count: int = None, crop: tuple = None):
        """Encode the (cropped) frame and POST the denied event off the
        inference thread."""
        try:
            face_image = encode_face_image(frame, crop=crop) if frame is not None else None
            self._cloud.report_access_denied(reason, "face", face_image=face_image,
                                             fail_count=fail_count)
        except Exception as e:
            print(f"[Cloud] Denied event error: {e}")


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _bbox_area(bbox) -> float:
    x1, y1, x2, y2 = bbox
    return (x2 - x1) * (y2 - y1)


def _boxes_overlap(a, b, iou_thresh: float = 0.3) -> bool:
    """True when two bboxes overlap by more than `iou_thresh` IoU."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = _bbox_area(a) + _bbox_area(b) - inter
    if union <= 0:
        return False
    return (inter / union) > iou_thresh


def _crop_region(face_bbox, hand_bbox, frame_shape) -> tuple:
    """Bounding box around the person's face and raised hand, expanded 40%
    of the box size on every side for a more natural photo, clamped to the
    frame. Falls back to the face alone when no hand is present. Returns
    (x1, y1, x2, y2) for slicing the frame."""
    xs = [face_bbox[0], face_bbox[2]]
    ys = [face_bbox[1], face_bbox[3]]
    if hand_bbox is not None:
        xs += [hand_bbox[0], hand_bbox[2]]
        ys += [hand_bbox[1], hand_bbox[3]]
    box_w = max(xs) - min(xs)
    box_h = max(ys) - min(ys)
    pad_x = int(0.4 * box_w)
    pad_y = int(0.4 * box_h)
    h, w = frame_shape[:2]
    x1 = max(0, min(xs) - pad_x)
    y1 = max(0, min(ys) - pad_y)
    x2 = min(w, max(xs) + pad_x)
    y2 = min(h, max(ys) + pad_y)
    return (x1, y1, x2, y2)


def _check_distance(bbox, frame_shape, min_ratio=None) -> str | None:
    """Returns 'too_close', 'too_far', or None if within acceptable range."""
    frame_w = frame_shape[1]
    face_w  = bbox[2] - bbox[0]
    ratio   = face_w / frame_w
    min_ratio = min_ratio if min_ratio is not None else cfg.MIN_FACE_WIDTH_RATIO
    if ratio > cfg.MAX_FACE_WIDTH_RATIO:
        return "too_close"
    if ratio < min_ratio:
        return "too_far"
    return None
