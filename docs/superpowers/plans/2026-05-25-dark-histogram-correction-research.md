# Dark Histogram Correction Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained offline analysis experiment that compares current moment-based dark correction against full-histogram correction methods on the provided phantom scan.

**Architecture:** Keep the experiment isolated under `sandbox/dark-correction-research/` with one pure-Python algorithm module, one CLI/report runner, one sandbox README, and local unit tests. The code streams or chunks raw CSV input, writes only compact outputs, and never imports sandbox code from production modules.

**Tech Stack:** Python 3.13, `numpy`, `pandas`, `matplotlib`, `pytest`; existing raw CSV format from `processing/visualize_bloodflow.py`.

---

## File Structure

- Create `sandbox/dark-correction-research/README.md`: sandbox experiment metadata, run instructions, input/output expectations.
- Create `sandbox/dark-correction-research/dark_correction.py`: pure functions and dataclasses for histogram moments, frame indexing, dark interpolation, correction methods, deconvolution, and metrics.
- Create `sandbox/dark-correction-research/analyze_dark_correction.py`: CLI that reads raw histogram CSVs, runs the comparison, writes summaries, plots, and a Markdown report.
- Create `sandbox/dark-correction-research/test_dark_correction.py`: unit tests for the sandbox module with small synthetic histograms and tiny CSV fixtures.
- Generated but do not commit: `sandbox/dark-correction-research/outputs/`.

## Task 1: Scaffold The Sandbox Experiment

**Files:**
- Create: `sandbox/dark-correction-research/README.md`
- Create: `sandbox/dark-correction-research/dark_correction.py`
- Create: `sandbox/dark-correction-research/test_dark_correction.py`

- [ ] **Step 1: Write the sandbox README**

Create `sandbox/dark-correction-research/README.md`:

```markdown
# Dark Correction Research

| Field | Value |
|-------|-------|
| **Status** | `prototype` |
| **Owner** | Ethan |
| **Created** | 2026-05-25 |
| **Target graduation** | exploratory |

## Description

Offline research comparing the current moment-based dark frame correction against full-histogram subtraction and deconvolution methods for OpenMotion histogram scans. The first target dataset is `scan_data/20260520_191204_owEENEJ6_left_maskF0_raw.csv`, with cameras 4-7 and camera 7 as the high-gain stress case.

## Run

```powershell
python sandbox/dark-correction-research/analyze_dark_correction.py `
  --csv scan_data/20260520_191204_owEENEJ6_left_maskF0_raw.csv `
  --cameras 4 5 6 7 `
  --dark-interval 600 `
  --output-dir sandbox/dark-correction-research/outputs/20260520_191204
```

## Outputs

The script writes compact CSV summaries, PNG plots, and `report.md` under the selected output directory. Raw multi-GB scan CSVs are inputs only and must not be copied or committed into this sandbox folder.
```

- [ ] **Step 2: Add a minimal importable module**

Create `sandbox/dark-correction-research/dark_correction.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
import numpy as np


HISTOGRAM_BINS = 1024
FRAME_ID_MAX = 256


@dataclass(frozen=True)
class HistogramMoments:
    total: float
    mean: float
    variance: float
    std: float
    contrast: float


def histogram_moments(hist: np.ndarray) -> HistogramMoments:
    counts = np.asarray(hist, dtype=float)
    if counts.ndim != 1:
        raise ValueError(f"hist must be 1-D, got shape {counts.shape}")
    total = float(counts.sum())
    if total <= 0:
        return HistogramMoments(total=0.0, mean=0.0, variance=0.0, std=0.0, contrast=0.0)
    bins = np.arange(counts.size, dtype=float)
    mean = float(np.dot(bins, counts) / total)
    variance = float(np.dot((bins - mean) ** 2, counts) / total)
    std = float(np.sqrt(max(variance, 0.0)))
    contrast = float(std / mean) if mean > 0 else 0.0
    return HistogramMoments(total=total, mean=mean, variance=variance, std=std, contrast=contrast)
```

- [ ] **Step 3: Write the first unit test**

Create `sandbox/dark-correction-research/test_dark_correction.py`:

```python
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dark_correction import histogram_moments

pytestmark = pytest.mark.unit


def test_histogram_moments_returns_weighted_mean_variance_and_contrast():
    hist = np.array([0, 1, 2, 1], dtype=float)

    moments = histogram_moments(hist)

    assert moments.total == 4.0
    assert moments.mean == pytest.approx(2.0)
    assert moments.variance == pytest.approx(0.5)
    assert moments.std == pytest.approx(np.sqrt(0.5))
    assert moments.contrast == pytest.approx(np.sqrt(0.5) / 2.0)
```

