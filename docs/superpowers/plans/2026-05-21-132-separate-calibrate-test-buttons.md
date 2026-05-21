# Separate Calibrate and Test Buttons — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the bloodflow app's single **Run Calibration** button into **Calibrate** + **Test**, extend the calibration scan to 15 s with every laser-on corrected sample averaged (not just the rolling-window subset), and add a non-modal Test Results window with a per-camera table (light mean / dark mean / contrast + thresholds + PASS/FAIL) and a Copy button.

**Architecture:** SDK gains an `average_full_scan` opt-in on `CalibrationRequest` (used by both flows) and a new `start_test_scan` method that runs only `CalibrationWorkflow` phase 1 + the existing row-builder, returning a `TestScanResult` and writing `test-{ts}.csv` / `test-{ts}.json` to the same `calibrations/` directory. The bloodflow connector mirrors `runCalibration` with a `runTestScan` slot, exposes the rows via a QML `testScanRows` property, and surfaces a single instanced `components/TestResultsWindow.qml` (Qt `Window`) that auto-shows on state changes.

**Tech Stack:** Python 3.12, PyQt6, QML 6.0, pytest, pytest-qt. Two repos: `openmotion-sdk` (calibration pipeline + unit/HIL tests) and `openmotion-bloodflow-app` (connector + QML + config + UI tests).

**Spec:** `openmotion-bloodflow-app/docs/superpowers/specs/2026-05-21-132-separate-calibrate-test-buttons-design.md`

---

## Repo / branch setup

This work spans two repos. Use feature branches in each:

- `openmotion-sdk`: branch off `next` (verify with `git status` in `C:/Users/ethan/Projects/openmotion-sdk`). Name: `feature/132-test-scan`.
- `openmotion-bloodflow-app`: branch `feature/132-separate-calibrate-test-buttons` already exists off `next` and holds the design spec. Continue on it.

Install the SDK in editable mode for development so app-side changes pick up SDK edits without a wheel rebuild:

```powershell
cd C:\Users\ethan\Projects\openmotion-sdk
pip install -e .
```

---

## File structure (locked-in decisions before tasks start)

**SDK (`openmotion-sdk`):**
- Modify: `omotion/CalibrationWorkflow.py` — extend `CalibrationRequest`, thread `average_full_scan`, add `TestScanResult`, add `CalibrationWorkflow.start_test_scan`, thread `mode` into `write_result_json`.
- Modify: `omotion/MotionInterface.py` — `start_test_scan`, `cancel_test_scan` facade passthroughs.
- Modify: `tests/test_calibration_workflow_compute.py` — pure-compute unit tests for the new fields and the row-overall convention.
- New (optional, hardware-dependent): `tests/test_calibration_test_scan_hil.py` — HIL coverage. Skip if no hardware available during implementation; ship the SDK changes with unit tests and add HIL later.

**Bloodflow-app (`openmotion-bloodflow-app`):**
- Modify: `motion_connector.py` — state, signals, properties, `runTestScan` slot, `_on_test_scan_complete` handler, `copyToClipboard` helper.
- Modify: `components/SettingsModal.qml` — replace single button with two side-by-side buttons; add `Connections` block that opens the Test Results window on state changes.
- Modify: `main.qml` — instantiate `TestResultsWindow` once at top level; extend `_anyInProgress` + `_inProgressLabel`.
- New: `components/TestResultsWindow.qml` — Qt `Window` with header strip + table + Copy / Close buttons.
- Modify: `config/app_config.json` — `calibration_scan_duration_sec` 5 → 15.
- New: `tests/test_test_scan_flow.py` — unit-level connector tests (no hardware).
- New (UI test, optional): `tests/test_test_results_ui.py` — pytest-qt scenario test. Add if the existing `tests/test_calibration_ui.py` pattern is in place; otherwise ship the connector tests + manual bench verification.

---

## Task 1 — SDK: add `average_full_scan` to `CalibrationRequest`

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/omotion/CalibrationWorkflow.py` (`CalibrationRequest` dataclass, around line 70-101)
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/tests/test_calibration_workflow_compute.py`

- [ ] **Step 1: Locate the existing `CalibrationRequest` dataclass and read its current shape**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
grep -n "class CalibrationRequest" omotion/CalibrationWorkflow.py
```

Expected output: a single line number around 70. Read the dataclass through its closing line (around 101) so you know what fields and defaults exist.

- [ ] **Step 2: Switch to a new SDK feature branch off `next`**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git fetch origin next
git checkout -b feature/132-test-scan origin/next
```

- [ ] **Step 3: Write the failing test**

Append to `tests/test_calibration_workflow_compute.py`:

```python
def test_request_average_full_scan_defaults_false():
    """The new request flag must default to False so existing callers
    (everything in the wild today) keep getting the rolling-window
    averaging they were getting before."""
    req = CalibrationRequest(
        operator_id="op",
        output_dir="/tmp",
        left_camera_mask=0x01,
        right_camera_mask=0x00,
        thresholds=_thresholds(),
        duration_sec=15,
    )
    assert req.average_full_scan is False


def test_request_average_full_scan_accepts_true():
    req = CalibrationRequest(
        operator_id="op",
        output_dir="/tmp",
        left_camera_mask=0x01,
        right_camera_mask=0x00,
        thresholds=_thresholds(),
        duration_sec=15,
        average_full_scan=True,
    )
    assert req.average_full_scan is True
```

If `_thresholds()` doesn't already exist as a helper in this test file, grep for it — it's used by the existing dark-test cases:

```bash
grep -n "_thresholds" tests/test_calibration_workflow_compute.py
```

If absent, add this near the top of the test file:

```python
def _thresholds():
    return CalibrationThresholds(
        min_mean_per_camera=[100.0] * 8,
        min_contrast_per_camera=[0.2] * 8,
        min_bfi_per_camera=[3.0] * 8,
        min_bvi_per_camera=[5.0] * 8,
    )
```

(Imports of `CalibrationRequest` and `CalibrationThresholds` should already be at the top of this file.)

- [ ] **Step 4: Run the tests to verify they fail**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
pytest tests/test_calibration_workflow_compute.py::test_request_average_full_scan_defaults_false tests/test_calibration_workflow_compute.py::test_request_average_full_scan_accepts_true -v
```

Expected: `TypeError: __init__() got an unexpected keyword argument 'average_full_scan'` on the second test.

- [ ] **Step 5: Add the field**

Edit `omotion/CalibrationWorkflow.py`. In the `CalibrationRequest` dataclass (around line 70-101), add the new field after `notes`. The full updated dataclass:

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
    average_full_scan: bool = False
```

(Preserve the existing multi-line docstring comment on `trigger_config` — only the new field is added.)

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
pytest tests/test_calibration_workflow_compute.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git add omotion/CalibrationWorkflow.py tests/test_calibration_workflow_compute.py
git commit -m "feat(sdk): add average_full_scan flag to CalibrationRequest (#132)"
```

---

## Task 2 — SDK: thread `average_full_scan` into phase-1 windowing

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/omotion/CalibrationWorkflow.py` (`CalibrationWorkflow._worker`, around line 873-1050)

No new unit test in this task — `_run_subscan_capture` requires live hardware. The unit-level coverage is the existing `test_request_average_full_scan_*` from Task 1 (verifies the field exists and round-trips). End-to-end verification lands in Task 11 (manual bench) and the optional HIL test in Task 6.

- [ ] **Step 1: Locate the existing phase-1 call site in `_worker`**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
grep -n "skip_frames = int(round" omotion/CalibrationWorkflow.py
grep -n "_run_subscan_capture" omotion/CalibrationWorkflow.py
```

The first match lands on the `skip_frames` / `window_frames` declarations around line 910-913. The second shows the three call sites (the function definition, the phase-1 call ~line 1033, and the phase-4 call ~line 1103). Read both calls so you know exactly what's being passed.

- [ ] **Step 2: Widen the phase-1 window when `average_full_scan` is set**

Edit `omotion/CalibrationWorkflow.py`. Find the block just before the phase-1 scan kickoff (around line 910-913):

```python
            skip_frames = int(round(request.scan_delay_sec * CAPTURE_HZ))
            # Bound the trailing edge to keep the firmware's terminal
            # dark frame (and any laser ramp-down) out of the average.
            window_frames = int(round(request.duration_sec * CAPTURE_HZ))
