# Camera Dropout Notification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect when a camera stops posting data during a scan (overheat dropout), fire a 30-second toast warning, log the event with temperature, and annotate the real-time plot for that camera lane.

**Architecture:** A 1 Hz `QTimer` watchdog runs inside `motion_connector.py` during active scans. It checks timestamp dictionaries updated on every `_on_uncorrected` callback (~40 Hz). On detecting silence > 2 s, it logs, calls `self.notify()`, and emits `cameraDropoutDetected(side, cam_id)`. QML plot components handle that signal to set a per-channel `droppedOut` flag and repaint with a dashed-line + amber label annotation.

**Tech Stack:** Python 3.12 / PyQt6, QML 6, `time.monotonic()`, existing `self.notify()` toast system, Canvas-based realtime plot.

---

## Files to be modified

| File | What changes |
|------|-------------|
| `config/app_config.json` | Add `cameraDropoutThresholdSec` key |
| `motion_connector.py` | New signal, state vars, watchdog timer, `_on_dropout_check`, updates to `__init__`, `startCapture`, `stopCapture`, `_on_uncorrected` |
| `components/EmbeddedRealtimePlot.qml` | `droppedOut` field in store, `markDroppedOut()`, `onPaint` annotation, `Connections` handler |
| `components/ReducedPlotView.qml` | Per-side dropout tracking, `Connections` handler, `_paintCanvas` overlay |

---

## Task 1: Add config key and load it in the connector

**Files:**
- Modify: `config/app_config.json`
- Modify: `motion_connector.py:181`

- [ ] **Step 1: Add the config key to `app_config.json`**

Open `config/app_config.json`. Add the new key alongside `cameraTempAlertThresholdC` (it currently appears around line 3):

```json
"cameraDropoutThresholdSec": 2.0,
```

- [ ] **Step 2: Load the key in `motion_connector.py` `__init__`**

In `motion_connector.py`, line 181 currently reads:
```python
        self._camera_temp_alert_threshold_c = float(cfg.get("cameraTempAlertThresholdC", 105.0))
```

Add the new line immediately after it:
```python
        self._camera_dropout_threshold_sec  = float(cfg.get("cameraDropoutThresholdSec", 2.0))
```

- [ ] **Step 3: Commit**

```bash
git add config/app_config.json motion_connector.py
git commit -m "feat: add cameraDropoutThresholdSec config key"
```

---

## Task 2: Add watchdog signal, state, and timer to `motion_connector.py`

**Files:**
- Modify: `motion_connector.py` — signal block (~line 92), `__init__` (~line 170), `startCapture` (~line 1191), `stopCapture` (~line 803), `_on_uncorrected` (~line 1258)

### 2a — New signal

- [ ] **Step 1: Declare `cameraDropoutDetected` signal**

In `motion_connector.py`, find the signal block (lines 73–141). After line 127:
```python
    scanCameraTemperature = pyqtSignal(str, int, float)  # side, cam_id, temperature_c
```
Add:
```python
    cameraDropoutDetected = pyqtSignal(str, int)         # side ("left"/"right"), cam_id (0-7)
```

### 2b — Instance state

- [ ] **Step 2: Add watchdog instance variables to `__init__`**

After the line you added in Task 1 (`self._camera_dropout_threshold_sec = ...`), add:

```python
        # Camera dropout watchdog state — reset at start of each scan
        self._camera_last_seen: dict[tuple[str, int], float] = {}   # (side, cam_id) → monotonic time
        self._camera_last_temp: dict[tuple[str, int], float] = {}   # (side, cam_id) → last temp_c
        self._camera_dropped:   set[tuple[str, int]]         = set() # cameras already alerted

        # 1 Hz watchdog timer — started/stopped around scan lifecycle
        self._dropout_timer = QTimer(self)
        self._dropout_timer.setInterval(1000)
        self._dropout_timer.timeout.connect(self._on_dropout_check)
```

