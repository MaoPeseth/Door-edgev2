"""
edge_app.py  —  Door-Edge entry point.

Startup order:
  1. Load InsightFace + YOLO models
  2. Connect to MQTT broker
  3. Register door presence + start heartbeat (room verified against Cloud)
  4. Sync face embeddings from Cloud API → Redis (blocking first sync)
  5. Start SSE listener (sync + enrolment events)
  6. Start background consistency check
  7. Start camera worker
  8. Start screen display (kiosk UI, optional)
"""
# ── OpenVINO PATH fix ─────────────────────────────────────────────────────────
# Must run FIRST before onnxruntime loads — adds openvino.dll folder to PATH.
import os
try:
    import openvino as _ov
    _ov_libs = os.path.join(os.path.dirname(_ov.__file__), "libs")
    if os.path.exists(_ov_libs):
        os.environ["PATH"] = _ov_libs + os.pathsep + os.environ.get("PATH", "")
        print(f"[OpenVINO] DLL path added: {_ov_libs}")
except ImportError:
    print("[OpenVINO] Package not found — InsightFace will use CPU")
# ─────────────────────────────────────────────────────────────────────────────

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import config as cfg

# Event-poster pool shared by the MQTT callbacks below. A burst of RFID taps
# used to spawn an unbounded daemon thread each (each Cloud POST can block
# ~30s × 3 retries); a capped pool keeps that bounded while still off the
# MQTT loop thread.
_EVENT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="CloudEvent")


def _cap_openvino_threads(n):
    """Cap OpenVINO CPU inference threads so the model runtimes don't span
    every core (avoids OpenVINO/OpenMP worker threads spinning idle)."""
    if n <= 0:
        return
    try:
        import openvino as ov
        core = ov.Core()
        core.set_property("CPU", {
            "INFERENCE_NUM_THREADS": str(n),
            "NUM_STREAMS": "1",
        })
        os.environ["OMP_NUM_THREADS"] = str(n)
        print(f"[OpenVINO] CPU threads capped to {n} (NUM_STREAMS=1)")
    except Exception as e:
        print(f"[OpenVINO] Could not set thread cap: {e}")


from core.models       import ModelHub
from core.sync_agent   import SyncAgent
from core.camera_worker import CameraWorker
from core.mqtt_publisher import MQTTPublisher
from core.face_matcher  import FaceMatcher
from core.alert_tracker import AlertTracker
from core.cloud_client  import CloudClient
from core.heartbeat     import HeartbeatReporter
from core.enroll_relay  import EnrollRelay
from core.screen_display import ScreenDisplay
from core.offline_queue import start_offline_queue, stop_offline_queue


