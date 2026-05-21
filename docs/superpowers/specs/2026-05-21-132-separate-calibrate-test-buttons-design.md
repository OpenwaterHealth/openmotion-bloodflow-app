# Separate Calibrate and Test Buttons — Design Spec

**Date:** 2026-05-21
**Issue:** openmotion-bloodflow-app#132
**Milestone:** 1.1.2 (due 2026-05-22)
**Feature:** Split the single **Run Calibration** button into two — **Calibrate** and **Test** — so an operator can either (a) run the full calibration sequence (now with the calibration scan extended to 15 s and *all* laser-on samples averaged for the C_max / I_max computation), or (b) run *only* the calibration scan as a 15 s diagnostic and see per-camera light-mean, dark-mean, and contrast plus the pass/fail status in a non-modal results window with a Copy button.

---

## Background

Today the **Run Calibration** button in `components/SettingsModal.qml` (line 781) calls `MOTIONInterface.runCalibration(target)`, which kicks the SDK's `CalibrationWorkflow.start_calibration` (`openmotion-sdk/omotion/CalibrationWorkflow.py`). That workflow runs five phases:

| Phase | What | Duration source |
|-------|------|-----------------|
| 0 | Re-flash sensors (FPGA + camera config) | — |
| 1 | Calibration scan (laser on, collect samples) | `calibration_scan_duration_sec` + `calibration_scan_delay_sec` |
| 2 | Compute (2, 8) C_max / I_max from phase-1 samples | — |
| 3 | Write calibration to console EEPROM | — |
| 4 | Validation scan (with the fresh calibration applied) | same duration as phase 1 |
| 5 | Build per-camera result rows from phase-4 samples; emit CSV + JSON manifest; PASS/FAIL gate against `CalibrationThresholds` | — |

Two issue requirements reshape phases 1 and 5:

1. **Phase 1 extended to 15 s and "all samples averaged".** Today `_run_subscan_capture` (`CalibrationWorkflow.py:736`) only keeps laser-on samples whose `absolute_frame_id` falls inside `[skip_leading_frames, skip_leading_frames + frame_window_count)`. With `calibration_scan_duration_sec=5` and `calibration_scan_delay_sec=1`, that window is 40 frames in, 200 frames wide — i.e. only seconds 1-6 of the scan contribute to the average, even if the operator pushed the duration up. The issue wants every laser-on corrected sample from the 15 s scan averaged for the C_max / I_max computation, not just a windowed subset. Dark samples (from the laser-pulse-skip schedule) continue to flow through the separate `on_dark_frame_fn` channel and are unaffected.
2. **A new Test button.** Same 15 s scan, no calibration write, no validation scan. Pops up a non-modal results window with a copy-paste-able per-camera table (light mean, dark mean, contrast, thresholds, PASS/FAIL).

The existing `_build_result_rows_from_samples` helper (`CalibrationWorkflow.py:301`) already aggregates per-camera mean / contrast / BFI / BVI plus PASS/FAIL against the same `CalibrationThresholds` the operator already configures via `ft_*_per_camera` in `config/app_config.json` — so the Test scan can reuse it directly. The dark-mean per-camera comes from the same `dark_samples` channel the #122 ambient-light gate consumes, and BFI/BVI are *not* required by the Test results table per the issue body. (The pipeline computes them anyway as a side effect of using `_build_result_rows_from_samples`; only the BFI/BVI test verdicts are degenerate when the scan target is a static calibration phantom rather than a perfusion sample. The Test results table simply hides the BFI/BVI columns — see "Test results table" below.)

`Run Calibration` lives in `SettingsModal.qml`; that's where the new `Test` button goes too. The modal layout already has horizontal real-estate for a second `ActionButton` next to the existing one (the `RowLayout` at `SettingsModal.qml:777`).

---

## Requirements

