# Ambient-Light FT Calibration Check — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-camera `ft_max_dark_per_camera` threshold to the FT calibration so a calibration run fails when any targeted camera's mean intensity in the firmware-emitted dark frames exceeds the cap. Devmode breakdown says "too much ambient light".

**Architecture:** The SDK's `_run_subscan_capture` already collects corrected samples and currently drops those outside the averaging window (the firmware's leading + trailing dark frames). The filter is widened to also capture those out-of-window samples into a separate `dark_samples` list. `_build_result_rows_from_samples` gains an optional `dark_samples` arg, computes per-camera dark means, and produces a new `dark_test` PASS/FAIL/NA. `evaluate_passed` ANDs in the new test. The bloodflow-app loads `ft_max_dark_per_camera` from `app_config.json`, passes it through `CalibrationThresholds`, and extends the dev-mode failure breakdown.

**Tech Stack:** Python 3.12, dataclasses, pytest. Two repos: `openmotion-sdk` (calibration pipeline + unit tests) and `openmotion-bloodflow-app` (connector + dev-mode UI + config).

**Spec:** `openmotion-bloodflow-app/docs/superpowers/specs/2026-05-12-ambient-light-ft-calibration-design.md`

---

## Repo / branch setup

This work spans two repos. Use feature branches in each:

- `openmotion-sdk`: branch off the SDK's default branch (verify with `git status` in `C:/Users/ethan/Projects/openmotion-sdk` first — most likely `main`). Name: `feature/122-ft-max-dark-per-camera`.
- `openmotion-bloodflow-app`: branch already exists at `feature/122-ambient-light-ft-calibration` off `next` and contains the design spec. Continue on that branch.

Install the SDK locally in editable mode so app-side changes pick up SDK edits without a wheel rebuild:

```powershell
cd C:\Users\ethan\Projects\openmotion-sdk
pip install -e .
```

---

## Task 1 — SDK: add `max_dark_per_camera` to `CalibrationThresholds`

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/omotion/CalibrationWorkflow.py` (around line 60-66, the `CalibrationThresholds` dataclass)
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/tests/test_calibration_workflow_compute.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration_workflow_compute.py` after the existing `test_thresholds_lengths_are_eight`:

```python
def test_thresholds_max_dark_defaults_to_none():
    t = _thresholds()
    assert t.max_dark_per_camera is None


def test_thresholds_max_dark_accepts_list():
    t = CalibrationThresholds(
        min_mean_per_camera=[100.0] * 8,
        min_contrast_per_camera=[0.2] * 8,
        min_bfi_per_camera=[3.0] * 8,
        min_bvi_per_camera=[3.0] * 8,
        max_dark_per_camera=[3.0] * 8,
    )
    assert t.max_dark_per_camera == [3.0] * 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/ethan/Projects/openmotion-sdk && pytest tests/test_calibration_workflow_compute.py::test_thresholds_max_dark_defaults_to_none tests/test_calibration_workflow_compute.py::test_thresholds_max_dark_accepts_list -v`
Expected: `TypeError: __init__() got an unexpected keyword argument 'max_dark_per_camera'`.

- [ ] **Step 3: Add the field**

Edit `omotion/CalibrationWorkflow.py`, replace the `CalibrationThresholds` dataclass (around line 60) with:

```python
@dataclass
class CalibrationThresholds:
    min_mean_per_camera: list[float]
    min_contrast_per_camera: list[float]
    min_bfi_per_camera: list[float]
    min_bvi_per_camera: list[float]
    max_bfi_per_camera: Optional[list[float]] = None
    max_bvi_per_camera: Optional[list[float]] = None
    max_dark_per_camera: Optional[list[float]] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd C:/Users/ethan/Projects/openmotion-sdk && pytest tests/test_calibration_workflow_compute.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git checkout -b feature/122-ft-max-dark-per-camera
git add omotion/CalibrationWorkflow.py tests/test_calibration_workflow_compute.py
git commit -m "feat(sdk): add max_dark_per_camera to CalibrationThresholds (#122)"
```

