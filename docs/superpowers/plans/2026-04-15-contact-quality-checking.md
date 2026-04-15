# Contact Quality Checking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-camera ambient-light and poor-contact warnings to OpenMOTION, surfaced both via a sidebar "Check" quick-scan button and live during normal scans, in a single accumulating notification modal.

**Architecture:** A new SDK module `omotion/ContactQuality.py` holds threshold constants (background-subtracted) and a stateful `ContactQualityMonitor` that consumes raw per-camera dark/light means. The monitor is wired into `SciencePipeline` (uncorrected-sample callback) and exposed via a new `MOTIONInterface.run_contact_quality_check()` short-scan helper that uses camera mask `0xFF` on both modules. The bloodflow app gains a `ContactQualityModal.qml` singleton, a sidebar "Check" button, and connector slots/signals to drive the modal in both quick-check and live-monitoring modes.

**Tech Stack:** Python 3.12 (SDK), pytest, PyQt6 + QML 6.0 (app).

**Spec:** `docs/superpowers/specs/2026-04-15-contact-quality-checking-design.md`

**Repos:** Tasks marked **[SDK]** are in `openmotion-sdk` (branch `feature/contact-quality`). Tasks marked **[APP]** are in `openmotion-bloodflow-app` (branch `feature/contact-quality`).

## File Structure

**SDK (`openmotion-sdk`):**
- Create: `omotion/ContactQuality.py` — thresholds, dataclass, monitor (single responsibility: warning logic).
- Create: `tests/test_contact_quality.py` — unit tests for the monitor.
- Modify: `omotion/MotionProcessing.py` — add optional `on_contact_quality_warning` callback to `SciencePipeline`; feed monitor with raw `u1` per camera (dark vs. non-dark) inside the existing pipeline loop.
- Modify: `omotion/Interface.py` — add `run_contact_quality_check(duration_s: float = 1.0)` method.
- Modify: `omotion/__init__.py` — re-export `ContactQualityMonitor`, `ContactQualityWarning`, `ContactQualityWarningType`, `ContactQualityResult`.

**App (`openmotion-bloodflow-app`):**
- Create: `components/ContactQualityModal.qml` — three-state modal singleton.
- Modify: `components/SidebarMenu.qml` — add "Check" button at index 2 with enabled binding.
- Modify: `pages/BloodFlow.qml` (or `main.qml`) — instantiate modal, wire connector signals.
- Modify: `motion_connector.py` — add slots, signals, monitor callback hookup, quick-check thread.

---

## [SDK] Task 1: Create ContactQuality module skeleton + unit tests

**Files:**
- Create: `omotion/ContactQuality.py`
- Create: `tests/test_contact_quality.py`

- [ ] **Step 1: Write the failing test file**

Create `tests/test_contact_quality.py`:

```python
"""Unit tests for the contact quality monitor."""
from __future__ import annotations

import pytest

from omotion.ContactQuality import (
    ContactQualityMonitor,
    ContactQualityWarning,
    ContactQualityWarningType,
    DARK_MEAN_THRESHOLD_DN,
    LIGHT_MEAN_THRESHOLD_DN,
    LOW_LIGHT_CONSEC_FRAMES,
)


PEDESTAL = 64.0  # matches MotionProcessing.PEDESTAL_HEIGHT at time of writing


def _bg_dark_above() -> float:
    return PEDESTAL + DARK_MEAN_THRESHOLD_DN + 1.0


def _bg_dark_below() -> float:
    return PEDESTAL + DARK_MEAN_THRESHOLD_DN - 1.0


def _bg_light_above() -> float:
    return PEDESTAL + LIGHT_MEAN_THRESHOLD_DN + 1.0


def _bg_light_below() -> float:
    return PEDESTAL + LIGHT_MEAN_THRESHOLD_DN - 1.0


def test_dark_mean_above_threshold_emits_ambient_warning():
    mon = ContactQualityMonitor(pedestal=PEDESTAL)
    warnings = mon.update_dark(camera_id=0, raw_dark_mean=_bg_dark_above(), frame_index=0)
    assert len(warnings) == 1
    w = warnings[0]
    assert w.camera_id == 0
    assert w.warning_type is ContactQualityWarningType.AMBIENT_LIGHT


def test_dark_mean_below_threshold_emits_nothing():
    mon = ContactQualityMonitor(pedestal=PEDESTAL)
    warnings = mon.update_dark(camera_id=0, raw_dark_mean=_bg_dark_below(), frame_index=0)
    assert warnings == []


def test_ambient_warning_latches_until_clear():
    mon = ContactQualityMonitor(pedestal=PEDESTAL)
    assert mon.update_dark(0, _bg_dark_above(), 0)  # first emission
    # Subsequent dark frames above threshold do NOT re-emit while latched.
    assert mon.update_dark(0, _bg_dark_above(), 1) == []
    assert mon.update_dark(0, _bg_dark_above(), 2) == []


def test_ambient_warning_rearms_after_clear_streak():
    mon = ContactQualityMonitor(pedestal=PEDESTAL)
    mon.update_dark(0, _bg_dark_above(), 0)
    # Clear for LOW_LIGHT_CONSEC_FRAMES dark frames.
    for i in range(LOW_LIGHT_CONSEC_FRAMES):
        assert mon.update_dark(0, _bg_dark_below(), 1 + i) == []
    # Next above-threshold dark frame should re-emit.
    out = mon.update_dark(0, _bg_dark_above(), 100)
    assert len(out) == 1
    assert out[0].warning_type is ContactQualityWarningType.AMBIENT_LIGHT


def test_low_light_streak_emits_after_n_consecutive():
    mon = ContactQualityMonitor(pedestal=PEDESTAL)
    for i in range(LOW_LIGHT_CONSEC_FRAMES - 1):
        assert mon.update_light(0, _bg_light_below(), i) == []
    out = mon.update_light(0, _bg_light_below(), LOW_LIGHT_CONSEC_FRAMES - 1)
    assert len(out) == 1
    assert out[0].warning_type is ContactQualityWarningType.POOR_CONTACT


def test_low_light_streak_resets_on_good_frame():
    mon = ContactQualityMonitor(pedestal=PEDESTAL)
    for i in range(LOW_LIGHT_CONSEC_FRAMES - 1):
        mon.update_light(0, _bg_light_below(), i)
    # Single good frame resets the counter.
    assert mon.update_light(0, _bg_light_above(), LOW_LIGHT_CONSEC_FRAMES - 1) == []
    # Now we need another full streak to fire.
    for i in range(LOW_LIGHT_CONSEC_FRAMES - 1):
        assert mon.update_light(0, _bg_light_below(), 100 + i) == []
    out = mon.update_light(0, _bg_light_below(), 200)
    assert len(out) == 1


def test_per_camera_state_is_independent():
    mon = ContactQualityMonitor(pedestal=PEDESTAL)
    # Camera 0 latches ambient.
    mon.update_dark(0, _bg_dark_above(), 0)
    # Camera 1 starting fresh should still emit on its first above-threshold dark.
    out = mon.update_dark(1, _bg_dark_above(), 0)
    assert len(out) == 1
    assert out[0].camera_id == 1


def test_pedestal_change_shifts_comparison():
    """Threshold constants are background-subtracted; raising the pedestal
    raises the absolute DN at which warnings fire."""
    mon_low = ContactQualityMonitor(pedestal=64.0)
    mon_high = ContactQualityMonitor(pedestal=100.0)
    raw = 64.0 + DARK_MEAN_THRESHOLD_DN + 1.0  # above for pedestal=64
    assert mon_low.update_dark(0, raw, 0)              # fires
    assert mon_high.update_dark(0, raw, 0) == []       # below for pedestal=100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_contact_quality.py -v`
Expected: ImportError / ModuleNotFoundError for `omotion.ContactQuality`.

- [ ] **Step 3: Write the minimal implementation**

Create `omotion/ContactQuality.py`:

```python
"""Per-camera contact-quality assessment.

Two warnings are emitted from raw (uncorrected) histogram means:

* AMBIENT_LIGHT — laser-off ("dark") frame mean exceeds an ambient-light
  threshold, suggesting stray light is leaking into the sensor.
* POOR_CONTACT — laser-on ("light") frame mean stays below a contact threshold
  for ``LOW_LIGHT_CONSEC_FRAMES`` consecutive frames, suggesting the laser or
  sensor is not coupled to the patient.

Thresholds are stored as **background-subtracted** values and compared against
``raw_mean - pedestal`` at evaluation time. This way, future changes to
``PEDESTAL_HEIGHT`` do not require re-tuning the threshold constants.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Tuple

# Background-subtracted thresholds in raw DN. Add the current pedestal at
# comparison time to obtain the absolute DN threshold.
DARK_MEAN_THRESHOLD_DN: float = 10.0   # ambient-light warning cutoff
LIGHT_MEAN_THRESHOLD_DN: float = 30.0  # poor-contact warning cutoff
LOW_LIGHT_CONSEC_FRAMES: int = 6       # consecutive low-light frames to fire


class ContactQualityWarningType(str, Enum):
    AMBIENT_LIGHT = "ambient_light"
    POOR_CONTACT = "poor_contact"


@dataclass(frozen=True)
class ContactQualityWarning:
    camera_id: int
    warning_type: ContactQualityWarningType
    value: float        # the raw DN that triggered the warning
    frame_index: int


@dataclass
class ContactQualityResult:
    """Aggregated result returned by ``run_contact_quality_check``."""
    ok: bool
    warnings: List[ContactQualityWarning] = field(default_factory=list)


@dataclass
class _CameraState:
    ambient_latched: bool = False
    ambient_clear_streak: int = 0
    low_light_streak: int = 0
    contact_latched: bool = False
    contact_clear_streak: int = 0


class ContactQualityMonitor:
    """Stateful per-camera contact-quality monitor."""

    def __init__(self, pedestal: float) -> None:
        self._pedestal = float(pedestal)
        self._state: Dict[int, _CameraState] = {}

    def reset(self, camera_id: int | None = None) -> None:
        if camera_id is None:
            self._state.clear()
        else:
            self._state.pop(camera_id, None)

    def _state_for(self, camera_id: int) -> _CameraState:
        s = self._state.get(camera_id)
        if s is None:
            s = _CameraState()
            self._state[camera_id] = s
        return s

    def update_dark(
        self, camera_id: int, raw_dark_mean: float, frame_index: int
    ) -> List[ContactQualityWarning]:
        s = self._state_for(camera_id)
        out: List[ContactQualityWarning] = []
        threshold_abs = self._pedestal + DARK_MEAN_THRESHOLD_DN
        above = raw_dark_mean > threshold_abs
        if above:
            s.ambient_clear_streak = 0
            if not s.ambient_latched:
                s.ambient_latched = True
                out.append(ContactQualityWarning(
                    camera_id=camera_id,
                    warning_type=ContactQualityWarningType.AMBIENT_LIGHT,
                    value=float(raw_dark_mean),
                    frame_index=int(frame_index),
                ))
        else:
            if s.ambient_latched:
                s.ambient_clear_streak += 1
                if s.ambient_clear_streak >= LOW_LIGHT_CONSEC_FRAMES:
                    s.ambient_latched = False
                    s.ambient_clear_streak = 0
        return out

    def update_light(
        self, camera_id: int, raw_light_mean: float, frame_index: int
    ) -> List[ContactQualityWarning]:
        s = self._state_for(camera_id)
        out: List[ContactQualityWarning] = []
        threshold_abs = self._pedestal + LIGHT_MEAN_THRESHOLD_DN
        below = raw_light_mean < threshold_abs
        if below:
            s.low_light_streak += 1
            s.contact_clear_streak = 0
            if (
                not s.contact_latched
                and s.low_light_streak >= LOW_LIGHT_CONSEC_FRAMES
            ):
                s.contact_latched = True
                out.append(ContactQualityWarning(
                    camera_id=camera_id,
                    warning_type=ContactQualityWarningType.POOR_CONTACT,
                    value=float(raw_light_mean),
                    frame_index=int(frame_index),
                ))
        else:
            s.low_light_streak = 0
            if s.contact_latched:
                s.contact_clear_streak += 1
                if s.contact_clear_streak >= LOW_LIGHT_CONSEC_FRAMES:
                    s.contact_latched = False
                    s.contact_clear_streak = 0
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_contact_quality.py -v`
Expected: 8 passed.

- [ ] **Step 5: Re-export from package**

Edit `omotion/__init__.py` — add to the existing exports:

```python
from omotion.ContactQuality import (
    ContactQualityMonitor,
    ContactQualityWarning,
    ContactQualityWarningType,
    ContactQualityResult,
    DARK_MEAN_THRESHOLD_DN,
    LIGHT_MEAN_THRESHOLD_DN,
    LOW_LIGHT_CONSEC_FRAMES,
)
```

If `__init__.py` defines `__all__`, append the new names there too.

- [ ] **Step 6: Run tests again to confirm package import still works**

Run: `pytest tests/test_contact_quality.py -v && python -c "from omotion import ContactQualityMonitor; print('ok')"`
Expected: 8 passed; `ok`.

- [ ] **Step 7: Commit**

```bash
git add omotion/ContactQuality.py omotion/__init__.py tests/test_contact_quality.py
git commit -m "feat: add per-camera ContactQualityMonitor with thresholds and tests"
```

---

## [SDK] Task 2: Wire monitor into SciencePipeline

The pipeline already separates dark from non-dark frames in its main loop. We add an optional `on_contact_quality_warning` callback to `SciencePipeline.__init__`, instantiate a monitor when it's set, and feed it raw `u1` values at the existing dark/non-dark branch points.

**Files:**
- Modify: `omotion/MotionProcessing.py` — `SciencePipeline.__init__` and the per-frame loop (around line 1013 init and ~1180/1235 emission points).

- [ ] **Step 1: Add an integration test using a fake pipeline driver**

Append to `tests/test_contact_quality.py`:

```python
def test_monitor_emits_distinct_warnings_per_camera():
    """Smoke-level integration: feed a sequence representing two cameras
    and verify aggregated warnings."""
    mon = ContactQualityMonitor(pedestal=PEDESTAL)
    # Camera 0: ambient on first dark frame.
    a = mon.update_dark(0, _bg_dark_above(), 0)
    # Camera 1: contact warning after streak.
    out_b = []
    for i in range(LOW_LIGHT_CONSEC_FRAMES):
        out_b.extend(mon.update_light(1, _bg_light_below(), i))
    cams = sorted({w.camera_id for w in [*a, *out_b]})
    types = sorted({w.warning_type.value for w in [*a, *out_b]})
    assert cams == [0, 1]
    assert types == ["ambient_light", "poor_contact"]
```

Run: `pytest tests/test_contact_quality.py -v`
Expected: 9 passed.

- [ ] **Step 2: Add the callback parameter to `SciencePipeline.__init__`**

Locate `class SciencePipeline:` (around line 955 in `omotion/MotionProcessing.py`). Add a new keyword argument and store it. Inside `__init__`, after the existing `self._on_uncorrected_fn = on_uncorrected_fn` line, add:

```python
        # Contact-quality monitor wiring (optional).
        from omotion.ContactQuality import ContactQualityMonitor  # local import to avoid cycles
        self._on_contact_quality_warning = on_contact_quality_warning
        self._cq_monitor: ContactQualityMonitor | None = (
            ContactQualityMonitor(pedestal=PEDESTAL_HEIGHT)
            if on_contact_quality_warning is not None
            else None
        )
```

Add `on_contact_quality_warning: Callable[[ContactQualityWarning], None] | None = None,` to the `__init__` signature (place it after `on_uncorrected_fn`). Add the matching parameter to the `run_pipeline` factory function near the bottom of the file (the one with `on_uncorrected_fn` around line 1506) and forward it into the `SciencePipeline(...)` call.

- [ ] **Step 3: Feed the monitor at the dark and non-dark emission sites**

In the per-frame loop, the dark-frame branch produces a `dark_uncorrected` Sample (~line 1182) and the non-dark branch produces an `uncorrected` Sample (~line 1220). At each site, after the Sample is constructed but before/around the `_on_uncorrected_fn` call, capture the **raw** mean (the pre-pedestal value) and feed the monitor.

`compute_realtime_metrics` currently subtracts the pedestal internally. To avoid changing that signature, recompute `raw_mean` locally from the histogram at each emission site (cheap: one numpy dot product already cached as `row_sum > 0` check). Add a helper near the top of `SciencePipeline._process_one_sample` (or wherever the per-frame work happens):

```python
def _raw_mean_from_hist(hist, row_sum: int) -> float:
    if row_sum > 0:
        return float(np.dot(hist, HISTO_BINS) / row_sum)
    return 0.0
```

At the dark-frame emission site, after computing `dark_uncorrected`:

```python
if self._cq_monitor is not None:
    raw = _raw_mean_from_hist(hist, row_sum)
    for w in self._cq_monitor.update_dark(int(cam_id), raw, int(absolute_frame_id)):
        try:
            self._on_contact_quality_warning(w)
        except Exception:
            logger.exception("contact-quality callback failed")
```

At the non-dark emission site, after computing `uncorrected`:

```python
if self._cq_monitor is not None:
    raw = _raw_mean_from_hist(hist, row_sum)
    for w in self._cq_monitor.update_light(int(cam_id), raw, int(absolute_frame_id)):
        try:
            self._on_contact_quality_warning(w)
        except Exception:
            logger.exception("contact-quality callback failed")
```

(Use the actual variable names for `hist`, `row_sum`, `cam_id`, `absolute_frame_id` already in scope at each site — read the surrounding code to confirm.)

- [ ] **Step 4: Verify the SDK still imports and unit tests still pass**

Run: `pytest tests/test_contact_quality.py -v && python -c "from omotion.MotionProcessing import SciencePipeline; print('ok')"`
Expected: 9 passed; `ok`.

- [ ] **Step 5: Commit**

```bash
git add omotion/MotionProcessing.py
git commit -m "feat: wire ContactQualityMonitor into SciencePipeline emissions"
```

---

## [SDK] Task 3: Add `MOTIONInterface.run_contact_quality_check`

A short helper that runs a 1 s scan with camera mask `0xFF` on both modules, collects warnings via the monitor callback, and returns a `ContactQualityResult`.

**Files:**
- Modify: `omotion/Interface.py`

- [ ] **Step 1: Add the method**

In `omotion/Interface.py`, add to the `MOTIONInterface` class (near `start_scan`, around line 343):

```python
    def run_contact_quality_check(
        self,
        duration_s: float = 1.0,
        subject_id: str = "_contact_quality_check",
        data_dir: str | None = None,
    ) -> "ContactQualityResult":
        """Run a brief acquisition and return contact-quality warnings.

        Always uses camera mask 0xFF on both sensor modules. Histograms are
        consumed only by the contact-quality monitor; no CSV files are written
        and no live-data callbacks are fired. Blocks until the scan completes
        or fails.
        """
        import threading
        import tempfile
        from omotion.ContactQuality import ContactQualityResult, ContactQualityWarning
        from omotion.ScanWorkflow import ScanRequest

        warnings: list[ContactQualityWarning] = []
        warnings_lock = threading.Lock()

        def _on_warning(w: ContactQualityWarning) -> None:
            with warnings_lock:
                warnings.append(w)

        # Hand the callback to the scan workflow so it propagates into
        # SciencePipeline construction. The workflow already accepts an
        # on_contact_quality_warning kwarg added in Task 2's run_pipeline
        # change; if start_scan does not yet forward it, extend start_scan
        # to accept and forward `contact_quality_callback`.
        request = ScanRequest(
            subject_id=subject_id,
            duration_sec=max(1, int(round(duration_s))),
            left_camera_mask=0xFF,
            right_camera_mask=0xFF,
            data_dir=data_dir or tempfile.gettempdir(),
            disable_laser=False,
            write_raw_csv=False,
            write_corrected_csv=False,
            write_telemetry_csv=False,
        )

        ok = self.start_scan(
            request, contact_quality_callback=_on_warning
        )
        if not ok:
            return ContactQualityResult(ok=False, warnings=[])

        # Block until the scan worker completes.
        self.scan_workflow._thread.join()  # type: ignore[union-attr]

        with warnings_lock:
            return ContactQualityResult(ok=True, warnings=list(warnings))
```

- [ ] **Step 2: Forward the callback through `ScanWorkflow.start_scan`**

In `omotion/ScanWorkflow.py`, modify `start_scan` to accept `contact_quality_callback: Callable | None = None` and forward it into the `run_pipeline(...)` / `SciencePipeline(...)` construction inside `_worker`. Search for the existing `on_uncorrected_fn=` kwarg in that function and add `on_contact_quality_warning=contact_quality_callback,` next to it.

- [ ] **Step 3: Smoke test the import path**

Run: `python -c "from omotion import MOTIONInterface; print(hasattr(MOTIONInterface, 'run_contact_quality_check'))"`
Expected: `True`.

- [ ] **Step 4: Commit**

```bash
git add omotion/Interface.py omotion/ScanWorkflow.py
git commit -m "feat: add MOTIONInterface.run_contact_quality_check helper"
```

---

## [APP] Task 4: Add ContactQualityModal QML component

A custom large modal with three states. Lives as a singleton instance in the page tree.

**Files:**
- Create: `components/ContactQualityModal.qml`

- [ ] **Step 1: Create the component**

Create `components/ContactQualityModal.qml`:

```qml
import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Dialog {
    id: root
    modal: true
    closePolicy: Popup.NoAutoClose
    width: 520
    height: 420
    anchors.centerIn: parent

    // States: "checking" | "ok" | "warnings" | "error"
    property string state_: "checking"
    // Whether the modal was opened during a live scan (controls footer).
    property bool liveScan: false
    property string errorText: ""

    // Each entry: { camera: "L4", typeText: "Poor sensor contact", value: 72.5 }
    property var entries: []

    signal stopScanRequested()
    signal continueRequested()
    signal dismissed()

    function reset(forLiveScan) {
        liveScan = !!forLiveScan
        entries = []
        errorText = ""
        state_ = "checking"
        if (!visible) open()
    }

    function showOk() {
        state_ = "ok"
        if (!visible) open()
    }

    function showError(msg) {
        errorText = msg || "Hardware error"
        state_ = "error"
        if (!visible) open()
    }

    // Append a warning row. Duplicates (same camera+type) are ignored.
    function addWarning(cameraLabel, typeText, value) {
        for (var i = 0; i < entries.length; ++i) {
            if (entries[i].camera === cameraLabel && entries[i].typeText === typeText)
                return
        }
        var copy = entries.slice()
        copy.push({ camera: cameraLabel, typeText: typeText, value: value })
        entries = copy
        state_ = "warnings"
        if (!visible) open()
    }

    background: Rectangle {
        color: "#1F2A36"
        radius: 8
        border.color: state_ === "ok" ? "#27AE60" : (state_ === "warnings" ? "#E67E22" : "#34495E")
        border.width: 2
    }

    contentItem: ColumnLayout {
        spacing: 16
        anchors.fill: parent
        anchors.margins: 24

        Text {
            Layout.fillWidth: true
            font.pixelSize: 22
            font.bold: true
            color: "white"
            text: {
                if (root.state_ === "checking") return "Checking contact quality…"
                if (root.state_ === "ok")       return "Good signal quality"
                if (root.state_ === "error")    return "Contact check failed"
                return "Contact quality warnings"
            }
        }

        // Spinner for "checking" state
        BusyIndicator {
            visible: root.state_ === "checking"
            running: visible
            Layout.alignment: Qt.AlignHCenter
        }

        // OK message
        Text {
            visible: root.state_ === "ok"
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: "#BDC3C7"
            text: "All cameras are reporting acceptable ambient light and contact levels."
        }

        // Error message
        Text {
            visible: root.state_ === "error"
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: "#E74C3C"
            text: root.errorText
        }

        // Warning list
        ListView {
            visible: root.state_ === "warnings"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            model: root.entries
            spacing: 6
            delegate: Rectangle {
                width: ListView.view.width
                height: 36
                color: "#2C3E50"
                radius: 4
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 12
                    Text {
                        text: modelData.camera
                        color: "#ECF0F1"
                        font.bold: true
                        font.pixelSize: 14
                        Layout.preferredWidth: 50
                    }
                    Text {
                        text: modelData.typeText
                        color: "#ECF0F1"
                        font.pixelSize: 14
                        Layout.fillWidth: true
                    }
                    Text {
                        text: modelData.value.toFixed(1) + " DN"
                        color: "#BDC3C7"
                        font.pixelSize: 12
                    }
                }
            }
        }

        // Footer buttons
        RowLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignRight
            spacing: 12
            visible: root.state_ !== "checking"

            // Live-scan footer
            Button {
                visible: root.liveScan && root.state_ === "warnings"
                text: "Stop scan"
                onClicked: { root.stopScanRequested(); root.close(); root.dismissed() }
            }
            Button {
                visible: root.liveScan && root.state_ === "warnings"
                text: "Continue"
                onClicked: { root.continueRequested(); root.close(); root.dismissed() }
            }

            // Quick-check / OK / error footer
            Button {
                visible: !(root.liveScan && root.state_ === "warnings")
                text: "Dismiss"
                onClicked: { root.close(); root.dismissed() }
            }
        }
    }
}
```

- [ ] **Step 2: Sanity check QML loads**

Run: `python -c "from PyQt6.QtCore import QUrl; from PyQt6.QtQml import QQmlEngine; e = QQmlEngine(); print('ok')"`
Expected: `ok`. (We can't fully load the QML without the app context; deeper validation happens in Task 7's smoke test.)

- [ ] **Step 3: Commit**

```bash
git add components/ContactQualityModal.qml
git commit -m "feat: add ContactQualityModal QML component"
```

---

## [APP] Task 5: Add "Check" button to SidebarMenu

**Files:**
- Modify: `components/SidebarMenu.qml`

- [ ] **Step 1: Add a third button**

In `components/SidebarMenu.qml`, add a property for the disabled state and a new `IconButton` after the "Analyze" button (insert before the closing `}` of the `ColumnLayout`):

```qml
    // Property to disable the Check button while a normal scan is running.
    property bool checkEnabled: true

    // ...inside ColumnLayout, after the Analyze IconButton...

        // Contact quality Check button
        IconButton {
            buttonIcon: "\uf012"   // signal-bars icon
            buttonText: "Check"
            Layout.alignment: Qt.AlignHCenter
            enabled: sidebarMenu.checkEnabled
            opacity: enabled ? 1.0 : 0.4
            backgroundColor: sidebarMenu.activeButtonIndex === 2 ? "white" : "transparent"
            iconColor: sidebarMenu.activeButtonIndex === 2 ? "#2C3E50" : "#BDC3C7"
            onClicked: {
                sidebarMenu.handleButtonClick(2);
            }
        }
```

(If `\uf012` is not present in the font in use, swap to any existing icon — confirm by grepping the font cheatsheet referenced by `IconButton.qml`.)

- [ ] **Step 2: Smoke-launch the app**

Run: `python main.py` and visually confirm the "Check" button appears below "Analyze" and is enabled.
Expected: button visible, click does nothing yet (wired in Task 6/7).

- [ ] **Step 3: Commit**

```bash
git add components/SidebarMenu.qml
git commit -m "feat: add Check button to sidebar menu"
```

---

## [APP] Task 6: Add connector signals, slots, and quick-check thread

**Files:**
- Modify: `motion_connector.py`

- [ ] **Step 1: Add signals**

Near the other `pyqtSignal` declarations at the top of the connector class:

```python
    contactQualityCheckStarted = pyqtSignal()
    contactQualityCheckFinished = pyqtSignal(bool, 'QVariantList')
    # Live-scan warning: (camera_label, type_key, type_text, value)
    contactQualityWarning = pyqtSignal(str, str, str, float)
    contactQualityScanInProgress = pyqtSignal(bool)
```

- [ ] **Step 2: Add a helper to format camera labels**

```python
    @staticmethod
    def _camera_label(side: str, cam_id: int) -> str:
        prefix = "L" if side == "left" else "R"
        return f"{prefix}{int(cam_id)}"

    @staticmethod
    def _warning_text(type_key: str) -> str:
        return {
            "ambient_light": "Ambient light detected",
            "poor_contact": "Poor sensor contact",
        }.get(type_key, type_key)
```

Note: the SDK warning carries only `camera_id`. To recover side, derive from id (cameras 0–7 = left, 8–15 = right) **only if** that mapping holds; otherwise extend the SDK warning with `side` in Task 2 and pipe it through. **Implementer:** verify by grepping `cam_id` usage in `MotionProcessing.py`. If the SDK uses a per-side cam_id (0–7 each side), add a `side: str` field to `ContactQualityWarning` and pass it from the wiring sites — both emission sites have `side` in scope.

- [ ] **Step 3: Wire the live-scan callback**

In the connector's scan-start path (search for where `start_scan` is invoked), pass:

```python
def _on_cq_warning(w):
    label = self._camera_label(getattr(w, "side", "left"), w.camera_id)
    type_key = w.warning_type.value
    self.contactQualityWarning.emit(label, type_key, self._warning_text(type_key), float(w.value))

self._interface.start_scan(request, contact_quality_callback=_on_cq_warning)
```

