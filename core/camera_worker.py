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
from core.face_matcher import FaceMatcher
from core.face_image import encode_face_image
from core.screen_state import state


_IDLE       = "idle"
_CONFIRMING = "confirming"
_UNLOCKED   = "unlocked"


def _hand_center(bbox):
    x1, y1, x2, y2 = bbox
    return (x1 + x2) / 2, (y1 + y2) / 2


def _hand_raised_near_face(face_bbox, hand_bbox) -> bool:
    """The hand counts as raised when its centre sits at head height (at or
    above the chin line) within a horizontal band that extends HAND_SIDE_FACTOR
    face-widths from the face centre — so a hand raised beside the face, even
    slightly turned away or angled, works, while hands at chest level or far
    out to the side do not."""
    x1, y1, x2, y2 = face_bbox
    hx1, hy1, hx2, hy2 = hand_bbox
    hx, hy = _hand_center(hand_bbox)
    face_h = y2 - y1
    if hy > y2 + 0.15 * face_h:
        return False
    # Hand must be BESIDE or ABOVE the face with at least HAND_MIN_CLEARANCE
    # face-widths of gap — a hand over the face (palm on forehead, facepalm)
    # or brushing the cheek/face edge does not count. The gap is measured from
    # the hand box's INNER edge (the side facing the face), not its centre —
    # hand boxes are often bigger than the hand itself, so a centre-based gap
    # lets a hand that is actually touching the face pass.
    face_w = x2 - x1
    clearance = face_w * cfg.HAND_MIN_CLEARANCE
    beside = (x1 - hx2) > clearance or (hx1 - x2) > clearance
    above = (y1 - hy2) > clearance
    if not (beside or above):
        return False
    face_cx = (x1 + x2) / 2
    band = face_w * cfg.HAND_SIDE_FACTOR
    return abs(hx - face_cx) <= band