- [ ] **Step 4: Run the first test**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit**

```powershell
git add sandbox/dark-correction-research/README.md sandbox/dark-correction-research/dark_correction.py sandbox/dark-correction-research/test_dark_correction.py
git commit -m "test: scaffold dark correction research sandbox"
```

## Task 2: Implement Frame Indexing And Dark Interpolation

**Files:**
- Modify: `sandbox/dark-correction-research/dark_correction.py`
- Modify: `sandbox/dark-correction-research/test_dark_correction.py`

- [ ] **Step 1: Add failing tests for rollover indexing and dark interpolation**

Append to `test_dark_correction.py`:

```python
from dark_correction import absolute_frame_ids, dark_anchor_mask, interpolate_dark_histograms


def test_absolute_frame_ids_reconstructs_rollovers_from_frame_id_counter():
    frame_ids = np.array([254, 255, 0, 1, 2, 255, 0, 1])

    absolute = absolute_frame_ids(frame_ids)

    np.testing.assert_array_equal(absolute, np.array([254, 255, 256, 257, 258, 511, 512, 513]))


def test_dark_anchor_mask_uses_zero_based_absolute_frames():
    absolute = np.array([0, 1, 599, 600, 601, 1200])

    mask = dark_anchor_mask(absolute, dark_interval=600)

    np.testing.assert_array_equal(mask, np.array([True, False, False, True, False, True]))


def test_interpolate_dark_histograms_linearly_between_anchor_frames():
    anchor_frames = np.array([0, 4])
    anchor_hists = np.array([[10, 0, 0], [0, 0, 10]], dtype=float)
    target_frames = np.array([0, 1, 2, 3, 4])

    interpolated = interpolate_dark_histograms(anchor_frames, anchor_hists, target_frames)

    expected = np.array([
        [10.0, 0.0, 0.0],
        [7.5, 0.0, 2.5],
        [5.0, 0.0, 5.0],
        [2.5, 0.0, 7.5],
        [0.0, 0.0, 10.0],
    ])
    np.testing.assert_allclose(interpolated, expected)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: FAIL because `absolute_frame_ids`, `dark_anchor_mask`, and `interpolate_dark_histograms` are not defined.

- [ ] **Step 3: Implement indexing and interpolation**

Append to `dark_correction.py`:

```python
def absolute_frame_ids(frame_ids: np.ndarray, frame_id_max: int = FRAME_ID_MAX) -> np.ndarray:
    raw = np.asarray(frame_ids, dtype=int)
    if raw.ndim != 1:
        raise ValueError(f"frame_ids must be 1-D, got shape {raw.shape}")
    if raw.size == 0:
        return raw.copy()
    rollovers = np.insert(np.cumsum(np.diff(raw) < 0), 0, 0)
    return rollovers * frame_id_max + raw


def dark_anchor_mask(absolute_frames: np.ndarray, dark_interval: int = 600) -> np.ndarray:
    frames = np.asarray(absolute_frames, dtype=int)
    if dark_interval <= 0:
        raise ValueError("dark_interval must be positive")
    return (frames % dark_interval) == 0


def interpolate_dark_histograms(
    anchor_frames: np.ndarray,
    anchor_hists: np.ndarray,
    target_frames: np.ndarray,
) -> np.ndarray:
    anchors = np.asarray(anchor_frames, dtype=float)
    hists = np.asarray(anchor_hists, dtype=float)
    targets = np.asarray(target_frames, dtype=float)
    if anchors.ndim != 1:
        raise ValueError("anchor_frames must be 1-D")
    if hists.ndim != 2:
        raise ValueError("anchor_hists must be 2-D")
    if hists.shape[0] != anchors.size:
        raise ValueError("anchor_hists row count must match anchor_frames")
    if anchors.size == 0:
        raise ValueError("at least one dark anchor is required")
    output = np.empty((targets.size, hists.shape[1]), dtype=float)
    for bin_idx in range(hists.shape[1]):
        output[:, bin_idx] = np.interp(targets, anchors, hists[:, bin_idx])
    return output
```

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add sandbox/dark-correction-research/dark_correction.py sandbox/dark-correction-research/test_dark_correction.py
git commit -m "feat: add dark frame indexing helpers"
```

## Task 3: Implement Correction Methods A-C

**Files:**
- Modify: `sandbox/dark-correction-research/dark_correction.py`
- Modify: `sandbox/dark-correction-research/test_dark_correction.py`

- [ ] **Step 1: Add failing tests for raw, current, and bin-subtraction methods**

Append to `test_dark_correction.py`:

```python
from dark_correction import correct_histogram, CorrectionDiagnostics


def test_raw_method_returns_observed_histogram_moments():
    light = np.array([0, 0, 10, 0], dtype=float)
    dark = np.array([10, 0, 0, 0], dtype=float)

    corrected, diagnostics = correct_histogram(light, dark, method="raw")

    np.testing.assert_allclose(corrected, light)
    assert diagnostics.method == "raw"
    assert diagnostics.clipped_mass == 0.0


def test_current_method_subtracts_dark_mean_and_variance_in_moment_space():
    light = np.array([0, 0, 0, 10, 0], dtype=float)
    dark = np.array([0, 10, 0, 0, 0], dtype=float)

    corrected, diagnostics = correct_histogram(light, dark, method="current")

    assert corrected is None
    assert diagnostics.method == "current"
    assert diagnostics.corrected_mean == pytest.approx(2.0)
    assert diagnostics.corrected_variance == pytest.approx(0.0)


def test_bin_subtract_clips_negative_bins_and_reports_clipped_mass():
    light = np.array([1, 5, 0], dtype=float)
    dark = np.array([3, 2, 0], dtype=float)

    corrected, diagnostics = correct_histogram(light, dark, method="bin_subtract")

    np.testing.assert_allclose(corrected, np.array([0, 3, 0], dtype=float))
    assert diagnostics.clipped_mass == pytest.approx(2.0)
    assert diagnostics.output_mass == pytest.approx(3.0)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: FAIL because `correct_histogram` and `CorrectionDiagnostics` are not defined.

- [ ] **Step 3: Implement methods A-C**

Append to `dark_correction.py`:

```python
@dataclass(frozen=True)
class CorrectionDiagnostics:
    method: str
    input_mass: float
    dark_mass: float
    output_mass: float
    clipped_mass: float
    corrected_mean: float
    corrected_variance: float
    corrected_contrast: float
    ringing_score: float = 0.0


def _diagnostics_from_hist(method: str, light: np.ndarray, dark: np.ndarray, corrected: np.ndarray, clipped_mass: float) -> CorrectionDiagnostics:
    moments = histogram_moments(corrected)
    return CorrectionDiagnostics(
        method=method,
        input_mass=float(np.asarray(light, dtype=float).sum()),
        dark_mass=float(np.asarray(dark, dtype=float).sum()),
        output_mass=float(corrected.sum()),
        clipped_mass=float(clipped_mass),
        corrected_mean=moments.mean,
        corrected_variance=moments.variance,
        corrected_contrast=moments.contrast,
        ringing_score=ringing_score(corrected),
    )


def ringing_score(hist: np.ndarray) -> float:
    counts = np.asarray(hist, dtype=float)
    if counts.size < 3 or counts.sum() <= 0:
        return 0.0
    second_diff = np.diff(counts, n=2)
    return float(np.mean(np.abs(second_diff)) / max(np.mean(counts), 1.0))


def correct_histogram(light_hist: np.ndarray, dark_hist: np.ndarray, method: str) -> tuple[np.ndarray | None, CorrectionDiagnostics]:
    light = np.asarray(light_hist, dtype=float)
    dark = np.asarray(dark_hist, dtype=float)
    if light.shape != dark.shape:
        raise ValueError(f"light and dark histograms must have same shape, got {light.shape} and {dark.shape}")

    if method == "raw":
        corrected = light.copy()
        return corrected, _diagnostics_from_hist(method, light, dark, corrected, clipped_mass=0.0)

    if method == "current":
        light_m = histogram_moments(light)
        dark_m = histogram_moments(dark)
        corrected_mean = light_m.mean - dark_m.mean
        corrected_variance = max(light_m.variance - dark_m.variance, 0.0)
        corrected_std = float(np.sqrt(corrected_variance))
        corrected_contrast = corrected_std / corrected_mean if corrected_mean > 0 else 0.0
        diagnostics = CorrectionDiagnostics(
            method=method,
            input_mass=light_m.total,
            dark_mass=dark_m.total,
            output_mass=light_m.total,
            clipped_mass=0.0,
            corrected_mean=float(corrected_mean),
            corrected_variance=float(corrected_variance),
            corrected_contrast=float(corrected_contrast),
        )
        return None, diagnostics

    if method == "bin_subtract":
        raw = light - dark
        clipped_mass = float(np.abs(raw[raw < 0]).sum())
        corrected = np.clip(raw, 0.0, None)
        return corrected, _diagnostics_from_hist(method, light, dark, corrected, clipped_mass=clipped_mass)

    raise ValueError(f"unknown correction method: {method}")
```

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add sandbox/dark-correction-research/dark_correction.py sandbox/dark-correction-research/test_dark_correction.py
git commit -m "feat: add baseline dark correction methods"
```

## Task 4: Implement Regularized Deconvolution And Bracketing Support

**Files:**
- Modify: `sandbox/dark-correction-research/dark_correction.py`
- Modify: `sandbox/dark-correction-research/test_dark_correction.py`

- [ ] **Step 1: Add failing tests for synthetic deconvolution recovery**

Append to `test_dark_correction.py`:

```python
from dark_correction import convolve_histograms, deconvolve_histogram


def test_convolve_histograms_preserves_probability_mass_shape():
    signal = np.array([0, 1, 0], dtype=float)
    dark = np.array([0, 1, 0], dtype=float)

    observed = convolve_histograms(signal, dark, output_size=5)

    np.testing.assert_allclose(observed, np.array([0, 0, 1, 0, 0], dtype=float))


def test_fft_deconvolution_recovers_simple_shifted_signal_peak():
    signal = np.array([0, 0, 10, 0, 0], dtype=float)
    dark = np.array([0, 1, 0, 0, 0], dtype=float)
    observed = convolve_histograms(signal, dark, output_size=5)

    recovered = deconvolve_histogram(observed, dark, method="fft", regularization=1e-3, iterations=20)

    assert int(np.argmax(recovered)) == 2
    assert recovered.sum() == pytest.approx(signal.sum())
    assert np.all(recovered >= 0)


def test_correct_histogram_supports_deconvolution_method():
    signal = np.array([0, 0, 10, 0, 0], dtype=float)
    dark = np.array([0, 1, 0, 0, 0], dtype=float)
    observed = convolve_histograms(signal, dark, output_size=5)

    corrected, diagnostics = correct_histogram(observed, dark, method="deconv_fft")

    assert corrected is not None
    assert diagnostics.method == "deconv_fft"
    assert int(np.argmax(corrected)) == 2
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: FAIL because convolution/deconvolution functions and `deconv_fft` method are missing.

- [ ] **Step 3: Implement FFT deconvolution first**

Add to `dark_correction.py` before `correct_histogram`, then add the `deconv_fft` branch inside `correct_histogram` before the final `raise`:

```python
def convolve_histograms(signal_hist: np.ndarray, dark_hist: np.ndarray, output_size: int | None = None) -> np.ndarray:
    signal = np.asarray(signal_hist, dtype=float)
    dark = np.asarray(dark_hist, dtype=float)
    full = np.convolve(signal, dark)
    if output_size is None:
        return full
    if output_size <= 0:
        raise ValueError("output_size must be positive")
    if full.size >= output_size:
        return full[:output_size]
    return np.pad(full, (0, output_size - full.size))


def _normalize_pdf(hist: np.ndarray) -> tuple[np.ndarray, float]:
    counts = np.asarray(hist, dtype=float)
    total = float(counts.sum())
    if total <= 0:
        return np.zeros_like(counts, dtype=float), 0.0
    return counts / total, total


def deconvolve_histogram(
    observed_hist: np.ndarray,
    dark_hist: np.ndarray,
    method: str = "fft",
    regularization: float = 1e-3,
    iterations: int = 20,
) -> np.ndarray:
    observed_pdf, observed_total = _normalize_pdf(observed_hist)
    dark_pdf, _ = _normalize_pdf(dark_hist)
    if observed_total <= 0:
        return np.zeros_like(observed_pdf)
    if dark_pdf.sum() <= 0:
        return np.asarray(observed_hist, dtype=float).copy()

    if method == "fft":
        n = observed_pdf.size
        obs_fft = np.fft.rfft(observed_pdf, n=n)
        dark_fft = np.fft.rfft(dark_pdf, n=n)
        denom = np.abs(dark_fft) ** 2 + regularization
        recovered_fft = obs_fft * np.conj(dark_fft) / denom
        recovered_pdf = np.fft.irfft(recovered_fft, n=n)
        recovered_pdf = np.clip(recovered_pdf, 0.0, None)
        pdf_sum = recovered_pdf.sum()
        if pdf_sum > 0:
            recovered_pdf = recovered_pdf / pdf_sum
        return recovered_pdf * observed_total

    if method == "richardson_lucy":
        estimate = np.full_like(observed_pdf, 1.0 / observed_pdf.size)
        dark_rev = dark_pdf[::-1]
        for _ in range(max(iterations, 1)):
            blurred = convolve_histograms(estimate, dark_pdf, output_size=observed_pdf.size)
            ratio = observed_pdf / np.clip(blurred, 1e-12, None)
            estimate *= convolve_histograms(ratio, dark_rev, output_size=observed_pdf.size)
            estimate = np.clip(estimate, 0.0, None)
            if estimate.sum() > 0:
                estimate /= estimate.sum()
        return estimate * observed_total

    raise ValueError(f"unknown deconvolution method: {method}")
```

Add this branch inside `correct_histogram`:

```python
    if method == "deconv_fft":
        corrected = deconvolve_histogram(light, dark, method="fft", regularization=1e-3)
        return corrected, _diagnostics_from_hist(method, light, dark, corrected, clipped_mass=0.0)
```

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add sandbox/dark-correction-research/dark_correction.py sandbox/dark-correction-research/test_dark_correction.py
git commit -m "feat: add regularized dark histogram deconvolution"
```

## Task 5: Implement Comparison Metrics

**Files:**
- Modify: `sandbox/dark-correction-research/dark_correction.py`
- Modify: `sandbox/dark-correction-research/test_dark_correction.py`

- [ ] **Step 1: Add failing metric tests**

Append to `test_dark_correction.py`:

```python
from dark_correction import MetricSummary, summarize_series, temperature_correlation, boundary_jumps


def test_summarize_series_reports_stability_stats():
    values = np.array([10.0, 11.0, 9.0, 10.0])

    summary = summarize_series(values)

    assert isinstance(summary, MetricSummary)
    assert summary.mean == pytest.approx(10.0)
    assert summary.std == pytest.approx(np.std(values))
    assert summary.mad == pytest.approx(0.5)


def test_temperature_correlation_returns_zero_for_constant_series():
    assert temperature_correlation(np.array([1, 2, 3]), np.array([5, 5, 5])) == 0.0


def test_boundary_jumps_measures_pre_post_difference():
    frames = np.arange(10)
    values = np.array([1, 1, 1, 1, 10, 11, 11, 11, 11, 11], dtype=float)

    jumps = boundary_jumps(frames, values, dark_frames=np.array([4]), window=2)

    assert jumps[0] == pytest.approx(10.0)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: FAIL because metric helpers are missing.

- [ ] **Step 3: Implement metric helpers**

Append to `dark_correction.py`:

```python
@dataclass(frozen=True)
class MetricSummary:
    mean: float
    std: float
    mad: float
    derivative_std: float
    coefficient_of_variation: float


def summarize_series(values: np.ndarray) -> MetricSummary:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return MetricSummary(mean=0.0, std=0.0, mad=0.0, derivative_std=0.0, coefficient_of_variation=0.0)
    mean = float(np.mean(arr))
    std = float(np.std(arr))
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    derivative_std = float(np.std(np.diff(arr))) if arr.size > 1 else 0.0
    cv = float(std / abs(mean)) if mean != 0 else 0.0
    return MetricSummary(mean=mean, std=std, mad=mad, derivative_std=derivative_std, coefficient_of_variation=cv)


def temperature_correlation(temperature: np.ndarray, values: np.ndarray) -> float:
    temp = np.asarray(temperature, dtype=float)
    vals = np.asarray(values, dtype=float)
    mask = np.isfinite(temp) & np.isfinite(vals)
    if mask.sum() < 3:
        return 0.0
    temp = temp[mask]
    vals = vals[mask]
    if np.std(temp) == 0 or np.std(vals) == 0:
        return 0.0
    return float(np.corrcoef(temp, vals)[0, 1])


def boundary_jumps(frames: np.ndarray, values: np.ndarray, dark_frames: np.ndarray, window: int = 5) -> list[float]:
    frame_arr = np.asarray(frames, dtype=int)
    value_arr = np.asarray(values, dtype=float)
    jumps: list[float] = []
    for dark_frame in np.asarray(dark_frames, dtype=int):
        pre = value_arr[(frame_arr >= dark_frame - window) & (frame_arr < dark_frame)]
        post = value_arr[(frame_arr > dark_frame) & (frame_arr <= dark_frame + window)]
        if pre.size and post.size:
            jumps.append(float(np.median(post) - np.median(pre)))
    return jumps
