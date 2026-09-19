"""
core/screen_display.py

Modern High-Contrast Fullscreen Door Access Kiosk (PyQt6 + QSS).
Maintains 100% of your exact layout and positioning:
  - Header: Logo + Title ("Door-Edge · Dept. Telecommunication & Electronic Engineering") + Door ID + Live Clock + System State Badge
  - Left (3 stretch): Camera feed with a glowing, breathing animated State Beacon framing the camera (watchful idle slate, amber verifying, emerald green access granted, crimson security alert) + clean non-overlapping HUD overlays + ROI zone
  - Right (2 stretch): 
      * Card 1: System Health (Enrolled faces metric + MQTT status + ESP32, Cloud, Schedule health rows)
      * Card 2: Last Access (Name, verification method badge, similarity % readout + recoloring similarity meter)
      * Card 3: Audit Log (Live event ticker with colored status pills and monospaced timestamps)
  - Footer: SPONSORED BY (Dr. Thap Tharoeun · Prof. Kuong Samnang) + DEVELOPED BY (Mao Peseth · Kouch Mengsrun · Chay Chhunlong · Tim Pannak v1.0)

100% compatible with your existing controllers, daemon threads, shared state, and hotkeys.

NOTE: Header logo size fixed to grow independently of the clock / brand text /
system-state badge. Previously the QHBoxLayout stretched all header children
to match the row's full height, so enlarging the logo distorted everything
else in the header. Each header widget is now explicitly vertically centered
(AlignVCenter) instead of being stretched, so it keeps its natural size
regardless of how tall the logo makes the row.
"""
import math
import os
import threading
import time

import cv2
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QBrush, QRadialGradient, QLinearGradient,
    QImage, QPixmap, QFont
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout,
    QProgressBar, QGraphicsDropShadowEffect
)

import config as cfg
from core.screen_state import state
from core.roi import get_zone, save_zone
from core.schedule_policy import StateType

# ── Color tokens ──────────────────────────────────────────────────────────
_INK        = "#0F172A"   # Primary text (slate-900)
_INK_MUTED  = "#475569"   # Secondary text (slate-600)
_INK_DIM    = "#94A3B8"   # Tertiary muted text (slate-400)

_BG_APP     = "#F8FAFC"   # Modern slate-50 clean background
_CARD_BG    = "#FFFFFF"   # Pure crisp card surface
_CARD_WELL  = "#F1F5F9"   # Slate-100 recessed container
_BORDER     = "#CBD5E1"   # Slate-300 clean borders
_BORDER_SUB = "#E2E8F0"   # Slate-200 micro dividers

# State Accents
_SLATE = "#475569"   # Locked / Standby
_AMBER = "#D97706"   # Verifying / Face Matching
_GREEN = "#16A34A"   # Access Granted / Open
_RED   = "#DC2626"   # Alert / Denied
_BLUE  = "#2563EB"   # Enrolled / Info

_FONT_MONO = "Consolas, 'DejaVu Sans Mono', 'Roboto Mono', monospace"
_FONT_UI   = "'Segoe UI', 'Inter', 'Noto Sans', 'DejaVu Sans', sans-serif"

_STATE_LABEL = {
    "idle": "LOCKED",
    "confirming": "VERIFYING",
    "unlocked": "ACCESS GRANTED",
    "alert": "SECURITY ALERT"
}

_STATE_COLOR = {
    "idle": _SLATE,
    "confirming": _AMBER,
    "unlocked": _GREEN,
    "alert": _RED,
}

_DEFAULT_LOGO = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "logo.png")

# ── Header logo size ───────────────────────────────────────────────────────
# Bump this single constant to resize the header logo. Nothing else in the
# header will distort because every other header widget is explicitly
# vertically centered rather than stretched (see _build_header).
_LOGO_SIZE = 60

_BEACON_SPECS = {
    "idle":       {"period": 3.2, "glow": 0.25, "pulse": False, "speed": 0.03},
    "confirming": {"period": 0.85, "glow": 0.60, "pulse": True,  "speed": 0.08},
    "unlocked":   {"period": 0.50, "glow": 0.75, "pulse": True,  "speed": 0.12},
    "alert":      {"period": 0.60, "glow": 0.70, "pulse": True,  "speed": 0.10},
}

