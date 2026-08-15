# ROI Zone Plan — Detection Area Restriction

Status: **PLAN — awaiting review/approval, nothing built yet**

## Goal

Define a **Region of Interest (ROI)** in the camera frame. Only faces, spoof
objects and hands **inside** the zone are processed by the system. Anything
outside the zone is ignored at the detection source, so background clutter can
no longer trigger spoof blocks, face recognition, hand verification, photos or
alerts.

## Motivation

The anti-spoof YOLO model detects objects across the entire camera field of
view. Background objects are frequently flagged as spoof ("detect spoofing in
the background"), blocking recognition or firing denied events even though
nobody is at the door. Restricting detection to a configured zone removes
these false triggers.

## Current detection flow

| Source | Function | Returns |
|---|---|---|
| Anti-spoof YOLO | `ModelHub.get_yolo(frame)` | `[{bbox (x1,y1,x2,y2), conf, cls}]` |
| Face (InsightFace) | `ModelHub.get_faces(frame)` | face objects with `.bbox` |
| Hand verification | `ModelHub.get_hand_detections(frame)` | `[{bbox, conf, cls}]` |

All bboxes are in frame pixel coordinates (native capture: 320x240). All three
go through one chokepoint: `ModelHub` — filter there and every consumer (spoof
block, face match, hand verification, denied events, alert tracker) only sees
in-zone objects.

## Design

### 1. Zone definition — single rectangle, set via calibration

**Decision (approved):** one rectangle; configured by **clicking two corners
on the display**; zone does **not** apply to RFID.

- Normalized coordinates (fraction of frame width/height) so they survive
  camera resolution changes.
- Stored in a small JSON file (`roi_zone.json` in the project root) so it
  persists and can be re-tuned without editing code.
- `ROI_ENABLED = False` or missing file → current behavior (whole frame).

### 2. Calibration mode — `screen_display.py`

- Toggled on the display with a keyboard key (e.g. `c`) or a config flag
  (`ROI_CALIBRATE`).
- While active, the live feed shows a crosshair cursor; the operator clicks
  **corner 1** (top-left) then **corner 2** (bottom-right) of the desired zone.
- Click coordinates are mapped from display pixels → normalized frame
  coordinates (accounting for the 2x feed scale).
- The rectangle is drawn as the operator clicks; pressing `c` again (or a
  confirm key) saves `roi_zone.json` and exits calibration.
- `ESC` cancels without saving.

### 3. Filter helper — new `core/roi.py`

```python
def in_roi(bbox, frame_shape) -> bool
```

- Keeps a detection only if its **bbox center** falls inside the zone.
- Center rule chosen because it is least sensitive to edge jitter between
  frames.
- O(1) per bbox — a few comparisons; no measurable CPU cost.

### 4. Apply at the source — `core/models.py`

Filter the returned detection lists inside `get_yolo`, `get_faces`,
`get_hand_detections`. One edit point covers all consumers.
**RFID is untouched** — card scans have no camera position and are handled
entirely outside the vision pipeline.

### 5. Optional diagnostics

When `YOLO_DEBUG = True`, log dropped detections
(`spoof det outside zone — ignored`) to make tuning empirical.

## Performance impact

- **Negligible added cost:** the filter is arithmetic only (center point vs
  zone bounds) on at most ~10–20 detections per frame.
- **Models still process the whole frame** — no partial-frame inference in
  this stack, so YOLO/face/hand inference cost is unchanged.
- **Net CPU can only stay equal or drop:** detections outside the zone are
  dropped before downstream work (face match, embedding, hand model, event
  POSTs), so fewer frames do the heavy follow-up work.
- Overlay drawing: one rectangle at 15 fps — negligible.

## Behavior after the change

- Spoof object in the background outside the zone → ignored, no block.
- Spoof object inside the zone → blocks as today.
- Face outside the zone → never matched, never alerted, no photo.
- RFID card scans → unaffected (no zone filtering).
- Distance gate (`MIN/MAX_FACE_WIDTH_RATIO`) still applies on top.
- Person at the zone edge may jitter in/out for a frame — acceptable with the
  center rule.

## Files touched

| File | Change |
|---|---|
| `config.py` | `ROI_ENABLED` flag |
| `core/roi.py` | new `in_roi()` helper + `load/save` of `roi_zone.json` |
| `core/models.py` | filter detections in the three getters |
| `screen_display.py` | zone overlay + click-two-corners calibration mode |
| `roi_zone.json` | new — saved zone rectangle |
| `MODIFIED (THIS AFTERNOON).md` | document the change |

## Verification

1. Enable `ROI_ENABLED`, enter calibration mode (`c` on the display), click two
   corners, save — confirm `roi_zone.json` is written and the overlay matches.
2. Place a spoof photo **outside** the zone → system stays normal
   (no block, no denied event).
3. Move the spoof photo **inside** the zone → spoof block + denied event fire.
4. Unknown face inside the zone → recognized/alerted as today; outside → ignored.
5. Scan an RFID card → works as before (zone has no effect).
6. Check the overlay on the display matches the configured zone.

## Decisions (approved)

1. **Shape:** one rectangle.
2. **Configuration:** calibration mode — click two corners on the display.
3. **RFID:** zone does not apply — card scans are outside the vision pipeline.

## Verification
