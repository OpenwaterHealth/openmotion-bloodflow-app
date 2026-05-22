# Ambient-Light Check in FT Calibration — Design Spec

**Date:** 2026-05-12
**Issue:** openmotion-bloodflow-app#122
**Feature:** Add a per-camera dark-frame mean threshold to the FT (final test) calibration so that a calibration run fails when any targeted camera's dark-frame intensity exceeds a configurable cap. This catches three operational failure modes today's calibration cannot distinguish: (a) sensor not seated on the calibration target, (b) the bench room is too bright, and (c) any other ambient-light contamination. The check is intentionally gross — a single max-DN threshold per camera — because the system has limited ability to sense these conditions otherwise.

---

## Background

FT calibration is the bloodflow app's per-unit acceptance step. The connector ships a `CalibrationRequest` (with `CalibrationThresholds`) to the SDK; the SDK runs a calibration scan, computes per-camera `mean`, `avg_contrast`, `bfi`, `bvi`, and applies pass/fail gates from the thresholds. Today's gates are: `min_mean`, `min_contrast`, `min_bfi`, `max_bfi`, `min_bvi`, `max_bvi`. The overall calibration is PASS only when every targeted camera passes every test.

The firmware's calibration sequence emits leading and trailing dark frames (laser off) around the main capture window. The SDK already trims these out of the BFI/BVI averages (`omotion/CalibrationWorkflow.py:847-848`) — i.e. they're acquired and identified, just discarded. Because the laser is off, the mean intensity in those frames is purely ambient light.

The contact-quality (CQ) machinery in the connector already runs a similar dark-frame ambient check during live scans, gated by `cq_dark_threshold_per_camera` (default 3.0 DN, motion_connector.py:78-80). It is intentionally separate from this work — different scan, different acceptance criteria — but the underlying signal and threshold defaults are the same.

---

## Requirements

| # | Requirement |
|---|-------------|
| R1 | New per-camera threshold `ft_max_dark_per_camera` in `app_config.json`, default `[3.0]*8`. |
| R2 | During FT calibration, average each targeted camera's mean intensity across the leading + trailing dark frames already in the firmware-emitted calibration sequence. |
| R3 | A camera passes the dark test when `dark_mean ≤ ft_max_dark_per_camera[cam_id]`. |
| R4 | A camera that fails the dark test fails overall. Calibration is PASS only when every targeted camera passes every test (now five: mean, contrast, BFI, BVI, dark). |
| R5 | Cameras outside the calibration mask report `dark_test = "NA"` and do not affect `passed` (matches existing convention for `mean_test`, `contrast_test`, etc.). |
| R6 | If `ft_max_dark_per_camera` is missing from `app_config.json`, the SDK marks `dark_test = "NA"` for every row and does not gate `passed` on it. Older configs keep working unchanged. |
| R7 | Developer-mode failure breakdown must explicitly include the phrase **"too much ambient light"** when at least one camera failed the dark test. |
| R8 | Per-row CSV/JSON output of the calibration result must include the measured `dark` value (DN) and the `dark_test` verdict, so post-hoc analysis can see the dark reading even on passing runs. |
| R9 | No behavior change for the validation scan in this iteration — the dark check applies only to the calibration scan. |

---

## Architecture

### `config/app_config.json`

Add one key, same shape as the existing `ft_*_per_camera` keys:

```json
"ft_max_dark_per_camera": [3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0]
```

### `motion_connector.py`

Load alongside the existing `ft_*` loaders (~line 408):

```python
ft_dark = cfg.get("ft_max_dark_per_camera")
self._ft_max_dark_per_camera = (
    list(ft_dark) if isinstance(ft_dark, (list, tuple)) else None
)
```

Pass to `CalibrationThresholds` in `runCalibration` (~line 3401):

```python
thresholds = CalibrationThresholds(
    min_mean_per_camera=...,
    min_contrast_per_camera=...,
    min_bfi_per_camera=...,
    min_bvi_per_camera=...,
    max_bfi_per_camera=...,
    max_bvi_per_camera=...,
    max_dark_per_camera=(
        list(self._ft_max_dark_per_camera)
        if self._ft_max_dark_per_camera is not None else None
    ),
)
```

Extend the dev-mode failure breakdown (motion_connector.py:3498-3506):

```python
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

### SDK `omotion/CalibrationWorkflow.py`

Extend `CalibrationThresholds`:

```python
@dataclass
class CalibrationThresholds:
    min_mean_per_camera: list[float]
    min_contrast_per_camera: list[float]
    min_bfi_per_camera: list[float]
    min_bvi_per_camera: list[float]
    max_bfi_per_camera: Optional[list[float]] = None
    max_bvi_per_camera: Optional[list[float]] = None
    max_dark_per_camera: Optional[list[float]] = None   # NEW
```

Extend `CalibrationResultRow`:

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
    dark: float                # NEW — mean DN of the trimmed dark frames
    mean_test: str
    contrast_test: str
    bfi_test: str
    bvi_test: str
    dark_test: str             # NEW — "PASS" | "FAIL" | "NA"
    security_id: str
    hwid: str
```

In `_compute_calibration_from_samples` (and the helper that builds rows from samples): when the function identifies the leading/trailing dark frames for trimming, additionally compute the per-camera mean of those frames' `mean_dn` and stash it on the row as `dark`. Apply the new test:

```python
if thresholds.max_dark_per_camera is None:
    row.dark_test = "NA"
elif not _camera_active(camera_mask, row.cam_id):
    row.dark_test = "NA"
elif row.dark > thresholds.max_dark_per_camera[row.cam_id]:
    row.dark_test = "FAIL"
else:
    row.dark_test = "PASS"
```

The aggregate `passed` boolean already AND-s the per-row test bools; thread `dark_test != "FAIL"` through that gate. Per-camera tests that are `"NA"` do not contribute to `passed` (existing convention).

### CSV / JSON output

The per-row CSV writer already emits each row's fields; adding `dark` and `dark_test` to the dataclass picks them up automatically if the writer iterates over `dataclasses.fields(...)`. If it spells the columns out explicitly, add `dark` and `dark_test` to that list. Same for `_calibration_to_dict`.

---

## Data Flow

```
runCalibration (connector)
  └─ build CalibrationThresholds with max_dark_per_camera=...
  └─ start_calibration(req)

CalibrationWorkflow (SDK)
  └─ run calibration scan
       └─ firmware emits: [dark frames] [light frames] [dark frames]
  └─ _compute_calibration_from_samples(samples)
       ├─ identify leading + trailing dark frames per camera (existing logic)
       ├─ for each camera: dark = mean(mean_dn over those frames)        ← NEW
       ├─ for each camera: existing mean/contrast/BFI/BVI from light frames
       └─ build CalibrationResultRow including dark + dark_test
  └─ apply thresholds → row.*_test fields
  └─ aggregate row.passed bools → overall result.passed

_on_calibration_complete (connector)
  └─ if not passed and developerMode:
       └─ breakdown includes "ambient" when dark_test fails
       └─ prefix "too much ambient light — " when any dark fail present
```

---

## Edge Cases

- **Threshold missing from config (backward compat):** connector passes `max_dark_per_camera=None`. SDK marks every row's `dark_test = "NA"`, does not gate `passed`. Operator sees no behavior change.
- **Camera not in calibration mask:** existing convention — `dark_test = "NA"`, does not affect `passed`.
- **Zero dark frames identified for an active camera:** shouldn't happen given the firmware sequence, but defensive: set `dark = NaN`, `dark_test = "FAIL"` so the anomaly surfaces rather than silently passing. Worth a debug-level log line for diagnosis.
- **All cameras fail dark, none of the other tests fail:** still results in `passed = False` with the dev-mode message reading e.g. `"too much ambient light — L1:ambient; L2:ambient; ..."`.
- **Mixed failures (some cameras dark-fail, others fail on BFI):** dev-mode prefix `"too much ambient light — "` appears once, then the breakdown lists each camera's failed tests. Easy to read at a glance.

---

## Testing

### SDK unit test — `openmotion-sdk/tests/test_calibration_workflow_compute.py`

Mirror the existing per-threshold test cases:

1. `test_dark_test_pass_when_below_threshold` — synthetic samples where dark frames have mean_dn = 1.0, threshold = 3.0. Assert every row has `dark_test == "PASS"` and overall `passed == True`.
2. `test_dark_test_fail_when_above_threshold` — synthetic samples where one camera's dark frames have mean_dn = 5.0, threshold = 3.0. Assert that camera's `dark_test == "FAIL"` and overall `passed == False`.
3. `test_dark_test_na_when_threshold_missing` — pass `max_dark_per_camera=None`. Assert every row has `dark_test == "NA"` and `passed` is governed only by the other tests.
4. `test_dark_test_na_for_masked_out_camera` — camera_mask excludes camera 3; assert row[3].dark_test == "NA" regardless of dark value.
5. `test_dark_value_recorded_on_passing_run` — even when calibration passes, every row's `dark` field is populated with the computed mean.

### Bloodflow-app side

The connector's role is plumbing: loading the config key, passing it through, and formatting the dev-mode breakdown. No new unit test required if the SDK tests cover the compute logic. The dev-mode breakdown formatting could be unit-tested with the existing `pytest.mark.unit` pattern using synthetic `CalibrationResultRow` lists, but it's optional — a one-line manual check on a real failing calibration confirms the wording.

### Manual verification

1. Set `ft_max_dark_per_camera` to a low value (e.g. `[0.5]*8`), run calibration on a normal bench setup — expect FAIL with dev-mode message containing `"too much ambient light — "`.
2. Restore default `[3.0]*8`, run on a bright bench (lights on, sensor off the target) — expect FAIL with the same dev-mode wording.
3. Restore default, run normally — expect PASS, with non-NaN `dark` values visible in the calibration CSV/JSON for every row.

---

## Implementation Order

The change is small but spans two repos; do it in the order that keeps each step shippable on its own.

1. **SDK first**: add `max_dark_per_camera` to `CalibrationThresholds`, `dark` + `dark_test` to `CalibrationResultRow`, the compute logic, the pass/fail thread, and the SDK unit tests. Publish a new SDK wheel.
2. **Bloodflow-app**: load the config key, pass it through, extend the dev-mode breakdown. Bump the SDK dependency.
3. **Config**: add `ft_max_dark_per_camera` to `config/app_config.json`. This is the activation switch — without it, the SDK still marks NA and behavior is unchanged.

---

## YAGNI / Out of Scope

- **Validation scan dark check.** Out of scope; calibration scan only.
- **Min-dark check.** A "dark too low" (= dead camera or stuck pixel) might be useful, but `ft_min_mean` already covers dead-camera detection on the light frames. Not adding a min-dark gate.
- **Live dark-frame monitoring during calibration.** The check runs after sample collection completes, not continuously. Aborting mid-scan on a bad dark reading would need separate firmware/SDK coordination.
- **Per-side aggregate (instead of per-camera) thresholds.** Per-camera is consistent with the existing `ft_*_per_camera` keys and gives operators per-camera diagnostics.
- **Soft-warning mode.** Hard pass/fail only; the operator must address the ambient condition and rerun.