---

## Task 2 — SDK: add `dark` + `dark_test` fields to `CalibrationResultRow` and thread through `evaluate_passed`

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/omotion/CalibrationWorkflow.py` (`CalibrationResultRow` around line 103-118, `evaluate_passed` around line 373-382)
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/tests/test_calibration_workflow_compute.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration_workflow_compute.py`:

```python
def _row(*, dark_test="NA", dark=0.0, mean_test="PASS",
         contrast_test="PASS", bfi_test="PASS", bvi_test="PASS"):
    return CalibrationResultRow(
        camera_index=0, side="left", cam_id=0,
        mean=100.0, avg_contrast=0.3, bfi=4.0, bvi=4.0, dark=dark,
        mean_test=mean_test, contrast_test=contrast_test,
        bfi_test=bfi_test, bvi_test=bvi_test, dark_test=dark_test,
        security_id="", hwid="",
    )


def test_result_row_has_dark_fields():
    r = _row(dark=1.5, dark_test="PASS")
    assert r.dark == 1.5
    assert r.dark_test == "PASS"


def test_evaluate_passed_all_pass_including_dark():
    assert evaluate_passed([_row(dark_test="PASS")]) is True


def test_evaluate_passed_dark_fail_overrides_all_other_pass():
    assert evaluate_passed([_row(dark_test="FAIL")]) is False


def test_evaluate_passed_dark_na_does_not_gate():
    assert evaluate_passed([_row(dark_test="NA")]) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/ethan/Projects/openmotion-sdk && pytest tests/test_calibration_workflow_compute.py -v -k "dark or evaluate_passed"`
Expected: `TypeError: __init__() got an unexpected keyword argument 'dark'` (and similar).

- [ ] **Step 3: Add the fields and thread the test bool**

Edit `omotion/CalibrationWorkflow.py`. Replace `CalibrationResultRow` (around line 103-118) with:

```python
@dataclass
class CalibrationResultRow:
    camera_index: int
    side: str
    cam_id: int
    mean: float
    avg_contrast: float
    bfi: float
    bvi: float
    dark: float
    mean_test: str
    contrast_test: str
    bfi_test: str
    bvi_test: str
    dark_test: str
    security_id: str
    hwid: str
```

Replace `evaluate_passed` (around line 373-382) with:

```python
def evaluate_passed(rows: list[CalibrationResultRow]) -> bool:
    if not rows:
        return False
    return all(
        r.mean_test == "PASS"
        and r.contrast_test == "PASS"
        and r.bfi_test == "PASS"
        and r.bvi_test == "PASS"
        and r.dark_test != "FAIL"
        for r in rows
    )
```

(Note `dark_test != "FAIL"` rather than `== "PASS"` so `"NA"` is permissive — consistent with the spec's "missing threshold or masked-out camera doesn't gate `passed`" requirement.)

- [ ] **Step 4: Run all SDK calibration-compute tests to verify**

Run: `cd C:/Users/ethan/Projects/openmotion-sdk && pytest tests/test_calibration_workflow_compute.py -v`
Expected: all pass (existing tests still pass because the new fields have no default — every `CalibrationResultRow` construction needs updating; if any existing test breaks, update its constructor call to include `dark=0.0, dark_test="NA"`).

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git add omotion/CalibrationWorkflow.py tests/test_calibration_workflow_compute.py
git commit -m "feat(sdk): add dark + dark_test fields to CalibrationResultRow (#122)"
```

---

## Task 3 — SDK: capture dark samples in `_run_subscan_capture`

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/omotion/CalibrationWorkflow.py` (`_run_subscan_capture` around line 678-769)
- No new tests in this task — `_run_subscan_capture` requires a live hardware connection so it's exercised end-to-end via HIL tests, not unit-tested in `test_calibration_workflow_compute.py`. The unit-level behavior of "compute dark mean from dark samples" lands in Task 4.

- [ ] **Step 1: Widen the filter to also capture out-of-window samples**

Edit `_run_subscan_capture` (around line 687-769). Change the return type and the filter block:

```python
def _run_subscan_capture(
    interface,
    request: CalibrationRequest,
    *,
    subject_id: str,
    duration_sec: int,
    skip_leading_frames: int,
    frame_window_count: int,
    stop_evt: threading.Event,
) -> tuple[str, str, list[Sample], list[Sample]]:
    """Submit a ScanRequest and capture corrected samples in-memory as
    the science pipeline emits them.

    The scan still writes its raw histogram CSV to disk (`write_raw_csv=True`)
    so operators retain the artifact for later verification, but we
    don't re-parse it — corrected samples are captured live via
    ``on_corrected_batch_fn``. This avoids running the science pipeline
    twice on the same data.

    Returns ``(left_path, right_path, captured_samples, dark_samples)``.
    ``captured_samples`` is the in-window averaging set (laser-on, the
    historical return). ``dark_samples`` is the leading + trailing
    out-of-window samples (laser-off; mean = ambient light per camera).
    Raises ``RuntimeError`` on scan failure. Honors ``stop_evt`` by
    calling ``cancel_scan`` and returning empty paths + empty lists.
    """
    scan_req = ScanRequest(
        subject_id=subject_id,
        duration_sec=duration_sec,
        left_camera_mask=request.left_camera_mask,
        right_camera_mask=request.right_camera_mask,
        data_dir=request.output_dir,
        disable_laser=False,
        write_raw_csv=True,
        write_corrected_csv=False,
        write_telemetry_csv=False,
        reduced_mode=False,
    )

    upper_bound = skip_leading_frames + int(frame_window_count)
    captured: list[Sample] = []
    dark: list[Sample] = []

    def _on_corrected_batch(batch: CorrectedBatch) -> None:
        for s in batch.samples:
            if s.absolute_frame_id < skip_leading_frames:
                dark.append(s)
            elif s.absolute_frame_id >= upper_bound:
                dark.append(s)
            else:
                captured.append(s)
```

- [ ] **Step 2: Update the function's two return statements**

Same function. Two places that return:

Replace the cancel-path return (around line 748):
```python
            return "", "", [], []
```

Replace the final return at the end of the function (around line 769):
```python
    captured.sort(key=lambda s: (s.side, s.cam_id, s.absolute_frame_id))
    dark.sort(key=lambda s: (s.side, s.cam_id, s.absolute_frame_id))
    return res.left_path or "", res.right_path or "", captured, dark
```

- [ ] **Step 3: Update the two callers of `_run_subscan_capture`**

Around line 964-971 (phase 1, calibration scan):

```python
                cal_left, cal_right, cal_samples, cal_dark_samples = _run_subscan_capture(
                    self._interface, request,
                    subject_id=f"calib1_{request.operator_id}",
                    duration_sec=request.duration_sec + request.scan_delay_sec,
                    skip_leading_frames=skip_frames,
                    frame_window_count=window_frames,
                    stop_evt=self._stop_evt,
                )
```

Around line 1034-1041 (phase 4, validation scan):

```python
                val_left, val_right, val_samples, _val_dark_samples = _run_subscan_capture(
                    self._interface, request,
                    subject_id=f"calib2_{request.operator_id}",
                    duration_sec=request.duration_sec + request.scan_delay_sec,
                    skip_leading_frames=skip_frames,
                    frame_window_count=window_frames,
                    stop_evt=self._stop_evt,
                )
```

The validation-scan dark samples are deliberately discarded (`_val_dark_samples`) — the spec says the dark check applies only to the calibration scan.

- [ ] **Step 4: Run the SDK test suite to verify no regression**

Run: `cd C:/Users/ethan/Projects/openmotion-sdk && pytest tests/test_calibration_workflow_compute.py tests/test_calibration_workflow.py -v`
Expected: all pass. The unit tests don't exercise `_run_subscan_capture` itself (it needs hardware), but they will fail to import / collect if the file has a syntax error.

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git add omotion/CalibrationWorkflow.py
git commit -m "feat(sdk): _run_subscan_capture returns dark samples separately (#122)"
```

---

## Task 4 — SDK: compute per-camera dark mean and apply threshold in `_build_result_rows_from_samples`

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/omotion/CalibrationWorkflow.py` (`_build_result_rows_from_samples` around line 298-370, plus the call site that uses its output)
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/tests/test_calibration_workflow_compute.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_calibration_workflow_compute.py`:

```python
import math

from omotion.CalibrationWorkflow import (
    _build_result_rows_from_samples,
)
from omotion.MotionProcessing import Sample


def _sample(side, cam_id, *, mean=200.0, contrast=0.3, bfi=4.0, bvi=4.0,
            frame_id=0):
    # Construct a Sample matching the science-pipeline-corrected shape.
    # The dataclass / class signature may have evolved; if this helper
    # fails to construct, adjust the kwargs to whatever the current
    # Sample requires while preserving the field names referenced below.
    return Sample(
        side=side,
        cam_id=cam_id,
        mean=mean,
        std_dev=mean * contrast,
        contrast=contrast,
        bfi=bfi,
        bvi=bvi,
        absolute_frame_id=frame_id,
    )


def _full_thresholds(*, max_dark_per_camera=None):
    return CalibrationThresholds(
        min_mean_per_camera=[100.0] * 8,
        min_contrast_per_camera=[0.2] * 8,
        min_bfi_per_camera=[3.0] * 8,
        min_bvi_per_camera=[5.0] * 8,
        max_dark_per_camera=max_dark_per_camera,
    )


def test_dark_test_pass_when_below_threshold():
    # Light samples for camera 0, both sides
    light = [_sample(side, 0) for side in ("left", "right")]
    # Dark samples: mean = 1.0 (well below threshold 3.0)
    dark = [_sample(side, 0, mean=1.0, contrast=0.0) for side in ("left", "right")]
    rows = _build_result_rows_from_samples(
        light,
        dark_samples=dark,
        left_camera_mask=0x01, right_camera_mask=0x01,
        thresholds=_full_thresholds(max_dark_per_camera=[3.0] * 8),
        sensor_left=None, sensor_right=None,
    )
    assert all(r.dark_test == "PASS" for r in rows)
    assert all(r.dark == 1.0 for r in rows)


def test_dark_test_fail_when_above_threshold():
    light = [_sample("left", 0)]
    dark = [_sample("left", 0, mean=5.0)]  # 5.0 > 3.0
    rows = _build_result_rows_from_samples(
        light,
        dark_samples=dark,
        left_camera_mask=0x01, right_camera_mask=0x00,
        thresholds=_full_thresholds(max_dark_per_camera=[3.0] * 8),
        sensor_left=None, sensor_right=None,
    )
    assert rows[0].dark == 5.0
    assert rows[0].dark_test == "FAIL"


def test_dark_test_na_when_threshold_missing():
    light = [_sample("left", 0)]
    dark = [_sample("left", 0, mean=5.0)]
    rows = _build_result_rows_from_samples(
        light,
        dark_samples=dark,
        left_camera_mask=0x01, right_camera_mask=0x00,
        thresholds=_full_thresholds(max_dark_per_camera=None),
        sensor_left=None, sensor_right=None,
    )
    assert rows[0].dark_test == "NA"


def test_dark_value_present_on_passing_run():
    light = [_sample("left", 0)]
    dark = [_sample("left", 0, mean=2.0)]
    rows = _build_result_rows_from_samples(
        light,
        dark_samples=dark,
        left_camera_mask=0x01, right_camera_mask=0x00,
        thresholds=_full_thresholds(max_dark_per_camera=[3.0] * 8),
        sensor_left=None, sensor_right=None,
    )
    assert rows[0].dark == 2.0
    assert rows[0].dark_test == "PASS"


def test_dark_test_fail_when_no_dark_samples_for_active_camera():
    light = [_sample("left", 0)]
    rows = _build_result_rows_from_samples(
        light,
        dark_samples=[],
        left_camera_mask=0x01, right_camera_mask=0x00,
        thresholds=_full_thresholds(max_dark_per_camera=[3.0] * 8),
        sensor_left=None, sensor_right=None,
    )
    assert math.isnan(rows[0].dark)
    assert rows[0].dark_test == "FAIL"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/ethan/Projects/openmotion-sdk && pytest tests/test_calibration_workflow_compute.py -v -k dark`
Expected: failures — `_build_result_rows_from_samples` doesn't accept `dark_samples` yet.

- [ ] **Step 3: Update `_build_result_rows_from_samples` signature and body**

Edit `omotion/CalibrationWorkflow.py`. Replace `_build_result_rows_from_samples` (around line 298-370) with:

```python
def _build_result_rows_from_samples(
    samples: list[Sample],
    *,
    dark_samples: list[Sample] = None,
    left_camera_mask: int,
    right_camera_mask: int,
    thresholds: CalibrationThresholds,
    sensor_left,
    sensor_right,
) -> list[CalibrationResultRow]:
    """Core row aggregation: per-camera mean/contrast/BFI/BVI averages
    and threshold pass/fail. Pure function — caller pre-filters.

    ``dark_samples`` is the leading + trailing out-of-window samples
    from the calibration scan (laser-off; the per-camera mean of these
    is the ambient-light reading exposed as ``CalibrationResultRow.dark``
    and gated by ``thresholds.max_dark_per_camera``).
    """
    rows: list[CalibrationResultRow] = []
    masks = (left_camera_mask, right_camera_mask)
    sensors = (sensor_left, sensor_right)
    dark_samples = dark_samples or []

    for module_idx, side in enumerate(("left", "right")):
        mask = masks[module_idx]
        sensor = sensors[module_idx]
        for cam_id in range(CAMS_PER_MODULE):
            if not _camera_active(mask, cam_id):
                continue
            cam_samples = [
                s for s in samples
                if s.side == side and s.cam_id == cam_id
            ]
            if not cam_samples:
                continue   # silently drop — no data for this active cam

            mean_val = float(np.mean([s.mean for s in cam_samples]))
            contrast_val = float(np.mean([s.contrast for s in cam_samples]))
            bfi_val = float(np.mean([s.bfi for s in cam_samples]))
            bvi_val = float(np.mean([s.bvi for s in cam_samples]))

            cam_dark_samples = [
                s for s in dark_samples
                if s.side == side and s.cam_id == cam_id
            ]
            if cam_dark_samples:
                dark_val = float(np.mean([s.mean for s in cam_dark_samples]))
            else:
                dark_val = float("nan")

            if thresholds.max_dark_per_camera is None:
                dark_test = "NA"
            elif not cam_dark_samples:
                # Active camera but no dark frames captured — surface
                # as FAIL rather than silently passing.
                dark_test = "FAIL"
            elif cam_id >= len(thresholds.max_dark_per_camera):
                dark_test = "NA"
            else:
                cap = thresholds.max_dark_per_camera[cam_id]
                dark_test = "PASS" if dark_val <= float(cap) else "FAIL"

            security_id = ""
            hwid = ""
            if sensor is not None and hasattr(sensor, "get_cached_camera_security_uid"):
                try:
                    security_id = str(sensor.get_cached_camera_security_uid(cam_id) or "")
                except Exception:
                    security_id = ""
                try:
                    hwid = str(sensor.get_cached_hardware_id() or "")
                except Exception:
                    hwid = ""

            bfi_test = _combined_test(
                _threshold_test(bfi_val, thresholds.min_bfi_per_camera, cam_id),
                _threshold_max_test(bfi_val, thresholds.max_bfi_per_camera, cam_id),
            )
            bvi_test = _combined_test(
                _threshold_test(bvi_val, thresholds.min_bvi_per_camera, cam_id),
                _threshold_max_test(bvi_val, thresholds.max_bvi_per_camera, cam_id),
            )
            rows.append(CalibrationResultRow(
                camera_index=len(rows),
                side=side,
                cam_id=cam_id,
                mean=mean_val,
                avg_contrast=contrast_val,
                bfi=bfi_val,
                bvi=bvi_val,
                dark=dark_val,
                mean_test=_threshold_test(mean_val, thresholds.min_mean_per_camera, cam_id),
                contrast_test=_threshold_test(contrast_val, thresholds.min_contrast_per_camera, cam_id),
                bfi_test=bfi_test,
                bvi_test=bvi_test,
                dark_test=dark_test,
                security_id=security_id,
                hwid=hwid,
            ))

    return rows
```

- [ ] **Step 4: Update the call site that invokes `_build_result_rows_from_samples`**

Search the file for the call site that invokes this function inside the `CalibrationWorkflow` class. (It's downstream of the `_run_subscan_capture` call that produced `cal_samples` + `cal_dark_samples` in Task 3.) Add `dark_samples=cal_dark_samples` to the kwargs.

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
grep -n "_build_result_rows_from_samples" omotion/CalibrationWorkflow.py
```

The call should pass `dark_samples=cal_dark_samples` alongside the existing `samples=cal_samples` (or wherever the positional arg goes). Example shape:

```python
rows = _build_result_rows_from_samples(
    cal_samples,
    dark_samples=cal_dark_samples,
    left_camera_mask=request.left_camera_mask,
    right_camera_mask=request.right_camera_mask,
    thresholds=request.thresholds,
    sensor_left=self._interface.left,
    sensor_right=self._interface.right,
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd C:/Users/ethan/Projects/openmotion-sdk && pytest tests/test_calibration_workflow_compute.py -v`
Expected: all pass, including the five new dark-related tests.

- [ ] **Step 6: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git add omotion/CalibrationWorkflow.py tests/test_calibration_workflow_compute.py
git commit -m "feat(sdk): compute per-camera dark mean and gate calibration on it (#122)"
```

---

## Task 5 — SDK: surface `dark` and `dark_test` in CSV and JSON output

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/omotion/CalibrationWorkflow.py` (`_CSV_FIELDS` around line 385-390, `write_result_csv` and `_calibration_to_dict`)
- Modify: `C:/Users/ethan/Projects/openmotion-sdk/tests/test_calibration_workflow_compute.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration_workflow_compute.py`:

```python
import csv
import tempfile
from pathlib import Path


def test_write_result_csv_includes_dark_columns():
    rows = [_row(dark=1.5, dark_test="PASS")]
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "result.csv")
        write_result_csv(path, rows)
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            assert "dark" in reader.fieldnames
            assert "dark_test" in reader.fieldnames
            row0 = next(reader)
            assert float(row0["dark"]) == 1.5
            assert row0["dark_test"] == "PASS"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd C:/Users/ethan/Projects/openmotion-sdk && pytest tests/test_calibration_workflow_compute.py::test_write_result_csv_includes_dark_columns -v`
Expected: assertion failure — `"dark"` not in fieldnames.

- [ ] **Step 3: Add `dark` and `dark_test` to `_CSV_FIELDS`**

Edit `omotion/CalibrationWorkflow.py`, replace `_CSV_FIELDS` (around line 385-390) with:

```python
_CSV_FIELDS = [
    "camera_index", "side", "cam",
    "mean", "avg_contrast", "bfi", "bvi", "dark",
    "mean_test", "contrast_test", "bfi_test", "bvi_test", "dark_test",
    "security_id", "hwid",
]
```

Then find the body of `write_result_csv` (immediately below the constant) — it iterates over rows and writes a dict per row. Add `"dark": r.dark` and `"dark_test": r.dark_test` to the dict literal that's passed to `writer.writerow`. If the function uses `asdict()` or a similar dataclass dump, the new fields are picked up automatically; verify by reading the function.

- [ ] **Step 4: Run the CSV test to verify it passes**

Run: `cd C:/Users/ethan/Projects/openmotion-sdk && pytest tests/test_calibration_workflow_compute.py::test_write_result_csv_includes_dark_columns -v`
Expected: pass.

- [ ] **Step 5: Update `_calibration_to_dict`**

Search the file for `_calibration_to_dict` and any other serializer (around line 491-...). If it produces a list of row dicts, ensure `dark` and `dark_test` are in those dicts. If it uses `dataclasses.asdict()`, no change is needed — re-read the function to confirm.

If a change is required, add the same two keys; if not, add a brief comment noting that the dataclass-based serializer covers it automatically.

- [ ] **Step 6: Run full SDK compute test suite**

Run: `cd C:/Users/ethan/Projects/openmotion-sdk && pytest tests/test_calibration_workflow_compute.py -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-sdk
git add omotion/CalibrationWorkflow.py tests/test_calibration_workflow_compute.py
git commit -m "feat(sdk): include dark + dark_test in calibration CSV/JSON output (#122)"
```

---

## Task 6 — App: load `ft_max_dark_per_camera` and pass through `CalibrationThresholds`

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-bloodflow-app/motion_connector.py`
- Modify: `C:/Users/ethan/Projects/openmotion-bloodflow-app/main.py` (the `_load_app_config` defaults dict)

- [ ] **Step 1: Load the new config key in `motion_connector.py`**

Find the block at `motion_connector.py:408-415` that loads `ft_bfi`, `ft_bvi`, etc. Add the dark loader directly below the existing `ft_min_contrast` block (around line 405-406):

```python
        ft_dark = cfg.get("ft_max_dark_per_camera")
        self._ft_max_dark_per_camera = (
            list(ft_dark) if isinstance(ft_dark, (list, tuple)) else None
        )
```

- [ ] **Step 2: Pass it into `CalibrationThresholds` in `runCalibration`**

Find the `CalibrationThresholds(...)` construction at `motion_connector.py:3401-3414`. Add `max_dark_per_camera=...` as the final kwarg:

```python
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
```

- [ ] **Step 3: Add the new key to the defaults dict in `main.py:_load_app_config`**

Open `main.py` and locate `_load_app_config` (around line 67). Add `"ft_max_dark_per_camera": [3.0] * 8` to the `defaults` dict alongside the other `ft_*_per_camera` defaults — search the dict for `ft_min_mean_per_camera` to find the right neighborhood.

- [ ] **Step 4: Smoke-import the connector**

Run:
```powershell
cd C:\Users\ethan\Projects\openmotion-bloodflow-app
python -c "import motion_connector; print('OK')"
```
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
git add motion_connector.py main.py
git commit -m "feat(bloodflow-app): plumb ft_max_dark_per_camera through to CalibrationThresholds (#122)"
```

---

## Task 7 — App: dev-mode failure breakdown mentions "too much ambient light"

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-bloodflow-app/motion_connector.py` (around lines 3498-3506)

- [ ] **Step 1: Update the dev-mode breakdown**

Find `_on_calibration_complete` at `motion_connector.py:3478`. The dev-mode breakdown is at lines 3498-3506. Replace the inner block (the `if self._app_config.get("developerMode", False):` body, currently lines 3498-3506) with:

```python
            if self._app_config.get("developerMode", False):
                tests = (("mean", "mean_test"), ("contrast", "contrast_test"),
                         ("bfi", "bfi_test"), ("bvi", "bvi_test"),
                         ("ambient", "dark_test"))
                breakdown = "; ".join(
                    f"{'L' if r.side == 'left' else 'R'}{r.cam_id + 1}:"
                    f"{','.join(n for n, a in tests if getattr(r, a) == 'FAIL')}"
                    for r in result.rows
                    if any(getattr(r, a) == "FAIL" for _, a in tests)
                )
                if any(r.dark_test == "FAIL" for r in result.rows):
                    breakdown = f"too much ambient light — {breakdown}"
                self._calibration_failure_reason = breakdown
```

- [ ] **Step 2: Smoke-import the connector**

Run:
```powershell
cd C:\Users\ethan\Projects\openmotion-bloodflow-app
python -c "import motion_connector; print('OK')"
```
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
git add motion_connector.py
git commit -m "feat(bloodflow-app): dev-mode calibration failure says 'too much ambient light' (#122)"
```

---

## Task 8 — Config: add `ft_max_dark_per_camera` to `app_config.json`

**Files:**
- Modify: `C:/Users/ethan/Projects/openmotion-bloodflow-app/config/app_config.json`

- [ ] **Step 1: Add the key**

Open `config/app_config.json`. Find the existing `ft_*_per_camera` block (around lines 12-50). After the closing `]` of `ft_max_bvi_per_camera`, add a new key:

```json
  "ft_max_dark_per_camera": [
    3.0,
    3.0,
    3.0,
    3.0,
    3.0,
    3.0,
    3.0,
    3.0
  ],
```

(Match the formatting of the surrounding `ft_*_per_camera` blocks — one value per line, indentation, trailing comma if not the last key.)

- [ ] **Step 2: Validate JSON syntax**

Run:
```powershell
python -c "import json; json.load(open(r'C:\Users\ethan\Projects\openmotion-bloodflow-app\config\app_config.json')); print('OK')"
```
Expected: `OK`.

- [ ] **Step 3: Verify the connector reads the new key**

Run a quick sanity script:
```powershell
cd C:\Users\ethan\Projects\openmotion-bloodflow-app
python -c "import json; cfg = json.load(open('config/app_config.json')); print(cfg['ft_max_dark_per_camera'])"
```
Expected: `[3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0]`.

- [ ] **Step 4: Commit**

```bash
cd C:/Users/ethan/Projects/openmotion-bloodflow-app
git add config/app_config.json
git commit -m "config(bloodflow-app): add ft_max_dark_per_camera (default 3.0 DN/camera) (#122)"
```

---

## Final verification (manual, on the bench)

After all eight tasks land:

1. Launch the app from source: `python main.py` in `openmotion-bloodflow-app`.
2. Toggle Settings → Developer Mode on (or set `"developerMode": true` in `app_config.json`).
3. Run a calibration normally. Inspect the calibration CSV in the configured output directory — every row should now have non-empty `dark` and `dark_test` columns. Calibration should pass.
4. Temporarily lower the threshold by editing `config/app_config.json` to `"ft_max_dark_per_camera": [0.1, 0.1, ...]` (well below the bench's typical 1-2 DN). Restart the app and rerun calibration. Expect FAIL with the toast / status containing `too much ambient light — ...`.
5. Restore the threshold to `3.0` per camera. Aim a flashlight at the seated sensor, rerun. Expect FAIL with the same wording.
6. Remove all ambient sources, rerun. Expect PASS.

---

## Repo handoff after implementation

- SDK feature branch `feature/122-ft-max-dark-per-camera` lives in `openmotion-sdk`. Open a PR against that repo's default branch.
- Bloodflow-app feature branch `feature/122-ambient-light-ft-calibration` (already exists, holds the design spec) gets Task 6-8 commits. Once both branches are reviewed/merged in their respective repos, the bloodflow-app's pinned SDK version (if any) may need a bump — check `setup.py` / `pyproject.toml` if the SDK is pinned by version rather than path/editable install.