```

Replace with:

```python
            skip_frames = int(round(request.scan_delay_sec * CAPTURE_HZ))
            # Bound the trailing edge to keep the firmware's terminal
            # dark frame (and any laser ramp-down) out of the average.
            window_frames = int(round(request.duration_sec * CAPTURE_HZ))
            # Phase 1 (calibration scan) widens its averaging window
            # to swallow every laser-on corrected sample after the
            # leading scan_delay_sec skip when average_full_scan is set
            # (#132 — "all of the corrected data ... averaged, not just
            # the rolling average numbers"). Dark frames flow through
            # on_dark_frame_fn, not on_corrected_batch, so they're not
            # affected by this widening. Phase 4 (validation scan)
            # keeps the original window.
            phase1_window_frames = (
                10 ** 9 if request.average_full_scan else window_frames
            )
```

- [ ] **Step 3: Use `phase1_window_frames` in the phase-1 call**

In the same file, locate the phase-1 call to `_run_subscan_capture` (around line 1033). Currently:

```python
                cal_left, cal_right, cal_samples, _cal_dark_samples = _run_subscan_capture(
                    self._interface, request,
                    subject_id=f"calib1_{request.operator_id}",
                    duration_sec=request.duration_sec + request.scan_delay_sec,
                    skip_leading_frames=skip_frames,
                    frame_window_count=window_frames,
                    stop_evt=self._stop_evt,
                )
```

Change `frame_window_count=window_frames` to `frame_window_count=phase1_window_frames`:

```python
                cal_left, cal_right, cal_samples, _cal_dark_samples = _run_subscan_capture(
                    self._interface, request,
                    subject_id=f"calib1_{request.operator_id}",
                    duration_sec=request.duration_sec + request.scan_delay_sec,
                    skip_leading_frames=skip_frames,
                    frame_window_count=phase1_window_frames,
                    stop_evt=self._stop_evt,
                )
```

The phase-4 call (around line 1103) keeps `frame_window_count=window_frames` — that's deliberate, the spec only changes phase 1.

- [ ] **Step 4: Add a log line so the widening is visible in the run log**

Find the phase-1 progress log immediately above the call (around line 1019-1026). After the existing `logger.info("Calibration phase 1: ...")` block, add:

```python
                if request.average_full_scan:
                    logger.info(
                        "Calibration phase 1: average_full_scan=True — "
                        "averaging every laser-on corrected sample after "
                        "the %d-frame leading skip (no upper-bound window).",
                        skip_frames,
                    )
```

- [ ] **Step 5: Smoke-import the SDK module to confirm no syntax error**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
python -c "from omotion.CalibrationWorkflow import CalibrationWorkflow, CalibrationRequest; print('OK')"
```

Expected: `OK`.

- [ ] **Step 6: Run the SDK unit tests to verify no regression**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
pytest tests/test_calibration_workflow_compute.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git add omotion/CalibrationWorkflow.py
git commit -m "feat(sdk): phase 1 averages all laser-on samples when average_full_scan set (#132)"
```

---

## Task 3 — SDK: `TestScanResult` dataclass + `write_result_json` `mode` field

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/omotion/CalibrationWorkflow.py` (add dataclass near `CalibrationResult` ~line 124; extend `write_result_json` ~line 579 to accept a `mode` kwarg)
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/tests/test_calibration_workflow_compute.py`

- [ ] **Step 1: Write the failing test for `TestScanResult`**

Append to `tests/test_calibration_workflow_compute.py`:

```python
def test_test_scan_result_default_mode_is_test():
    from omotion.CalibrationWorkflow import TestScanResult

    r = TestScanResult(
        ok=True, passed=True, canceled=False, error="",
        csv_path="", json_path="", rows=[],
        test_scan_left_path="", test_scan_right_path="",
        started_timestamp="20260521_000000",
    )
    assert r.mode == "test"
```

- [ ] **Step 2: Write the failing test for `write_result_json` accepting a `mode` kwarg**

Append:

```python
def test_write_result_json_records_mode():
    import json
    import tempfile
    from pathlib import Path

    from omotion.CalibrationWorkflow import write_result_json

    class _StubInterface:
        console = None
        left = None
        right = None

    req = CalibrationRequest(
        operator_id="op",
        output_dir="/tmp",
        left_camera_mask=0x01,
        right_camera_mask=0x00,
        thresholds=_thresholds(),
        duration_sec=15,
    )
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "out.json")
        write_result_json(
            path,
            started_timestamp="20260521_000000",
            passed=True,
            canceled=False,
            error="",
            request=req,
            rows=[],
            calibration=None,
            scan_paths={},
            interface=_StubInterface(),
            mode="test",
        )
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["mode"] == "test"
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
pytest tests/test_calibration_workflow_compute.py::test_test_scan_result_default_mode_is_test tests/test_calibration_workflow_compute.py::test_write_result_json_records_mode -v
```

Expected: ImportError on the first (no `TestScanResult` exists), TypeError on the second (`mode` is not a kwarg of `write_result_json`).

- [ ] **Step 4: Add the `TestScanResult` dataclass**

Edit `omotion/CalibrationWorkflow.py`. Immediately after the existing `CalibrationResult` dataclass (ends around line 138), add:

```python
@dataclass
class TestScanResult:
    """Outcome of a stand-alone Test scan — phase 1 only, no calibration
    write, no validation scan. Shape mirrors ``CalibrationResult`` so the
    bloodflow-app's QML layer can re-use the row formatting code, but the
    fields are scoped to what a test scan actually produces (no
    ``calibration`` field — Test scans don't write to console EEPROM, no
    ``validation_scan_*_path`` — there's no validation scan).
    """
    ok: bool
    passed: bool
    canceled: bool
    error: str
    csv_path: str
    json_path: str
    rows: list[CalibrationResultRow]
    test_scan_left_path: str
    test_scan_right_path: str
    started_timestamp: str
    mode: str = "test"
```

- [ ] **Step 5: Add the `mode` kwarg to `write_result_json`**

In the same file, find `write_result_json` (around line 579). Change its signature to accept an optional `mode`:

```python
def write_result_json(
    path: str,
    *,
    started_timestamp: str,
    passed: bool,
    canceled: bool,
    error: str,
    request: CalibrationRequest,
    rows: list[CalibrationResultRow],
    calibration: Optional[Calibration],
    scan_paths: dict,
    interface,
    mode: str = "calibrate",
) -> None:
```

Inside the function, in the `manifest = { ... }` dict literal, add `"mode": mode,` immediately after the `"schema_version"` key:

```python
    manifest = {
        "schema_version": _JSON_SCHEMA_VERSION,
        "mode": mode,
        "started_timestamp": started_timestamp,
        ...
    }
```

The default `mode="calibrate"` preserves backward compatibility — existing calibration-flow callers don't have to pass it.

- [ ] **Step 6: Run the new tests to verify they pass**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
pytest tests/test_calibration_workflow_compute.py::test_test_scan_result_default_mode_is_test tests/test_calibration_workflow_compute.py::test_write_result_json_records_mode -v
```

Expected: both pass.

- [ ] **Step 7: Run the full SDK compute suite to verify no regression**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
pytest tests/test_calibration_workflow_compute.py -v
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git add omotion/CalibrationWorkflow.py tests/test_calibration_workflow_compute.py
git commit -m "feat(sdk): add TestScanResult dataclass and mode field in JSON manifest (#132)"
```

---

## Task 4 — SDK: `CalibrationWorkflow.start_test_scan` method

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/omotion/CalibrationWorkflow.py` (`CalibrationWorkflow` class, after `start_calibration` ~line 849-1231)

No new unit test in this task — the method orchestrates `_run_subscan_capture` and `_build_result_rows_from_samples`, both of which need hardware to exercise meaningfully. The pure-compute coverage (rows, CSV, JSON) is already exercised by existing tests. Manual + HIL verification land in Tasks 6 and 11.

