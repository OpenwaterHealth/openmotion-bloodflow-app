# Per-Camera Temperature in Plot Cells Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each camera's latest temperature in the top-right corner of its plot cell, orange, developer mode only (issue #165).

**Architecture:** Temperature becomes a fifth metric stream (`"temp"`) in `LiveScanSource`, fed by `_LivePlotSink.consume` for light frames only. QML reads it through the existing `value_at` slot — no new signals or QML-facing API. Display is a `Text` in `PlotCell.qml` gated by a `showTemperature` property that `PlotViewer.qml` binds to `appConfig.developerMode`.

**Tech Stack:** Python 3.13 / PyQt6 / QML / pytest. Spec: `docs/superpowers/specs/2026-06-11-plot-cell-temperature-design.md`. Branch: `feature/165-plot-cell-temps` (already created, off `next`).

---

### Task 1: `temp` metric in `LiveScanSource.append_uncorrected`

**Files:**
- Modify: `data_sources.py:680-702` (`append_uncorrected`)
- Test: `tests/test_data_sources.py` (append after `test_live_scan_source_skips_none_mean_or_contrast`, ~line 210)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_data_sources.py` after `test_live_scan_source_skips_none_mean_or_contrast`:

```python
def test_live_scan_source_append_uncorrected_stores_temp():
    """temp is an optional fifth metric — stored under metric key "temp"
    so PlotCell can read it via value_at (issue #165)."""
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(
        side="left", cam_id=2, frame_id=42, t=0.025,
        bfi=4.5, bvi=3.1, temp=54.3,
    )
    buf = src.buffers[("left", 2, "temp")]
    assert buf.n == 1
    assert buf.v[0] == np.float32(54.3)
    assert buf.t[0] == 0.025
    assert src.value_at("left", 2, "temp", 0.025) == pytest.approx(54.3, abs=1e-4)


def test_live_scan_source_skips_none_temp():
    """temp=None (the default) creates no "temp" buffer — same contract
    as mean/contrast."""
    src = LiveScanSource(plot_t0=0.0)
    src.append_uncorrected(
        side="left", cam_id=0, frame_id=1, t=0.0,
        bfi=4.0, bvi=3.0,
    )
    assert ("left", 0, "temp") not in src.buffers
