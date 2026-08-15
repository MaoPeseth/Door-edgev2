"""
core/screen_display.py

Fullscreen door display (Tkinter + Pillow). Reads the thread-safe shared
state in core/screen_state.py and renders:

  - live camera feed with detection overlays
  - system status (enrolled faces, MQTT, Cloud)
  - last-access panel (auto-clears after DISPLAY_ACCESS_CLEAR_MS)
  - warning banner (suspicious access)
  - event ticker (face + RFID events)
  - sponsor + developer credits bar

Runs in its own daemon thread. Rendering is capped at DISPLAY_FPS and
skips unchanged frames so the CPU cost stays negligible (~1-2% of a core).
Press ESC to close the display — the system continues headless.
"""
import threading
import time

import cv2

import config as cfg
from core.screen_state import state
from core.face_matcher import FaceMatcher
from core.roi import get_zone, save_zone

# ── Palette (dark, cool-toned theme) ─────────────────────────────────────────
_BG      = "#0A0D12"   # app background
_PANEL   = "#141922"   # card background
_PANEL2  = "#1B222D"   # nested / recessed surface (ticker, feed well)
_BORDER  = "#242C39"   # hairline borders
_FG      = "#EAEFF6"   # primary text
_FG_SOFT = "#B7C0CD"   # secondary text
_DIM     = "#6B7686"   # tertiary / muted text
_ACCENT  = "#4C8DFF"   # brand accent (blue)
_GREEN   = "#33D17A"
_RED     = "#F0555C"
_AMBER   = "#FFB648"

_FONT      = "Helvetica"
_FONT_MONO = "Consolas"

_STATE_LABEL = {"idle": "LOCKED", "confirming": "VERIFYING", "unlocked": "OPEN"}
_STATE_COLOR = {"idle": _DIM, "confirming": _AMBER, "unlocked": _GREEN}


