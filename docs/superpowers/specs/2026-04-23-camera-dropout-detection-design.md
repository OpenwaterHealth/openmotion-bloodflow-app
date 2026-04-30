# Camera Dropout Detection — Design Spec

**Date:** 2026-04-23  
**Feature:** Detect when a camera channel stops posting during an active scan (overheat dropout), warn the user, log the event with temperature, and annotate the real-time plot.

---

## Background

During a blood flow scan, up to 16 cameras (8 per sensor module, left + right) stream histogram data at ~40 Hz. On occasion a camera overheats and its data stream silences while all other cameras continue posting. This is distinct from the true end-of-scan signal. The app currently has no detection or notification for this condition.

---

## Requirements

| # | Requirement |
|---|-------------|
| R1 | Detect when an active camera has posted no samples for ≥ 2 s while at least one other camera on the same scan is still posting. |
| R2 | Display a dismissable warning notification to the user for 30 s. |
| R3 | Log the dropout event to both the app logger and the per-run log, including side, camera ID, and last-known temperature. |
| R4 | Annotate the affected camera's lane in `EmbeddedRealtimePlot.qml` with a visual "dropped out" indicator for the remainder of the scan. |
| R5 | Only alert once per camera per scan (no repeated toasts for the same dropout). |
| R6 | The silence threshold is config-driven (`cameraDropoutThresholdSec`, default 2.0 s). |
| R7 | The feature must not introduce any per-frame overhead on the 40 Hz data path. |

---

## Architecture

### New state in `motion_connector.py`

```python
_camera_last_seen:  dict[tuple[str, int], float]  # (side, cam_id) → monotonic timestamp
_camera_last_temp:  dict[tuple[str, int], float]  # (side, cam_id) → last temperature_c
_camera_dropped:    set[tuple[str, int]]           # cameras that have already fired an alert
_dropout_timer:     QTimer                         # 1 Hz, active only during scan
_dropout_threshold: float                          # loaded from app_config.json
```

### New signal

```python
cameraDropoutDetected = pyqtSignal(str, int)  # side ("left"/"right"), cam_id (0–7)
```

### New config key (`app_config.json`)

```json
"cameraDropoutThresholdSec": 2.0
```

---

## Data Flow

```
_on_uncorrected(sample)
  └─ _camera_last_seen[(side, cam_id)] = time.monotonic()   ← no new overhead
  └─ _camera_last_temp[(side, cam_id)] = sample.temperature_c

_dropout_timer → _on_dropout_check()  [1 Hz, Python side]
  for each (side, cam_id) in active mask:
    if (side, cam_id) not in _camera_last_seen:
        continue   ← never posted; skip
    if (side, cam_id) in _camera_dropped:
        continue   ← already alerted; skip
    age = time.monotonic() - _camera_last_seen[(side, cam_id)]
    if age > _dropout_threshold:
        _camera_dropped.add((side, cam_id))
        temp = _camera_last_temp.get((side, cam_id), float("nan"))
        → logger.warning(...)
        → run_logger.warning(...)
        → self.notify(message, duration=30000)
        → emit cameraDropoutDetected(side, cam_id)
```

**"Active mask" definition:** iterate over bits set in `leftCameraMask` / `rightCameraMask` — the same masks already passed into `startCapture`. These are cached on the connector at scan start.

---

## Dropout Detection Logic

`_on_dropout_check` runs while `_scan_active` is True (same guard used by the temperature alert). On each tick it iterates the active camera set (≤16 entries). A dropout is triggered when all of the following are true:

1. The camera was seen at least once (has an entry in `_camera_last_seen`) — cameras that were masked out from the start are excluded.
2. The camera has not already been reported (`_camera_dropped`).
3. `time.monotonic() - _camera_last_seen[(side, cam_id)] > _dropout_threshold`.

State is fully reset at the start of each `startCapture` call so a new scan starts clean.

---

## Log Messages

**App logger (`openmotion.bloodflow-app.connector`):**
```
WARNING - Camera LEFT 5 dropout detected (no data for 2.0 s). Last temperature: 87.3°C
```

**Per-run log (`bloodflow-app.runlog`):**
```
WARNING - [DROPOUT] side=left cam=5 temp=87.3°C threshold=2.0s
```

---

## Notification

Uses the existing `self.notify()` path → `NotificationCenter.qml`.

```
⚠ Camera LEFT 5 stopped posting — possible overheat (87.3°C)
```

Duration: 30 000 ms (30 s). The user can dismiss it early. No repeat for the same camera within the same scan.

---

## Plot Annotation (`EmbeddedRealtimePlot.qml`)

When `cameraDropoutDetected(side, cam_id)` is received via `Connections`:

1. Set `_store[key].droppedOut = true` (new boolean field on the per-channel store entry).
2. In the `onPaint` handler for that canvas lane:
   - After the last real data point, draw a **dashed gray horizontal line** at the last known Y value extending to the right edge of the plot.
   - Draw a small **"⚠ DROPOUT"** text label in the upper-left corner of the lane in amber (`#FFA500`).
3. Annotation persists until `stopScan()` clears the store.

`ReducedPlotView.qml`: receives the same signal and renders a similar amber text label on the affected side's aggregate canvas.

---

## Files Changed

| File | Change |
|------|--------|
| `motion_connector.py` | Add `_camera_last_seen`, `_camera_last_temp`, `_camera_dropped`, `_dropout_timer`, `_dropout_threshold`, `cameraDropoutDetected` signal, `_on_dropout_check()` slot; update `_on_uncorrected`, `startCapture`, `stopCapture`, `_load_app_config` |
| `components/EmbeddedRealtimePlot.qml` | Add `droppedOut` field to channel store; handle `cameraDropoutDetected`; annotate `onPaint` |
| `components/ReducedPlotView.qml` | Handle `cameraDropoutDetected`; render side-level amber label |
| `config/app_config.json` | Add `"cameraDropoutThresholdSec": 2.0` |

---

## Out of Scope

- Automatic scan abort on dropout (user may choose to continue collecting from remaining cameras).
- Recovery detection (camera comes back online after dropout).
- Modifying the SDK or firmware.