- [ ] **Step 1: Locate the insertion point**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
grep -n "def cancel_calibration" omotion/CalibrationWorkflow.py
```

Expected: a single line number around 1233. Insert `start_test_scan` immediately above `cancel_calibration`.

- [ ] **Step 2: Add the `start_test_scan` method**

Insert this method into the `CalibrationWorkflow` class, immediately before `cancel_calibration`:

```python
    def start_test_scan(
        self,
        request: CalibrationRequest,
        *,
        on_log_fn: Optional[Callable[[str], None]] = None,
        on_progress_fn: Optional[Callable[[str], None]] = None,
        on_complete_fn: Optional[Callable[["TestScanResult"], None]] = None,
    ) -> bool:
        """Run just the calibration scan (CalibrationWorkflow phase 1)
        as a stand-alone diagnostic. No calibration write, no validation
        scan. Returns False if a calibration or test scan is already in
        flight. Forces ``request.average_full_scan = True`` so the Test
        results reflect the same averaging the calibration math would
        use (#132).
        """
        with self._lock:
            if self._running:
                logger.warning("start_test_scan refused: already running.")
                return False
            self._running = True
        self._stop_evt = threading.Event()

        # Test scans always average all laser-on samples — single source
        # of truth so the connector doesn't have to remember to set this.
        request = dataclasses.replace(request, average_full_scan=True)

        def _emit_log(msg: str) -> None:
            logger.info(msg)
            if on_log_fn:
                on_log_fn(msg)

        def _emit_progress(stage: str) -> None:
            if on_progress_fn:
                on_progress_fn(stage)

        def _worker() -> None:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            test_left = test_right = ""
            csv_path = ""
            json_path = ""
            rows: list[CalibrationResultRow] = []
            ok = False
            passed = False
            error = ""
            canceled = False

            logger.info(
                "Test scan: starting (operator=%s, output_dir=%s, "
                "masks=(0x%02X, 0x%02X), duration_sec=%d, scan_delay_sec=%d, "
                "max_duration_sec=%d, ts=%s)",
                request.operator_id, request.output_dir,
                request.left_camera_mask, request.right_camera_mask,
                request.duration_sec, request.scan_delay_sec,
                request.max_duration_sec, ts,
            )

            def _watchdog() -> None:
                self._stop_evt.set()
                logger.warning(
                    "Test-scan watchdog fired after %d sec; aborting.",
                    request.max_duration_sec,
                )
                try:
                    self._interface.scan_workflow.cancel_scan()
                except Exception:
                    pass

            wd = threading.Timer(request.max_duration_sec, _watchdog)
            wd.daemon = True
            wd.start()

            skip_frames = int(round(request.scan_delay_sec * CAPTURE_HZ))
            window_frames = int(round(request.duration_sec * CAPTURE_HZ))
            phase1_window_frames = (
                10 ** 9 if request.average_full_scan else window_frames
            )

            # Inner helpers — duplicate the calibration worker's shape
            # rather than refactor, so this method ships as a single
            # contained change. The two flash/trigger helpers below are
            # textually identical to the calibration worker's; consider
            # extracting later if a third caller appears.
            def _flash_sensors() -> tuple[bool, str]:
                from omotion.ScanWorkflow import ConfigureRequest, ConfigureResult

                cfg_req = ConfigureRequest(
                    left_camera_mask=request.left_camera_mask,
                    right_camera_mask=request.right_camera_mask,
                    power_off_unused_cameras=False,
                )
                evt = threading.Event()
                holder: dict[str, ConfigureResult] = {}

                def _on_done(r: ConfigureResult) -> None:
                    holder["r"] = r
                    evt.set()

                def _on_log(msg: str) -> None:
                    logger.info("Test-scan flash: %s", msg)

                started = self._interface.start_configure_camera_sensors(
                    cfg_req,
                    on_log_fn=_on_log,
                    on_complete_fn=_on_done,
                )
                if not started:
                    return False, (
                        "start_configure_camera_sensors refused "
                        "(another configure already running?)"
                    )

                while not evt.wait(timeout=0.2):
                    if self._stop_evt.is_set():
                        return False, "canceled during flash"
                res = holder.get("r")
                if res is None:
                    return False, "flash completed with no result"
                return bool(res.ok), str(res.error or "")

            def _reset_firmware_trigger(phase_label: str) -> None:
                trigger_cfg = self._interface.resolve_trigger_config(
                    request.trigger_config
                )
                try:
                    self._interface.console.set_trigger_json(data=trigger_cfg)
                    logger.info(
                        "Test scan %s: trigger reset OK "
                        "(firmware fsync_counter=1).", phase_label,
                    )
                except Exception as e:
                    logger.error(
                        "Test scan %s: trigger reset failed: %s. "
                        "Continuing — dark-integrity monitor will catch "
                        "any schedule misalignment.",
                        phase_label, e,
                    )

            try:
                _emit_progress("flash_sensors")
                _emit_log("Test scan: flashing sensors / FPGA…")
                flash_ok, flash_err = _flash_sensors()
                if not flash_ok:
                    error = f"flash phase failed: {flash_err}"
                    if "canceled" in flash_err:
                        canceled = True
                    return
                if self._stop_evt.is_set():
                    canceled = True
                    error = "canceled after flash"
                    return

                _emit_progress("test_scan")
                _emit_log("Test scan: starting…")
                _reset_firmware_trigger("test (pre-scan)")
                test_left, test_right, test_samples, test_dark_samples = _run_subscan_capture(
                    self._interface, request,
                    subject_id=f"test_{request.operator_id}",
                    duration_sec=request.duration_sec + request.scan_delay_sec,
                    skip_leading_frames=skip_frames,
                    frame_window_count=phase1_window_frames,
                    stop_evt=self._stop_evt,
                )
                logger.info(
                    "Test scan done: %d corrected samples captured live; "
                    "raw CSVs: left=%s  right=%s",
                    len(test_samples),
                    test_left or "(none)", test_right or "(none)",
                )
                if self._stop_evt.is_set():
                    canceled = True
                    error = "canceled during test scan"
                    return

                _emit_progress("evaluate")
                _emit_log("Test scan: evaluating…")
                rows = _build_result_rows_from_samples(
                    test_samples,
                    dark_samples=test_dark_samples,
                    left_camera_mask=request.left_camera_mask,
                    right_camera_mask=request.right_camera_mask,
                    thresholds=request.thresholds,
                    sensor_left=getattr(self._interface, "left", None),
                    sensor_right=getattr(self._interface, "right", None),
                )
                csv_path = os.path.join(
                    request.output_dir, f"test-{ts}.csv"
                )
                write_result_csv(csv_path, rows)
                # Test "passed" uses the same gate as calibration but
                # without BFI/BVI participating — Test acceptance is
                # mean + contrast + dark only (see spec R5/R6).
                passed = bool(rows) and all(
                    r.mean_test == "PASS"
                    and r.contrast_test == "PASS"
                    and r.dark_test != "FAIL"
                    for r in rows
                )
                pass_count = sum(
                    1 for r in rows
                    if r.mean_test == "PASS"
                    and r.contrast_test == "PASS"
                    and r.dark_test != "FAIL"
                )
                logger.info(
                    "Test scan result table:\n%s",
                    _format_result_rows_table(rows, request.thresholds),
                )
                logger.info(
                    "Test scan done: %d/%d cameras PASS, overall=%s. CSV: %s",
                    pass_count, len(rows), "PASS" if passed else "FAIL",
                    csv_path,
                )
                ok = True
            except Exception as e:
                logger.exception("Test scan worker failed.")
                if not error:
                    error = f"{type(e).__name__}: {e}"
            finally:
                wd.cancel()
                if self._stop_evt.is_set() and not canceled:
                    canceled = True
                    if not error:
                        error = (
                            f"test scan exceeded max_duration_sec="
                            f"{request.max_duration_sec}"
                        )

                try:
                    json_path = os.path.join(
                        request.output_dir, f"test-{ts}.json"
                    )
                    write_result_json(
                        json_path,
                        started_timestamp=ts,
                        passed=passed,
                        canceled=canceled,
                        error=error,
                        request=request,
                        rows=rows,
                        calibration=None,
                        scan_paths={
                            "test_left": test_left,
                            "test_right": test_right,
                        },
                        interface=self._interface,
                        mode="test",
                    )
                    logger.info("Test scan manifest written: %s", json_path)
                except Exception:
                    logger.exception("Failed to write test scan JSON manifest.")
                    json_path = ""

                logger.info(
                    "Test scan: procedure complete (ok=%s, passed=%s, "
                    "canceled=%s, error=%r)",
                    ok, passed, canceled, error,
                )

                result = TestScanResult(
                    ok=ok, passed=passed, canceled=canceled, error=error,
                    csv_path=csv_path, json_path=json_path,
                    rows=rows,
                    test_scan_left_path=test_left,
                    test_scan_right_path=test_right,
                    started_timestamp=ts,
                )
                with self._lock:
                    self._running = False
                if on_complete_fn:
                    try:
                        on_complete_fn(result)
                    except Exception:
                        logger.exception("on_complete_fn raised.")

        self._thread = threading.Thread(
            target=_worker, name="TestScanWorker", daemon=True,
        )
        self._thread.start()
        return True
```

- [ ] **Step 3: Add the `dataclasses` import**

At the top of `omotion/CalibrationWorkflow.py`, find the existing `import` block. After `from dataclasses import dataclass`, change it to:

```python
import dataclasses
from dataclasses import dataclass
```

(Required for the `dataclasses.replace(request, average_full_scan=True)` call inside `start_test_scan`.)

- [ ] **Step 4: Smoke-import the SDK module**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
python -c "from omotion.CalibrationWorkflow import CalibrationWorkflow, TestScanResult; print('OK')"
```