# ── QSS (High-Contrast Modern Kiosk Styling) ─────────────────────────────
QSS = f"""
QWidget {{
    background-color: {_BG_APP};
    color: {_INK};
    font-family: {_FONT_UI};
}}

/* ── Top Header ── */
#Header {{
    background-color: {_CARD_BG};
    border-bottom: 1px solid {_BORDER};
}}
#BrandTitle {{
    font-size: 17px;
    font-weight: 800;
    letter-spacing: 0.2px;
    color: {_INK};
}}
#BrandSub {{
    font-family: {_FONT_MONO};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.2px;
    color: {_INK_MUTED};
}}
#ClockLabel {{
    font-family: {_FONT_MONO};
    font-size: 13px;
    font-weight: 700;
    color: {_INK_MUTED};
    background-color: {_CARD_WELL};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 5px 12px;
}}
#SystemState {{
    font-family: {_FONT_MONO};
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 1.5px;
    padding: 7px 22px;
    border-radius: 8px;
    border: 1.5px solid {_BORDER};
    background-color: {_CARD_BG};
}}
#SystemState[state="locked"]     {{ color: {_SLATE}; border-color: {_SLATE}; background: rgba(71, 85, 105, 0.08); }}
#SystemState[state="confirming"] {{ color: {_AMBER}; border-color: {_AMBER}; background: rgba(217, 119, 6, 0.08); }}
#SystemState[state="unlocked"]   {{ color: {_GREEN}; border-color: {_GREEN}; background: rgba(22, 163, 74, 0.08); }}
#SystemState[state="alert"]      {{ color: {_RED};   border-color: {_RED};   background: rgba(220, 38, 38, 0.08); }}

/* ── Cards ── */
.Card {{
    background-color: {_CARD_BG};
    border: 1px solid {_BORDER};
    border-radius: 14px;
}}
.CardTitle {{
    font-family: {_FONT_MONO};
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 1.8px;
    color: {_INK_MUTED};
    padding: 0 0 6px 0;
}}

/* ── Camera Container ── */
#CameraWell {{
    background-color: #030712;
    border: 1.5px solid #1E293B;
    border-radius: 14px;
}}

/* ── Metric Summary Tiles ── */
.MetricTile {{
    background-color: {_CARD_WELL};
    border: 1px solid {_BORDER};
    border-radius: 10px;
    padding: 8px 12px;
}}
.MetricTile[accent="blue"]  {{ border-top: 3.5px solid {_BLUE}; }}
.MetricTile[accent="green"] {{ border-top: 3.5px solid {_GREEN}; }}
.MetricTile[accent="amber"] {{ border-top: 3.5px solid {_AMBER}; }}
.MetricTile[accent="red"]   {{ border-top: 3.5px solid {_RED}; }}

.MetricValue {{
    font-family: {_FONT_MONO};
    font-size: 26px;
    font-weight: 800;
    color: {_INK};
}}
.MetricValue[tone="ok"]   {{ color: {_GREEN}; }}
.MetricValue[tone="warn"] {{ color: {_AMBER}; }}
.MetricValue[tone="bad"]  {{ color: {_RED}; }}
.MetricValue[tone="blue"] {{ color: {_BLUE}; }}
.MetricValue[tone="dim"]  {{ color: {_INK_MUTED}; }}

.MetricLabel {{
    font-family: {_FONT_MONO};
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 1.5px;
    color: {_INK_MUTED};
    text-transform: uppercase;
}}

/* ── Status Health Rows ── */
.StatusRow {{
    font-size: 12px;
    font-weight: 600;
    color: {_INK_MUTED};
}}
.StatusValue {{
    font-family: {_FONT_MONO};
    font-size: 11px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 5px;
}}
.StatusValue[state="ok"]   {{ color: {_GREEN}; background: rgba(22, 163, 74, 0.12); }}
.StatusValue[state="warn"] {{ color: {_AMBER}; background: rgba(217, 119, 6, 0.12); }}
.StatusValue[state="bad"]  {{ color: {_RED};   background: rgba(220, 38, 38, 0.12); }}
.StatusValue[state="dim"]  {{ color: {_INK_MUTED};  background: {_CARD_WELL}; }}

/* ── Last Access Card ── */
#LastAccessName {{
    font-size: 24px;
    font-weight: 900;
    letter-spacing: -0.3px;
    color: {_INK_DIM};
}}
#LastAccessMeta {{
    font-family: {_FONT_MONO};
    font-size: 11px;
    font-weight: 700;
    color: {_INK_MUTED};
}}

/* Similarity Progress Bar */
QProgressBar#SimilarityBar {{
    background-color: {_CARD_WELL};
    border: 1px solid {_BORDER};
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: transparent;
}}
QProgressBar#SimilarityBar::chunk {{
    background-color: {_GREEN};
    border-radius: 4px;
}}
QProgressBar#SimilarityBar[verdict="warn"]::chunk {{ background-color: {_AMBER}; }}
QProgressBar#SimilarityBar[verdict="bad"]::chunk  {{ background-color: {_RED}; }}

/* ── Audit Log Stream ── */
#AuditWell {{
    background-color: {_CARD_WELL};
    border: 1px solid {_BORDER};
    border-radius: 10px;
}}
.LogLine {{
    font-family: {_FONT_MONO};
    font-size: 11px;
    color: {_INK_MUTED};
}}
.LogGlyph[state="ok"]   {{ color: {_GREEN}; font-weight: 800; }}
.LogGlyph[state="bad"]  {{ color: {_RED};   font-weight: 800; }}
.LogGlyph[state="info"] {{ color: {_BLUE};  font-weight: 800; }}

/* ── Camera HUD Badges ── */
.HudBadge {{
    font-family: {_FONT_MONO};
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 1.5px;
    padding: 6px 14px;
    border-radius: 14px;
    background-color: rgba(15, 23, 42, 0.85);
}}
.HudBadge[state="idle"]     {{ color: #94A3B8; border: 1px solid #475569; }}
.HudBadge[state="scan"]     {{ color: #38BDF8; border: 1px solid {_BLUE}; }}
.HudBadge[state="wait"]     {{ color: #FBBF24; border: 1px solid {_AMBER}; }}
.HudBadge[state="ok"]       {{ color: #4ADE80; border: 1px solid {_GREEN}; }}
.HudBadge[state="alert"]    {{ color: #F87171; border: 1px solid {_RED}; }}

QProgressBar#HandRaiseBar {{
    background-color: rgba(15, 23, 42, 0.85);
    border: 1px solid {_AMBER};
    border-radius: 6px;
    height: 10px;
    text-align: center;
    color: transparent;
}}
QProgressBar#HandRaiseBar::chunk {{
    background-color: {_AMBER};
    border-radius: 5px;
}}

/* ── Warning Banner ── */
#WarnBanner {{
    background-color: rgba(220, 38, 38, 0.08);
    border: 1.5px solid {_RED};
    border-radius: 10px;
    color: {_RED};
    font-size: 13px;
    font-weight: 700;
    padding: 10px 14px;
}}

/* ── Footer ── */
#Footer {{
    background-color: {_CARD_BG};
    border-top: 1px solid {_BORDER};
}}
#FooterSponsorTitle {{
    font-family: {_FONT_MONO};
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.2px;
    color: {_INK_MUTED};
    text-transform: uppercase;
}}
#FooterSponsor {{
    font-size: 13px;
    font-weight: 700;
    color: {_INK};
    letter-spacing: 0.2px;
}}
#FooterDevTitle {{
    font-family: {_FONT_MONO};
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.2px;
    color: {_INK_MUTED};
    text-transform: uppercase;
}}
#FooterDev {{
    font-size: 12px;
    font-weight: 600;
    color: {_INK_MUTED};
    letter-spacing: 0.2px;
}}
"""