def main():
    print("=" * 52)
    print("  Door-Edge Face Recognition System")
    print("=" * 52)

    # Cap inference threads before any model compiles (reduces CPU burn)
    _cap_openvino_threads(cfg.INFERENCE_THREADS)

    # 1. Load models
    print("\n[Init] Loading models...")
    ModelHub.load()

    # 2. Connect MQTT
    print("[Init] Connecting to MQTT broker...")
    mqtt = MQTTPublisher()
    mqtt.connect()

    # 3. Build the components (constructor I/O-free — no network yet)
    print("[Init] Building Cloud client, alert tracker, sync...")
    cloud = CloudClient()

    alert_tracker = AlertTracker(cloud_client=cloud, mqtt_publisher=mqtt)

    # Unknown RFID card → audit event + alert (every tap by default)
    def _on_rfid_denied(data):
        card_uid = data.get("card_uid", "")

        # Cloud POST must not block the MQTT loop thread when the Cloud is
        # unreachable (30s x 3 timeout cycle); report_access_denied falls
        # back to the offline queue on failure.
        def _send():
            try:
                cloud.report_access_denied("unknown_card", "card",
                                           room_id=cfg.ROOM_ID, card_uid=card_uid)
            except Exception as e:
                print(f"[Cloud] Unknown card event error: {e}")
        _EVENT_POOL.submit(_send)
        alert_tracker.record_unknown_card(card_uid)

    mqtt.set_access_denied_callback(_on_rfid_denied)

    # Successful RFID grants — closed the access-log gap: every allowlisted
    # card scan is now POSTed to the Cloud. The ESP has already opened the
    # door; this is audit/telemetry only, so it must not block the MQTT loop.
    def _on_rfid_granted(data):
        card_uid = data.get("card_uid", "")
        person_id = data.get("person_id", "")
        name = data.get("name", "")

        def _send():
            try:
                cloud.report_rfid_match(person_id, name, card_uid,
                                        room_id=cfg.ROOM_ID)
            except Exception as e:
                print(f"[Cloud] RFID granted event error: {e}")
        _EVENT_POOL.submit(_send)

    mqtt.set_rfid_granted_callback(_on_rfid_granted)

    # ESP32 availability → Telegram: offline alert (grace + escalation, handled
    # inside AlertTracker) and a restored alert when it comes back. Coexists
    # with SyncAgent's own online callback (both run on the transition).
    mqtt.set_esp_offline_callback(alert_tracker.record_esp_offline)
    mqtt.set_esp_online_callback(alert_tracker.record_esp_restored)

    # Enrolment relay (SSE enroll/enroll_cancel → ESP32 arm → Cloud capture)
    enroll_relay = EnrollRelay(cloud_client=cloud, mqtt_publisher=mqtt)
    sync = SyncAgent(mqtt_publisher=mqtt, on_enroll=enroll_relay.on_enroll,
                     on_enroll_cancel=enroll_relay.on_enroll_cancel)

    # Offline event queue — buffers events/alerts while Cloud is down and
    # flushes them automatically once it is reachable again.
    start_offline_queue(cloud)

    # 4. Camera + display FIRST — they run on the last locally-synced data,
    #    so the screen and the door work immediately even when the Cloud is
    #    offline. The blocking Cloud calls below happen after the UI is up.
    print("\n[Init] Starting camera worker...")
    if sync.policy().is_empty():
        print("[Schedule] No bundle yet — door behaves as before (allow + log)")
    worker = CameraWorker(
        mqtt_publisher=mqtt,
        cloud_client=cloud,
        alert_tracker=alert_tracker,
        schedule_policy=sync.policy()
    )
    worker.start()

    display = None
    if cfg.DISPLAY_ENABLED:
        print("[Init] Starting screen display...")
        display = ScreenDisplay(mqtt_publisher=mqtt, sync_agent=sync)
        display.start()

    # 5. Cloud-dependent init — these block on retries while the Cloud is
    #    offline, but nothing vital is on this thread anymore.
    print("[Init] Connecting to Cloud API...")
    cloud_status = cloud.get_sync_status()
    if cloud_status:
        print(f"[Init] Cloud connected — revision={cloud_status.get('revision')}")
    else:
        print("[Init] WARNING: Cannot connect to Cloud — using last local sync")

    print("[Init] Registering door presence...")
    heartbeat = HeartbeatReporter(cloud_client=cloud)
    heartbeat.register()
    heartbeat.start()

    # 6. First sync (blocking — camera + display already running on the data
    #    from the last local sync; this just refreshes it)
    print("[Init] Running first embedding sync from Cloud...")
    sync.sync_now()

    enrolled = FaceMatcher.count()
    print(f"[Init] {enrolled} face(s) enrolled in Redis")
    mqtt.publish_health(enrolled)

    # 7. Start background sync (SSE + consistency check)
    sync.start()

    # ESP presence watchdog. ESP-offline alerts were purely transition-driven
    # (online→offline on door/status / broker LWT), so an ESP that NEVER
    # connects — or silently disappears without a retained message — never
    # fired an alert and the Cloud thought the room was healthy. This loop
    # periodically treats "not online" (None/False, incl. never-seen) as an
    # offline trigger; AlertTracker's own grace→alert→escalation logic still
    # applies and its pending-timer de-dupe stops spam.
    _last_esp_probe = time.time()
    try:
        while True:
            time.sleep(1)
            if time.time() - _last_esp_probe >= cfg.HEARTBEAT_INTERVAL:
                _last_esp_probe = time.time()
                if not mqtt.is_esp_online():
                    alert_tracker.record_esp_offline()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[Edge] Shutting down...")
        if display:
            display.stop()
        worker.stop()
        sync.stop()
        enroll_relay.stop()
        heartbeat.stop()
        stop_offline_queue()
        mqtt.disconnect()
        print("[Edge] Goodbye.")
        # The display thread owns the Tcl/Tk interpreter. Skipping Python's
        # normal interpreter teardown avoids the classic "Tcl_AsyncDelete:
        # async handler deleted by the wrong thread" abort on exit. All
        # components above are already stopped and flushed.
        os._exit(0)


if __name__ == "__main__":
    main()