Expected: `OK`.

- [ ] **Step 5: Run the SDK unit tests to verify no regression**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
pytest tests/test_calibration_workflow_compute.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git add omotion/CalibrationWorkflow.py
git commit -m "feat(sdk): add CalibrationWorkflow.start_test_scan for #132 Test button"
```

---

## Task 5 — SDK: `MotionInterface` facade passthroughs

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/omotion/MotionInterface.py`

- [ ] **Step 1: Locate `start_calibration` in the facade**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
grep -n "def start_calibration\|def cancel_calibration" omotion/MotionInterface.py
```

Expected: two line numbers — read both methods to know the exact wrapping shape.

- [ ] **Step 2: Add `start_test_scan` immediately after `start_calibration`**

In the same class, directly below `start_calibration` (preserve any blank line and existing decorators), add:

```python
    def start_test_scan(self, request, **kw):
        """Facade passthrough for CalibrationWorkflow.start_test_scan.
        See that method for parameter and return-value documentation."""
        return self._calibration_workflow.start_test_scan(request, **kw)
```

If the facade uses a different attribute name to reach the calibration workflow (look for `self._calibration_workflow` in the existing `start_calibration` body — it might be `self.calibration_workflow` without the underscore, depending on the file's conventions), use the same attribute name.

- [ ] **Step 3: Add `cancel_test_scan` immediately after `cancel_calibration`**

```python
    def cancel_test_scan(self, *, join_timeout: float = 10.0) -> None:
        """Cancel an in-progress test scan. Delegates to
        ``cancel_calibration`` because both flows share the same
        worker thread + stop-event on CalibrationWorkflow."""
        return self._calibration_workflow.cancel_calibration(
            join_timeout=join_timeout,
        )
```

- [ ] **Step 4: Smoke-import the facade**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
python -c "from omotion import MOTIONInterface; print(hasattr(MOTIONInterface, 'start_test_scan'))"
```

Expected: `True`.

(If `MOTIONInterface` isn't the export name, look in `omotion/__init__.py`: `grep "MOTIONInterface\|MotionInterface" omotion/__init__.py` — the facade may be exported under a slightly different name. Adjust the import accordingly.)

- [ ] **Step 5: Run the SDK unit tests**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
pytest tests/test_calibration_workflow_compute.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git add omotion/MotionInterface.py
git commit -m "feat(sdk): facade passthroughs for start_test_scan / cancel_test_scan (#132)"
```

---

## Task 6 — SDK: optional HIL test for `start_test_scan`

**Files:**
- New: `C:/Users/ethan/Projects/openmotion-sdk/tests/test_calibration_test_scan_hil.py`

**Skip this task** if no hardware is available during implementation. Mark the task done in the plan, ship Tasks 1-5 as-is, and add HIL coverage when hardware comes online. Mark in the commit message that HIL is deferred.

If hardware IS available:

- [ ] **Step 1: Look at the existing calibration HIL test for shape**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
ls tests/
grep -l "start_calibration" tests/
```

Read the most relevant HIL-style test (look for files matching `test_calibration*` that use `pytest.fixture` to construct a `MotionInterface` against real hardware). Note the fixture name + camera-mask setup; use the same in the new file.

- [ ] **Step 2: Write the test file**

Create `tests/test_calibration_test_scan_hil.py`:

```python
"""HIL coverage for CalibrationWorkflow.start_test_scan (#132).

Requires a connected console + at least one sensor and a calibration
phantom (or any stable target — the assertions only check that data
flows, not that the target produces particular values).
"""
import threading

import pytest

from omotion.CalibrationWorkflow import (
    CalibrationRequest,
    CalibrationThresholds,
    TestScanResult,
)


def _wait_for_complete(workflow, *, timeout: float = 60.0) -> TestScanResult:
    evt = threading.Event()
    holder = {}

    def _on_complete(r):
        holder["r"] = r
        evt.set()

    return evt, holder, _on_complete


@pytest.mark.hil
def test_start_test_scan_completes_with_rows(motion_interface):
    """Sanity: a 5s test scan returns non-empty rows with finite mean,
    contrast, and dark values for every active camera."""
    thresholds = CalibrationThresholds(
        min_mean_per_camera=[0.0] * 8,
        min_contrast_per_camera=[0.0] * 8,
        min_bfi_per_camera=[-1e9] * 8,
        min_bvi_per_camera=[-1e9] * 8,
    )
    req = CalibrationRequest(
        operator_id="hil",
        output_dir="./_hil_test_scan_out",
        left_camera_mask=0xFF if motion_interface.left else 0x00,
        right_camera_mask=0xFF if motion_interface.right else 0x00,
        thresholds=thresholds,
        duration_sec=5,
        scan_delay_sec=1,
        max_duration_sec=60,
    )

    evt, holder, on_complete = _wait_for_complete(motion_interface)
    started = motion_interface.start_test_scan(req, on_complete_fn=on_complete)
    assert started, "start_test_scan refused"
    evt.wait(timeout=120)
    res = holder["r"]
    assert isinstance(res, TestScanResult)
    assert res.ok, f"test scan failed: {res.error}"
    assert not res.canceled
    assert res.mode == "test"
    assert len(res.rows) > 0
    for row in res.rows:
        assert row.mean > 0
        assert row.avg_contrast > 0
        # dark may be NaN if no dark samples landed, but that's a defect
        # we'd want to catch — use `is not None` rather than `!= NaN`.
        assert row.dark is not None


@pytest.mark.hil
def test_start_test_scan_does_not_touch_console_calibration(motion_interface):
    """The Test scan must not write to console EEPROM. Read the cached
    calibration before and after; confirm byte-for-byte equality."""
    before = motion_interface.get_calibration()

    thresholds = CalibrationThresholds(
        min_mean_per_camera=[0.0] * 8,
        min_contrast_per_camera=[0.0] * 8,
        min_bfi_per_camera=[-1e9] * 8,
        min_bvi_per_camera=[-1e9] * 8,
    )
    req = CalibrationRequest(
        operator_id="hil",
        output_dir="./_hil_test_scan_out",
        left_camera_mask=0xFF if motion_interface.left else 0x00,
        right_camera_mask=0xFF if motion_interface.right else 0x00,
        thresholds=thresholds,
        duration_sec=5,
        scan_delay_sec=1,
        max_duration_sec=60,
    )
    evt, holder, on_complete = _wait_for_complete(motion_interface)
    motion_interface.start_test_scan(req, on_complete_fn=on_complete)
    evt.wait(timeout=120)
    after = motion_interface.get_calibration()

    assert (after.c_max == before.c_max).all()
    assert (after.c_min == before.c_min).all()
    assert (after.i_max == before.i_max).all()
    assert (after.i_min == before.i_min).all()
```

(Reuse the existing `motion_interface` pytest fixture if present in `tests/conftest.py`. If the fixture is named differently — e.g. `hil_interface` — adjust the test's parameter name.)

- [ ] **Step 3: Run the HIL tests against hardware**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
pytest tests/test_calibration_test_scan_hil.py -v -m hil
```

Expected: both pass.

- [ ] **Step 4: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git add tests/test_calibration_test_scan_hil.py
git commit -m "test(sdk): HIL coverage for start_test_scan (#132)"
```

---

## Task 7 — App: connector state, signals, properties, `copyToClipboard` helper

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-bloodflow-app/motion_connector.py`

- [ ] **Step 1: Write the failing test for connector state**

Create `tests/test_test_scan_flow.py` (this file holds all the app-side connector unit tests for this feature):

```python
"""Unit tests for the test-scan slot path (#132). No hardware — fakes
the interface so we exercise just the connector's state machine + the
result-to-rows translation."""
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def connector(monkeypatch):
    """Build a MOTIONConnector against a fake MotionInterface.

    Mirrors what existing tests in tests/ already do — search
    tests/conftest.py / tests/test_calibration_ui*.py for a fixture
    that constructs a connector and reuse it if present. The shape
    below matches motion_connector.MOTIONConnector.__init__'s
    actual signature (interface, app_config, output_path, config_dir,
    parent, log_level)."""
    from motion_connector import MOTIONConnector

    fake_iface = MagicMock()
    fake_iface.console = MagicMock()
    fake_iface.left = MagicMock()
    fake_iface.right = MagicMock()
    fake_iface.is_device_connected.return_value = (True, True, True)
    fake_iface.start_test_scan.return_value = True
    fake_iface.start_calibration.return_value = True
    fake_iface.scan_workflow = MagicMock()

    c = MOTIONConnector(
        interface=fake_iface,
        app_config={"developerMode": False},
        output_path=".",
        config_dir="config",
    )
    # The constructor reads connection state from is_device_connected,
    # but downstream slot logic also reads _consoleConnected etc.
    # directly. Force-set them for clarity in tests that mutate.
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    return c


def test_initial_test_scan_state_is_idle(connector):
    assert connector.testScanRunning is False
    assert connector.testScanStatus == ""
    assert connector.testScanFailureReason == ""
    assert connector.testScanRows == []
```

(Look at any existing test in `tests/` that builds a `MOTIONConnector` to confirm the constructor args. `tests/conftest.py` likely has a fixture that does this — reuse it if so.)

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
pytest tests/test_test_scan_flow.py::test_initial_test_scan_state_is_idle -v
```

Expected: AttributeError — no `testScanRunning` / `testScanStatus` / etc.

- [ ] **Step 3: Add the new signal declarations**

Open `motion_connector.py`. Find the existing `calibrationStateChanged = pyqtSignal()` and `_calibrationCompleteSignal = pyqtSignal(object)` declarations (around line 253-254). Add directly below them:

```python
    testScanStateChanged = pyqtSignal()                # any of running/done/aborted/failed/idle
    _testScanCompleteSignal = pyqtSignal(object)       # private worker→main marshalling
```

- [ ] **Step 4: Initialise the new state in `__init__`**

In the same file, find the line `self._calibration_status = ""` (around line 434). Add directly below:

```python
        self._test_scan_status = ""              # "", "running", "done", "aborted", "failed"
        self._test_scan_failure_reason = ""
        self._test_scan_rows: list[dict] = []
```

Also find where `self._calibration_scan_duration_sec` is read from config (search for `calibration_scan_duration_sec` in `__init__` — likely near the other `calibration_*` config reads, around line 434-450). Immediately after that line, add a sibling read for the new key:

```python
        self._test_scan_duration_sec = int(
            cfg.get("test_scan_duration_sec", 5)
        )
```

This ensures `runTestScan` can reference `self._test_scan_duration_sec` when building its `CalibrationRequest` (OQ8 override: Test stays at 5 s while Calibrate's phase 1 goes to 15 s).

- [ ] **Step 5: Add the new pyqtProperty declarations**

Find the existing calibration-property block (around lines 904-918, starts with `@pyqtProperty(bool, notify=calibrationStateChanged)` for `calibrationRunning`). After the closing of `maxCalibrationTimeSec` (around line 918), insert:

```python
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

- [ ] **Step 6: Wire the worker→main signal in `connect_signals`**

Find `connect_signals` (around line 3385). After the existing `self._calibrationCompleteSignal.connect(self._on_calibration_complete)` line (around line 3397), add:

```python
        self._testScanCompleteSignal.connect(self._on_test_scan_complete)
```

- [ ] **Step 7: Add the `copyToClipboard` helper**

Anywhere in the class (a good spot is right after the existing `get_sdk_version` slot around line 3381). Add:

```python
    @pyqtSlot(str)
    def copyToClipboard(self, text: str) -> None:
        """Push a string to the system clipboard via Qt — used by the
        Test Results window's Copy button. Centralised here so QML
        doesn't need a direct dependency on PyQt6.QtGui."""
        from PyQt6.QtGui import QGuiApplication
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text)
```

- [ ] **Step 8: Run the connector test to verify it passes**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
pytest tests/test_test_scan_flow.py::test_initial_test_scan_state_is_idle -v
```

Expected: pass.

- [ ] **Step 9: Smoke-import the connector**

```powershell
cd C:\Users\ethan\Projects\openmotion-bloodflow-app
python -c "import motion_connector; print('OK')"
```

Expected: `OK`.

- [ ] **Step 10: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
git add motion_connector.py tests/test_test_scan_flow.py
git commit -m "feat(bloodflow-app): connector state + clipboard helper for Test scan (#132)"
```

---

## Task 8 — App: `runTestScan` slot + `_on_test_scan_complete` handler

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-bloodflow-app/motion_connector.py`
- Modify: `C:/Users/ethan/Projects/openmotion-bloodflow-app/tests/test_test_scan_flow.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_test_scan_flow.py`:

```python
def test_run_test_scan_refused_when_console_disconnected(connector):
    connector._consoleConnected = False
    # captureLog signal — we want to assert it gets emitted.
    seen = []
    connector.captureLog.connect(lambda m: seen.append(m))
    connector.runTestScan("both")
    assert connector._test_scan_status == ""  # unchanged
    assert any("console not connected" in m for m in seen)


def test_run_test_scan_refused_when_calibration_running(connector):
    connector._calibration_status = "running"
    seen = []
    connector.captureLog.connect(lambda m: seen.append(m))
    connector.runTestScan("both")
    # Either silently ignored or emits a warn — assert state unchanged.
    assert connector._test_scan_status == ""


def test_run_test_scan_starts_workflow(connector):
    connector.runTestScan("both")
    assert connector._test_scan_status == "running"
    connector._interface.start_test_scan.assert_called_once()


def test_on_test_scan_complete_passes_builds_rows(connector):
    """Synthesise a passing TestScanResult and confirm row dicts + status."""
    from omotion.CalibrationWorkflow import (
        CalibrationResultRow,
        TestScanResult,
    )

    rows = [
        CalibrationResultRow(
            camera_index=0, side="left", cam_id=0,
            mean=120.0, avg_contrast=0.30, bfi=0.0, bvi=4.5,
            dark=1.0, mean_test="PASS", contrast_test="PASS",
            bfi_test="PASS", bvi_test="PASS", dark_test="PASS",
            security_id="", hwid="",
        ),
    ]
    res = TestScanResult(
        ok=True, passed=True, canceled=False, error="",
        csv_path="/tmp/x.csv", json_path="/tmp/x.json",
        rows=rows, test_scan_left_path="", test_scan_right_path="",
        started_timestamp="20260521_000000",
    )
    connector._on_test_scan_complete(res)
    assert connector._test_scan_status == "done"
    assert len(connector._test_scan_rows) == 1
    row = connector._test_scan_rows[0]
    assert row["side"] == "left"
    assert row["cam"] == 1
    assert row["light_mean"] == 120.0
    assert row["mean_pf"] == "PASS"
    assert row["dark_pf"] == "PASS"
    assert row["overall"] == "PASS"


def test_on_test_scan_complete_dev_mode_failure_reason(connector):
    from omotion.CalibrationWorkflow import (
        CalibrationResultRow,
        TestScanResult,
    )

    connector._app_config["developerMode"] = True
    rows = [
        CalibrationResultRow(
            camera_index=0, side="left", cam_id=0,
            mean=120.0, avg_contrast=0.30, bfi=0.0, bvi=4.5,
            dark=10.0,
            mean_test="PASS", contrast_test="PASS",
            bfi_test="PASS", bvi_test="PASS",
            dark_test="FAIL",  # dark fails
            security_id="", hwid="",
        ),
    ]
    res = TestScanResult(
        ok=True, passed=False, canceled=False, error="",
        csv_path="/tmp/x.csv", json_path="/tmp/x.json",
        rows=rows, test_scan_left_path="", test_scan_right_path="",
        started_timestamp="20260521_000000",
    )
    connector._on_test_scan_complete(res)
    assert connector._test_scan_status == "failed"
    assert connector._test_scan_rows[0]["overall"] == "FAIL"
    assert connector._test_scan_failure_reason.startswith("too much ambient light")
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
pytest tests/test_test_scan_flow.py -v
```

Expected: most new tests fail because `runTestScan` and `_on_test_scan_complete` don't exist yet.

- [ ] **Step 3: Add the `runTestScan` slot**

Open `motion_connector.py`. Find `runCalibration` (around line 3399-3520). Insert `runTestScan` immediately after `runCalibration`'s closing line. Full method:

```python
    @pyqtSlot()
    @pyqtSlot(str)
    def runTestScan(self, target: str = "both"):
        """Run just the calibration scan (phase 1) as a Test diagnostic.
        Mirrors runCalibration but does NOT write calibration to the
        console EEPROM and does NOT run a validation scan. Idempotent
        if a calibration or test scan is already in flight.

        ``target`` selects which side(s) to test: ``"left"``,
        ``"right"``, or ``"both"`` (default). Issue #117 — test stations
        with only one static phantom need to test one side at a time.
        """
        from omotion import CalibrationRequest, CalibrationThresholds

        # Mutual exclusion with the Calibrate flow.
        if self._test_scan_status == "running":
            return
        if self._calibration_status == "running":
            self.captureLog.emit(
                "⚠️ Cannot run Test scan: calibration in progress."
            )
            return

        if not self._consoleConnected:
            self.captureLog.emit(
                "⚠️ Cannot run Test scan: console not connected."
            )
            return

        target = (target or "both").lower().strip()
        if target not in ("left", "right", "both"):
            self.captureLog.emit(
                f"⚠️ Cannot run Test scan: invalid target '{target}'."
            )
            return

        want_left  = target in ("left", "both")
        want_right = target in ("right", "both")
        left_mask  = 0xFF if (want_left  and self._leftSensorConnected)  else 0x00
        right_mask = 0xFF if (want_right and self._rightSensorConnected) else 0x00
        if (left_mask | right_mask) == 0:
            if target == "left" and not self._leftSensorConnected:
                self.captureLog.emit("⚠️ Cannot run Test scan: left sensor not connected.")
            elif target == "right" and not self._rightSensorConnected:
                self.captureLog.emit("⚠️ Cannot run Test scan: right sensor not connected.")
            else:
                self.captureLog.emit("⚠️ Cannot run Test scan: no sensors connected.")
            return

        thresholds = CalibrationThresholds(
            min_mean_per_camera=list(self._ft_min_mean_per_camera or [0.0]*8),
            min_contrast_per_camera=list(self._ft_min_contrast_per_camera or [0.0]*8),
            min_bfi_per_camera=list(self._ft_min_bfi_per_camera or [0.0]*8),
            min_bvi_per_camera=list(self._ft_min_bvi_per_camera or [0.0]*8),
            max_bfi_per_camera=(
                list(self._ft_max_bfi_per_camera)
                if self._ft_max_bfi_per_camera is not None else None
            ),
            max_bvi_per_camera=(
                list(self._ft_max_bvi_per_camera)
                if self._ft_max_bvi_per_camera is not None else None
            ),
            max_dark_per_camera=(
                list(self._ft_max_dark_per_camera)
                if self._ft_max_dark_per_camera is not None else None
            ),
        )
        output_dir = os.path.join(self._directory, "calibrations")
        os.makedirs(output_dir, exist_ok=True)
        req = CalibrationRequest(
            operator_id="bloodflow-app",
            output_dir=output_dir,
            left_camera_mask=left_mask,
            right_camera_mask=right_mask,
            thresholds=thresholds,
            duration_sec=self._test_scan_duration_sec,   # OQ8: Test uses shorter duration (default 5s), NOT _calibration_scan_duration_sec (15s)
            scan_delay_sec=self._calibration_scan_delay_sec,
            max_duration_sec=self._max_calibration_time_sec,
        )

        self._test_scan_status = "running"
        self._test_scan_rows = []
        self._test_scan_failure_reason = ""
        self.testScanStateChanged.emit()
        self.captureLog.emit("Test scan: starting…")

        # Same #108 laser-power cold-start guard the Calibrate path uses.
        try:
            ok = self.set_laser_power_from_config(self._interface)
            if not ok:
                logger.warning(
                    "runTestScan: set_laser_power_from_config returned "
                    "False — proceeding anyway, but the test scan will "
                    "likely abort with 'zero or negative aggregate' if "
                    "this is a cold start. See issue #108."
                )
            else:
                logger.info("runTestScan: laser params applied")
        except Exception as e:
            logger.error(
                "runTestScan: applying laser params raised: %s — "
                "proceeding anyway", e
            )

        started = self._interface.start_test_scan(
            req,
            on_log_fn=lambda msg: self.captureLog.emit(msg),
            on_complete_fn=self._testScanCompleteSignal.emit,
        )
        if not started:
            self._test_scan_status = ""
            self.testScanStateChanged.emit()
            self.captureLog.emit("⚠️ Test scan failed to start.")
```

- [ ] **Step 4: Add the `_on_test_scan_complete` handler**

Insert directly below `_on_calibration_complete` (around line 3521-3561). Full method:

```python
    @pyqtSlot(object)
    def _on_test_scan_complete(self, result):
        """Runs on the Qt main thread (queued from the SDK worker via
        _testScanCompleteSignal). Translates a TestScanResult into the
        QML-friendly _test_scan_rows model and updates _test_scan_status.
        """
        self._test_scan_failure_reason = ""
        if result.canceled:
            self._test_scan_status = "aborted"
            self.captureLog.emit(
                f"⚠️ Test scan aborted: {result.error or 'canceled'}"
            )
        elif not result.ok:
            self._test_scan_status = "aborted"
            self.captureLog.emit(
                f"⚠️ Test scan aborted: {result.error or 'unknown error'}"
            )
        elif result.passed:
            self._test_scan_status = "done"
            self.captureLog.emit(
                f"✅ Test scan: PASS  (CSV: {result.csv_path})"
            )
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
            self.captureLog.emit(
                f"❌ Test scan: FAIL  (CSV: {result.csv_path})"
            )

        # Build the QML-friendly row dicts.
        self._test_scan_rows = [
            {
                "side": r.side,
                "cam": r.cam_id + 1,
                "light_mean": r.mean,
                "min_mean": (
                    self._ft_min_mean_per_camera[r.cam_id]
                    if self._ft_min_mean_per_camera
                    and r.cam_id < len(self._ft_min_mean_per_camera)
                    else None
                ),
                "mean_pf": r.mean_test,
                "dark_mean": r.dark,
                "max_dark": (
                    self._ft_max_dark_per_camera[r.cam_id]
                    if self._ft_max_dark_per_camera
                    and r.cam_id < len(self._ft_max_dark_per_camera)
                    else None
                ),
                "dark_pf": r.dark_test,
                "contrast": r.avg_contrast,
                "min_contrast": (
                    self._ft_min_contrast_per_camera[r.cam_id]
                    if self._ft_min_contrast_per_camera
                    and r.cam_id < len(self._ft_min_contrast_per_camera)
                    else None
                ),
                "contrast_pf": r.contrast_test,
                "overall": (
                    "PASS"
                    if r.mean_test == "PASS"
                    and r.contrast_test == "PASS"
                    and r.dark_test != "FAIL"
                    else "FAIL"
                ),
            }
            for r in result.rows
        ]
        self.testScanStateChanged.emit()
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
pytest tests/test_test_scan_flow.py -v
```

Expected: all pass.

- [ ] **Step 6: Smoke-import the connector**

```powershell
python -c "import motion_connector; print('OK')"
```

Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
git add motion_connector.py tests/test_test_scan_flow.py
git commit -m "feat(bloodflow-app): runTestScan slot + completion handler (#132)"
```

---

## Task 9 — App: `components/TestResultsWindow.qml`

**Files:**
- New: `C:/Users/ethan/Projects/openmotion-bloodflow-app/components/TestResultsWindow.qml`

No unit test in this task — the QML file is consumed by Task 10 (pytest-qt scenario test) and Task 11 (manual). Pure layout / binding code lives or dies in those flows.

- [ ] **Step 1: Examine an existing styled component for theme tokens**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
head -40 components/AppTheme.qml
head -60 components/SettingsModal.qml
```

Note `theme.bgBase`, `theme.bgCard`, `theme.borderSoft`, `theme.textPrimary`, `theme.textSecondary` — the new window uses the same tokens for visual consistency.

- [ ] **Step 2: Create the new QML file**

Write `components/TestResultsWindow.qml`:

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
    flags: Qt.Window
    modality: Qt.NonModal

    AppTheme { id: theme }

    color: theme.bgBase

    readonly property var rows: MOTIONInterface.testScanRows
    readonly property string status: MOTIONInterface.testScanStatus
    readonly property string failureReason: MOTIONInterface.testScanFailureReason
    readonly property bool running: MOTIONInterface.testScanRunning

    function _fmtNum(v, decimals) {
        if (v === null || v === undefined) return ""
        if (typeof v !== "number") return String(v)
        if (isNaN(v)) return ""
        return v.toFixed(decimals)
    }

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
                _fmtNum(r.light_mean, 2),
                _fmtNum(r.min_mean, 2),
                r.mean_pf,
                _fmtNum(r.dark_mean, 2),
                _fmtNum(r.max_dark, 2),
                r.dark_pf,
                _fmtNum(r.contrast, 5),
                _fmtNum(r.min_contrast, 4),
                r.contrast_pf,
                r.overall,
            ].join("\t"))
        }
        MOTIONInterface.copyToClipboard(lines.join("\n"))
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        // Header strip — live status + Copy + Close
        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            Text {
                Layout.fillWidth: true
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
                wrapMode: Text.WordWrap
            }

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

        // Table header
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 28
            color: theme.bgCard
            border.color: theme.borderSoft
            border.width: 1
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 8
                anchors.rightMargin: 8
                spacing: 0
                Text { Layout.preferredWidth: 50;  text: "Side"; color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 40;  text: "Cam";  color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 90;  text: "Light Mean";    color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 70;  text: "Min Mean";      color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 60;  text: "Mean PF";       color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 90;  text: "Dark Mean";     color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 70;  text: "Max Dark";      color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 60;  text: "Dark PF";       color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 90;  text: "Contrast";      color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 90;  text: "Min Contrast";  color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.preferredWidth: 80;  text: "Contrast PF";   color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
                Text { Layout.fillWidth: true;     text: "Overall";       color: theme.textSecondary; font.bold: true; font.pixelSize: 12 }
            }
        }

        // Table body
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true

            Column {
                width: parent.width
                spacing: 0
                Repeater {
                    model: testWin.rows
                    delegate: Rectangle {
                        width: parent.width
                        implicitHeight: 24
                        color: (index % 2 === 0) ? "transparent" : Qt.darker(theme.bgBase, 1.05)
                        border.color: theme.borderSoft
                        border.width: 0

                        property color _passColor: "#4CAF50"
                        property color _failColor: "#F44336"
                        property color _naColor:   theme.textSecondary
                        function _pfColor(s) {
                            if (s === "PASS") return _passColor
                            if (s === "FAIL") return _failColor
                            return _naColor
                        }

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 8
                            anchors.rightMargin: 8
                            spacing: 0

                            Text { Layout.preferredWidth: 50;  text: modelData.side; color: theme.textPrimary;  font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 40;  text: modelData.cam;  color: theme.textPrimary;  font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 90;  text: testWin._fmtNum(modelData.light_mean, 2); color: theme.textPrimary;  font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 70;  text: testWin._fmtNum(modelData.min_mean, 2);   color: theme.textSecondary; font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 60;  text: modelData.mean_pf;     color: parent.parent._pfColor(modelData.mean_pf); font.family: "Consolas"; font.bold: true; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 90;  text: testWin._fmtNum(modelData.dark_mean, 2); color: theme.textPrimary;  font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 70;  text: testWin._fmtNum(modelData.max_dark, 2);  color: theme.textSecondary; font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 60;  text: modelData.dark_pf;     color: parent.parent._pfColor(modelData.dark_pf); font.family: "Consolas"; font.bold: true; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 90;  text: testWin._fmtNum(modelData.contrast, 5); color: theme.textPrimary;  font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 90;  text: testWin._fmtNum(modelData.min_contrast, 4); color: theme.textSecondary; font.family: "Consolas"; font.pixelSize: 12 }
                            Text { Layout.preferredWidth: 80;  text: modelData.contrast_pf; color: parent.parent._pfColor(modelData.contrast_pf); font.family: "Consolas"; font.bold: true; font.pixelSize: 12 }
                            Text { Layout.fillWidth: true;     text: modelData.overall;    color: parent.parent._pfColor(modelData.overall);    font.family: "Consolas"; font.bold: true; font.pixelSize: 12 }
                        }
                    }
                }
            }
        }
    }
}
```

- [ ] **Step 3: Smoke-load the QML in the app**

Launch the app from source briefly to confirm the file parses without errors:

```powershell
cd C:\Users\ethan\Projects\openmotion-bloodflow-app
python main.py
```

The Test Results window won't be visible yet (no signal wired to show it), but the QML engine logs every parse error at startup. Close the app.

Expected: no QML errors in the console output during startup.

- [ ] **Step 4: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
git add components/TestResultsWindow.qml
git commit -m "feat(bloodflow-app): TestResultsWindow QML component for #132"
```

---

## Task 10 — App: wire the new buttons in `SettingsModal.qml` + instantiate window in `main.qml`

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-bloodflow-app/components/SettingsModal.qml` (around lines 773-874)
- Modify: `C:/Users/ethan/Projects/openmotion-bloodflow-app/main.qml`

- [ ] **Step 1: Replace the single button row in `SettingsModal.qml`**

Open `components/SettingsModal.qml`. Find the "── Calibration ──" SectionCard (around lines 773-874). Inside it, replace the existing `RowLayout` (lines 777-854) so the first three children become the two ActionButtons and the combo, in this order:

```qml
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

                        Rectangle {
                            id: calibLight
                            width: 14
                            height: 14
                            radius: 7
                            border.width: 1
                            border.color: root.colBorderSoft
                            color: {
                                switch (MOTIONInterface.calibrationStatus) {
                                case "running": return "#2196F3"
                                case "passed":  return "#4CAF50"
                                case "failed":  return "#F44336"
                                case "aborted": return "#FF9800"
                                default:        return "#9E9E9E"
                                }
                            }
                        }

                        TextArea {
                            id: calibStatusLabel
                            Layout.fillWidth: true
                            readOnly: true
                            selectByMouse: false
                            activeFocusOnTab: false
                            background: null
                            padding: 0
                            wrapMode: TextEdit.Wrap
                            color: root.colTextPri
                            font.pixelSize: 13
                            text: {
                                switch (MOTIONInterface.calibrationStatus) {
                                case "running":
                                    return "Calibrating... (" + calibTimer.elapsedSec
                                           + "s / " + MOTIONInterface.maxCalibrationTimeSec + "s)"
                                case "passed":  return "Calibration Passed"
                                case "failed":
                                    var reason = MOTIONInterface.calibrationFailureReason
                                    return reason
                                        ? "Calibration Failed — " + reason
                                        : "Calibration Failed"
                                case "aborted": return "Calibration Aborted"
                                default:        return ""
                                }
                            }
                        }
                    }