(If the existing `start_scan` invocation is in `_scan_workflow.start_scan`, route the kwarg the same way as Task 3's `MOTIONInterface.start_scan` extension.)

- [ ] **Step 4: Add the quick-check slot**

```python
    @pyqtSlot()
    def runContactQualityCheck(self):
        if self._scan_workflow.running:
            self.contactQualityCheckFinished.emit(False, [])
            return

        self.contactQualityCheckStarted.emit()

        def _worker():
            try:
                result = self._interface.run_contact_quality_check(duration_s=1.0)
            except Exception as exc:
                logger.exception("contact-quality check failed: %s", exc)
                self.contactQualityCheckFinished.emit(False, [])
                return

            payload = []
            for w in result.warnings:
                payload.append({
                    "camera": self._camera_label(getattr(w, "side", "left"), w.camera_id),
                    "typeKey": w.warning_type.value,
                    "typeText": self._warning_text(w.warning_type.value),
                    "value": float(w.value),
                })
            self.contactQualityCheckFinished.emit(bool(result.ok), payload)

        import threading
        threading.Thread(target=_worker, daemon=True, name="ContactQualityCheck").start()
```

- [ ] **Step 5: Sanity-import the connector**

Run: `python -c "import motion_connector; print('ok')"`
Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
git add motion_connector.py
git commit -m "feat: add contact-quality slots and signals to motion_connector"
```

---

## [APP] Task 7: Wire modal into BloodFlow page and connect signals

**Files:**
- Modify: `pages/BloodFlow.qml` (or `main.qml` — wherever `SidebarMenu` is instantiated)

- [ ] **Step 1: Locate the SidebarMenu instance**

Run: `grep -n "SidebarMenu" pages/*.qml main.qml components/*.qml 2>/dev/null`
Expected: at least one usage in `pages/BloodFlow.qml` (or main.qml). Open that file.

- [ ] **Step 2: Add the modal and connect signals**

Inside the same root `Item`/`Page` that contains `SidebarMenu`, add:

```qml
import "../components"   // adjust path if needed

ContactQualityModal {
    id: contactQualityModal
    onStopScanRequested: MOTIONInterface.stopScan()   // use existing slot name; verify
    onContinueRequested: { /* no-op */ }
}

Connections {
    target: MOTIONInterface
    function onContactQualityCheckStarted() {
        contactQualityModal.reset(false)
    }
    function onContactQualityCheckFinished(ok, warnings) {
        if (!ok) { contactQualityModal.showError("Quick check failed"); return }
        if (warnings.length === 0) { contactQualityModal.showOk(); return }
        for (var i = 0; i < warnings.length; ++i) {
            var w = warnings[i]
            contactQualityModal.addWarning(w.camera, w.typeText, w.value)
        }
    }
    function onContactQualityWarning(camera, typeKey, typeText, value) {
        if (contactQualityModal.state_ === "checking" || !contactQualityModal.visible) {
            contactQualityModal.reset(true)
        } else {
            contactQualityModal.liveScan = true
        }
        contactQualityModal.addWarning(camera, typeText, value)
    }
}
```

- [ ] **Step 3: Wire the sidebar click**

Find the `SidebarMenu.onButtonClicked` handler in the same file. Add a case for index 2:

```qml
onButtonClicked: function(idx) {
    if (idx === 0) { /* existing Demo nav */ }
    else if (idx === 1) { /* existing Analyze nav */ }
    else if (idx === 2) {
        MOTIONInterface.runContactQualityCheck()
    }
}
```

(Adapt to the file's existing handler shape.)

- [ ] **Step 4: Bind sidebar enabled state**

Add `checkEnabled: !bloodFlowPage.scanning` (or equivalent boolean already in scope) to the `SidebarMenu` instantiation so the Check button greys out during a normal scan.

- [ ] **Step 5: Smoke test the quick-check happy path**

Run: `python main.py`. With hardware connected, click **Check**. Expected:
- Modal opens immediately in "Checking contact quality…" state.
- After ~1 second, modal transitions to "Good signal quality" (covered cameras → "Contact quality warnings" with rows per affected camera).
- "Dismiss" closes the modal.

- [ ] **Step 6: Smoke test live-scan warning**

Force a low-light condition (cover the sensor) during a normal scan. Expected:
- Modal opens with "Contact quality warnings" and a row for the affected camera.
- Cover a second camera → row appended to the same modal, no second modal opens.
- "Stop scan" cancels the scan; "Continue" leaves the scan running.

- [ ] **Step 7: Commit**

```bash
git add pages/BloodFlow.qml   # or main.qml
git commit -m "feat: mount ContactQualityModal and wire to sidebar Check + live warnings"
```

---

## Self-Review (already performed)

- **Spec coverage:** Each spec section maps to a task — algorithm + thresholds (Task 1), pipeline integration (Task 2), quick-check API with mask 0xFF (Task 3), modal (Task 4), sidebar button (Task 5), connector glue (Task 6), modal mount + wiring (Task 7).
- **Placeholder scan:** No TBDs. Two implementer notes call out specific verification steps (cam_id side mapping, exact `start_scan` invocation site) — these are unavoidable because they depend on code paths that vary by current state.
- **Type consistency:** `ContactQualityWarning.warning_type` is the enum throughout; QML payloads use `.value` (string). The `side` field is added in Task 2 if cam_id is per-side (verification step in Task 6).
- **Scope:** Single feature, two repos, ~7 small tasks. Appropriate for one plan.