| # | Requirement |
|---|-------------|
| R1 | Replace the single **Run Calibration** button in `SettingsModal.qml` with two side-by-side `ActionButton`s: **Calibrate** and **Test**. Both honor the existing `calibrationTargetCombo` (Both / Left / Right). |
| R2 | The **Calibrate** flow runs the same five phases as today, with two modifications: (a) the calibration scan (phase 1) lasts 15 s by default (configurable via `calibration_scan_duration_sec`), and (b) *all* laser-on corrected samples from phase 1 — not just the rolling-window subset — are averaged in `_compute_calibration_from_samples`. Phase 4 (validation scan) keeps its current windowed averaging behavior. |
| R3 | The **Test** flow runs only the phase 1 scan (15 s by default), against the same camera mask the Calibrate button would use, with the same `CalibrationThresholds` loaded from `app_config.json`. It does NOT write calibration to the console EEPROM, does NOT run a validation scan, and does NOT touch `CalibrationResult.calibration`. |
| R4 | When the Test scan completes, the app opens a separate top-level window (Qt `Window`, not a `Popup` / not the existing in-canvas `Item` modal pattern) titled "Test Results". The window is non-modal — the main window stays interactive while it's open, and multiple Test scans replace the table contents of the existing window (single instance, reused). |
| R5 | The Test Results window shows a table with one row per *active* camera (`side`, `cam 1..8`), with columns: **Side**, **Cam**, **Light Mean**, **Min Mean**, **Mean PF**, **Dark Mean**, **Max Dark**, **Dark PF**, **Contrast**, **Min Contrast**, **Contrast PF**, **Overall**. (BFI/BVI columns are deliberately not shown — see "Test results table" below for why.) PF cells render the literal string `PASS` or `FAIL`; "Overall" is `PASS` only if Mean, Contrast, *and* Dark all pass for that row. |
| R6 | The Test Results window has a **Copy** button that copies the entire table to the system clipboard as tab-separated text including the header row, ready to paste into Excel / Google Sheets / a Slack DM. |
| R7 | The Test Results window has a **Close** button (plus the standard window-close X). Closing it does not cancel an in-progress Test scan; the scan completes and the window simply doesn't auto-reopen. Conversely, starting a new Test scan while the window is open updates the table in place (clear → "running" placeholder → result table). |
| R8 | While a Test scan is running, the Test button is disabled, the Calibrate button is disabled, and the calibration target combo is disabled — same lock-out behavior as today's Calibration flow. The aggregate `_anyInProgress` state machine (`main.qml`) gains a new contributor so the close-while-busy guard fires on a mid-flight Test scan. |
| R9 | If a Test scan fails to start (no sensors, console disconnected, another scan running), the app emits a `captureLog` entry just like today's calibration failures, and the Test Results window does not open. |
| R10 | `app_config.json`'s default `calibration_scan_duration_sec` changes from `5` to `15`. `calibration_scan_delay_sec` stays at `1`. |
| R11 | The connector exposes the new state via existing-shape pyqtProperties: `testScanRunning`, `testScanStatus`, `testScanRows` (list of dicts for the QML model), `testScanFailureReason`. The signal `testScanStateChanged` notifies on transitions between idle / running / done / aborted / failed. |
| R12 | The SDK gains either (a) a new `start_test_scan` method on `CalibrationWorkflow` that runs phase 1 only and emits a `TestScanResult` containing `rows` + scan paths + the same `started_timestamp`, or (b) a `mode: "calibrate" \| "test"` parameter on `start_calibration` plus a discriminator on `CalibrationResult`. (Recommended: (a) — see "Architecture" for rationale.) The Test path writes the same per-camera CSV + JSON manifest the Calibrate path writes, distinguished by a `test-{ts}.csv` / `test-{ts}.json` filename prefix and a `mode: "test"` field in the JSON. |
| R13 | The Test flow does not invoke the laser-power-from-config step gated by `set_laser_power_from_config` (issue #108 path) any differently than the Calibrate flow does — it applies the same laser params before kicking the SDK, for the same cold-start reason. |
| R14 | The Test flow honors the same `max_calibration_time_sec` watchdog as Calibrate. |

---

## Architecture

### `config/app_config.json`

Change the default duration:

```json
"calibration_scan_duration_sec": 15,
```

No other config changes. The Test button reuses every existing `ft_*_per_camera` threshold and `calibration_*_sec` key.

### SDK — `openmotion-sdk/omotion/CalibrationWorkflow.py`

Three small surface changes, no behavior change to the existing calibration flow except the "all samples" averaging.

**1. New `CalibrationRequest` field — `average_full_scan: bool`.**

```python
@dataclass
class CalibrationRequest:
    operator_id: str
    output_dir: str
    left_camera_mask: int
    right_camera_mask: int
    thresholds: CalibrationThresholds
    duration_sec: int
    scan_delay_sec: int = CALIBRATION_DEFAULT_SCAN_DELAY_SEC
    max_duration_sec: int = CALIBRATION_DEFAULT_MAX_DURATION_SEC
    trigger_config: Optional[dict] = None
    notes: str = ""
    average_full_scan: bool = False   # NEW — when True, phase 1 averages
                                      # every laser-on corrected sample
                                      # (not just the post-delay window).
```

**2. Phase 1's call to `_run_subscan_capture` widens the window to "everything after `skip_leading_frames`" when `average_full_scan` is `True`.**

In `_worker`, before the phase-1 call:

```python
skip_frames = int(round(request.scan_delay_sec * CAPTURE_HZ))
window_frames = int(round(request.duration_sec * CAPTURE_HZ))
# Phase 1: when average_full_scan is True, widen the averaging window
# to swallow every laser-on corrected sample after skip_frames. The
# trailing-edge guard the original window provided (keep firmware's
# terminal dark frame out of the average) is no longer needed because
# (a) is_dark frames go through on_dark_frame_fn, not on_corrected_batch,
# and (b) any residual laser-ramp-down frames belong in the new "average
# everything" semantic.
phase1_window_frames = (
    10 ** 9 if request.average_full_scan else window_frames
)
```

Pass `phase1_window_frames` as the `frame_window_count` arg to the phase-1 call. Phase 4 keeps using `window_frames` (unchanged). This is a single-line widening — no signature changes to `_run_subscan_capture`. The 10^9 sentinel keeps the existing in-range filter trivially true for every frame.

**3. New `start_test_scan` method on `CalibrationWorkflow`.**

```python
@dataclass
class TestScanResult:
    ok: bool
    passed: bool       # all targeted cameras pass mean+contrast+dark
    canceled: bool
    error: str
    csv_path: str
    json_path: str
    rows: list[CalibrationResultRow]
    test_scan_left_path: str
    test_scan_right_path: str
    started_timestamp: str
    mode: str = "test"   # discriminator; manifest writes this verbatim


class CalibrationWorkflow:
    ...

    def start_test_scan(
        self,
        request: CalibrationRequest,
        *,
        on_log_fn: Optional[Callable[[str], None]] = None,
        on_progress_fn: Optional[Callable[[str], None]] = None,
        on_complete_fn: Optional[Callable[[TestScanResult], None]] = None,
    ) -> bool:
        """Run just the calibration scan (CalibrationWorkflow phase 1)
        as a stand-alone diagnostic. No calibration write, no validation
        scan. Returns False if a calibration or test scan is already in
        flight. ``request.average_full_scan`` is honored the same as in
        a full calibration. ``request.duration_sec`` is the scan length.
        """
```

Internally `start_test_scan` shares the same `_thread` / `_stop_evt` / `_lock` / `_running` guards as `start_calibration` — a single CalibrationWorkflow instance can run either flow but never both at once. The worker function:

1. `_emit_progress("flash_sensors")` → `_flash_sensors()` (same path as calibration).
2. `_emit_progress("test_scan")` → `_reset_firmware_trigger("test (pre-scan)")` → `_run_subscan_capture(... duration_sec=request.duration_sec + request.scan_delay_sec, skip_leading_frames=skip_frames, frame_window_count=phase1_window_frames, ...)`. Same widening as phase 1 (and `average_full_scan` defaults to `True` for test scans — see below).
3. `_emit_progress("evaluate")` → `_build_result_rows_from_samples(samples, dark_samples=test_dark_samples, ...)` using the same masks + thresholds the caller supplied. (The "active camera but zero dark samples → FAIL" defense kicks in here naturally — Test scans run for 15 s, so plenty of firmware-scheduled dark frames will arrive.)
4. Write `test-{ts}.csv` (same column set as `calibration-{ts}.csv` so the Test CSV is grep-able alongside calibration runs) and `test-{ts}.json` (manifest with `"mode": "test"`).
5. Emit `TestScanResult` on `on_complete_fn`.

Failure modes that abort with an empty rows list (scan refused to start, watchdog tripped, canceled mid-scan) still emit a `TestScanResult` with `ok=False` so the connector can route the event to QML without an extra error channel — same convention as `CalibrationResult`.

Defaulting `average_full_scan` for test scans: `start_test_scan` forces `request.average_full_scan = True` before kicking the worker. The Test results have to reflect "the whole 15 s of laser-on samples averaged together" — same data the Calibrate button uses to compute its C_max / I_max — otherwise the Test wouldn't be a faithful preview of the calibration math.

**4. `MotionInterface` facade.**

Add a passthrough in `openmotion-sdk/omotion/MotionInterface.py` mirroring the existing `start_calibration`:

```python
def start_test_scan(self, request, **kw):
    return self._calibration_workflow.start_test_scan(request, **kw)

def cancel_test_scan(self, *, join_timeout: float = 10.0) -> None:
    return self._calibration_workflow.cancel_calibration(
        join_timeout=join_timeout,
    )
```

`cancel_test_scan` delegates to `cancel_calibration` because both flows share the same worker thread / stop event — one cancel path covers both.

### `motion_connector.py`

A near-copy of `runCalibration`'s shape, with the Test routing.

**New state.** Add alongside `_calibration_status` (motion_connector.py:434):

```python
self._test_scan_status = ""              # "", "running", "done", "aborted", "failed"
self._test_scan_failure_reason = ""
self._test_scan_rows: list[dict] = []    # list-of-dicts ready for QML ListModel
```

**New signals + properties.** Add alongside `calibrationStateChanged` (motion_connector.py:253):

```python
testScanStateChanged = pyqtSignal()
_testScanCompleteSignal = pyqtSignal(object)   # worker → main marshalling


@pyqtProperty(bool, notify=testScanStateChanged)
def testScanRunning(self) -> bool:
    return self._test_scan_status == "running"

@pyqtProperty(str, notify=testScanStateChanged)
def testScanStatus(self) -> str:
    return self._test_scan_status

@pyqtProperty(str, notify=testScanStateChanged)
def testScanFailureReason(self) -> str:
    return self._test_scan_failure_reason

@pyqtProperty('QVariantList', notify=testScanStateChanged)
def testScanRows(self) -> list:
    return self._test_scan_rows
```

**`connect_signals` addition:**

```python
self._testScanCompleteSignal.connect(self._on_test_scan_complete)
```

**New slot — `runTestScan(target)`.** Structurally identical to `runCalibration` (motion_connector.py:3399), with the following differences:

- Skip if `_test_scan_status == "running"` **OR** `_calibration_status == "running"` (mutual exclusion).
- Build the same `CalibrationThresholds` from the same `_ft_*` fields.
- Build a `CalibrationRequest` with the same masks + thresholds + durations + `max_duration_sec`. The connector does *not* pass `average_full_scan=True`; the SDK's `start_test_scan` forces it on internally (single source of truth).
- `output_dir` reuses the existing `calibrations/` subdirectory — Test CSVs and Calibrate CSVs cohabit (distinguished by the `test-` vs `calibration-` filename prefix that the SDK writes).
- Call `set_laser_power_from_config(self._interface)` the same way `runCalibration` does (#108).
- Call `self._interface.start_test_scan(req, ...)`.

**New main-thread handler — `_on_test_scan_complete(result)`.** Mirrors `_on_calibration_complete` (motion_connector.py:3521):

```python
@pyqtSlot(object)
def _on_test_scan_complete(self, result):
    self._test_scan_failure_reason = ""
    if result.canceled:
        self._test_scan_status = "aborted"
        self.captureLog.emit(f"⚠️ Test scan aborted: {result.error or 'canceled'}")
    elif not result.ok:
        self._test_scan_status = "aborted"
        self.captureLog.emit(f"⚠️ Test scan aborted: {result.error or 'unknown error'}")
    elif result.passed:
        self._test_scan_status = "done"
        self.captureLog.emit(f"✅ Test scan: PASS  (CSV: {result.csv_path})")
    else:
        self._test_scan_status = "failed"
        if self._app_config.get("developerMode", False):
            tests = (("mean", "mean_test"), ("contrast", "contrast_test"),
                     ("ambient", "dark_test"))
            breakdown = "; ".join(
                f"{'L' if r.side == 'left' else 'R'}{r.cam_id + 1}:"
                f"{','.join(n for n, a in tests if getattr(r, a) == 'FAIL')}"
                for r in result.rows
                if any(getattr(r, a) == "FAIL" for _, a in tests)
            )
            if any(r.dark_test == "FAIL" for r in result.rows):
                breakdown = f"too much ambient light — {breakdown}"
            self._test_scan_failure_reason = breakdown
        self.captureLog.emit(f"❌ Test scan: FAIL  (CSV: {result.csv_path})")

    # Build the QML-friendly row dicts (also exposed via testScanRows
    # property so the QML window can bind directly to the model).
    self._test_scan_rows = [
        {
            "side": r.side,
            "cam": r.cam_id + 1,
            "light_mean": r.mean,
            "min_mean": (self._ft_min_mean_per_camera[r.cam_id]
                        if self._ft_min_mean_per_camera else None),
            "mean_pf": r.mean_test,
            "dark_mean": r.dark,
            "max_dark": (self._ft_max_dark_per_camera[r.cam_id]
                        if self._ft_max_dark_per_camera else None),
            "dark_pf": r.dark_test,
            "contrast": r.avg_contrast,
            "min_contrast": (self._ft_min_contrast_per_camera[r.cam_id]
                            if self._ft_min_contrast_per_camera else None),
            "contrast_pf": r.contrast_test,
            "overall": (
                "PASS" if r.mean_test == "PASS"
                and r.contrast_test == "PASS"
                and r.dark_test != "FAIL"
                else "FAIL"
            ),
        }
        for r in result.rows
    ]
    self.testScanStateChanged.emit()
```

Note that the "overall" verdict per row deliberately uses `dark_test != "FAIL"` (so `"NA"` is permissive) and excludes BFI/BVI tests entirely — those don't make sense as an acceptance gate for a static phantom and aren't shown to the operator either. This matches what's already in `evaluate_passed` for the missing-threshold convention.

### QML — `components/SettingsModal.qml`

Replace the existing single-button row (lines 777-799) with a two-button row. The combo and the status TextArea stay where they are.

```qml
SectionCard {
    title: "Calibration"

    RowLayout {
        Layout.fillWidth: true
        spacing: 12

        ActionButton {
            id: runCalibrationButton
            text: "Calibrate"
            Layout.preferredWidth: 110
            Layout.preferredHeight: 40
            enabled: MOTIONInterface.consoleConnected
                  && !MOTIONInterface.calibrationRunning
                  && !MOTIONInterface.testScanRunning
            onClicked: MOTIONInterface.runCalibration(
                calibrationTargetCombo.currentText.toLowerCase()
            )
        }

        ActionButton {
            id: runTestButton
            text: "Test"
            Layout.preferredWidth: 110
            Layout.preferredHeight: 40
            enabled: MOTIONInterface.consoleConnected
                  && !MOTIONInterface.calibrationRunning
                  && !MOTIONInterface.testScanRunning
            onClicked: MOTIONInterface.runTestScan(
                calibrationTargetCombo.currentText.toLowerCase()
            )
        }

        StyledCombo {
            id: calibrationTargetCombo
            Layout.preferredWidth: 110
            model: ["Both", "Left", "Right"]
            currentIndex: 0
            enabled: !MOTIONInterface.calibrationRunning
                  && !MOTIONInterface.testScanRunning
        }

        // …existing indicator light + status TextArea unchanged…
    }

    // …existing calibTimer + Connections unchanged…

    // New: hook for opening the Test Results window when a test scan completes.
    Connections {
        target: MOTIONInterface
        function onTestScanStateChanged() {
            if (MOTIONInterface.testScanStatus === "running"
                || MOTIONInterface.testScanStatus === "done"
                || MOTIONInterface.testScanStatus === "failed"
                || MOTIONInterface.testScanStatus === "aborted") {
                testResultsWindow.show()
                testResultsWindow.raise()
                testResultsWindow.requestActivate()
            }
        }
    }
}
```

The "indicator light" + status TextArea show **calibration** status today and will continue to. Test scan status (and its in-progress "(Xs / Ys)" ticker) lives inside the Test Results window itself — see below.

### New QML — `components/TestResultsWindow.qml`

A separate Qt top-level window. Single instance, instantiated once at the bottom of `main.qml` (or inside `SettingsModal.qml` — see below for the rationale).

Skeleton:

```qml
import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import QtQuick.Window 6.0
import OpenMotion 1.0

Window {
    id: testWin
    title: "Test Results"
    width: 920
    height: 480
    minimumWidth: 720
    minimumHeight: 360
    flags: Qt.Window     // top-level, non-modal, resizable, in OS task list
    modality: Qt.NonModal
    color: theme.bgBase

    AppTheme { id: theme }

    // Convenience: bind directly to the connector so reruns refresh the model
    readonly property var rows: MOTIONInterface.testScanRows
    readonly property string status: MOTIONInterface.testScanStatus
    readonly property string failureReason: MOTIONInterface.testScanFailureReason
    readonly property bool running: MOTIONInterface.testScanRunning

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        // Header strip: live status + Copy + Close
        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            Text {
                text: {
                    if (testWin.running) return "Running…"
                    switch (testWin.status) {
                    case "done":    return "PASS"
                    case "failed":
                        return testWin.failureReason
                            ? "FAIL — " + testWin.failureReason
                            : "FAIL"
                    case "aborted": return "Aborted"
                    default:        return ""
                    }
                }
                color: {
                    switch (testWin.status) {
                    case "done":    return "#4CAF50"
                    case "failed":  return "#F44336"
                    case "aborted": return "#FF9800"
                    case "running": return "#2196F3"
                    default:        return theme.textPrimary
                    }
                }
                font.bold: true
                font.pixelSize: 16
            }

            Item { Layout.fillWidth: true }

            Button {
                text: "Copy"
                enabled: testWin.rows && testWin.rows.length > 0
                onClicked: testWin._copyToClipboard()
            }
            Button {
                text: "Close"
                onClicked: testWin.close()
            }
        }

        // Table (header row + data rows)
        // Implementation: a Column of headers + a Repeater binding to
        // testWin.rows. Each row is a RowLayout of small monospaced Text
        // cells; PF cells colored green/red on the literal "PASS" / "FAIL"
        // for at-a-glance reading. The table reads as plain text — no
        // ListView virtualization needed for 16-row max.
        // Header columns (R5): Side, Cam, Light Mean, Min Mean, Mean PF,
        //                       Dark Mean, Max Dark, Dark PF,
        //                       Contrast, Min Contrast, Contrast PF, Overall.
    }

    // Build TSV (header + rows) and push to clipboard. Uses Qt's
    // QGuiApplication clipboard via the existing connector helper.
    function _copyToClipboard() {
        var lines = []
        lines.push([
            "Side", "Cam", "LightMean", "MinMean", "MeanPF",
            "DarkMean", "MaxDark", "DarkPF",
            "Contrast", "MinContrast", "ContrastPF", "Overall",
        ].join("\t"))
        for (var i = 0; i < testWin.rows.length; i++) {
            var r = testWin.rows[i]
            lines.push([
                r.side, r.cam,
                r.light_mean.toFixed(2),
                r.min_mean !== null ? r.min_mean.toFixed(2) : "",
                r.mean_pf,
                isNaN(r.dark_mean) ? "" : r.dark_mean.toFixed(2),
                r.max_dark !== null ? r.max_dark.toFixed(2) : "",
                r.dark_pf,
                r.contrast.toFixed(5),
                r.min_contrast !== null ? r.min_contrast.toFixed(4) : "",
                r.contrast_pf,
                r.overall,
            ].join("\t"))
        }
        MOTIONInterface.copyToClipboard(lines.join("\n"))
    }
}
```

The component is instantiated **once**, eagerly, at the bottom of `main.qml`:

```qml
TestResultsWindow {
    id: testResultsWindow
}
```

(Instantiating it inside `SettingsModal.qml` would mean the window's lifetime is bound to whether the Settings modal has been opened. Putting it at the top of `main.qml` lets the connector's signal-driven `show()` call work whether or not Settings has ever been opened during the session.)

### `MOTIONInterface.copyToClipboard(text)` helper

Add a tiny `@pyqtSlot(str)` to `motion_connector.py`:

```python
@pyqtSlot(str)
def copyToClipboard(self, text: str) -> None:
    """Push a string to the system clipboard via Qt — used by the Test
    Results window's Copy button. Centralised here so QML doesn't need
    a direct dependency on PyQt6.QtGui."""
    from PyQt6.QtGui import QGuiApplication
    QGuiApplication.clipboard().setText(text)
```

### `main.qml` busy-state hook

Extend the existing aggregate (lines 22-26) and the "what's in progress" label (lines 37-45) to include the test scan:

```qml
readonly property bool _anyInProgress:
    bloodFlowPage.scanning ||
    bloodFlowPage.configuring ||
    bloodFlowPage.checkRunning ||
    MOTIONInterface.calibrationRunning ||
    MOTIONInterface.testScanRunning

function _inProgressLabel() {
    var m = bloodFlowPage.modalManager.current
    if (m && m.dismissable === false && m.label) return m.label
    if (MOTIONInterface.calibrationRunning) return "Calibration"
    if (MOTIONInterface.testScanRunning)    return "Test scan"
    if (bloodFlowPage.scanning)             return "Scan"
    if (bloodFlowPage.configuring)          return "Camera configuration"
    if (bloodFlowPage.checkRunning)         return "Contact-quality check"
    return ""
}
```

### Test results table — column choice

The issue body says "light mean, dark mean, and contrast for all of the relevant cameras as well as the thresholds and the pass/fail status." We deliberately *omit* BFI and BVI columns:

- **BFI / BVI are not in the issue's column list.**
- **They don't make sense as a Test acceptance check for the calibration target.** The target is a static phantom; BFI ≈ 0 and BVI is whatever the static-phantom thresholds happen to land on. The Test button is a per-camera "are mean / contrast / dark in spec" diagnostic, not a perfusion check.
- **The existing `_build_result_rows_from_samples` still computes them** because the function shape is shared with the Calibrate flow. The columns are simply hidden in the table.

The full CSV / JSON written to disk *does* include BFI / BVI for traceability — the on-disk artifact is a superset of the on-screen table.

---

## Data Flow

```
SettingsModal.qml → "Test" button click
  └─ MOTIONInterface.runTestScan(target)

motion_connector.py.runTestScan
  ├─ guard: not consoleConnected? captureLog warn, return
  ├─ guard: testScanRunning or calibrationRunning? return silently
  ├─ resolve target → left_mask + right_mask (same logic as runCalibration)
  ├─ build CalibrationThresholds from self._ft_* (same as runCalibration)
  ├─ build CalibrationRequest (duration_sec=15, scan_delay_sec=1, …)
  │     ※ average_full_scan flag is not set here — SDK forces it inside
  │       start_test_scan, so the connector code path stays simple.
  ├─ self._test_scan_status = "running"; testScanStateChanged.emit()
  ├─ set_laser_power_from_config(self._interface)  (#108)
  └─ self._interface.start_test_scan(
        req,
        on_log_fn=lambda m: self.captureLog.emit(m),
        on_complete_fn=self._testScanCompleteSignal.emit,
     )

CalibrationWorkflow.start_test_scan worker (SDK)
  ├─ request.average_full_scan = True  (forced)
  ├─ phase 0: _flash_sensors()
  ├─ phase 1: _reset_firmware_trigger("test (pre-scan)")
  ├─        _run_subscan_capture(
  │             … frame_window_count=10**9, …)   ← all laser-on samples
  ├─ phase 5: rows = _build_result_rows_from_samples(
  │             test_samples, dark_samples=test_dark_samples,
  │             left_camera_mask=…, right_camera_mask=…,
  │             thresholds=request.thresholds, …)
  ├─ write test-{ts}.csv via write_result_csv(...)
  ├─ write test-{ts}.json via write_result_json(... mode="test" ...)
  └─ on_complete_fn(TestScanResult(...))

motion_connector.py._on_test_scan_complete (main thread, queued)
  ├─ derive self._test_scan_status from result
  ├─ rebuild self._test_scan_rows as list-of-dicts for QML
  ├─ in dev mode, fill self._test_scan_failure_reason
  └─ self.testScanStateChanged.emit()

SettingsModal.qml.Connections.onTestScanStateChanged
  └─ testResultsWindow.show() + raise() + requestActivate()

TestResultsWindow.qml
  ├─ rows binding (MOTIONInterface.testScanRows) repopulates the table
  ├─ status string drives the header strip color + text
  ├─ Copy button → MOTIONInterface.copyToClipboard(tsv)
  └─ Close button → testWin.close()  (does NOT cancel a running scan)
```

---

## Edge Cases

- **Test results window opened, scan triggered, scan re-triggered before close.** Second `runTestScan` is rejected by the `_test_scan_status == "running"` guard. The window's bindings update once on first completion; rerun only refreshes when the first scan finishes (or fails/aborts).
- **Calibrate triggered while Test scan running (or vice versa).** Both `runCalibration` and `runTestScan` guard on both `_calibration_status == "running"` and `_test_scan_status == "running"`. The QML buttons disable on the OR of `calibrationRunning || testScanRunning`. The SDK's single `_running` flag on `CalibrationWorkflow` provides the final safety belt.
- **App closed mid-Test-scan via the framelees window's close button.** Caught by `_anyInProgress` two-click guard in `main.qml` — same UX as a mid-calibration close attempt. The toast message reads "Closing… ⚠ Test scan in progress."
- **Test scan watchdog trips.** Same `max_calibration_time_sec` watchdog — `TestScanResult.canceled = True, error = "calibration exceeded max_duration_sec=…"`. Connector marks status `"aborted"`. The Test Results window shows "Aborted" in orange.
- **Active camera produces no dark samples.** Reuses the `_build_result_rows_from_samples` defense: `dark = NaN, dark_test = "FAIL"`. Row's "Overall" column reads FAIL.
- **No `ft_max_dark_per_camera` configured.** `max_dark_per_camera=None` → every row's `dark_test = "NA"`. The Test Results window's Overall column still computes correctly (`dark_test != "FAIL"`).
- **Single-side calibration (Left or Right) chosen in the combo.** The masks passed to the SDK already zero out the unused side. The Test Results table simply has 8 rows instead of 16, matching the active mask.
- **Reducedmode camera mask.** Issue #117's behavior — the Test/Calibrate path uses `0xFF` per active side regardless of `reducedModeLeftMask` / `reducedModeRightMask`. Documented in the existing `runCalibration` docstring; the new `runTestScan` slot inherits the same convention and the same comment.
- **15 s scan + windowing.** `phase1_window_frames = 10**9` is safely larger than 40 fps × 15 s = 600 frames. No overflow risk.

---

## Testing

### SDK — `openmotion-sdk/tests/test_calibration_workflow_compute.py`

Add tests against the pure compute helpers and the new request flag.

1. `test_request_average_full_scan_defaults_false` — constructing a `CalibrationRequest` without `average_full_scan` keeps it `False`. Existing callers continue to behave as before.
2. `test_request_average_full_scan_accepts_true` — round-trip the new field.
3. `test_test_scan_result_default_mode_is_test` — `TestScanResult().mode == "test"`.

### SDK — new HIL fixture `openmotion-sdk/tests/test_calibration_test_scan_hil.py` (or extension of `test_calibration_workflow.py`)

Hardware-in-the-loop, like the existing calibration HIL tests. Two cases:

1. `test_start_test_scan_completes_with_rows` — start a 5 s test scan against the active hardware mask, assert `ok=True, canceled=False`, every active-camera row has non-NaN `mean`, `avg_contrast`, `dark`.
2. `test_start_test_scan_does_not_touch_console_calibration` — read the console's cached calibration before and after; assert byte-for-byte equality (the Test scan must not write).

### Bloodflow-app — `tests/test_test_scan_flow.py`

Two unit-level (no-hardware) checks against the connector:

1. `test_run_test_scan_refused_when_calibration_running` — set `_calibration_status = "running"`, call `runTestScan`, assert it returns without changing state and emits a captureLog warning.
2. `test_run_test_scan_refused_when_console_disconnected` — `_consoleConnected = False`, assert captureLog warning + state unchanged.
3. `test_on_test_scan_complete_populates_rows` — synthesize a `TestScanResult` with three `CalibrationResultRow`s, invoke `_on_test_scan_complete`, assert `testScanRows` has the right shape (list of dicts, `overall` column derived from the per-row tests).
4. `test_on_test_scan_complete_dev_mode_failure_reason` — `developerMode=True`, dark_test=FAIL on row[0], assert `testScanFailureReason` starts with `"too much ambient light"`.

### Bloodflow-app — `tests/test_test_results_ui.py`

A pytest-qt UI test (matches the existing `test_calibration_ui` style):

1. Open Settings modal, assert two ActionButtons present: "Calibrate" and "Test".
2. Click "Test" with the connector mocked to immediately complete a `TestScanResult` containing two rows. Assert `testResultsWindow.visible == True`.
3. Assert the table has the expected 12 columns and the right per-row PASS/FAIL strings.
4. Click the Copy button (with the clipboard mocked at the `MOTIONInterface.copyToClipboard` boundary) — assert the TSV string was emitted including the header row and matches the visible table contents.
5. Click Close; assert `testResultsWindow.visible == False`. Trigger a second mock test scan; assert the window auto-reopens.

### Manual verification on hardware

1. Launch the app, open Settings → Calibration. Confirm "Calibrate" and "Test" sit side by side, both enabled, combo defaulting to "Both".
2. Click "Test" with a known-good calibration phantom in both bays. Expect: a Test Results window opens within ~5 s of starting, scrolls through a "Running…" status, then shows 16 rows with mean, dark, contrast, and PASS in the Overall column.
3. Click the Copy button. Paste into a text editor. Confirm 17 lines (1 header + 16 data), tab-separated, with the same PASS/FAIL strings.
4. Click "Calibrate" with the same phantom. Expect: same five-phase flow as today but the calibration scan is visibly longer (~15 s + 1 s delay). Inspect the resulting `calibration-{ts}.json`; confirm `request.duration_sec == 15` and that the phase 1 sample count is roughly `15 s × 40 fps × 8 cams = 4800` per side.
5. Confirm `test-{ts}.csv` and `calibration-{ts}.csv` co-exist in the same `calibrations/` directory after both flows run.
6. While a Test scan is running, attempt to click Calibrate. Expect: button is disabled, no action. Attempt to close the app. Expect: close-while-busy toast reading "Test scan in progress."
7. Lower `ft_max_dark_per_camera` to a tight value, restart, run Test, hold a flashlight to a sensor. Expect: row PF FAIL on Dark, Overall FAIL, header strip red, dev-mode (if enabled) message prefixed "too much ambient light".

---

## YAGNI / Out of Scope

- **Per-Test threshold overrides in the UI.** Operators edit `app_config.json` if they want different thresholds for Test vs Calibrate; no GUI editor.
- **Tying Test Results window to a specific scan run.** The window shows the *latest* scan's data only. No history within the session, no "previous scan" / "next scan" navigation. The on-disk CSV/JSON cover the audit trail.
- **Saving the Test Results window position / size across runs.** Defaults each time; not bothering with persistence.
- **Showing BFI / BVI columns in the Test Results window.** Out of scope per the issue body. They're written to disk for traceability but not displayed.
- **Running Test on a customized mask.** The Test flow honors the same Left / Right / Both combo as Calibrate, and within a side it uses `0xFF` (every camera). No per-camera enable in the Test path. (Matches existing Calibrate semantics — issue #117.)
- **Re-using the same window instance across the Calibrate flow.** The Calibrate button continues to surface its status via the existing in-modal indicator + status TextArea. The Test Results window is for Test only. Surfacing per-row Calibrate results in the same window would be a larger feature; the issue body asks for it on Test only.
- **Cancellation button in the Test Results window.** The watchdog + the app-close two-click guard cover the user need; an explicit cancel button isn't requested and would expand the surface area.

---

## Implementation Order

Same shippability gate as #122 — keep each commit installable.

1. **SDK:** add `average_full_scan` to `CalibrationRequest`, thread it into the phase-1 windowing inside `_worker`. Existing tests + a new dataclass-default test pass.
2. **SDK:** add `TestScanResult` dataclass and `start_test_scan` method (re-using `_run_subscan_capture`, `_build_result_rows_from_samples`, `write_result_csv`, `write_result_json`). Extend `write_result_json` to accept a `mode` string. Add unit tests against the new dataclass + a HIL test once hardware is available.
3. **SDK:** add `MotionInterface.start_test_scan` + `cancel_test_scan` passthroughs.
4. **Bloodflow-app:** add the connector state, signals, properties, slot, completion handler, and `copyToClipboard` helper. Smoke-import; unit-test the no-hardware paths.
5. **Bloodflow-app:** add `components/TestResultsWindow.qml`. Wire it into `main.qml`. Wire the new buttons in `SettingsModal.qml`. Update `_anyInProgress` / `_inProgressLabel` in `main.qml`.
6. **Bloodflow-app:** `config/app_config.json` bump `calibration_scan_duration_sec` 5 → 15. Verify JSON syntax.
7. **Bloodflow-app:** add pytest-qt UI test + connector unit tests. Run the existing calibration tests to confirm no regression.
8. Manual bench verification on a calibration phantom.

---

## Open Questions

Each of these is an assumption the spec made without operator/PM confirmation. Sanity-check them before implementation starts.

1. **"The test scan (that first one)" = phase 1 calibration scan.** Confirmed against `CalibrationWorkflow.py:1020-1040` (the first sub-scan in `_worker` is phase 1 = "calibration scan"). The spec treats this as definite. If the user meant something else — e.g. a brand-new free-running diagnostic scan with different trigger config — the architecture changes substantially. Confirm.
2. **"Not just the rolling average numbers" = drop the `frame_window_count` windowing in phase 1.** Today the SDK averages a 40-frame-skip + 200-frame-keep slice (with default config). The spec interprets the issue as "average every laser-on corrected sample after the leading skip-delay." If the user wanted to also drop the `scan_delay_sec` skip, change `skip_leading_frames` to 0 for phase 1. Confirm.
3. **Test results table column set.** The issue lists "light mean, dark mean, contrast, thresholds, pass/fail." The spec renders 12 columns: Side, Cam, LightMean, MinMean, MeanPF, DarkMean, MaxDark, DarkPF, Contrast, MinContrast, ContrastPF, Overall. The "Overall" column is the spec's addition — it's not strictly requested but matches the operator workflow ("which cameras am I in trouble with?"). Confirm Overall is wanted; confirm we should hide BFI/BVI (spec assumes yes).
4. **Test Results window is a Qt `Window` (separate top-level OS window), not a `Popup` / new in-canvas modal.** The issue body says "a small window to pop up (not a modal, a separate window)." The spec reads "separate window" literally as an OS-level top-level window. If the user actually wanted "a non-modal panel inside the main window canvas" (still in-app, but not blocking), use a `Popup` with `modal: false` instead. Confirm.
5. **Closing the Test Results window mid-scan does NOT cancel the scan.** The spec separates window lifecycle from scan lifecycle — the window is a passive viewer. Confirm. (If cancellation on close is wanted, route `onClosing` to `MOTIONInterface.cancelTestScan()` and add the corresponding slot.)
6. **Single window instance, replaced on rerun (vs. one window per scan).** Spec assumes single instance — re-running a Test scan refills the same table. Confirm.
7. **Test scan reuses `calibrations/` output directory with a `test-{ts}.` filename prefix.** The spec assumes operators want all per-unit acceptance artifacts in one folder. Alternative: a sibling `tests/` directory. Confirm.
8. **`calibration_scan_duration_sec` default goes 5 → 15.** Spec changes the default in `app_config.json` so both Calibrate and Test get the longer scan. Confirm — or specify a separate `test_scan_duration_sec` if the Calibrate scan should keep 5 s and only Test go to 15 s. (Issue body strongly implies *both* should be 15 s: "the calibration scan ... should run for 15 seconds.")
9. **Calibrate button label change "Run Calibration" → "Calibrate".** Spec shortens it so it visually matches the new "Test" button width. Confirm — or keep "Run Calibration" / use "Run Test" for both for parity.
10. **Average-full-scan behavior also applied to the Test scan?** Spec forces `average_full_scan=True` for both Calibrate's phase 1 and the Test scan, so Test displays the same averaging the calibration math will use. If the Test should instead reflect the *windowed* (rolling) average, set the request's `average_full_scan=False` from `start_test_scan`. Confirm. (The issue body implies Test = same scan as the calibration scan, which we read as same averaging too.)
11. **Validation scan (phase 4) keeps its current windowed averaging.** Spec leaves phase 4 alone — the issue body only mentions changing phase 1. Confirm.
12. **Copy format = TSV with header row.** Spec assumes Excel/Sheets-compatible TSV. If the user wants Markdown table or CSV (commas) instead, swap the join character + add markdown delimiters. Confirm.
13. **`developerMode` failure breakdown propagates to the Test results.** Spec reuses the same `"too much ambient light — L1:ambient; …"` formatting for Test failures when dev mode is on. Confirm — or keep dev-mode failure breakdowns calibrate-only, in which case strip the breakdown code from `_on_test_scan_complete`.
14. **No `notes` field or per-test operator metadata.** The Test JSON manifest will inherit `request.notes = ""`. If a Test run wants a note field in the popup, add a `TextField` and route it through `request.notes`. Spec defers.
15. **Watchdog uses the same `max_calibration_time_sec` (default 600 s) as Calibrate.** For a 15 s Test scan this is wildly generous, but it's also the right ceiling for a stuck-firmware fall-back. Confirm — or add a separate `max_test_scan_time_sec`. Spec reuses.