```

The two `ActionButton` blocks (Calibrate + Test) replace the single previous `runCalibrationButton`. The combo, indicator light, and `TextArea` are preserved unchanged (they still describe Calibrate state — the Test Results window has its own status display).

Then, immediately below the existing `Connections { target: MOTIONInterface; function onCalibrationStateChanged() {...} }` block (around line 866-873), add a sibling block:

```qml
                    Connections {
                        target: MOTIONInterface
                        function onTestScanStateChanged() {
                            var s = MOTIONInterface.testScanStatus
                            if (s === "running" || s === "done"
                                || s === "failed" || s === "aborted") {
                                testResultsWindow.show()
                                testResultsWindow.raise()
                                testResultsWindow.requestActivate()
                            }
                        }
                    }
```

(`testResultsWindow` is the `id` we'll assign in `main.qml` — Step 3 below.)

- [ ] **Step 2: Update `_anyInProgress` + `_inProgressLabel` in `main.qml`**

Open `main.qml`. Find the `_anyInProgress` property (lines 22-26). Add `MOTIONInterface.testScanRunning`:

```qml
    readonly property bool _anyInProgress:
        bloodFlowPage.scanning ||
        bloodFlowPage.configuring ||
        bloodFlowPage.checkRunning ||
        MOTIONInterface.calibrationRunning ||
        MOTIONInterface.testScanRunning
```

Then find `_inProgressLabel` (lines 37-45). Insert a `Test scan` branch after the `calibrationRunning` branch and before the `scanning` branch:

```qml
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

- [ ] **Step 3: Instantiate the `TestResultsWindow` at the top level**

In `main.qml`, find the closing `}` of the outermost `ApplicationWindow`. Immediately before it, add:

```qml
    TestResultsWindow {
        id: testResultsWindow
    }
```

This is the QML idiom for instantiating a sibling top-level `Window` from inside an `ApplicationWindow`: the QML engine handles the second window as a stand-alone OS-level window with its own native frame.

- [ ] **Step 4: Smoke-launch the app**

```powershell
cd C:\Users\ethan\Projects\openmotion-bloodflow-app
python main.py
```

Confirm:

1. No QML parse errors at startup.
2. Open Settings → Calibration. See two side-by-side buttons: "Calibrate" and "Test", with the combo to their right.
3. Don't actually click them (no hardware in the loop) — close the app.

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
git add components/SettingsModal.qml main.qml
git commit -m "feat(bloodflow-app): split Calibrate / Test buttons; wire results window (#132)"
```

---

## Task 11 — App: change `calibration_scan_duration_sec` default to 15

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-bloodflow-app/config/app_config.json`
- Modify (if it has a defaults dict): `C:/Users/ethan/Projects/openmotion-bloodflow-app/main.py`

- [ ] **Step 1: Update the config — bump Calibrate duration and add Test duration key**

Open `config/app_config.json`. Find the line `"calibration_scan_duration_sec": 5,` (around line 83). Change `5` to `15`:

```json
"calibration_scan_duration_sec": 15,
```

Then, on the very next line after `"calibration_scan_duration_sec"`, insert the new Test key:

```json
"test_scan_duration_sec": 5,
```

The two lines together should read:

```json
"calibration_scan_duration_sec": 15,
"test_scan_duration_sec": 5,
```

- [ ] **Step 2: Update the defaults dict in `main.py` if one exists**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
grep -n "calibration_scan_duration_sec" main.py
```

If a match exists in a defaults dict (look for the `_load_app_config` pattern documented in the spec for #122), change its default to `15` to keep behavior consistent when the config file is missing the key.

- [ ] **Step 3: Validate JSON syntax**

```powershell
python -c "import json; json.load(open(r'C:\Users\ethan\Projects\openmotion-bloodflow-app\config\app_config.json')); print('OK')"
```

Expected: `OK`.

- [ ] **Step 4: Confirm both keys parse correctly**

```powershell
cd C:\Users\ethan\Projects\openmotion-bloodflow-app
python -c "import json; cfg = json.load(open('config/app_config.json')); print('calibration:', cfg['calibration_scan_duration_sec'], 'test:', cfg['test_scan_duration_sec'])"
```

Expected: `calibration: 15 test: 5`

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
git add config/app_config.json main.py
git commit -m "config(bloodflow-app): bump calibration to 15s, add test_scan_duration_sec (#132)"
```

(If `main.py` had no relevant default to update, drop it from the `git add`.)

---

## Task 12 — Optional UI test (pytest-qt) for the new buttons + Test Results window

**Files:**
- New: `C:/Users/ethan/Projects/openmotion-bloodflow-app/tests/test_test_results_ui.py`

**Skip this task** if `pytest-qt` UI tests aren't already in the project's CI loop. The unit tests from Task 8 plus the manual bench verification (Task 13) cover the feature.

If pytest-qt is in the loop:

- [ ] **Step 1: Look at the existing calibration UI test for the pattern**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
ls tests/test_calibration_ui*.py
```

Read the file(s) — note how they construct the engine, load `main.qml`, find QML items by `objectName` / `id`, and drive clicks.

- [ ] **Step 2: Write the test**

(The exact shape depends heavily on the project's existing harness — copy from `test_calibration_ui*.py` and adapt with these assertions.)

```python
"""UI smoke test for the Calibrate/Test button split and Test Results
window (#132). Mocks MOTIONInterface.start_test_scan to immediately
complete with a synthetic result; verifies the popup window opens with
the right table contents and that the Copy button TSV-ifies correctly.
"""
# Adapt the harness setup from tests/test_calibration_ui*.py — fixture
# names, engine bring-up, etc. — and assert against:
#
# 1. SettingsModal.qml renders both "Calibrate" and "Test" buttons.
# 2. Clicking "Test" calls MOTIONInterface.start_test_scan.
# 3. When _on_test_scan_complete fires with synthetic rows, the
#    testResultsWindow becomes visible.
# 4. The table has 12 header columns matching the spec.
# 5. Clicking "Copy" emits a TSV string through MOTIONInterface.copyToClipboard
#    (mock the slot at construction time and capture the argument).
```

- [ ] **Step 3: Run the test**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
pytest tests/test_test_results_ui.py -v
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
git add tests/test_test_results_ui.py
git commit -m "test(bloodflow-app): pytest-qt coverage for Calibrate/Test split (#132)"
```

---

## Task 13 — Manual bench verification

After all the above land on the feature branch, do this end-to-end check on actual hardware.

- [ ] **1. Launch the app**

```powershell
cd C:\Users\ethan\Projects\openmotion-bloodflow-app
python main.py
```

- [ ] **2. Verify both buttons are visible**

Open Settings → scroll to Calibration. Expect:
- Two side-by-side buttons: **Calibrate** and **Test**.
- Calibration target combo (Both / Left / Right) to their right.
- Existing indicator light + status TextArea unchanged.

- [ ] **3. Run a Test scan against a calibration phantom**

Click **Test** with the phantom seated. Expect:
- "Test scan: starting…" in the capture log.
- After ~1 s of flash + 16 s of scan + ~1 s of evaluate, the Test Results window opens.
- Header strip reads "PASS" in green (or "FAIL — too much ambient light — …" with dev mode on if you forced a failure).
- Table has one row per active camera, with finite Light Mean, Dark Mean, Contrast values and PASS/FAIL per cell.

- [ ] **4. Click Copy, paste into a text editor**

Confirm:
- Header row: `Side\tCam\tLightMean\tMinMean\tMeanPF\tDarkMean\tMaxDark\tDarkPF\tContrast\tMinContrast\tContrastPF\tOverall`
- Data rows tab-separated, one per camera. Numbers formatted as in the spec (2 decimals for means, 5 for contrast, 4 for min_contrast).

- [ ] **5. Close the Test Results window mid-rerun**

Click **Test** again. Close the window before the scan completes. Expect: scan completes anyway (no cancellation), `test-{ts}.csv` appears in the calibrations directory, and the window auto-reopens via the state change on completion.

- [ ] **6. Run a Calibrate after the Test**

Click **Calibrate** with the same phantom. Expect:
- Phase 1 takes ~15 s (visibly longer than today's 5 s).
- After all five phases, calibration status reads "Calibration Passed".
- Inspect `calibration-{ts}.json` in the configured `calibrations/` directory: `"request"."duration_sec": 15`. The `dark` values per row look the same as the most recent test scan's `dark` values (same hardware, same target).

- [ ] **7. Check mutual exclusion**

Start a Calibrate. While running, attempt to click Test → button is disabled. Vice versa from a Test scan in flight.

- [ ] **8. Check the close-while-busy guard**

Start a Test scan. Click the app's close X. Expect a toast reading "Test scan in progress" (or whichever wording `_inProgressLabel` formats). Click X again within 5 s to confirm exit.

- [ ] **9. Force a Test failure (optional)**

Edit `config/app_config.json` to set `"ft_max_dark_per_camera": [0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]`. Restart the app. Click Test. Expect:
- Header strip reads "FAIL — …" in red.
- One or more rows show Dark PF = FAIL.
- "Overall" column for those rows reads FAIL.
- Dev-mode breakdown (if enabled) starts with "too much ambient light".

Restore the file to `[3.0]*8` afterwards.

---

## Repo handoff after implementation

- SDK feature branch `feature/132-test-scan` lives in `openmotion-sdk`. Open a PR against that repo's `next` (or whatever branch the bloodflow app's dev tags pull from; verify against the SDK's branch policy).
- Bloodflow-app feature branch `feature/132-separate-calibrate-test-buttons` (already exists, holds the design spec + this plan) gets Tasks 7-13's commits. Once both branches are reviewed/merged in their respective repos, the bloodflow app's pinned SDK version (if any) may need a bump — check `setup.py` / `pyproject.toml`.
- Companion `next-next` branch handling (Milestone 1.1.2 release flow) is the user's later step; this plan stops at the feature branch.

---

## Self-review (run before handing the plan off)

Spec coverage:

- R1 — Task 10 (two buttons in SettingsModal).
- R2 — Tasks 1 + 2 (calibration uses average_full_scan on phase 1).
- R3 — Task 4 (start_test_scan runs phase 1 only, no calibration write).
- R4 — Tasks 9 + 10 (Qt Window, non-modal, single instance, signal-driven show).
- R5 — Task 9 (12-column table; BFI/BVI omitted; Overall column).
- R6 — Task 9 (Copy button → MOTIONInterface.copyToClipboard, TSV format).
- R7 — Tasks 9 + 10 (Close button; closing does not cancel; state-change signal reopens on rerun).
- R8 — Tasks 7 + 10 (buttons disabled while running; main.qml busy state extended).
- R9 — Task 8 (runTestScan emits captureLog on refusal).
- R10 — Task 11 (config bump `calibration_scan_duration_sec` to 15 s; add `test_scan_duration_sec: 5`).
- R11 — Task 7 (testScanRunning / testScanStatus / testScanRows / testScanFailureReason; `_test_scan_duration_sec` read from config).
- R12 — Tasks 3 + 4 (TestScanResult + start_test_scan).
- R13 — Task 8 (set_laser_power_from_config call same as runCalibration).
- R14 — Task 4 (max_calibration_time_sec watchdog reused).

Open question carve-outs:

- The Open Questions section of the spec lists 15 assumptions for user sanity-check before this plan executes. The plan implements them as written. If any open question lands differently, the affected tasks need a delta — most often Task 4 (which forces `average_full_scan=True` for Test) and Task 11 (the duration bump).