`QTimer` is already imported at the top of `motion_connector.py` (it's used elsewhere). Verify with a quick search before proceeding — if it isn't imported, add `from PyQt6.QtCore import QTimer` to the existing PyQt6 import block.

### 2c — Watchdog slot

- [ ] **Step 3: Add `_on_dropout_check` method**

Find `stopCapture` at line 803. Insert a new method just above it:

```python
    @pyqtSlot()
    def _on_dropout_check(self):
        """1 Hz watchdog: emit cameraDropoutDetected for any camera silent > threshold."""
        if not self._capture_running:
            return
        now = time.monotonic()
        threshold = self._camera_dropout_threshold_sec
        for key, last_t in list(self._camera_last_seen.items()):
            if key in self._camera_dropped:
                continue
            if now - last_t > threshold:
                side, cam_id = key
                temp = self._camera_last_temp.get(key, float("nan"))
                temp_str = f"{temp:.1f}°C" if not isinstance(temp, float) or not (temp != temp) else "unknown"
                msg = (
                    f"Camera {side.upper()} {cam_id + 1} dropout detected "
                    f"(no data for >{threshold:.0f} s). Last temperature: {temp:.1f}°C"
                )
                logger.warning(msg)
                run_logger.warning("[DROPOUT] side=%s cam=%d temp=%.1f°C threshold=%.0fs",
                                   side, cam_id, temp, threshold)
                self.notify(
                    f"⚠ Camera {side.upper()} {cam_id + 1} stopped posting — possible overheat ({temp:.1f}°C)",
                    type_="warning",
                    duration_ms=30000,
                    tag=f"dropout_{side}_{cam_id}",
                )
                self._camera_dropped.add(key)
                self.cameraDropoutDetected.emit(side, cam_id)
```

Note: `run_logger` and `logger` are already defined at module level (lines 59–61). `time` is already imported. `self.notify()` is defined at line 1115.

### 2d — Wire timer into scan lifecycle

- [ ] **Step 4: Reset watchdog state and start timer in `startCapture`**

In `startCapture` (line ~1249), there is already:
```python
        temp_alerted_by_side = {"left": set(), "right": set()}
```

Add three lines immediately after it:
```python
        self._camera_last_seen = {}
        self._camera_last_temp = {}
        self._camera_dropped   = set()
        self._dropout_timer.start()
```

- [ ] **Step 5: Stop timer in `stopCapture`**

In `stopCapture` (line 803), after `self._capture_stop.set()` and before the try/except block, add:
```python
        self._dropout_timer.stop()
```

### 2e — Update `_on_uncorrected` to track last-seen time

- [ ] **Step 6: Update `_on_uncorrected` to populate tracking dicts**

`_on_uncorrected` is defined at line 1258. It currently starts:
```python
        def _on_uncorrected(sample):
            """Fires for every non-dark frame (~40 Hz). Feeds the realtime plot."""
            current_side = sample.side
            alerted = temp_alerted_by_side.setdefault(current_side, set())
```

Add two lines immediately after `current_side = sample.side`:
```python
            _key = (sample.side, int(sample.cam_id))
            self._camera_last_seen[_key] = time.monotonic()
            self._camera_last_temp[_key] = float(sample.temperature_c)
```

These lines go before the existing temperature alert logic. They access `self` because `_on_uncorrected` is a closure inside `startCapture` and `self` is in scope.

- [ ] **Step 7: Commit**

```bash
git add motion_connector.py
git commit -m "feat: add camera dropout watchdog timer and signal"
```

---

## Task 3: Annotate `EmbeddedRealtimePlot.qml`

**Files:**
- Modify: `components/EmbeddedRealtimePlot.qml:184–190` (`_ensureEntry`), `~494–614` (`onPaint`), `757–777` (`Connections`)

### 3a — Add `droppedOut` field to the store entry

- [ ] **Step 1: Add `droppedOut: false` to `_ensureEntry`**

`_ensureEntry` is at line 182. It currently reads:
```javascript
    function _ensureEntry(key) {
        if (_store[key]) return
        _store[key] = {
            bfi: [], bvi: [], mean: [], contrast: [],
            latestBfi: NaN, latestBvi: NaN,
            latestMean: NaN, latestContrast: NaN,
            latestTemp: NaN,
            bviLpState: NaN
        }
    }
```

Change to:
```javascript
    function _ensureEntry(key) {
        if (_store[key]) return
        _store[key] = {
            bfi: [], bvi: [], mean: [], contrast: [],
            latestBfi: NaN, latestBvi: NaN,
            latestMean: NaN, latestContrast: NaN,
            latestTemp: NaN,
            bviLpState: NaN,
            droppedOut: false
        }
    }
```

### 3b — Add `markDroppedOut` function

- [ ] **Step 2: Add `markDroppedOut` helper after `stopScan`**

After `stopScan()` (line 226–229), add:
```javascript
    function markDroppedOut(side, camId) {
        const key = _seriesKey(side, camId)
        _ensureEntry(key)
        _store[key].droppedOut = true
        _store = _store   // trigger QML property change notification so canvases repaint
    }
```

### 3c — Annotate `onPaint`

- [ ] **Step 3: Add dropout overlay to `onPaint`**

In the `onPaint` handler (line 494–614), after the `"Waiting for data"` block (around line 601–611):

```javascript
                // "Waiting for data" placeholder
                const hasData = showBfi
                    ? (s.bfi.length > 0 || s.bvi.length > 0)
                    : (s.mean.length > 0 || s.contrast.length > 0)
                if (!hasData) {
                    ctx.fillStyle    = theme.textTertiary.toString()
                    ctx.textAlign    = "center"
                    ctx.textBaseline = "middle"
                    ctx.font         = "12px sans-serif"
                    ctx.fillText("Waiting for data...", padL + w / 2, padT + h / 2)
                }
```

Replace this entire block with:
```javascript
                // "Waiting for data" placeholder
                const hasData = showBfi
                    ? (s.bfi.length > 0 || s.bvi.length > 0)
                    : (s.mean.length > 0 || s.contrast.length > 0)
                if (!hasData && !s.droppedOut) {
                    ctx.fillStyle    = theme.textTertiary.toString()
                    ctx.textAlign    = "center"
                    ctx.textBaseline = "middle"
                    ctx.font         = "12px sans-serif"
                    ctx.fillText("Waiting for data...", padL + w / 2, padT + h / 2)
                }

                // Dropout annotation — dashed trailing line + amber label
                if (s.droppedOut) {
                    const activeSeries = showBfi ? s.bfi : s.mean
                    if (activeSeries.length > 0) {
                        const lastPt  = activeSeries[activeSeries.length - 1]
                        const bounds  = showBfi ? bfiB : meanB
                        const invR    = bounds.range > 0 ? 1.0 / bounds.range : 1.0
                        const invert  = plotArea.invertPlotAxes
                        const lastY   = invert
                            ? padT + ((lastPt.v - bounds.minVal) * invR) * h
                            : padT + h - ((lastPt.v - bounds.minVal) * invR) * h
                        const lastX   = padL + ((lastPt.t - xMin) / xRange) * w

                        ctx.save()
                        ctx.strokeStyle = "#888888"
                        ctx.lineWidth   = 1.5
                        ctx.setLineDash([6, 4])
                        ctx.beginPath()
                        ctx.moveTo(Math.max(padL, lastX), lastY)
                        ctx.lineTo(padL + w, lastY)
                        ctx.stroke()
                        ctx.restore()
                    }

                    ctx.fillStyle    = "#FFA500"
                    ctx.font         = "bold 10px sans-serif"
                    ctx.textAlign    = "left"
                    ctx.textBaseline = "top"
                    ctx.fillText("⚠ DROPOUT", padL + 4, padT + 4)
                }
```

### 3d — Handle the signal in `Connections`

- [ ] **Step 4: Add `onCameraDropoutDetected` to the `Connections` block**

The `Connections` block starts at line 757. Add the new handler after `onScanCameraTemperature`:

```javascript
        function onScanCameraTemperature(side, camId, tempC) {
            plotArea.handleTempSample(side, camId, tempC)
        }
        function onCameraDropoutDetected(side, camId) {
            plotArea.markDroppedOut(side, camId)
        }
```

- [ ] **Step 5: Commit**

```bash
git add components/EmbeddedRealtimePlot.qml
git commit -m "feat: annotate EmbeddedRealtimePlot on camera dropout"
```

---

## Task 4: Annotate `ReducedPlotView.qml`

**Files:**
- Modify: `components/ReducedPlotView.qml:51–54` (data properties), `180–192` (Connections), `195–` (`_paintCanvas`)

### 4a — Track dropped sides

- [ ] **Step 1: Add `droppedSides` property**

In `ReducedPlotView.qml`, after `property var rightData` (line ~54), add:
```javascript
    // Tracks which sides have had a camera dropout this scan
    property var droppedSides: ({ left: false, right: false })
```

### 4b — Handle signal in `Connections`

- [ ] **Step 2: Add `onCameraDropoutDetected` to `Connections`**

The `Connections` block starts at line 180. Add after the existing handlers:
```javascript
        function onCameraDropoutDetected(side, camId) {
            var d = droppedSides
            d[side] = true
            droppedSides = d
        }
```

### 4c — Add overlay to `_paintCanvas`

- [ ] **Step 3: Update `_paintCanvas` to show amber label**

`_paintCanvas` is at line 195. It receives `data` (either `leftData` or `rightData`). The function does not currently know which side it is, so we need to check the dropout overlay at the call sites instead.

Find where `leftCanvas.requestPaint()` and `rightCanvas.requestPaint()` are called (around line 175). The canvases call `_paintCanvas` from their own `onPaint` handlers. Locate those `onPaint` handlers.

Read the `leftCanvas` and `rightCanvas` Canvas elements to find their `onPaint` bodies — they call `root._paintCanvas(ctx, width, height, root.leftData, ...)` and `root._paintCanvas(ctx, width, height, root.rightData, ...)`.

Add the dropout overlay **after** the `root._paintCanvas(...)` call in each canvas's `onPaint`:

For **`leftCanvas.onPaint`**, after the `_paintCanvas` call:
```javascript
                    if (root.droppedSides.left) {
                        var ctx2 = leftCanvas.getContext("2d")
                        ctx2.fillStyle    = "#FFA500"
                        ctx2.font         = "bold 11px sans-serif"
                        ctx2.textAlign    = "left"
                        ctx2.textBaseline = "top"
                        ctx2.fillText("⚠ CAMERA DROPOUT", 54, 16)
                    }
```

For **`rightCanvas.onPaint`**, after the `_paintCanvas` call:
```javascript
                    if (root.droppedSides.right) {
                        var ctx2 = rightCanvas.getContext("2d")
                        ctx2.fillStyle    = "#FFA500"
                        ctx2.font         = "bold 11px sans-serif"
                        ctx2.textAlign    = "left"
                        ctx2.textBaseline = "top"
                        ctx2.fillText("⚠ CAMERA DROPOUT", 54, 16)
                    }
```

> **Note:** Before writing this step, read lines 195–380 of `ReducedPlotView.qml` to find the exact `onPaint` bodies and correct padding offsets. Adjust `54` (padL) and `16` (padT) to match whatever padding the canvas actually uses.

- [ ] **Step 4: Reset `droppedSides` on `startScan` / `stopScan`**

Find `startScan` and `stopScan` functions in `ReducedPlotView.qml`. In each, reset the flag:
```javascript
    droppedSides = ({ left: false, right: false })
```

- [ ] **Step 5: Commit**

```bash
git add components/ReducedPlotView.qml
git commit -m "feat: annotate ReducedPlotView on camera dropout"
```

---

## Task 5: Manual smoke test

No automated tests exist for this code path (hardware-in-loop), so verify by manual inspection and a simulated test.

- [ ] **Step 1: Launch the app**

```bash
cd c:\Users\ethan\Projects\openmotion-bloodflow-app
python main.py
```

- [ ] **Step 2: Simulate a dropout via Python console**

With the app running and a scan active, open a second Python shell and inject a dropout signal:

```python
# In a separate Python shell — attach to the running Qt event loop is not feasible,
# so instead add a temporary debug slot to motion_connector.py for testing.
```

Alternatively, to test without hardware: temporarily lower `cameraDropoutThresholdSec` to `0.1` in `app_config.json`, start the app with no hardware connected, and verify that `_on_dropout_check` logs appropriately (all cameras in mask will be in `_camera_last_seen` as empty → no false positives, because the "never seen" guard skips them).

- [ ] **Step 3: Verify log output**

Run the app and check the log file (`app-logs/ow-bloodflowapp-<timestamp>.log`) for:
```
WARNING - Camera LEFT 5 dropout detected (no data for >2 s). Last temperature: ...
WARNING - [DROPOUT] side=left cam=4 temp=...°C threshold=2s
```

- [ ] **Step 4: Commit (if any fixups made)**

```bash
git add -A
git commit -m "fix: camera dropout smoke test fixups"
```

---

## Self-Review

**Spec coverage:**
- R1 — Detected in `_on_dropout_check` with `>threshold` guard and "seen at least once" check ✓
- R2 — `self.notify(..., duration_ms=30000, type_="warning")` ✓
- R3 — Both `logger.warning` and `run_logger.warning` called with side, cam_id, temp ✓
- R4 — `EmbeddedRealtimePlot` dashed line + amber label; `ReducedPlotView` amber overlay ✓
- R5 — `_camera_dropped` set prevents re-alerting; `tag=f"dropout_{side}_{cam_id}"` deduplicates toasts ✓
- R6 — Config key `cameraDropoutThresholdSec` with default 2.0 ✓
- R7 — Two dict writes per frame in `_on_uncorrected`; watchdog runs at 1 Hz in separate slot ✓

**Type consistency:**
- Signal: `cameraDropoutDetected(str, int)` — matches QML handler `(side, camId)` ✓
- `markDroppedOut(side, camId)` matches signal argument order ✓
- `droppedOut` bool initialized `false` in `_ensureEntry`, read as boolean in `onPaint` ✓
