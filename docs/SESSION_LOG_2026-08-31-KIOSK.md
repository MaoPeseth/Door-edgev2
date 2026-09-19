# Door-Edge Session Log — Kiosk Display Redesign (2026-08-31)

**Date:** August 31, 2026
**Scope:** Redesign the kiosk screen display (`core/screen_display.py`) with a
green-on-dark brand theme + logo, and fix a batch of display bugs. **Work
paused overnight — resume tomorrow.**

---

## Summary

Redesigned the fullscreen kiosk from the old dark blue-grey theme to a green
brand theme, added the project logo to the header, and fixed six display
issues. A static HTML/CSS mockup was also produced as a design reference. The
changes compile and run, but the **visual result on the live screen still needs
human confirmation** (this model cannot view images, and programmatic
screenshot analysis was inconclusive).

---

## Done (implemented, compile-clean)

### Theme & layout
- **Green-on-dark palette** in `core/screen_display.py`:
  `_BG #0A0F0C`, `_PANEL #101612`, `_PANEL2 #16201A`, `_ACCENT/_GREEN #2BD37A`,
  `_AMBER #FFB648`, `_RED #F0555C`.
- **Header**: green 2px border ring (`highlightthickness=2`), logo (58px) +
  DOOR-EDGE title (green) + room/device id; right side live clock/date + status
  badge.
- **Camera feed** is the dominant expanding area, with an in-feed bottom-center
  hint chip + a dedicated **alert/denial banner** below the feed.
- **Sidebar** (fixed 400px): SYSTEM STATUS, LAST ACCESS, warning banner,
  RECENT EVENTS ticker.
- **Footer**: sponsors left, developers + version, green style.
- **Logo** (`Untitled.jpeg`, square 1667x1667): path built from `cfg.BASE_DIR`
  (handles the space in "Door-Edge V2"); both PIL `Image` and `ImageTk.PhotoImage`
  kept as instance refs (`self._logo_img`, `self._logo`) to avoid GC. Graceful
  fallback brand mark if absent.

### Bug fixes (the 6 requested)
1. **Green theme** — strengthened: green header ring, green section headers
   (`_make_panel` label fg `_GREEN` + green accent bar), locked badge now green
   (`_STATE_COLOR idle=_GREEN`).
2. **Logo rendering** — persistent refs + verified path resolves to the logo.
3. **Alert text off the video feed** — banner-labeled draw_cmds are no longer
   `putText`-baked onto the frame. New `_banner_for()` classifier + solid
   `_alert_banner` Label below the feed (crisp Tk text, background coloured by
   outcome: ok=green, deny=red, warn=amber).
4. **Garbled alert text** — now renders `"{name} — SCHEDULE BLOCKED (REASON)"`
   (unit-tested: `Peseth bros smos — SCHEDULE BLOCKED (LOCKDOWN)`).
5. **LAST ACCESS overflow** — labels now `wraplength=380`; raw reason codes
   humanized via `_humanize_reason()` (`schedule_blocked_LOCKDOWN` →
   `SCHEDULE BLOCKED (LOCKDOWN)`).
6. **Ticker mid-word wrap** — `tk.Text` set to `wrap="word"` so "LOCKDOWN"
   won't split mid-word.

### Also
- HTML/CSS mockup: `docs/kiosk_mockup.html` (design reference; logo at
  `../Untitled.jpeg`).
- `config.py` `DISPLAY_VERSION` bumped to `v2.0`.
- Footer restored to bottom-left sponsors/developers lines (was clipped when
  packed left/right on one row).

---

## Verified (executed)
- `venv/bin/python -m py_compile core/screen_display.py` → OK
- `_banner_for()` unit checks → correct kind/message mapping
- App runs under `test_display_sim.py` (windowed 8s) with no Tk errors
- Logo path + `cfg.BASE_DIR` resolve correctly
- Fixed runtime bug: `tk` not defined in `_update_banner()` (added local import)

---

## NOT verified / blocked on human eyes 👀 (for tomorrow)
- **Final visual check of the live screen** — this model cannot view images.
  Pixel analysis of a screenshot could NOT confirm the green accents/banner
  reliably (suspected screen/colour-profile capture issue, not a code issue).
  A human must run the display and confirm:
  1. Logo actually appears in the header (not the fallback mark).
  2. Green reads as the brand colour at a glance.
  3. The schedule-blocked banner appears below the feed (readable, no overlay
     on the face).
  4. LAST ACCESS wraps instead of clipping a long "schedule_blocked" reason.
  5. "LOCKDOWN" doesn't split in the ticker.
- **Shutdown artefact**: process sometimes prints
  `Tcl_AsyncDelete: async handler deleted by the wrong thread` and can core
  dump on exit (`display.stop()` join). Benign during run, but consider a clean
  shutdown fix.
- Existing known-good note: the trailing `_draw_hud`/`_draw_zone` overlay text is
  still drawn on the frame (ROI zone, state HUD) — that's intentional overlay on
  empty video areas, not on faces.

---

## How to verify tomorrow
```bash
cd "/home/tee/Desktop/my_project/Door-Edge V2"
venv/bin/python test_display_sim.py          # fullscreen, ESC to exit
venv/bin/python ../../... edge_app.py        # real camera + MQTT + Cloud
```
The regular sim's event loop does NOT emit `SCHEDULE BLOCKED` draw_cmds, so to
see the banner use the real app (or a small script feeding a synthetic
schedule-blocked result).

---

## Files touched
```
core/screen_display.py    theme + layout + logo + banner + wrap fixes
config.py                 DISPLAY_VERSION v2.0
docs/kiosk_mockup.html    NEW design mockup
```