```

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add sandbox/dark-correction-research/dark_correction.py sandbox/dark-correction-research/test_dark_correction.py
git commit -m "feat: add dark correction comparison metrics"
```

## Task 6: Implement The Chunked CLI And Report Writer

**Files:**
- Create: `sandbox/dark-correction-research/analyze_dark_correction.py`
- Modify: `sandbox/dark-correction-research/test_dark_correction.py`

- [ ] **Step 1: Add a failing CLI smoke test with a tiny CSV**

Append to `test_dark_correction.py`:

```python
import subprocess


def test_cli_writes_report_for_tiny_csv(tmp_path):
    csv_path = tmp_path / "tiny_raw.csv"
    bins = [str(i) for i in range(8)]
    header = ["cam_id", "frame_id", "timestamp_s", *bins, "temperature", "sum", "tcm", "tcl", "pdc"]
    rows = [
        [7, 0, 0.0, 10, 0, 0, 0, 0, 0, 0, 0, 30.0, 10, 1, 1, 1],
        [7, 1, 0.1, 0, 0, 5, 5, 0, 0, 0, 0, 30.1, 10, 1, 1, 1],
        [7, 2, 0.2, 0, 0, 4, 6, 0, 0, 0, 0, 30.2, 10, 1, 1, 1],
        [7, 3, 0.3, 0, 10, 0, 0, 0, 0, 0, 0, 30.3, 10, 1, 1, 1],
    ]
    csv_path.write_text(
        ",".join(header) + "\n" + "\n".join(",".join(str(v) for v in row) for row in rows),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parent / "analyze_dark_correction.py"),
            "--csv",
            str(csv_path),
            "--cameras",
            "7",
            "--dark-interval",
            "3",
            "--output-dir",
            str(output_dir),
            "--histogram-bins",
            "8",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (output_dir / "report.md").exists()
    assert (output_dir / "metrics_summary.csv").exists()
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: FAIL because `analyze_dark_correction.py` does not exist.

- [ ] **Step 3: Implement the CLI**

Create `sandbox/dark-correction-research/analyze_dark_correction.py`:

```python
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dark_correction import (
    absolute_frame_ids,
    boundary_jumps,
    correct_histogram,
    dark_anchor_mask,
    interpolate_dark_histograms,
    summarize_series,
    temperature_correlation,
)