class CameraWorker:
    def __init__(self, mqtt_publisher, cloud_client=None, alert_tracker=None):
        self._mqtt = mqtt_publisher
        self._cloud = cloud_client
        self._alert_tracker = alert_tracker
        self._stop = threading.Event()

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
        # FIFO of (reason, frame, fail_count) — a single worker POSTs them one
        # at a time so attempts reach the Cloud/Telegram in order (concurrent
        # threads could deliver #3 before #2 when HTTP latency varies).
        self._denied_queue = queue.Queue()
        self._denied_worker = None
        # Per-key last print time — per-frame status lines are throttled so
        # the console doesn't flood at inference rate.
        self._last_logs = {}

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
        while not self._stop.is_set():
            cap, backend = self._open_camera(cfg.CAMERA_INDEX)
            if cap is None:
                if was_online:
                    was_online = False
                    state.set_frame(None)   # display shows "Waiting for camera…"
                    state.add_event("Camera offline — reconnecting", "info")
                self._log("cam_open_fail",
                          f"[Camera] Cannot open camera index {cfg.CAMERA_INDEX} — "
                          f"retrying in {cfg.CAMERA_RETRY_DELAY:.0f}s",
                          interval=cfg.CAMERA_RETRY_DELAY)
                self._stop.wait(cfg.CAMERA_RETRY_DELAY)
                continue

            if not was_online:
                was_online = True
                state.add_event("Camera online", "info")
            print(f"[Camera] Opened index {cfg.CAMERA_INDEX} ({backend} backend)")
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
                read_fails = 0
                state.set_frame(frame.copy())

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
                self._process(frame)
            delay = interval - (time.time() - t0)
            if delay > 0:
                self._stop.wait(delay)

    # ── Core inference logic ──────────────────────────────────────────────────

    def _hand_present(self, bbox, frame, other_face_boxes=()) -> bool:
        """True only when a hand has been HELD still near the face for
        HAND_STREAK_FRAMES consecutive inference frames. A hand that moves
        faster than HAND_MAX_MOVE_PER_FRAME face-widths between frames is a
        swipe (hair-brush, wave, scratch) and resets the streak, so
        pass-through motions never count.

        Once accepted the result latches True until the hand has been absent
        for HAND_STREAK_FRAMES consecutive frames (a dropped detection for
        1-2 frames no longer flips the label back and forth). The latch is
        cleared immediately when no face is in frame.

        `other_face_boxes`: bboxes of the OTHER people in frame. Hand
        detections overlapping one of those boxes are dropped — the hand
        model misreads a face as a hand, and a real raised hand never
        overlaps a face anyway."""
        detections = ModelHub.get_hand_detections(frame)
        if other_face_boxes:
            detections = [
                d for d in detections
                if not any(_boxes_overlap(d["bbox"], fb) for fb in other_face_boxes)
            ]
        near_hands = [
            det for det in detections
            if _hand_raised_near_face(bbox, det["bbox"])
        ]
        if detections:
            self._log("hand_debug",
                      f"[HAND-DEBUG] face={tuple(bbox)} clearance={int((bbox[2]-bbox[0])*cfg.HAND_MIN_CLEARANCE)}px "
                      f"hands={[tuple(map(int, d['bbox'])) for d in detections]} "
                      f"near={[tuple(map(int, d['bbox'])) for d in near_hands]}")

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
            if self._hand_streak >= cfg.HAND_STREAK_FRAMES:
                self._hand_latched = True
            self._hand_off = 0
        else:
            self._hand_streak = 0
            self._hand_pos = None
            if self._hand_latched:
                self._hand_off += 1
                if self._hand_off >= cfg.HAND_STREAK_FRAMES:
                    self._hand_latched = False
                    self._hand_off = 0
                    self._hand_bbox = None
        return self._hand_latched

    def _process(self, frame: np.ndarray):
        if FaceMatcher.count() == 0:
            print("[Inference] No enrolled faces available")
            state.set_results([], [], _IDLE, 0)
            return

        faces = ModelHub.get_faces(frame)

        # ── No face ───────────────────────────────────────────────────────────
        if not faces:
            self._no_face_count += 1
            self._hand_latched = False
            self._hand_off = 0
            self._hand_bbox = None
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

        # ── Distance gate (far) FIRST — a far-away face is not a spoof ───────
        # The liveness crop is only 80x80; a face too far away is mostly
        # upscaled background inside that crop, which the model can misjudge
        # as FAKE → false spoof alert. Too-far faces only get a "Move closer"
        # prompt — no deny, no event, no /alert.
        dist_flag = _check_distance(bbox, frame.shape) if cfg.DISTANCE_CHECK_ENABLED else None
        if dist_flag == "too_far":
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
            self._liveness_buffer.append(liveness["liveness_score"])
            avg_score = sum(self._liveness_buffer) / len(self._liveness_buffer)
            if avg_score < cfg.LIVENESS_THRESHOLD:
                self._fake_streak += 1
                spoof_present = True
                if self._fake_streak >= cfg.LIVENESS_FAKE_STREAK:
                    self._log("spoof", f"[Inference] FAKE face — blocking face unlock "
                                        f"(score={avg_score:.3f} < {cfg.LIVENESS_THRESHOLD}, "
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

        # ── Distance gate (close) — after liveness so a close-up photo attack
        # still triggers the FAKE channel instead of just "too close".
        if dist_flag == "too_close":
            self._log("too_close", f"[Inference] Face/object too close bbox={bbox} — blocking")
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
        result = FaceMatcher.search(embedding)
        if result is None:
            if spoof_present:
                # Face is FAKE — the spoof event/alert already fired above;
                # don't also run the unknown-face flow (no duplicate
                # "Unknown Face" events or Telegram messages).
                return
            self._log("unknown_face", f"[Inference] Unknown face detected for bbox={bbox}")

            # Only treat it as an access attempt when the person signals intent
            # (hand near face, stable across consecutive frames). A stranger
            # merely walking past is shown as "Unknown" but is NOT reported to
            # Cloud / alerted / MQTT-denied.
            hand_near_face = False
            if cfg.HAND_VERIFICATION_ENABLED:
                hand_near_face = self._hand_present(bbox, frame, other_face_boxes)
            is_access_attempt = (not cfg.HAND_VERIFICATION_ENABLED) or hand_near_face

            self._reset()
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
                    embedding, frame, face_crop=crop)
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

        # Anti-spoof: a recognized face never unlocks while the face is FAKE
        # (the live person may still be reported via the denied flow, but
        # access stays blocked).
        if spoof_present:
            state.set_results(
                [{"bbox": bbox, "label": "FAKE - NO ACCESS", "color": (0, 0, 255)}],
                [], _IDLE, 0
            )
            return

        # ── Already unlocked ──────────────────────────────────────────────────
        if self._fstate == _UNLOCKED and person_id == self._confirm_id:
            state.set_results(
                [{"bbox": bbox, "label": f"{name}  ACCESS GRANTED", "color": (0, 230, 0)}],
                [], _UNLOCKED, cfg.CONFIRM_FRAMES
            )
            return

        # ── Hand verification (second confirmation step) ─────────────────────
        if cfg.HAND_VERIFICATION_ENABLED:
            hand_verified = self._hand_present(bbox, frame, other_face_boxes)

            if not hand_verified:
                self._log("wait_hand", f"[Inference] {name} recognized, waiting for hand near face")
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

        ratio = min(self._confirm_count / cfg.CONFIRM_FRAMES, 1.0)
        color = (0, int(220 * ratio), int(220 * (1.0 - ratio)))

        if self._confirm_count >= cfg.CONFIRM_FRAMES:
            self._fstate = _UNLOCKED
            state.set_results(
                [{"bbox": bbox, "label": f"{name}  Status: UNLOCK SENT  {similarity:.2f}", "color": (0, 255, 0)}],
                [], _UNLOCKED, cfg.CONFIRM_FRAMES
            )
            self._mqtt.publish_unlock(person_id, name, similarity)
            print(f"[Inference] Unlock was sent: {name} ({person_id})  sim={similarity:.3f}")
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
            label = f"{name}  [{self._confirm_count}/{cfg.CONFIRM_FRAMES}]  {similarity:.2f}"
            state.set_results(
                [{"bbox": bbox, "label": label, "color": color}],
                [], _CONFIRMING, self._confirm_count
            )

    # ── State reset ───────────────────────────────────────────────────────────

    def _reset(self):
        if self._fstate != _IDLE:
            self._fstate         = _IDLE
            self._confirm_id     = None
            self._confirm_name   = None
            self._confirm_count  = 0

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
            self._denied_queue.put((reason, frame, fail_count, crop))
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


def _check_distance(bbox, frame_shape) -> str | None:
    """Returns 'too_close', 'too_far', or None if within acceptable range."""
    frame_w = frame_shape[1]
    face_w  = bbox[2] - bbox[0]
    ratio   = face_w / frame_w
    if ratio > cfg.MAX_FACE_WIDTH_RATIO:
        return "too_close"
    if ratio < cfg.MIN_FACE_WIDTH_RATIO:
        return "too_far"
    return None
