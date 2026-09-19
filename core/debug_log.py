"""Debug event log for diagnosing edge access decisions.

Writes timestamped feature lines (liveness scores, match similarity,
schedule state, hand geometry) to logs/debug.log so a failed door attempt
can be explained from the server even when the kiosk terminal is not
visible. Cheap, thread-safe and never throws — a logging fault must never
disturb the door's critical path.
"""
import os
import threading
import time

import config as cfg

_lock = threading.Lock()
_path = None


def _log_path() -> str:
    global _path
    if _path is None:
        base = getattr(cfg, "DEBUG_LOG_DIR",
                       os.path.join(cfg.BASE_DIR, "logs"))
        _path = os.path.join(base, "debug.log")
    return _path


def log(message: str):
    """Append one timestamped line to logs/debug.log (best-effort)."""
    if not getattr(cfg, "DEBUG_LOG_ENABLED", True):
        return
    try:
        with _lock:
            path = _log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            line = f"{time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {message}\n"
            max_bytes = int(getattr(cfg, "DEBUG_LOG_MAX_BYTES", 500 * 1024))
            if os.path.exists(path) and os.path.getsize(path) >= 2 * max_bytes:
                with open(path, "rb") as f:
                    f.seek(-max_bytes, os.SEEK_END)
                    tail = f.read()
                with open(path, "wb") as f:
                    f.write(tail + b"\n[debug.log truncated]\n")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass