"""
door_edge_preview.py — PyQt6 + QSS preview of the "Flat, Layered,
Technology Kiosk" design system for Door-Edge.

Run it directly to see the live window:
    python3 door_edge_preview.py

This is a STANDALONE PREVIEW — it uses mock/static data (no camera,
no MQTT, no face matcher) so you can evaluate the visual design in
isolation before we wire it into the real app. Once you're happy with
the look, the real integration replaces the mock data calls with your
actual state (core/screen_state.py, FaceMatcher, mqtt_publisher, etc.)
— the widget structure and QSS stay the same.

Design spec implemented:
  - Flat 2D surfaces, no gradients/shadows — tonal layering via color only
  - Consolas (monospace) for data/metrics, Segoe UI for names — falls
    back to DejaVu Sans Mono / DejaVu Sans on Linux if those aren't
    installed (Qt does this automatically; see FONT NOTES below)
  - 3-layer color system: background / surface / accent (cyan·green·amber·red)
  - Card depth via tonal raised surfaces + 12px border-radius (real
    border-radius, unlike Tkinter — QSS supports it natively)
  - Vector status dots (QLabel + QSS border-radius circle) instead of emoji
  - Metric tiles with accent top-border
  - Similarity progress bar that recolors by verdict (QProgressBar +
    dynamic QSS property)
  - Camera HUD: rounded badges + hand-raise progress bar, drawn as
    overlay widgets positioned with a QStackedLayout — real widgets,
    not text baked into a video frame, so they stay crisp

FONT NOTES: Consolas and Segoe UI are Windows-native fonts. On your
Ubuntu machine, Qt will silently substitute them with whatever's
closest (usually DejaVu Sans Mono / DejaVu Sans, sometimes Noto).
That substitution looks fine — both are clean, readable, similar
x-height families — but if you want the *exact* look, install:
    sudo apt install fonts-liberation ttf-mscorefonts-installer
(ttf-mscorefonts-installer pulls in a Consolas-compatible metric font;
true Segoe UI isn't legally redistributable on Linux, "Liberation
Sans"/"Noto Sans" are the closest open substitutes.)
"""
import sys

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame,
    QProgressBar, QStackedLayout, QSizePolicy, QGridLayout,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QPixmap, QPainter, QColor

# ── Color system ──────────────────────────────────────────────────────────
BG        = "#060a13"
SURFACE   = "#0a1522"
SURFACE2  = "#0d1b2b"
BORDER    = "#16233a"
TEXT      = "#e8eef6"
TEXT_SOFT = "#8f9db0"
TEXT_DIM  = "#5a6b82"

CYAN   = "#22d3ee"
GREEN  = "#34d399"
AMBER  = "#fbbf24"
RED    = "#f87171"

FONT_MONO = "Consolas, 'DejaVu Sans Mono', monospace"
FONT_UI   = "'Segoe UI', 'Noto Sans', 'DejaVu Sans', sans-serif"

QSS = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: {FONT_UI};
}}

/* ── Header ── */
#Header {{
    background-color: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}
#BrandTitle {{
    font-family: {FONT_UI};
    font-size: 14px;
    font-weight: 700;
    color: {TEXT};
    letter-spacing: 0.3px;
}}
#BrandSub {{
    font-family: {FONT_MONO};
    font-size: 9px;
    color: {TEXT_DIM};
    letter-spacing: 1px;
}}
#SystemState {{
    font-family: {FONT_MONO};
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 6px 16px;
    border-radius: 6px;
}}
#SystemState[state="ready"] {{
    color: {GREEN};
    background-color: rgba(52, 211, 153, 0.12);
    border: 1px solid rgba(52, 211, 153, 0.35);
}}
#SystemState[state="locked"] {{
    color: {RED};
    background-color: rgba(248, 113, 113, 0.12);
    border: 1px solid rgba(248, 113, 113, 0.35);
}}
#SystemState[state="unlocked"] {{
    color: {GREEN};
    background-color: rgba(52, 211, 153, 0.12);
    border: 1px solid rgba(52, 211, 153, 0.35);
}}