# ── Component: Animated Status Dot ────────────────────────────────────────

class _Dot(QLabel):
    def __init__(self, state_="dim"):
        super().__init__()
        self._property_state = state_
        self.setProperty("state", state_)
        self.setFixedSize(10, 10)
        self._pulse = 0.0
        self._t = None

    def set_state(self, state_, pulsing=False):
        if state_ != self._property_state:
            self._property_state = state_
            self.setProperty("state", state_)
            self.update()
        if pulsing and self._t is None:
            self._t = QTimer(self)
            self._t.setInterval(80)
            self._t.timeout.connect(self._tick_pulse)
            self._t.start()
        elif not pulsing and self._t is not None:
            self._t.stop()
            self._t = None
            self._pulse = 0.0
            self.update()

    def _tick_pulse(self):
        self._pulse += 0.35
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c_map = {
            "ok": QColor(_GREEN),
            "warn": QColor(_AMBER),
            "bad": QColor(_RED),
            "dim": QColor(_INK_DIM),
            "blue": QColor(_BLUE),
        }
        c = c_map.get(self._property_state, QColor(_INK_DIM))
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        base_r = 4.0

        if self._t is not None:
            pulse_factor = 0.5 * (math.sin(self._pulse) + 1.0)
            glow_r = base_r + pulse_factor * 2.5
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(c.red(), c.green(), c.blue(), int(60 + 80 * pulse_factor)))
            p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(QPointF(cx, cy), base_r, base_r)
        p.end()