```

(`pytest` is already imported in the file; if not, the existing imports at the top include `numpy as np` and `math` — check the header and add `import pytest` only if missing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_data_sources.py -k temp -v`
Expected: `test_live_scan_source_append_uncorrected_stores_temp` FAILS with `TypeError: append_uncorrected() got an unexpected keyword argument 'temp'`; `test_live_scan_source_skips_none_temp` PASSES (no buffer is created today — that's fine, it pins the contract).

- [ ] **Step 3: Implement**

In `data_sources.py`, change `LiveScanSource.append_uncorrected` (currently lines 680–702):

```python
    def append_uncorrected(
        self,
        side: str,
        cam_id: int,
        frame_id: int,
        t: float,
        bfi: float,
        bvi: float,
        mean: Optional[float] = None,
        contrast: Optional[float] = None,
        temp: Optional[float] = None,
    ) -> None:
        """Append one frame's worth of uncorrected metrics.

        bfi and bvi are always appended (NaN included — the source stores
        what arrives). mean/contrast/temp are appended only when non-None;
        the existing _LivePlotSink passes None for samples where the
        SDK reported a non-finite mean_dc_rt / contrast_sn_rt, and None
        temp for dark frames (whose camera-temp reading is meaningless)."""
        self._append_one(side, cam_id, "bfi", frame_id, t, bfi)
        self._append_one(side, cam_id, "bvi", frame_id, t, bvi)
        if mean is not None:
            self._append_one(side, cam_id, "mean", frame_id, t, mean)
        if contrast is not None:
            self._append_one(side, cam_id, "contrast", frame_id, t, contrast)
        if temp is not None:
            self._append_one(side, cam_id, "temp", frame_id, t, temp)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_data_sources.py -v`
Expected: all PASS (the two new tests plus no regressions in the file).

- [ ] **Step 5: Commit**

```powershell
git add data_sources.py tests/test_data_sources.py
git commit -m "feat: optional temp metric stream in LiveScanSource (#165)"
```

---

### Task 2: `_LivePlotSink` passes per-camera temp for light frames

**Files:**
- Modify: `motion_connector.py:263-294` (`_LivePlotSink.consume`, light/dark metric block + `append_uncorrected` call)
- Test: `tests/test_live_plot_sink.py`

- [ ] **Step 1: Update the recorder stub and write the failing tests**

In `tests/test_live_plot_sink.py`, extend `_RecorderLiveSource.append_uncorrected` (line 24) to accept and record `temp`:

```python
    def append_uncorrected(self, *, side, cam_id, frame_id, t, bfi, bvi,
                           mean=None, contrast=None, temp=None):
        self.appended.append({
            "side": side, "cam_id": cam_id, "frame_id": frame_id, "t": t,
            "bfi": bfi, "bvi": bvi, "mean": mean, "contrast": contrast,
            "temp": temp,
        })
```

Then add at the end of the file:

```python
def test_live_plot_sink_passes_temp_for_light_frames():
    """Light frames carry the camera temperature into the plot source as
    the "temp" metric (issue #165 — per-cell dev-mode readout)."""
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((1, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((1, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((1, 2, 8), dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 54.3, dtype=np.float32),
        frame_type=np.array(["light"], dtype="<U8"),
        timestamp_s=np.array([0.5], dtype=np.float64),
        abs_frame_ids=np.array([1], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([0], dtype=np.int8),
    )
    batch.bfi_live[0, 0, 0] = 0.3
    batch.bvi_live[0, 0, 0] = 5.0

    sink.consume("live", batch)

    assert len(src.appended) == 1
    assert src.appended[0]["temp"] == pytest.approx(54.3, abs=1e-4)


def test_live_plot_sink_no_temp_for_dark_frames():
    """Dark frames have no meaningful camera-temp reading — temp must be
    None so the plot's temp stream only carries light-frame readings."""
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((1, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((1, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((1, 2, 8), dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 54.3, dtype=np.float32),
        frame_type=np.array(["dark"], dtype="<U8"),
        timestamp_s=np.array([0.5], dtype=np.float64),
        abs_frame_ids=np.array([1], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([0], dtype=np.int8),
    )
    batch.bfi_live[0, 0, 0] = 0.3
    batch.bvi_live[0, 0, 0] = 5.0

    sink.consume("live", batch)

    assert len(src.appended) == 1
    assert src.appended[0]["temp"] is None


def test_live_plot_sink_no_temp_when_non_finite():
    """A NaN temperature (e.g. sensor metadata gap) is not appended —
    the temp buffer carries only real readings."""
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((1, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((1, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((1, 2, 8), dtype=np.float32),
        temperature_c=np.full((1, 2, 8), np.nan, dtype=np.float32),
        frame_type=np.array(["light"], dtype="<U8"),
        timestamp_s=np.array([0.5], dtype=np.float64),
        abs_frame_ids=np.array([1], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([0], dtype=np.int8),
    )
    batch.bfi_live[0, 0, 0] = 0.3
    batch.bvi_live[0, 0, 0] = 5.0

    sink.consume("live", batch)

    assert len(src.appended) == 1
    assert src.appended[0]["temp"] is None
```

Note: the NaN-temp batch keeps finite bfi/bvi, so the sample itself IS appended (the existing NaN skip at motion_connector.py:231 only gates on bfi/bvi). Only `temp` is withheld. Also note the temperature-alert check at line 254 compares `temp_c >= threshold`, which is False for NaN — no alert fires; the stub `_connector()` threshold is 100.0 so 54.3 doesn't alert either.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live_plot_sink.py -v`
Expected: `test_live_plot_sink_passes_temp_for_light_frames` FAILS with `assert None == approx(54.3)` (the stub records `temp=None` because `consume` doesn't pass it yet). The dark-frame and non-finite tests PASS already (they pin the None contract). Existing tests still PASS.

- [ ] **Step 3: Implement**

In `motion_connector.py`, `_LivePlotSink.consume`: extend the light-frame metric block (currently lines 263–280) and the `append_uncorrected` call (lines 285–294):

```python
                mean_for_source: Optional[float] = None
                contrast_for_source: Optional[float] = None
                temp_for_source: Optional[float] = None
                if not is_dark:
                    # Record the realtime dark-corrected mean (mean_dc_rt) and
                    # shot-noise-corrected contrast (contrast_sn_rt). The
                    # live display is realtime-only by design; the accurate
                    # corrected record lands in the scan DB via the SDK's
                    # ScanDBSink and is what replay shows. Skip NaN samples —
                    # early light frames before the first dark observation
                    # have NaN mean_dc_rt and shouldn't poison the plot.
                    if batch.mean_dc_rt is not None:
                        mean_val = float(batch.mean_dc_rt[i, side_idx, cam_id])
                        if math.isfinite(mean_val):
                            mean_for_source = mean_val
                    if batch.contrast_sn_rt is not None:
                        contrast_val = float(batch.contrast_sn_rt[i, side_idx, cam_id])
                        if math.isfinite(contrast_val):
                            contrast_for_source = contrast_val
                    # Camera temperature — light frames only (dark-frame
                    # readings are meaningless for display, same rule as the
                    # temperature alert above). Feeds the per-cell dev-mode
                    # readout (issue #165).
                    if math.isfinite(temp_c):
                        temp_for_source = temp_c

                # mean/contrast already filtered for is_dark / non-finite
                # above; None here means "metric not available for this
                # sample".
                self._live_source.append_uncorrected(
                    side=side,
                    cam_id=cam_id,
                    frame_id=abs_frame_id,
                    t=plot_ts,
                    bfi=bfi,
                    bvi=bvi,
                    mean=mean_for_source,
                    contrast=contrast_for_source,
                    temp=temp_for_source,
                )
```

(Only the `temp_for_source` lines and the `temp=` kwarg are new; the surrounding code is shown for placement.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live_plot_sink.py tests/test_data_sources.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```powershell
git add motion_connector.py tests/test_live_plot_sink.py
git commit -m "feat: feed per-camera temp into the live plot source (#165)"
```

---

### Task 3: temperature readout in `PlotCell.qml`

**Files:**
- Modify: `components/PlotCell.qml` (new property near line 55; new Text after the top-left label Column, ~line 265)

No QML unit-test framework exists in this repo — verification is visual (Task 5).

- [ ] **Step 1: Add the `showTemperature` property**

In `components/PlotCell.qml`, directly below the `showValueLabels` property (line 53–55):

```qml
    // Top-right temperature readout — developer mode only; the viewer
    // binds this from appConfig.developerMode (issue #165).
    property bool showTemperature: false
```

- [ ] **Step 2: Add the readout Text**

After the closing brace of the top-left label `Column` (line 265), insert:

```qml
    // Camera temperature — top-right, orange, dev mode only. Reads the
    // "temp" metric stream (light frames only; absent for the reduced-
    // mode cam_id=-1 average and for past scans) and hides itself when
    // no finite reading exists.
    Text {
        property real tempC: {
            void cell.paintTick  // dependency
            if (!cell.source || !cell.showTemperature) return NaN
            return cell.source.value_at(cell.side, cell.camId, "temp",
                                        cell.liveEdgeSnapshot)
        }
        visible: cell.showTemperature && cell.width >= 80 && isFinite(tempC)
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: 8
        text: isFinite(tempC) ? tempC.toFixed(1) + "°C" : ""
        color: theme.readableInk(theme.accentOrange)
        font.pixelSize: 10
        font.family: "Roboto Mono"
    }
```

(The `!cell.showTemperature → NaN` early-out keeps the per-tick `value_at` call out of clinical builds entirely.)

- [ ] **Step 3: Commit**

```powershell
git add components/PlotCell.qml
git commit -m "feat: orange top-right temp readout in PlotCell (#165)"
```

---

### Task 4: developer-mode gate in `PlotViewer.qml`

**Files:**
- Modify: `components/PlotViewer.qml:656-680` (the `Repeater` delegate)

- [ ] **Step 1: Bind `showTemperature` on the delegate**

In the `Repeater`'s `PlotCell` delegate (after `showValueLabels: viewer.showCellValues`, line 675):

```qml
                        showTemperature: MotionInterface.appConfig.developerMode === true
```

- [ ] **Step 2: Commit**

```powershell
git add components/PlotViewer.qml
git commit -m "feat: gate plot-cell temp readout on developerMode (#165)"
```

---

### Task 5: full test run + visual verification

- [ ] **Step 1: Run the app-side unit tests**

Run: `python -m pytest tests/test_data_sources.py tests/test_live_plot_sink.py -v`
Expected: all PASS. (Do NOT run the whole `tests/` dir blindly — it contains hardware-in-loop tests.)

- [ ] **Step 2: Flake8 the touched Python**

Run: `python -m flake8 data_sources.py motion_connector.py tests/test_data_sources.py tests/test_live_plot_sink.py`
Expected: no output (repo pins flake8 7.1.1; match existing style).

- [ ] **Step 3: Visual verification (QML changes need a visual check)**

1. In `config/app_config.json` confirm `cameraFakeData: true` and `developerMode: true` (the working tree already has a modified config — set keys without reverting the user's other edits, and restore anything you change afterward).
2. `python main.py`, start a scan so the live plot grid appears.
3. Screenshot the window (PrintWindow flag 2 — see memory note) and confirm: orange `NN.N°C` in the top-right of every active cell, no overlap with the top-left labels or the back-to-live pill.
4. Flip `developerMode: false`, restart, confirm the readout is gone (reduced mode shows no temp).
5. Restore the config to its prior state.

- [ ] **Step 4: Final commit (if verification produced fixes)**

```powershell
git add -A -- components data_sources.py motion_connector.py tests docs
git commit -m "fix: post-verification adjustments for plot-cell temps (#165)"
```

(Skip if nothing changed.)