/* ── Cards (tonal raised surfaces) ── */
.Card {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
.CardTitle {{
    font-family: {FONT_MONO};
    font-size: 10px;
    font-weight: 700;
    color: {TEXT_DIM};
    letter-spacing: 1.5px;
    padding: 2px 0 6px 0;
}}

/* ── Camera well ── */
#CameraWell {{
    background-color: #000000;
    border: 1px solid {BORDER};
    border-radius: 12px;
}}

/* ── Metric tiles (accent top-border) ── */
.MetricTile {{
    background-color: {SURFACE2};
    border-radius: 8px;
    border-top: 3px solid {CYAN};
}}
.MetricTile[accent="green"] {{ border-top: 3px solid {GREEN}; }}
.MetricTile[accent="amber"] {{ border-top: 3px solid {AMBER}; }}
.MetricTile[accent="red"] {{ border-top: 3px solid {RED}; }}
.MetricValue {{
    font-family: {FONT_MONO};
    font-size: 20px;
    font-weight: 700;
    color: {TEXT};
}}
.MetricLabel {{
    font-family: {FONT_MONO};
    font-size: 9px;
    color: {TEXT_DIM};
    letter-spacing: 1px;
}}

/* ── Status rows ── */
.StatusRow {{
    font-family: {FONT_MONO};
    font-size: 11px;
    color: {TEXT_SOFT};
}}
.StatusValue {{
    font-family: {FONT_MONO};
    font-size: 11px;
    font-weight: 700;
}}
.StatusValue[state="ok"]   {{ color: {GREEN}; }}
.StatusValue[state="warn"] {{ color: {AMBER}; }}
.StatusValue[state="bad"]  {{ color: {RED}; }}
.StatusValue[state="dim"]  {{ color: {TEXT_DIM}; }}

/* ── Vector status dot ── */
.Dot {{
    border-radius: 5px;
    min-width: 10px; max-width: 10px;
    min-height: 10px; max-height: 10px;
}}
.Dot[state="ok"]   {{ background-color: {GREEN}; }}
.Dot[state="warn"] {{ background-color: {AMBER}; }}
.Dot[state="bad"]  {{ background-color: {RED}; }}
.Dot[state="dim"]  {{ background-color: {TEXT_DIM}; }}

/* ── Last access ── */
#LastAccessName {{
    font-family: {FONT_UI};
    font-size: 18px;
    font-weight: 700;
}}
#LastAccessMeta {{
    font-family: {FONT_MONO};
    font-size: 10px;
    color: {TEXT_DIM};
}}

/* ── Similarity progress bar (recolors by verdict) ── */
QProgressBar#SimilarityBar {{
    background-color: {SURFACE2};
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar#SimilarityBar::chunk {{
    background-color: {GREEN};
    border-radius: 4px;
}}
QProgressBar#SimilarityBar[verdict="warn"]::chunk {{ background-color: {AMBER}; }}
QProgressBar#SimilarityBar[verdict="bad"]::chunk  {{ background-color: {RED}; }}

/* ── Audit log ── */
#AuditWell {{
    background-color: #05080f;
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
.LogLine {{
    font-family: {FONT_MONO};
    font-size: 10px;
    color: {TEXT_SOFT};
}}
.LogGlyph[state="ok"]  {{ color: {GREEN}; font-weight: 700; }}
.LogGlyph[state="bad"] {{ color: {RED}; font-weight: 700; }}

/* ── Camera HUD badges ── */
.HudBadge {{
    font-family: {FONT_MONO};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 5px 14px;
    border-radius: 14px;
    background-color: rgba(10, 21, 34, 0.85);
}}
.HudBadge[state="idle"] {{ color: {TEXT_DIM}; border: 1px solid {TEXT_DIM}; }}
.HudBadge[state="scan"] {{ color: {CYAN}; border: 1px solid {CYAN}; }}
.HudBadge[state="wait"] {{ color: {AMBER}; border: 1px solid {AMBER}; }}
.HudBadge[state="ok"]   {{ color: {GREEN}; border: 1px solid {GREEN}; }}

QProgressBar#HandRaiseBar {{
    background-color: rgba(10, 21, 34, 0.85);
    border: 1px solid {AMBER};
    border-radius: 6px;
    height: 10px;
    text-align: center;
    color: transparent;
}}
QProgressBar#HandRaiseBar::chunk {{
    background-color: {AMBER};
    border-radius: 5px;
}}

/* ── Footer ── */
#Footer {{
    background-color: {SURFACE};
    border-top: 1px solid {BORDER};
}}
#FooterSponsor {{
    font-family: {FONT_UI};
    font-size: 13px;
    font-weight: 600;
    color: {TEXT};
    letter-spacing: 0.2px;
}}
#FooterDev {{
    font-family: {FONT_UI};
    font-size: 12px;
    font-weight: 500;
    color: {TEXT_SOFT};
    letter-spacing: 0.2px;
}}
#SchoolLogo {{
    background-color: transparent;
    border: none;
}}
"""


# ── School logo ──────────────────────────────────────────────────────────
# Point this at your own logo file when you have one, e.g. "assets/school_logo.png".
# Until then, a placeholder emblem is drawn automatically so the layout can
# be reviewed with something in that spot.
LOGO_PATH = None


def make_logo_pixmap(size=44):
    if LOGO_PATH:
        pix = QPixmap(LOGO_PATH)
        if not pix.isNull():
            return pix.scaled(
                size, size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
    # Placeholder emblem: flat circular badge with initials, matching the
    # existing accent/tonal color system. Replace by setting LOGO_PATH above.
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor(CYAN))
    painter.setBrush(QColor(SURFACE2))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.setPen(QColor(CYAN))
    font = QFont("DejaVu Sans", max(9, int(size * 0.30)), QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "DTE")
    painter.end()
    return pix


def dot(state="dim"):
    d = QLabel()
    d.setProperty("class", "Dot")
    d.setProperty("state", state)
    return d


def card(title):
    outer = QFrame()
    outer.setProperty("class", "Card")
    lay = QVBoxLayout(outer)
    lay.setContentsMargins(16, 14, 16, 14)
    t = QLabel(title.upper())
    t.setProperty("class", "CardTitle")
    lay.addWidget(t)
    return outer, lay


def metric_tile(value, label, accent="cyan"):
    tile = QFrame()
    tile.setProperty("class", "MetricTile")
    tile.setProperty("accent", accent)
    lay = QVBoxLayout(tile)
    lay.setContentsMargins(12, 10, 12, 10)
    v = QLabel(str(value))
    v.setProperty("class", "MetricValue")
    l = QLabel(label.upper())
    l.setProperty("class", "MetricLabel")
    lay.addWidget(v)
    lay.addWidget(l)
    return tile


def status_row(caption, value_text, state="ok"):
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 4, 0, 4)
    d = dot(state)
    caption_lbl = QLabel(caption.upper())
    caption_lbl.setProperty("class", "StatusRow")
    lay.addWidget(d)
    lay.addSpacing(8)
    lay.addWidget(caption_lbl)
    lay.addStretch()
    val = QLabel(value_text)
    val.setProperty("class", "StatusValue")
    val.setProperty("state", state)
    lay.addWidget(val)
    return row


class CameraWell(QFrame):
    """Camera feed placeholder + HUD badges.

    IMPLEMENTATION NOTE: an earlier version used a QStackedLayout with a
    translucent overlay widget (StackAll mode) to place the HUD on top of
    the video label. That's the "textbook" Qt approach, but it silently
    failed to composite under this environment's rendering path — a real,
    documented Qt gotcha where WA_TranslucentBackground stops behaving
    correctly once a QSS stylesheet is applied to the widget tree (Qt
    switches the widget to "styled panel" background painting, which
    fights with the translucent attribute). Rather than fight that, this
    version places the HUD badges as direct child widgets of CameraWell
    itself, positioned manually in resizeEvent() and raised to the front
    with .raise_(). This is simpler, has no translucency/z-order surprises,
    and is the more common real-world pattern for "floating controls over
    a video widget" in PyQt anyway.
    """

    def __init__(self):
        super().__init__()
        self.setObjectName("CameraWell")
        self.setMinimumSize(640, 480)

        self.video_label = QLabel(self)
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = QPixmap(640, 480)
        pix.fill(QColor("#04070c"))
        painter = QPainter(pix)
        painter.setPen(QColor(TEXT_DIM))
        painter.setFont(QFont("DejaVu Sans Mono", 11))
        painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "[ live camera feed ]")
        painter.end()
        self.video_label.setPixmap(pix)
        self.video_label.setScaledContents(True)

        self.badge = QLabel("SCANNING", self)
        self.badge.setProperty("class", "HudBadge")
        self.badge.setProperty("state", "scan")

        self.hand_label = QLabel("HAND-RAISE CONFIRM", self)
        self.hand_label.setProperty("class", "LogLine")

        self.hand_bar = QProgressBar(self)
        self.hand_bar.setObjectName("HandRaiseBar")
        self.hand_bar.setRange(0, 100)
        self.hand_bar.setValue(62)
        self.hand_bar.setTextVisible(False)
        self.hand_bar.setFixedSize(240, 10)

        self._position_hud()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.video_label.setGeometry(self.rect())
        self._position_hud()

    def _position_hud(self):
        # Re-measure here (not in __init__) so sizeHint reflects the QSS
        # padding/font that's only applied once this widget is part of the
        # styled tree and shown — measuring too early sizes the label for
        # unstyled metrics and the text overlaps its own box.
        self.badge.adjustSize()
        self.badge.move(16, 16)
        self.badge.raise_()

        h = self.height()
        self.hand_label.adjustSize()
        self.hand_label.move(16, h - 42)
        self.hand_label.raise_()
        self.hand_bar.move(16, h - 24)
        self.hand_bar.raise_()


class DoorEdgePreview(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Door-Edge — Flat Layered Kiosk Preview")
        self.setStyleSheet(QSS)
        self.resize(1440, 860)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ──
        header = QFrame()
        header.setObjectName("Header")
        h = QHBoxLayout(header)
        h.setContentsMargins(20, 14, 20, 14)

        logo_lbl = QLabel()
        logo_lbl.setObjectName("SchoolLogo")
        logo_lbl.setPixmap(make_logo_pixmap(44))
        logo_lbl.setFixedSize(44, 44)
        logo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        brand_box = QVBoxLayout()
        brand_box.setSpacing(2)
        title = QLabel("Dept. Telecommunication and Electronic engineering")
        title.setObjectName("BrandTitle")
        sub = QLabel("door-01  ·  FACE + RFID ACCESS CONTROL")
        sub.setObjectName("BrandSub")
        brand_box.addWidget(title)
        brand_box.addWidget(sub)

        h.addWidget(logo_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        h.addSpacing(12)
        h.addLayout(brand_box)
        h.addStretch()

        state_lbl = QLabel("SYSTEM READY")
        state_lbl.setObjectName("SystemState")
        state_lbl.setProperty("state", "ready")
        h.addWidget(state_lbl)
        root.addWidget(header)

        # ── Main body: camera (3x) + sidebar (2x) ──
        body = QHBoxLayout()
        body.setContentsMargins(16, 16, 16, 16)
        body.setSpacing(16)

        camera = CameraWell()
        body.addWidget(camera, 3)

        sidebar = QVBoxLayout()
        sidebar.setSpacing(14)

        # System status card w/ metric tiles
        status_card, status_lay = card("System Status")
        tiles = QHBoxLayout()
        tiles.addWidget(metric_tile(1, "Enrolled", "cyan"))
        tiles.addWidget(metric_tile("OK", "MQTT", "green"))
        tiles.addWidget(metric_tile("RESTR.", "Schedule", "amber"))
        status_lay.addLayout(tiles)
        status_lay.addSpacing(8)
        status_lay.addWidget(status_row("MQTT", "connected", "ok"))
        status_lay.addWidget(status_row("ESP32", "unknown", "dim"))
        status_lay.addWidget(status_row("Cloud", "connected", "ok"))
        status_lay.addWidget(status_row("Schedule", "RESTRICTED", "warn"))
        sidebar.addWidget(status_card)

        # Last access card
        last_card, last_lay = card("Last Access")
        name = QLabel("Peseth")
        name.setObjectName("LastAccessName")
        name.setStyleSheet(f"color: {GREEN};")
        meta = QLabel("ACCESS GRANTED · face · 18:34:20")
        meta.setObjectName("LastAccessMeta")
        last_lay.addWidget(name)
        last_lay.addWidget(meta)
        last_lay.addSpacing(6)
        sim_label = QLabel("SIMILARITY  0.87")
        sim_label.setProperty("class", "LogLine")
        last_lay.addWidget(sim_label)
        sim_bar = QProgressBar()
        sim_bar.setObjectName("SimilarityBar")
        sim_bar.setRange(0, 100)
        sim_bar.setValue(87)
        sim_bar.setTextVisible(False)
        last_lay.addWidget(sim_bar)
        sidebar.addWidget(last_card)

        # Audit log card
        log_card, log_lay = card("Audit Log")
        well = QFrame()
        well.setObjectName("AuditWell")
        well_lay = QVBoxLayout(well)
        well_lay.setContentsMargins(10, 8, 10, 8)
        well_lay.setSpacing(4)
        entries = [
            ("18:34:20", "✓", "Peseth · face · GRANTED", "ok"),
            ("18:33:58", "✕", "Unknown card · DENIED", "bad"),
            ("18:33:12", "·", "Camera online", "dim"),
            ("18:32:40", "✕", "Schedule blocked · LOCKDOWN", "bad"),
        ]
        for ts, glyph, text, state in entries:
            line = QWidget()
            line_lay = QHBoxLayout(line)
            line_lay.setContentsMargins(0, 0, 0, 0)
            ts_lbl = QLabel(ts)
            ts_lbl.setProperty("class", "LogLine")
            ts_lbl.setStyleSheet(f"color: {TEXT_DIM};")
            g_lbl = QLabel(glyph)
            g_lbl.setProperty("class", "LogGlyph")
            g_lbl.setProperty("state", state if state != "dim" else "ok")
            if state == "dim":
                g_lbl.setStyleSheet(f"color: {TEXT_DIM};")
            txt_lbl = QLabel(text)
            txt_lbl.setProperty("class", "LogLine")
            txt_lbl.setWordWrap(True)
            line_lay.addWidget(ts_lbl)
            line_lay.addWidget(g_lbl)
            line_lay.addWidget(txt_lbl, 1)
            well_lay.addWidget(line)
        log_lay.addWidget(well)
        sidebar.addWidget(log_card, 1)

        sidebar_wrap = QWidget()
        sidebar_wrap.setLayout(sidebar)
        body.addWidget(sidebar_wrap, 2)

        root.addLayout(body, 1)

        # ── Footer ──
        # "DOOR-EDGE v1.0" removed. Sponsor and developer blocks sit left-
        # aligned with a thin vertical divider between them, matching the
        # reference layout.
        footer = QFrame()
        footer.setObjectName("Footer")
        f = QHBoxLayout(footer)
        f.setContentsMargins(24, 16, 24, 16)
        f.setSpacing(28)

        sp = QVBoxLayout()
        sp.setSpacing(6)
        sp_label = QLabel("SPONSORED BY")
        sp_label.setProperty("class", "LogLine")
        sp_names = QLabel("Dr. Thap Tharoeun  ·  Prof. Kuong Samnang")
        sp_names.setObjectName("FooterSponsor")
        sp_names.setWordWrap(True)
        sp.addWidget(sp_label)
        sp.addWidget(sp_names)
        sp_wrap = QWidget()
        sp_wrap.setLayout(sp)
        f.addWidget(sp_wrap, 0, Qt.AlignmentFlag.AlignVCenter)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFixedWidth(1)
        divider.setMinimumHeight(34)
        divider.setStyleSheet(f"background-color: {BORDER}; border: none;")
        f.addWidget(divider, 0, Qt.AlignmentFlag.AlignVCenter)

        dv = QVBoxLayout()
        dv.setSpacing(6)
        dv_label = QLabel("DEVELOPED BY")
        dv_label.setProperty("class", "LogLine")
        dv_names = QLabel("Mao Peseth · Kouch Mengsrun · Chay Chhunlong · Tim Pannak")
        dv_names.setObjectName("FooterDev")
        dv_names.setWordWrap(True)
        dv.addWidget(dv_label)
        dv.addWidget(dv_names)
        dv_wrap = QWidget()
        dv_wrap.setLayout(dv)
        f.addWidget(dv_wrap, 0, Qt.AlignmentFlag.AlignVCenter)

        f.addStretch()

        root.addWidget(footer)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = DoorEdgePreview()
    win.show()
    sys.exit(app.exec())