def _make_logo_pixmap(size=44):
    """Load school logo or generate modern geometric monogram."""
    path = getattr(cfg, "LOGO_PATH", None) or _DEFAULT_LOGO
    if path and os.path.exists(path):
        pix = QPixmap(path)
        if not pix.isNull():
            return pix.scaled(
                size, size, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )

    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#1E3A8A"))
    grad.setColorAt(1.0, QColor("#2563EB"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(QRectF(1, 1, size - 2, size - 2), 10, 10)

    p.setPen(QColor("#FFFFFF"))
    font = QFont("Segoe UI", int(size * 0.32), QFont.Weight.Bold)
    p.setFont(font)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "DTE")
    p.end()
    return pix


class _Card(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setProperty("class", "Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        self.lay = lay
        t = QLabel(title.upper())
        t.setProperty("class", "CardTitle")
        lay.addWidget(t)


class _MetricTile(QFrame):
    def __init__(self, value, label, accent="blue"):
        super().__init__()
        self.setProperty("class", "MetricTile")
        self.setProperty("accent", accent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(2)

        self.value_lbl = QLabel(str(value))
        self.value_lbl.setProperty("class", "MetricValue")
        self.value_lbl.setProperty("tone", accent if accent in ("ok", "warn", "bad", "blue") else "dim")

        l = QLabel(label.upper())
        l.setProperty("class", "MetricLabel")

        lay.addWidget(self.value_lbl)
        lay.addWidget(l)

    def set_val(self, val_str, tone=None):
        self.value_lbl.setText(str(val_str))
        if tone is not None:
            self.value_lbl.setProperty("tone", tone)
            self._repolish(self.value_lbl)

    def _repolish(self, w):
        app = QApplication.instance()
        if app and w:
            app.style().unpolish(w)
            app.style().polish(w)


class _StatusRow(QWidget):
    def __init__(self, caption, value_text="--", state_="dim"):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(8)

        self.dot = _Dot(state_)
        lay.addWidget(self.dot)

        cap = QLabel(caption.upper())
        cap.setProperty("class", "StatusRow")
        lay.addWidget(cap)

        lay.addStretch()

        self.value_lbl = QLabel(value_text)
        self.value_lbl.setProperty("class", "StatusValue")
        self.value_lbl.setProperty("state", state_)
        lay.addWidget(self.value_lbl)

    def set(self, text, state_=None, pulsing=False):
        self.value_lbl.setText(str(text))
        if state_ is not None:
            self.value_lbl.setProperty("state", state_)
            self.dot.set_state(state_, pulsing)
            self._repolish(self.value_lbl)

    def _repolish(self, w):
        app = QApplication.instance()
        if app and w:
            app.style().unpolish(w)
            app.style().polish(w)


# ── The Animated State Beacon (Visible Glowing Framing Bezel) ─────────────

class _Beacon(QWidget):
    """Glow framing bezel that coordinates breathing and expanding pulse waves."""

    def __init__(self):
        super().__init__()
        self._state = "idle"
        self._color = QColor(_STATE_COLOR.get("idle", _SLATE))
        self._phase = 0.0
        self._pulse_progress = 0.0
        self._pulse_fire = True

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

    def set_state(self, state_):
        if state_ == self._state:
            return
        self._state = state_
        self._color = QColor(_STATE_COLOR.get(state_, _SLATE))
        self._phase = 0.0
        self._pulse_progress = 0.0
        self._pulse_fire = True
        self.update()

    def _advance(self):
        spec = _BEACON_SPECS.get(self._state, _BEACON_SPECS["idle"])
        period = spec["period"]
        self._phase += (2.0 * math.pi) / (period * 30.0)

        if spec["pulse"] or self._pulse_fire:
            self._pulse_progress += spec["speed"]
            if self._pulse_progress >= 1.0:
                self._pulse_progress = 1.0
                if spec["pulse"]:
                    self._pulse_progress = 0.0
                else:
                    self._pulse_fire = False
                    self._pulse_progress = 1.0
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        rect = QRectF(6, 6, w - 12, h - 12)
        corner_r = 16.0

        c = self._color
        spec = _BEACON_SPECS.get(self._state, _BEACON_SPECS["idle"])
        wave = (math.sin(self._phase) + 1.0) / 2.0

        # 1. Breathing Glow aura
        glow_alpha = int(spec["glow"] * 160 * (0.45 + 0.55 * wave))
        glow_pen = QPen(QColor(c.red(), c.green(), c.blue(), glow_alpha), 10.0)
        p.setPen(glow_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, corner_r, corner_r)

        # 2. Dynamic Expanding Pulse wave
        if self._pulse_progress < 1.0:
            pr = self._pulse_progress
            expand = pr * 12.0
            pulse_rect = QRectF(
                rect.x() - expand,
                rect.y() - expand,
                rect.width() + 2 * expand,
                rect.height() + 2 * expand
            )
            pulse_alpha = int(220 * (1.0 - pr))
            pulse_pen = QPen(
                QColor(c.red(), c.green(), c.blue(), pulse_alpha),
                max(1.5, 4.0 * (1.0 - pr))
            )
            p.setPen(pulse_pen)
            p.drawRoundedRect(pulse_rect, corner_r + expand, corner_r + expand)

        # 3. Crisp Inner Framing Ring
        frame_pen = QPen(QColor(c.red(), c.green(), c.blue(), 240), 3.0)
        p.setPen(frame_pen)
        p.drawRoundedRect(rect, corner_r, corner_r)

        p.end()


class _Preview(QFrame):
    """Camera feed display with non-overlapping HUD overlay widgets."""

    def __init__(self, feed_scale):
        super().__init__()
        self._scale = feed_scale
        self.setObjectName("CameraWell")
        self.setMinimumSize(560, 420)

        self.video_label = QLabel(self)
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setScaledContents(False)

        # Top-Left HUD Badge
        self.badge = QLabel("LOCKED", self)
        self.badge.setProperty("class", "HudBadge")
        self.badge.setProperty("state", "idle")

        # Top-Left Warning overlay
        self.feed_warn = QLabel("", self)
        self.feed_warn.setProperty("class", "HudBadge")
        self.feed_warn.setProperty("state", "alert")
        self.feed_warn.setWordWrap(True)
        self.feed_warn.hide()

        # Bottom-Left Hand Raise Progress
        self.hand_label = QLabel("CONFIRMING ACCESS...", self)
        self.hand_label.setStyleSheet(
            "font-family: %s; font-size: 11px; font-weight: 800; "
            "color: #FBBF24; background: rgba(15, 23, 42, 0.85); "
            "padding: 3px 8px; border-radius: 4px;" % _FONT_MONO
        )
        self.hand_label.hide()

        self.hand_bar = QProgressBar(self)
        self.hand_bar.setObjectName("HandRaiseBar")
        self.hand_bar.setRange(0, 100)
        self.hand_bar.setValue(0)
        self.hand_bar.setTextVisible(False)
        self.hand_bar.setFixedSize(240, 10)
        self.hand_bar.hide()

        self._position_hud()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.video_label.setGeometry(self.rect())
        self._position_hud()

    def _position_hud(self):
        self.badge.adjustSize()
        self.badge.move(16, 16)

        self.feed_warn.adjustSize()
        self.feed_warn.move(16, 56)

        h = self.height()
        self.hand_label.adjustSize()
        self.hand_label.move(16, max(20, h - 58))
        self.hand_bar.move(16, max(42, h - 32))


class _DisplayWindow(QWidget):
    def __init__(self, feed_scale, display):
        super().__init__()
        self._display = display
        self.setStyleSheet(QSS)
        self.setWindowTitle("Door-Edge Access Kiosk")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._build_header(root)
        self._build_body(root)
        self._build_footer(root)

        self._frame_pixmap = None
        self._last_frame_seq = -1
        self._last_events = None
        self._beacon_state = None
        self._last_sig = None

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start()
        self._update_clock()

    def _build_header(self, root):
        header = QFrame()
        header.setObjectName("Header")
        h = QHBoxLayout(header)
        h.setContentsMargins(22, 14, 22, 14)
        h.setSpacing(16)

        # ── Logo ──
        # Size driven by _LOGO_SIZE at module scope — bump that constant to
        # resize. Because every other item in this row is explicitly
        # vertically centered instead of stretched, growing the logo only
        # makes the header bar taller; it no longer distorts the brand
        # text, clock pill, or state badge.
        logo_lbl = QLabel()
        logo_lbl.setObjectName("SchoolLogo")
        logo_lbl.setPixmap(_make_logo_pixmap(_LOGO_SIZE))
        logo_lbl.setFixedSize(_LOGO_SIZE, _LOGO_SIZE)
        logo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ── Brand text block ──
        brand_box = QVBoxLayout()
        brand_box.setSpacing(2)
        title = QLabel("Dept. Telecommunication & Electronic Engineering")
        title.setObjectName("BrandTitle")
        sub = QLabel(f"{getattr(cfg, 'DOOR_ID', 'DOOR-01')}   FACE + RFID ACCESS CONTROL")
        sub.setObjectName("BrandSub")
        brand_box.addWidget(title)
        brand_box.addWidget(sub)

        h.addWidget(logo_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        h.addLayout(brand_box)
        # addLayout() has no alignment parameter, so set it separately —
        # keeps the title/subtitle block at its natural height, centered.
        h.setAlignment(brand_box, Qt.AlignmentFlag.AlignVCenter)
        h.addStretch()

        self.clock_lbl = QLabel("00:00:00")
        self.clock_lbl.setObjectName("ClockLabel")
        h.addWidget(self.clock_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        self.state_lbl = QLabel("LOCKED")
        self.state_lbl.setObjectName("SystemState")
        self.state_lbl.setProperty("state", "locked")
        h.addWidget(self.state_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        root.addWidget(header)

    def _update_clock(self):
        self.clock_lbl.setText(time.strftime("%H:%M:%S  ·  %d %b %Y"))

    def _build_body(self, root):
        body = QHBoxLayout()
        body.setContentsMargins(20, 20, 20, 20)
        body.setSpacing(22)

        # ── Left: Beacon framing Camera ──
        left = QVBoxLayout()
        left.setSpacing(14)

        self.beacon = _Beacon()

        self.camera = _Preview(self._display._feed_scale)
        cam_host = QWidget(self.beacon)
        cam_host.setObjectName("CameraWell")
        cam_host_layout = QVBoxLayout(cam_host)
        cam_host_layout.setContentsMargins(0, 0, 0, 0)
        cam_host_layout.addWidget(self.camera)
        self._cam_host = cam_host

        left.addWidget(self.beacon, 3)
        body.addLayout(left, 3)

        # ── Right: Sidebar Cards ──
        sidebar = QVBoxLayout()
        sidebar.setSpacing(16)

        # 1. System Health Card
        health_card = _Card("System Health")
        self.metric_enrolled = _MetricTile("0", "Enrolled", "blue")
        self.metric_mqtt = _MetricTile("OK", "MQTT", "green")
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        tiles.addWidget(self.metric_enrolled)
        tiles.addWidget(self.metric_mqtt)
        health_card.lay.addLayout(tiles)
        health_card.lay.addSpacing(6)

        self.row_mqtt = _StatusRow("MQTT", "--", "dim")
        self.row_esp = _StatusRow("ESP32", "--", "dim")
        self.row_cloud = _StatusRow("Cloud", "--", "dim")
        self.row_sched = _StatusRow("Schedule", "--", "dim")

        for r in (self.row_mqtt, self.row_esp, self.row_cloud, self.row_sched):
            health_card.lay.addWidget(r)
        sidebar.addWidget(health_card)

        # Warning Banner
        self.warn_banner = QLabel("")
        self.warn_banner.setObjectName("WarnBanner")
        self.warn_banner.setWordWrap(True)
        self.warn_banner.hide()
        sidebar.addWidget(self.warn_banner)

        # 2. Last Access Card
        last_card = _Card("Last Access")
        self.last_name = QLabel("--")
        self.last_name.setObjectName("LastAccessName")

        self.last_meta = QLabel("Waiting for face or RFID card scan...")
        self.last_meta.setObjectName("LastAccessMeta")

        last_card.lay.addWidget(self.last_name)
        last_card.lay.addWidget(self.last_meta)
        last_card.lay.addSpacing(4)

        self.sim_label = QLabel("SIMILARITY")
        self.sim_label.setProperty("class", "LogLine")
        last_card.lay.addWidget(self.sim_label)

        self.sim_bar = QProgressBar()
        self.sim_bar.setObjectName("SimilarityBar")
        self.sim_bar.setRange(0, 100)
        self.sim_bar.setValue(0)
        self.sim_bar.setTextVisible(False)
        self.sim_bar.setProperty("verdict", "ok")
        last_card.lay.addWidget(self.sim_bar)

        sidebar.addWidget(last_card)

        # 3. Audit Log Card
        log_card = _Card("Audit Log")
        well = QFrame()
        well.setObjectName("AuditWell")
        self.log_lay = QVBoxLayout(well)
        self.log_lay.setContentsMargins(12, 10, 12, 10)
        self.log_lay.setSpacing(6)
        self.log_lay.addStretch()
        log_card.lay.addWidget(well)
        sidebar.addWidget(log_card, 1)

        sidebar_wrap = QWidget()
        sidebar_wrap.setLayout(sidebar)
        body.addWidget(sidebar_wrap, 2)
        root.addLayout(body, 1)

    def _build_footer(self, root):
        footer = QFrame()
        footer.setObjectName("Footer")
        f = QHBoxLayout(footer)
        f.setContentsMargins(24, 16, 24, 16)
        f.setSpacing(28)

        # Sponsors
        sp = QVBoxLayout()
        sp.setSpacing(3)
        sp_lbl = QLabel("SPONSORED BY")
        sp_lbl.setObjectName("FooterSponsorTitle")

        sponsors = getattr(cfg, "DISPLAY_SPONSORS", [
            "Dr.Thap Tharoeun", "Prof. kuong Samnang"
        ])
        sp_names = QLabel("   ·   ".join(sponsors) if sponsors else "Telecom Innovation Lab")
        sp_names.setObjectName("FooterSponsor")
        sp_names.setWordWrap(True)
        sp.addWidget(sp_lbl)
        sp.addWidget(sp_names)

        sp_wrap = QWidget()
        sp_wrap.setLayout(sp)
        f.addWidget(sp_wrap, 0, Qt.AlignmentFlag.AlignVCenter)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFixedWidth(1)
        divider.setMinimumHeight(34)
        divider.setStyleSheet("background: %s; border: none;" % _BORDER)
        f.addWidget(divider, 0, Qt.AlignmentFlag.AlignVCenter)

        # Developers
        dv = QVBoxLayout()
        dv.setSpacing(3)
        dv_lbl = QLabel("DEVELOPED BY")
        dv_lbl.setObjectName("FooterDevTitle")

        devs = getattr(cfg, "DISPLAY_DEVELOPERS", [
            "Mao Peseth", "Kouch Mengsrun", "Chay Chhunlong", "Tim Pannak"
        ])
        version = getattr(cfg, "DISPLAY_VERSION", "v1.0")
        dv_names = QLabel("   ·   ".join(devs) + f"      {version}")
        dv_names.setObjectName("FooterDev")
        dv_names.setWordWrap(True)
        dv.addWidget(dv_lbl)
        dv.addWidget(dv_names)

        dv_wrap = QWidget()
        dv_wrap.setLayout(dv)
        f.addWidget(dv_wrap, 0, Qt.AlignmentFlag.AlignVCenter)

        f.addStretch()
        root.addWidget(footer)

    def shutdown(self):
        for t in self.findChildren(QTimer):
            try:
                t.stop()
            except Exception:
                pass
        self.beacon._pulse_fire = False

    def handle_escape(self):
        if self._display._calibrating:
            self._display._calibrating = False
            self._display._calib_corner = None
            print("[ROI] Calibration cancelled")
        else:
            self.close()
            app = QApplication.instance()
            if app:
                app.quit()

    def toggle_calibrate(self):
        self._display.toggle_calibrate_requested()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._display._calibrating:
            cam = self.camera
            g = cam.mapFrom(self, event.position().toPoint())
            if 0 <= g.x() <= cam.width() and 0 <= g.y() <= cam.height():
                self._display.handle_calibrate_click(g.x(), g.y(), cam.width(), cam.height())
                self.update()
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_camera()

    def _layout_camera(self):
        if self._cam_host is None:
            return
        m = 12
        self._cam_host.setGeometry(
            m, m, max(1, self.beacon.width() - 2 * m),
            max(1, self.beacon.height() - 2 * m)
        )
        self._cam_host.raise_()

    # ── Per-tick State Update ──
    def update_from_state(self, display):
        data = display.get_live_data()

        if data["frame_seq"] != self._last_frame_seq:
            self._last_frame_seq = data["frame_seq"]
            self._render_feed(data)

        # Skip all widget panel updates when nothing display-relevant changed —
        # repolishing widgets / rebuilding panels every tick (even when the
        # state and the frame are identical) costs layout + style re-evaluation
        # for zero visual difference.
        last_access = data["last_access"]
        sig = (
            data["disp_state"],
            data["confirm"],
            data["alert_active"],
            data["alert_text"],
            data["mqtt"],
            data["esp"],
            data["cloud"],
            data["sched_state"],
            data["sched_text"],
            data["enrolled"],
            None if last_access is None else (
                last_access.get("epoch"),
                last_access.get("name"),
                last_access.get("reason"),
                last_access.get("granted"),
                last_access.get("similarity"),
            ),
            tuple(data["events"]),
        )
        if sig == self._last_sig:
            return
        self._last_sig = sig

        self._update_beacon(data)
        self._update_status(data)
        self._update_panels(data)

    def _update_beacon(self, data):
        state_key = "alert" if data["alert_active"] else data["disp_state"]

        if state_key != self._beacon_state:
            self._beacon_state = state_key
            self.beacon.set_state(state_key)

            label_text = _STATE_LABEL.get(state_key, "LOCKED")
            self.state_lbl.setText(label_text)
            self.state_lbl.setProperty("state", state_key if state_key != "idle" else "locked")
            self._repolish(self.state_lbl)

    def _update_status(self, data):
        self.metric_enrolled.set_val(data["enrolled"], "blue")

        mqtt_ok = data["mqtt"]
        self.metric_mqtt.set_val("OK" if mqtt_ok else "OFF", "ok" if mqtt_ok else "bad")

        sched_state = data["sched_state"]
        self.row_mqtt.set("on" if mqtt_ok else "off",
                          "ok" if mqtt_ok else "bad", pulsing=not mqtt_ok)

        if data["esp"] is None:
            self.row_esp.set("unknown", "dim")
        else:
            esp_ok = data["esp"]
            self.row_esp.set("on" if esp_ok else "off",
                             "ok" if esp_ok else "bad", pulsing=not esp_ok)

        cloud_ok = data["cloud"]
        self.row_cloud.set("on" if cloud_ok else "off",
                           "ok" if cloud_ok else "bad", pulsing=not cloud_ok)

        self.row_sched.set(data["sched_text"], sched_state,
                           pulsing=(sched_state == "bad"))

    def _update_panels(self, data):
        disp = data["disp_state"]
        if data["alert_active"]:
            self.camera.badge.setText("ALERT")
            self.camera.badge.setProperty("state", "alert")
        elif disp == "confirming":
            self.camera.badge.setText("VERIFYING")
            self.camera.badge.setProperty("state", "wait")
        elif disp == "unlocked":
            self.camera.badge.setText("OPEN")
            self.camera.badge.setProperty("state", "ok")
        else:
            self.camera.badge.setText("LOCKED")
            self.camera.badge.setProperty("state", "idle")
        self._repolish(self.camera.badge)

        if disp == "confirming":
            total = max(getattr(cfg, "CONFIRM_FRAMES", 5), 1)
            self.camera.hand_label.setText(f"CONFIRMING ({int(100 * data['confirm'] / total)}%)")
            self.camera.hand_bar.setValue(int(100 * data["confirm"] / total))
            self.camera.hand_bar.show()
            self.camera.hand_label.show()
        else:
            self.camera.hand_bar.hide()
            self.camera.hand_label.hide()

        if data["alert_active"]:
            self.camera.feed_warn.setText("⚠  " + data["alert_text"].upper())
            self.camera.feed_warn.show()
            self.warn_banner.setText("⚠  WARNING: " + data["alert_text"])
            self.warn_banner.show()
        else:
            self.camera.feed_warn.hide()
            self.warn_banner.hide()

        self._update_last_access(data)

        events = data["events"]
        if events != self._last_events:
            self._last_events = list(events)
            self._rebuild_log(events)

    def _update_last_access(self, data):
        last_access = data["last_access"]
        clear_ms = getattr(cfg, "DISPLAY_ACCESS_CLEAR_MS", 6000)
        show = bool(last_access and
                    (time.time() - last_access.get("epoch", 0)) < clear_ms / 1000)

        if show and last_access.get("granted"):
            self.last_name.setText(last_access.get("name", "--"))
            self.last_name.setStyleSheet(f"color: {_GREEN}; font-size: 24px; font-weight: 900;")

            sim = last_access.get("similarity")
            sim_s = (" · %.2f" % sim) if isinstance(sim, (int, float)) else ""
            self.last_meta.setText("ACCESS GRANTED · %s%s"
                                   % (last_access.get("method", ""), sim_s))
            self.last_meta.setStyleSheet(f"color: {_GREEN};")

            if isinstance(sim, (int, float)):
                self.sim_label.setText("SIMILARITY  %.2f" % sim)
                self.sim_bar.setValue(int(sim * 100))
                self.sim_bar.setProperty("verdict", "ok")
            else:
                self.sim_label.setText("SIMILARITY")
                self.sim_bar.setValue(100)
                self.sim_bar.setProperty("verdict", "ok")

        elif show:
            self.last_name.setText("ACCESS DENIED")
            self.last_name.setStyleSheet(f"color: {_RED}; font-size: 22px; font-weight: 900;")

            self.last_meta.setText("REASON · %s" % last_access.get("reason", "UNAUTHORIZED"))
            self.last_meta.setStyleSheet(f"color: {_RED};")

            self.sim_label.setText("SIMILARITY")
            self.sim_bar.setValue(0)
            self.sim_bar.setProperty("verdict", "bad")
        else:
            self.last_name.setText("--")
            self.last_name.setStyleSheet(f"color: {_INK_DIM}; font-size: 24px; font-weight: 900;")
            self.last_meta.setText("Waiting for face or RFID card scan...")
            self.last_meta.setStyleSheet(f"color: {_INK_MUTED};")

            self.sim_label.setText("SIMILARITY")
            self.sim_bar.setValue(0)
            self.sim_bar.setProperty("verdict", "ok")

        self._repolish(self.sim_bar)

    def _rebuild_log(self, events):
        while self.log_lay.count() > 1:
            item = self.log_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for ts, text, kind in events[:8]:
            line = QWidget()
            lay = QHBoxLayout(line)
            lay.setContentsMargins(0, 1, 0, 1)
            lay.setSpacing(6)

            ts_lbl = QLabel(ts)
            ts_lbl.setProperty("class", "LogLine")
            ts_lbl.setStyleSheet(f"color: {_INK_DIM};")

            g_lbl = QLabel("·")
            g_lbl.setProperty("class", "LogGlyph")
            g_lbl.setProperty("state", "ok" if kind == "ok" else ("bad" if kind == "bad" else "info"))

            txt = QLabel(text)
            txt.setProperty("class", "LogLine")
            txt.setWordWrap(True)

            lay.addWidget(ts_lbl)
            lay.addWidget(g_lbl)
            lay.addWidget(txt, 1)

            self.log_lay.insertWidget(self.log_lay.count() - 1, line)

    def _render_feed(self, data):
        frame = data["frame"]
        if frame is None:
            return

        img = frame.copy()
        self._display._frame_w, self._display._frame_h = img.shape[1], img.shape[0]

        self._draw_zone(img)

        for cmd in data["draw_cmds"]:
            bbox = cmd.get("bbox")
            if bbox is not None and any(bbox):
                self._draw_box(img, bbox, cmd.get("label", ""), cmd.get("color", (255, 255, 255)))

        for det in data["yolo_dets"]:
            self._draw_box(img, det["bbox"], "SPOOF %.2f" % det.get("conf", 0.0), (220, 38, 38))

        for det in data["hand_dets"]:
            self._draw_box(img, det["bbox"], "HAND %.2f" % det.get("conf", 0.0), (255, 0, 200))

        # IR mode badge (top-right), so operators see relaxed thresholds active.
        if data.get("ir_mode"):
            badge = "IR MODE"
            font_scale, thickness = 0.55, 2
            (tw, th), _ = cv2.getTextSize(
                badge, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
            bx, by = img.shape[1] - tw - 16, 18
            cv2.rectangle(img, (bx - 8, by - th - 8), (bx + tw + 8, by + 8),
                          (0, 102, 204), -1)
            cv2.putText(img, badge, (bx, by), cv2.FONT_HERSHEY_SIMPLEX,
                        font_scale, (255, 255, 255), thickness)

        # Draw at the NATIVE camera resolution — OpenCV drawing costs 4x more
        # at 2x scale. Convert to QPixmap once, then let Qt do the final
        # upscale for display (single pass, hardware-accelerated where possible).
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(qimg.copy())
        if self._display._feed_scale > 1:
            pix = pix.scaled(
                w * self._display._feed_scale, h * self._display._feed_scale,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation
            )
        self._frame_pixmap = pix
        self.camera.video_label.setPixmap(self._frame_pixmap)

    def _draw_zone(self, frame):
        zone = self._display._zone
        if zone is None:
            return

        h, w = frame.shape[:2]
        x1, y1, x2, y2 = zone
        color = (217, 126, 72) if not self._display._calibrating else (40, 126, 217)
        p1 = (int(x1 * w), int(y1 * h))
        p2 = (int(x2 * w), int(y2 * h))

        # Thin clean boundary line
        cv2.rectangle(frame, p1, p2, color, 2)

        # Corner brackets
        corner_len = 22
        corners = [
            (p1[0], p1[1], 1, 1),
            (p2[0], p1[1], -1, 1),
            (p1[0], p2[1], 1, -1),
            (p2[0], p2[1], -1, -1)
        ]
        for cx, cy, dx, dy in corners:
            cv2.line(frame, (cx, cy), (cx + dx * corner_len, cy), color, 3)
            cv2.line(frame, (cx, cy), (cx, cy + dy * corner_len), color, 3)

        # Draw ROI tag cleanly outside top-left if possible
        tag_y = max(p1[1] - 8, 22)
        cv2.putText(
            frame, "FACE RECOGNITION ZONE", (p1[0] + 8, tag_y),
            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 2
        )

        if self._display._calibrating:
            prompt = (
                "Click Top-Left Corner"
                if self._display._calib_corner is None
                else "Click Bottom-Right Corner"
            )
            cv2.putText(
                frame, f"[CALIBRATION] {prompt}", (14, h - 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (40, 220, 255), 2
            )
            if self._display._calib_corner is not None:
                cx, cy = self._display._calib_corner
                cv2.circle(frame, (int(cx * w), int(cy * h)), 7, (40, 220, 255), -1)

    def _draw_box(self, frame, bbox, label, color):
        x1, y1, x2, y2 = bbox
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

        font_scale = 0.52
        thickness = 2
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        tag_y = max(y1 - 6, th + 8)
        cv2.rectangle(frame, (x1, tag_y - th - 6), (x1 + tw + 10, tag_y + 4), color, -1)
        cv2.putText(frame, label, (x1 + 5, tag_y - 2), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), thickness)

    def _repolish(self, w):
        app = QApplication.instance()
        if app and w:
            app.style().unpolish(w)
            app.style().polish(w)


# ── Public Controller API (100% Backward-Compatible) ─────────────────────

class ScreenDisplay:
    def __init__(self, mqtt_publisher=None, sync_agent=None):
        self._mqtt = mqtt_publisher
        self._sync = sync_agent
        self._stop = threading.Event()
        self._thread = None
        self._app = None
        self._window = None

        fps = getattr(cfg, "DISPLAY_FPS", 25)
        self._tick_ms = max(20, int(1000.0 / max(fps, 1)))
        self._feed_scale = max(1, int(getattr(cfg, "DISPLAY_FEED_SCALE", 2)))

        self._last_frame_seq = -1
        self._enrolled = 0
        self._enrolled_ts = 0.0

        self._zone = get_zone()
        self._frame_w = 0
        self._frame_h = 0
        self._calibrating = False
        self._calib_corner = None

    def start(self):
        try:
            from PyQt6.QtWidgets import QApplication  # noqa: F401
        except ImportError as e:
            print(f"[Display] Missing PyQt6 ({e}) — display disabled, running headless")
            return

        self._thread = threading.Thread(target=self._run, daemon=True, name="KioskDisplay")
        self._thread.start()
        print(
            f"[Display] Started Kiosk Display — fullscreen={getattr(cfg, 'DISPLAY_FULLSCREEN', False)}, "
            f"FPS={getattr(cfg, 'DISPLAY_FPS', 25)} (ESC to close)"
        )

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        from PyQt6.QtGui import QShortcut, QKeySequence

        self._app = QApplication([])
        self._app.setApplicationName("Door-Edge")

        self._window = _DisplayWindow(feed_scale=self._feed_scale, display=self)

        if getattr(cfg, "DISPLAY_FULLSCREEN", False):
            self._window.showFullScreen()
        else:
            self._window.resize(1440, 880)
            self._window.show()

        self._window._layout_camera()

        esc = QShortcut(QKeySequence("Escape"), self._window)
        esc.activated.connect(self._window.handle_escape)
        ckey = QShortcut(QKeySequence("c"), self._window)
        ckey.activated.connect(self._window.toggle_calibrate)

        timer = QTimer()
        timer.setInterval(self._tick_ms)
        timer.timeout.connect(self._tick)
        timer.start()

        QApplication.processEvents()
        self._app.exec()
        print("[Display] Closed — system continues headless")

    def _tick(self):
        if self._stop.is_set():
            if self._window:
                self._window.shutdown()
                self._window.close()
            if self._app:
                self._app.quit()
            return
        if self._window:
            self._window.update_from_state(self)

    def toggle_calibrate_requested(self):
        self._calibrating = not self._calibrating
        self._calib_corner = None
        if self._calibrating:
            print("[ROI] Calibration ON — click top-left, then bottom-right (ESC cancels)")
        else:
            print("[ROI] Calibration OFF")

    def handle_calibrate_click(self, wx, wy, feed_w, feed_h):
        if not self._calibrating:
            return
        fx, fy = self._feed_to_norm(wx, wy, feed_w, feed_h)
        if self._calib_corner is None:
            self._calib_corner = (fx, fy)
            print("[ROI] Corner 1 at (%.3f, %.3f) — click bottom-right" % (fx, fy))
            return

        x1, y1 = self._calib_corner
        x2, y2 = fx, fy
        zone = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self._zone = save_zone(zone)
        self._calibrating = False
        self._calib_corner = None
        print(f"[ROI] Zone saved: {zone}")

    def _feed_to_norm(self, x, y, feed_w, feed_h):
        if self._frame_w <= 0 or self._frame_h <= 0:
            return 0.0, 0.0
        iw = self._frame_w * self._feed_scale
        ih = self._frame_h * self._feed_scale
        ox = (feed_w - iw) / 2.0
        oy = (feed_h - ih) / 2.0
        return ((x - ox) / iw, (y - oy) / ih)

    def get_live_data(self):
        seq, frame = state.get_frame_ref()
        draw_cmds, yolo_dets, disp_state, confirm = state.get_results()
        hand_dets = state.get_hand_dets()
        ir_mode = state.get_ir_mode()
        last_access, alert_active, alert_text, events = state.get_panel()

        now = time.time()
        if now - self._enrolled_ts >= 5.0:
            self._enrolled_ts = now
            try:
                from core.face_matcher import FaceMatcher
                self._enrolled = FaceMatcher.count()
            except Exception:
                pass

        sched_text, sched_state = self._schedule_status()
        mqtt_on = self._mqtt.is_connected() if self._mqtt else False
        esp_on = self._mqtt.is_esp_online() if self._mqtt else None
        cloud_on = self._sync.is_connected() if self._sync else False

        return {
            "frame_seq": seq,
            "frame": frame,
            "draw_cmds": draw_cmds,
            "yolo_dets": yolo_dets,
            "hand_dets": hand_dets,
            "ir_mode": ir_mode,
            "disp_state": disp_state,
            "confirm": confirm,
            "last_access": last_access,
            "alert_active": alert_active,
            "alert_text": alert_text,
            "events": events,
            "enrolled": self._enrolled,
            "mqtt": mqtt_on,
            "esp": esp_on,
            "cloud": cloud_on,
            "sched_text": sched_text,
            "sched_state": sched_state,
        }

    def _schedule_status(self):
        policy = self._sync.policy() if self._sync else None
        if policy is None or policy.is_empty():
            return "No Policy", "dim"
        st = policy.state(policy.now())
        if policy.in_run_window(policy.now()):
            if st in (StateType.WEEKDAY, StateType.OPEN):
                return st, "ok"
            return st, "warn"
        return "OFF (%s)" % policy.run_window_label(), "bad"