METHODS = ["raw", "current", "bin_subtract", "deconv_fft"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare dark histogram correction methods.")
    parser.add_argument("--csv", required=True, help="Raw histogram CSV path.")
    parser.add_argument("--cameras", nargs="+", type=int, required=True, help="Camera ids to analyze.")
    parser.add_argument("--dark-interval", type=int, default=600)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--histogram-bins", type=int, default=1024)
    parser.add_argument("--max-light-frames-per-camera", type=int, default=5000)
    return parser.parse_args()


def read_selected_rows(csv_path: Path, cameras: set[int], histogram_bins: int) -> pd.DataFrame:
    usecols = ["cam_id", "frame_id", "timestamp_s", *[str(i) for i in range(histogram_bins)], "temperature", "sum"]
    chunks = []
    for chunk in pd.read_csv(csv_path, usecols=usecols, chunksize=25000):
        chunk = chunk[chunk["cam_id"].isin(cameras)]
        if not chunk.empty:
            chunks.append(chunk)
    if not chunks:
        raise ValueError(f"no rows found for cameras {sorted(cameras)}")
    return pd.concat(chunks, ignore_index=True)


def analyze_camera(df: pd.DataFrame, cam_id: int, dark_interval: int, histogram_bins: int, max_light_frames: int) -> tuple[list[dict], pd.DataFrame]:
    cam = df[df["cam_id"] == cam_id].copy()
    cam["absolute_frame"] = absolute_frame_ids(cam["frame_id"].to_numpy(dtype=int))
    hist_cols = [str(i) for i in range(histogram_bins)]
    hists = cam[hist_cols].to_numpy(dtype=float)
    frames = cam["absolute_frame"].to_numpy(dtype=int)
    temperatures = cam["temperature"].to_numpy(dtype=float)
    dark_mask = dark_anchor_mask(frames, dark_interval=dark_interval)
    if dark_mask.sum() < 1:
        raise ValueError(f"camera {cam_id} has no dark anchors")
    dark_frames = frames[dark_mask]
    dark_hists = hists[dark_mask]
    light_mask = ~dark_mask
    light_indices = np.flatnonzero(light_mask)
    if light_indices.size > max_light_frames:
        light_indices = np.linspace(light_indices[0], light_indices[-1], max_light_frames, dtype=int)
    light_frames = frames[light_indices]
    interpolated_dark = interpolate_dark_histograms(dark_frames, dark_hists, light_frames)

    metric_rows = []
    frame_rows = []
    for method in METHODS:
        means = []
        contrasts = []
        clipped = []
        ringing = []
        start = time.perf_counter()
        for row_idx, dark_hist in zip(light_indices, interpolated_dark):
            _, diagnostics = correct_histogram(hists[row_idx], dark_hist, method=method)
            means.append(diagnostics.corrected_mean)
            contrasts.append(diagnostics.corrected_contrast)
            clipped.append(diagnostics.clipped_mass)
            ringing.append(diagnostics.ringing_score)
        elapsed = time.perf_counter() - start
        means_arr = np.asarray(means, dtype=float)
        contrast_arr = np.asarray(contrasts, dtype=float)
        temp_arr = temperatures[light_indices]
        mean_summary = summarize_series(means_arr)
        contrast_summary = summarize_series(contrast_arr)
        jumps = boundary_jumps(light_frames, contrast_arr, dark_frames, window=5)
        metric_rows.append({
            "cam_id": cam_id,
            "method": method,
            "frames": len(light_indices),
            "mean_std": mean_summary.std,
            "mean_mad": mean_summary.mad,
            "contrast_mean": contrast_summary.mean,
            "contrast_std": contrast_summary.std,
            "contrast_mad": contrast_summary.mad,
            "contrast_temp_corr": temperature_correlation(temp_arr, contrast_arr),
            "median_boundary_jump": float(np.median(np.abs(jumps))) if jumps else 0.0,
            "mean_clipped_mass": float(np.mean(clipped)) if clipped else 0.0,
            "mean_ringing_score": float(np.mean(ringing)) if ringing else 0.0,
            "ms_per_frame": (elapsed / max(len(light_indices), 1)) * 1000.0,
        })
        frame_rows.extend({
            "cam_id": cam_id,
            "method": method,
            "absolute_frame": int(frame),
            "temperature": float(temp),
            "corrected_mean": float(mean),
            "corrected_contrast": float(contrast),
        } for frame, temp, mean, contrast in zip(light_frames, temp_arr, means_arr, contrast_arr))
    return metric_rows, pd.DataFrame(frame_rows)


def write_report(output_dir: Path, metrics: pd.DataFrame, frame_metrics: pd.DataFrame) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(output_dir / "metrics_summary.csv", index=False)
    frame_metrics.to_csv(output_dir / "frame_metrics_sample.csv", index=False)

    for cam_id in sorted(frame_metrics["cam_id"].unique()):
        fig, ax = plt.subplots(figsize=(10, 5))
        cam = frame_metrics[frame_metrics["cam_id"] == cam_id]
        for method in METHODS:
            subset = cam[cam["method"] == method]
            ax.plot(subset["absolute_frame"], subset["corrected_contrast"], label=method, linewidth=1)
        ax.set_title(f"Camera {cam_id} Corrected Contrast")
        ax.set_xlabel("Absolute frame")
        ax.set_ylabel("Contrast")
        ax.legend()
        fig.tight_layout()
        fig.savefig(output_dir / f"camera_{cam_id}_contrast.png", dpi=140)
        plt.close(fig)

    report = [
        "# Dark Histogram Correction Report",
        "",
        "## Summary Table",
        "",
        dataframe_to_markdown(metrics),
        "",
        "## Recommendation Notes",
        "",
        "Review camera 7 variance, temperature correlation, boundary jumps, clipping, ringing, and runtime against cameras 4-6 before changing production correction.",
    ]
    (output_dir / "report.md").write_text("\n".join(report), encoding="utf-8")


def dataframe_to_markdown(df: pd.DataFrame) -> str:
    columns = list(df.columns)
    rows = [["" if pd.isna(value) else str(value) for value in row] for row in df.to_numpy()]
    widths = [
        max(len(str(column)), *(len(row[idx]) for row in rows)) if rows else len(str(column))
        for idx, column in enumerate(columns)
    ]
    header = "| " + " | ".join(str(column).ljust(widths[idx]) for idx, column in enumerate(columns)) + " |"
    separator = "| " + " | ".join("-" * widths[idx] for idx in range(len(columns))) + " |"
    body = [
        "| " + " | ".join(row[idx].ljust(widths[idx]) for idx in range(len(columns))) + " |"
        for row in rows
    ]
    return "\n".join([header, separator, *body])


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv)
    output_dir = Path(args.output_dir)
    df = read_selected_rows(csv_path, set(args.cameras), args.histogram_bins)
    all_metrics = []
    all_frames = []
    for cam_id in args.cameras:
        rows, frame_df = analyze_camera(df, cam_id, args.dark_interval, args.histogram_bins, args.max_light_frames_per_camera)
        all_metrics.extend(rows)
        all_frames.append(frame_df)
    metrics = pd.DataFrame(all_metrics)
    frame_metrics = pd.concat(all_frames, ignore_index=True)
    write_report(output_dir, metrics, frame_metrics)
    print(f"Wrote report: {output_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add sandbox/dark-correction-research/analyze_dark_correction.py sandbox/dark-correction-research/test_dark_correction.py
git commit -m "feat: add dark correction analysis cli"
```

## Task 7: Run The Real Dataset And Capture Outputs

**Files:**
- Generated only: `sandbox/dark-correction-research/outputs/20260520_191204/`
- Modify only if findings need manual interpretation: `sandbox/dark-correction-research/outputs/20260520_191204/report.md`

- [ ] **Step 1: Run the unit test suite before the long analysis**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the real scan analysis with bounded frame sampling**

Run:

```powershell
python sandbox/dark-correction-research/analyze_dark_correction.py `
  --csv scan_data/20260520_191204_owEENEJ6_left_maskF0_raw.csv `
  --cameras 4 5 6 7 `
  --dark-interval 600 `
  --output-dir sandbox/dark-correction-research/outputs/20260520_191204 `
  --max-light-frames-per-camera 5000
```

Expected: `report.md`, `metrics_summary.csv`, `frame_metrics_sample.csv`, and one contrast PNG per camera are written under `sandbox/dark-correction-research/outputs/20260520_191204/`.

- [ ] **Step 3: Inspect the summary table for decision criteria**

Run:

```powershell
python -c "import pandas as pd; df = pd.read_csv('sandbox/dark-correction-research/outputs/20260520_191204/metrics_summary.csv'); print(df.sort_values(['cam_id', 'method']).to_string(index=False))"
```

Expected: rows for cameras 4, 5, 6, and 7, with methods `raw`, `current`, `bin_subtract`, and `deconv_fft`.

- [ ] **Step 4: Add a short recommendation to the generated report**

Open `sandbox/dark-correction-research/outputs/20260520_191204/report.md` and add a short `## Recommendation` section that states one of:

```markdown
## Recommendation

Keep the current moment correction. In this scan, deconvolution did not reduce camera 7 contrast variability or temperature coupling enough to justify production complexity.
```

or:

```markdown
## Recommendation

Continue deconvolution research for high-gain far cameras only. In this scan, `deconv_fft` improved camera 7 stability while preserving cameras 4-6, but production adoption still needs artifact and runtime hardening.
```

- [ ] **Step 5: Commit code and report if outputs are small enough**

Check sizes first:

```powershell
Get-ChildItem sandbox/dark-correction-research/outputs/20260520_191204 -Recurse | Select-Object FullName,Length
```

If the report and summaries are small enough for git, commit only `report.md`, `metrics_summary.csv`, and PNG plots. Do not commit `frame_metrics_sample.csv` if it is large.

```powershell
git add sandbox/dark-correction-research/outputs/20260520_191204/report.md sandbox/dark-correction-research/outputs/20260520_191204/metrics_summary.csv sandbox/dark-correction-research/outputs/20260520_191204/*.png
git commit -m "docs: report dark correction comparison results"
```

## Task 8: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run sandbox unit tests**

Run:

```powershell
python -m pytest sandbox/dark-correction-research/test_dark_correction.py -m unit -q
```

Expected: all tests pass.

- [ ] **Step 2: Verify the plan's core deliverables exist**

Run:

```powershell
Test-Path sandbox/dark-correction-research/README.md
Test-Path sandbox/dark-correction-research/dark_correction.py
Test-Path sandbox/dark-correction-research/analyze_dark_correction.py
Test-Path sandbox/dark-correction-research/outputs/20260520_191204/report.md
Test-Path sandbox/dark-correction-research/outputs/20260520_191204/metrics_summary.csv
```

Expected: every line prints `True`.

- [ ] **Step 3: Check git status for accidental raw-data or unrelated changes**

Run:

```powershell
git status --short
```

Expected: no staged or unstaged changes related to the multi-GB raw CSV. Existing unrelated working tree changes may still be present; do not revert them.
