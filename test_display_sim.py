"""
test_display_sim.py — standalone test for the screen display UI.

Feeds synthetic frames and events into core.screen_state so the
fullscreen display can be tested WITHOUT camera / Redis / Cloud / MQTT.

Usage:
    python test_display_sim.py            # fullscreen
    python test_display_sim.py --window   # windowed
    python test_display_sim.py --seconds 15
    python test_display_sim.py --fps 10   # lower render rate
"""
import argparse
import threading
import time

import cv2
import numpy as np

import config as cfg
from core.screen_state import state
from core.screen_display import ScreenDisplay


def feed_loop(stop, fps=15):
    """Publish a synthetic frame at ~fps."""
    interval = 1.0 / max(fps, 1)
    while not stop.is_set():
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.putText(frame, "SIM TEST", (30, 130), cv2.FONT_HERSHEY_SIMPLEX,
                    1.2, (0, 255, 0), 2)
        state.set_frame(frame)
        time.sleep(interval)


def event_loop(stop):
    """Simulate access events / alerts / last-access updates."""
    step = 0
    while not stop.is_set():
        step += 1
        if step % 10 == 0:
            state.set_last_access({
                "name": "Sok Dara", "method": "face",
                "similarity": 0.8521, "granted": True,
                "epoch": time.time(), "ts": time.strftime("%H:%M:%S"),
            })
        if step % 30 == 0:
            state.set_last_access({
                "method": "rfid", "granted": False, "reason": "unknown_card",
                "epoch": time.time(), "ts": time.strftime("%H:%M:%S"),
            })
        if step % 15 == 0:
            state.add_event("Sok Dara (STU-001) sim=0.85", "ok")
        if step % 25 == 0:
            state.add_event("Unknown card", "deny")
        if step % 40 == 0:
            state.set_alert(True, "3 unknown attempts")
        if step % 60 == 0:
            state.set_alert(False, "")
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Screen display UI simulator")
    parser.add_argument("--window", action="store_true",
                        help="windowed instead of fullscreen")
    parser.add_argument("--seconds", type=int, default=0,
                        help="auto-close after N seconds (0 = run until ESC)")
    parser.add_argument("--fps", type=int, default=15,
                        help="synthetic feed rate (default 15)")
    args = parser.parse_args()

    cfg.DISPLAY_FULLSCREEN = not args.window

    stop = threading.Event()
    threading.Thread(target=feed_loop, args=(stop, args.fps), daemon=True).start()
    threading.Thread(target=event_loop, args=(stop,), daemon=True).start()

    display = ScreenDisplay()   # no mqtt/sync needed for this test
    display.start()
    if not display.is_running():
        print("\nDisplay failed to start — is python3-tk installed?")
        print("  sudo apt install python3-tk")
        return 1

    try:
        if args.seconds > 0:
            print(f"[Sim] Running {args.seconds}s, then closing...")
            time.sleep(args.seconds)
            stop.set()
            display.stop()
        else:
            print("[Sim] Running until ESC is pressed...")
            while display.is_running():
                time.sleep(1)
            stop.set()
    except KeyboardInterrupt:
        stop.set()
        display.stop()

    print("[Sim] Finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