class ScreenDisplay:
    def __init__(self, mqtt_publisher=None, sync_agent=None):
        self._mqtt = mqtt_publisher
        self._sync = sync_agent
        self._stop = threading.Event()
        self._thread = None

        self._root = None
        self._photo = None
        self._last_frame_seq = -1
        self._last_events = None
        self._warn_shown = False
        self._enrolled = 0
        self._enrolled_ts = 0.0
        self._tick_ms = max(20, int(1000 / max(cfg.DISPLAY_FPS, 1)))
        self._feed_scale = max(1, int(getattr(cfg, "DISPLAY_FEED_SCALE", 2)))

        # ROI zone overlay + calibration state
        self._zone = get_zone()
        print(f"[Display] ROI zone: {self._zone}")
        self._frame_w = 0
        self._frame_h = 0
        self._calibrating = False
        self._calib_corner = None   # (fx, fy) normalized first click

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        try:
            import tkinter  # noqa: F401
            from PIL import Image, ImageTk  # noqa: F401
        except ImportError as e:
            print(f"[Display] Missing dependency ({e}) — display disabled, running headless")
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="Display")
        self._thread.start()
        print(f"[Display] Started — fullscreen={cfg.DISPLAY_FULLSCREEN}, "
              f"max {cfg.DISPLAY_FPS} fps (ESC closes display, system keeps running)")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Tk thread (owns every Tk call) ────────────────────────────────────────

    def _run(self):
        import tkinter as tk

        self._root = tk.Tk()
        self._root.title("Door-Edge")
        if cfg.DISPLAY_FULLSCREEN:
            self._root.attributes("-fullscreen", True)
        self._root.configure(bg=_BG)
        self._root.bind("<Escape>", lambda e: self._on_escape())
        self._root.bind("c", lambda e: self._toggle_calibrate())

        self._build_ui(tk)
        self._feed.bind("<Button-1>", self._on_calibrate_click)
        self._root.focus_set()
        self._tick()
        self._root.mainloop()
        print("[Display] Closed — system continues headless")

    def _quit(self):
        if self._root:
            self._root.destroy()

    # ── ROI zone calibration ───────────────────────────────────────────────────

    def _on_escape(self):
        if self._calibrating:
            self._calibrating = False
            self._calib_corner = None
            print("[ROI] Calibration cancelled")
            return
        self._quit()

    def _toggle_calibrate(self):
        if self._zone is None:
            print("[ROI] Zone disabled in config — calibration not available")
            return
        self._calibrating = not self._calibrating
        self._calib_corner = None
        if self._calibrating:
            print("[ROI] Calibration ON — click top-left, then bottom-right (ESC cancels)")
        else:
            print("[ROI] Calibration OFF")

    def _feed_to_norm(self, x, y):
        """Display widget coords -> normalized frame coords (image is centered)."""
        w = max(self._feed.winfo_width(), 1)
        h = max(self._feed.winfo_height(), 1)
        iw = self._frame_w * self._feed_scale
        ih = self._frame_h * self._feed_scale
        if iw <= 0 or ih <= 0:
            return 0.0, 0.0
        ox = (w - iw) / 2.0
        oy = (h - ih) / 2.0
        return ((x - ox) / iw, (y - oy) / ih)

    def _on_calibrate_click(self, event):
        if not self._calibrating:
            return
        fx, fy = self._feed_to_norm(event.x, event.y)
        if self._calib_corner is None:
            self._calib_corner = (fx, fy)
            print(f"[ROI] Corner 1 at ({fx:.3f}, {fy:.3f}) — click bottom-right")
            return
        x1, y1 = self._calib_corner
        x2, y2 = fx, fy
        zone = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self._zone = save_zone(zone)
        self._calibrating = False
        self._calib_corner = None
        print(f"[ROI] Zone saved — detections outside it will be ignored")

    def _tick(self):
        if self._stop.is_set():
            self._quit()
            return
        self._update()
        self._root.after(self._tick_ms, self._tick)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self, tk):
        # Top bar: brand mark + door id + state badge
        top = tk.Frame(self._root, bg=_PANEL)
        top.pack(fill=tk.X)

        top_inner = tk.Frame(top, bg=_PANEL)
        top_inner.pack(fill=tk.X, padx=4, pady=4)

        brand = tk.Frame(top_inner, bg=_PANEL)
        brand.pack(side=tk.LEFT, padx=8, pady=8)
        tk.Frame(brand, bg=_ACCENT, width=8, height=34).pack(side=tk.LEFT, padx=(0, 12))
        title_box = tk.Frame(brand, bg=_PANEL)
        title_box.pack(side=tk.LEFT)
        tk.Label(title_box, text="DOOR-EDGE", font=(_FONT, 10, "bold"),
                 bg=_PANEL, fg=_DIM).pack(anchor="w")
        tk.Label(title_box, text=f"{cfg.DOOR_ID}", font=(_FONT, 18, "bold"),
                 bg=_PANEL, fg=_FG).pack(anchor="w")

        self._badge = tk.Label(top_inner, text="LOCKED", font=(_FONT, 13, "bold"),
                               bg=_DIM, fg=_BG, padx=18, pady=7)
        self._badge.pack(side=tk.RIGHT, padx=14, pady=8)

        tk.Frame(self._root, bg=_BORDER, height=1).pack(fill=tk.X)

        # Main area
        main = tk.Frame(self._root, bg=_BG)
        main.pack(fill=tk.BOTH, expand=True)

        # Left: camera feed
        feed_frame = tk.Frame(main, bg=_PANEL2,
                              highlightbackground=_BORDER, highlightthickness=1)
        feed_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 6), pady=12)
        self._feed = tk.Label(feed_frame, bg=_PANEL2, text="Waiting for camera…",
                              font=(_FONT, 14), fg=_DIM)
        self._feed.pack(fill=tk.BOTH, expand=True)

        # Right: status panels (fixed-width sidebar keeps everything aligned)
        sidebar = tk.Frame(main, bg=_BG, width=360)
        sidebar.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 12), pady=12)
        sidebar.pack_propagate(False)

        status = self._make_panel(sidebar, tk, "SYSTEM STATUS")
        self._enrolled_lbl = self._status_row(status, tk, "Enrolled faces")
        self._mqtt_lbl = self._status_row(status, tk, "MQTT")
        self._esp_lbl = self._status_row(status, tk, "ESP32")
        self._cloud_lbl = self._status_row(status, tk, "Cloud")
        tk.Frame(status, bg=_PANEL, height=6).pack()

        last = self._make_panel(sidebar, tk, "LAST ACCESS")
        self._last_name = tk.Label(last, text="--", font=(_FONT, 27, "bold"),
                                   bg=_PANEL, fg=_DIM, anchor="w")
        self._last_name.pack(fill=tk.X, padx=12, pady=(2, 0))
        self._last_result = tk.Label(last, text="", font=(_FONT, 12, "bold"),
                                     bg=_PANEL, fg=_DIM, anchor="w")
        self._last_result.pack(fill=tk.X, padx=12, pady=(2, 12))

        # Warning banner — reserved slot so panels below don't jump around
        self._warn_slot = tk.Frame(sidebar, bg=_BG)
        self._warn_slot.pack(fill=tk.X)
        self._warn = tk.Label(self._warn_slot, text="", font=(_FONT, 13, "bold"),
                              bg=_RED, fg=_BG, pady=10)

        ticker = self._make_panel(sidebar, tk, "RECENT EVENTS")
        ticker_well = tk.Frame(ticker, bg=_PANEL2, highlightbackground=_BORDER,
                                highlightthickness=1)
        ticker_well.pack(fill=tk.X, padx=10, pady=(0, 10))
        self._ticker = tk.Text(ticker_well, height=9, width=44, bg=_PANEL2, fg=_FG_SOFT,
                               font=(_FONT_MONO, 10), state=tk.DISABLED,
                               borderwidth=0, highlightthickness=0,
                               padx=8, pady=6)
        self._ticker.pack(fill=tk.X)
        self._ticker.tag_config("ok", foreground=_GREEN)
        self._ticker.tag_config("deny", foreground=_RED)
        self._ticker.tag_config("info", foreground=_DIM)

        # Bottom: sponsor + credits bar (always visible)
        tk.Frame(self._root, bg=_BORDER, height=1).pack(fill=tk.X)
        credits = tk.Frame(self._root, bg=_PANEL)
        credits.pack(fill=tk.X)
        if cfg.DISPLAY_SPONSORS:
            tk.Label(credits, text="Sponsored by   " + "   ·   ".join(cfg.DISPLAY_SPONSORS),
                     font=(_FONT, 11, "bold"), bg=_PANEL, fg=_FG_SOFT
                     ).pack(anchor="w", padx=16, pady=(8, 0))
        tk.Label(credits,
                 text="Developed by   " + "   ·   ".join(cfg.DISPLAY_DEVELOPERS)
                      + f"      Door-Edge {cfg.DISPLAY_VERSION}",
                 font=(_FONT, 10), bg=_PANEL, fg=_DIM
                 ).pack(anchor="w", padx=16, pady=(2, 8))

    def _make_panel(self, parent, tk, title):
        panel = tk.Frame(parent, bg=_PANEL,
                         highlightbackground=_BORDER, highlightthickness=1)
        panel.pack(fill=tk.X, pady=(0, 10))
        header = tk.Frame(panel, bg=_PANEL)
        header.pack(fill=tk.X, padx=12, pady=(10, 6))
        tk.Frame(header, bg=_ACCENT, width=3, height=12).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(header, text=title, font=(_FONT, 10, "bold"),
                 bg=_PANEL, fg=_DIM).pack(side=tk.LEFT)
        return panel

    def _status_row(self, parent, tk, caption):
        row = tk.Frame(parent, bg=_PANEL)
        row.pack(fill=tk.X, padx=12, pady=4)
        tk.Label(row, text=caption, font=(_FONT, 11), bg=_PANEL, fg=_FG_SOFT
                 ).pack(side=tk.LEFT)
        lbl = tk.Label(row, text="--", font=(_FONT, 11, "bold"), bg=_PANEL, fg=_FG)
        lbl.pack(side=tk.RIGHT)
        return lbl

    # ── Per-tick update ───────────────────────────────────────────────────────

    def _update(self):
        seq, frame = state.get_frame()
        if seq != self._last_frame_seq:
            self._last_frame_seq = seq
            self._render_feed(frame)
        self._update_status()
        self._update_panels()

    # ── Feed rendering ────────────────────────────────────────────────────────

    def _render_feed(self, frame):
        from PIL import Image, ImageTk

        draw_cmds, yolo_dets, disp_state, confirm = state.get_results()
        if frame is None:
            self._feed.configure(image="", text="Waiting for camera…")
            return

        img = frame.copy()
        self._frame_w, self._frame_h = frame.shape[1], frame.shape[0]
        self._draw_zone(img)
        for cmd in draw_cmds:
            self._draw_box(img, cmd["bbox"], cmd["label"], cmd["color"])
        for det in yolo_dets:
            self._draw_box(img, det["bbox"],
                           f"SPOOF {det['conf']:.2f}", (200, 0, 200))
        self._draw_hud(img, disp_state, confirm)

        if self._feed_scale > 1:
            h, w = img.shape[:2]
            img = cv2.resize(img, (w * self._feed_scale, h * self._feed_scale),
                             interpolation=cv2.INTER_NEAREST)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))   # keep reference!
        self._feed.configure(image=self._photo, text="")

    def _draw_zone(self, frame):
        if self._zone is None:
            return
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = self._zone
        color = (255, 182, 72) if self._calibrating else (76, 141, 255)
        p1 = (int(x1 * w), int(y1 * h))
        p2 = (int(x2 * w), int(y2 * h))
        cv2.rectangle(frame, p1, p2, color, 3)
        m = 18
        for cx, cy, dx, dy in ((p1[0], p1[1], 1, 1), (p2[0], p1[1], -1, 1),
                               (p1[0], p2[1], 1, -1), (p2[0], p2[1], -1, -1)):
            cv2.line(frame, (cx, cy), (cx + dx * m, cy), color, 3)
            cv2.line(frame, (cx, cy), (cx, cy + dy * m), color, 3)
        cv2.putText(frame, "ROI ZONE", (p1[0] + 6, max(p1[1] - 10, 16)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        if self._calibrating:
            text = "Click top-left corner" if self._calib_corner is None \
                else "Click bottom-right corner"
            cv2.putText(frame, text, (10, h - 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            if self._calib_corner is not None:
                cx, cy = self._calib_corner
                cv2.circle(frame, (int(cx * w), int(cy * h)), 6, color, -1)

    def _draw_box(self, frame, bbox, label, color):
        if bbox is None:
            # Text-only message, top-left corner, no box.
            cv2.putText(frame, label, (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
            return
        x1, y1, x2, y2 = bbox
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame, label, (x1, max(y1 - 8, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

    def _draw_hud(self, frame, disp_state, confirm):
        h, w = frame.shape[:2]
        state_color = {
            "idle": (166, 118, 107),
            "confirming": (72, 182, 255),
            "unlocked": (122, 209, 51),
        }.get(disp_state, (255, 255, 255))
        cv2.putText(frame, disp_state.upper(), (w - 150, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, state_color, 2)
        if disp_state == "confirming" and confirm > 0:
            bar_w = int(w * confirm / max(cfg.CONFIRM_FRAMES, 1))
            cv2.rectangle(frame, (0, h - 10), (w, h), (35, 30, 26), -1)
            cv2.rectangle(frame, (0, h - 10), (bar_w, h), (72, 182, 255), -1)

    # ── Status panel ──────────────────────────────────────────────────────────

    def _update_status(self):
        now = time.time()
        if now - self._enrolled_ts >= 5:
            self._enrolled_ts = now
            self._enrolled = FaceMatcher.count()
        mqtt_on = self._mqtt.is_connected() if self._mqtt else False
        esp_on = self._mqtt.is_esp_online() if self._mqtt else None
        cloud_on = self._sync.is_connected() if self._sync else False

        self._enrolled_lbl.configure(text=str(self._enrolled), fg=_FG)
        self._mqtt_lbl.configure(text="connected" if mqtt_on else "offline",
                                 fg=_GREEN if mqtt_on else _RED)
        if esp_on is None:
            self._esp_lbl.configure(text="unknown", fg=_DIM)
        else:
            self._esp_lbl.configure(text="online" if esp_on else "offline",
                                    fg=_GREEN if esp_on else _RED)
        self._cloud_lbl.configure(text="connected" if cloud_on else "offline",
                                  fg=_GREEN if cloud_on else _RED)

    # ── Panels (badge / last access / warning / ticker) ──────────────────────

    def _update_panels(self):
        import tkinter as tk

        last_access, alert_active, alert_text, events = state.get_panel()

        # Badge
        _, _, disp_state, _ = state.get_results()
        if alert_active:
            badge, bcolor = "ALERT", _RED
        else:
            badge = _STATE_LABEL.get(disp_state, "LOCKED")
            bcolor = _STATE_COLOR.get(disp_state, _DIM)
        self._badge.configure(text=badge, bg=bcolor)

        # Last access (auto-clears)
        show = False
        if last_access and (time.time() - last_access.get("epoch", 0)) < cfg.DISPLAY_ACCESS_CLEAR_MS / 1000:
            show = True
        if show and last_access.get("granted"):
            self._last_name.configure(text=last_access.get("name", "--"), fg=_GREEN)
            sim = last_access.get("similarity")
            sim_s = f" · {sim:.2f}" if isinstance(sim, (int, float)) else ""
            self._last_result.configure(
                text=f"ACCESS GRANTED · {last_access.get('method', '')}{sim_s}", fg=_GREEN)
        elif show:
            self._last_name.configure(text="--", fg=_RED)
            self._last_result.configure(
                text=f"ACCESS DENIED · {last_access.get('reason', '')}", fg=_RED)
        else:
            self._last_name.configure(text="--", fg=_DIM)
            self._last_result.configure(text="", fg=_DIM)

        # Warning banner
        if alert_active:
            self._warn.configure(text=f"⚠   WARNING: {alert_text}")
            if not self._warn_shown:
                self._warn.pack(fill=tk.X, pady=(0, 10))
                self._warn_shown = True
        else:
            if self._warn_shown:
                self._warn.pack_forget()
                self._warn_shown = False

        # Ticker (only rebuild when events changed)
        if events != self._last_events:
            self._last_events = events
            self._ticker.configure(state=tk.NORMAL)
            self._ticker.delete("1.0", tk.END)
            for ts, text, kind in events[:8]:
                tag = kind if kind in ("ok", "deny") else "info"
                glyph = {"ok": "✓", "deny": "✕"}.get(kind, "·")
                self._ticker.insert(tk.END, f"{ts}  {glyph}  {text}\n", tag)
            self._ticker.configure(state=tk.DISABLED)