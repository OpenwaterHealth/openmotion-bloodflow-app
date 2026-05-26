# Dark Histogram Correction Research Design

**Date:** 2026-05-25

**Dataset:** `scan_data/20260520_191204_owEENEJ6_left_maskF0_raw.csv`

**Goal:** Decide whether the sensor data pipeline should keep the current dark correction strategy or move to a full-histogram correction strategy for high-gain far cameras.

## Background

The current offline processing path in `processing/visualize_bloodflow.py` treats scheduled dark frames as dark anchors. It interpolates dark mean and dark variance across the scan, then computes:

- corrected mean = observed light mean - interpolated dark mean
- corrected variance = observed light variance - interpolated dark variance
- corrected contrast = corrected standard deviation / corrected mean

This is mathematically consistent for first and second moments if observed light samples are `Y = S + D`, where `S` is illumination signal, `D` is dark noise, and the two are independent. A full deconvolution of the estimated dark histogram from each observed light histogram is more physically complete, but it should only replace the current method if it improves downstream stability, plausibility, or calibration behavior enough to justify extra complexity and runtime cost.

Initial streaming inspection of the target CSV found four cameras:

| Camera | Rows | Mean histogram bin | Histogram SD | Notes |
| --- | ---: | ---: | ---: | --- |
| 4 | 93,497 | 208.9 | 44.0 | Shorter capture coverage than 5-7. |
| 5 | 363,735 | 209.6 | 39.7 | Stable high-count camera. |
| 6 | 363,735 | 205.6 | 30.8 | Narrower distribution. |
| 7 | 363,735 | 168.2 | 45.5 | Wider and more tail-sensitive; primary stress case. |

Camera 7 is the main target because it has a lower mean bin and wider/less stable tails, matching the high-gain far-camera concern.

## Scope

This research phase is offline analysis only. It should not change live capture, QML UI, SDK behavior, calibration thresholds, or production correction code until the model comparison provides clear evidence.

The analysis should use the same dark/light labeling assumptions as the current pipeline:

- Reconstruct absolute frame index from `frame_id` rollover.
- Use `dark_interval=600`.
- Treat scheduled frames `0, 600, 1200, ...` as dark anchors.
- Treat non-anchor frames as illuminated frames.

The analysis should focus on left-side cameras 4, 5, 6, and 7 in the provided phantom scan, with camera 7 as the stress case and cameras 4-6 as controls.

## Research Questions

1. Does dark histogram shape drift over time in a way that is not captured by mean and variance alone?
2. Does full-histogram correction reduce residual temperature dependence, frame-to-frame instability, or dark-anchor boundary artifacts?
3. Does full-histogram correction improve camera 7 without degrading cameras 4-6?
4. Are deconvolution artifacts, regularization sensitivity, or runtime costs small enough for a production pipeline?
5. If deconvolution helps, should it be applied globally, only to high-gain far cameras, or only as an offline/post-processing option?

## Compared Methods

### Method A: Raw Light Moments

Compute observed illuminated-frame mean, variance, and contrast with no dark correction. This is a negative-control baseline.

### Method B: Current Moment Correction

Reproduce the existing correction from `processing/visualize_bloodflow.py`:

1. Apply the same baseline/noise-floor cleanup.
2. Extract scheduled dark histograms.
3. Replace the first dark anchor with the first good early dark frame, matching current behavior.
4. Interpolate dark mean and dark variance between scheduled anchors.
5. Subtract interpolated dark mean and variance from observed illuminated-frame moments.
6. Compute corrected contrast from the corrected moments.

This is the production baseline.

### Method C: Bin-Wise Histogram Subtraction

Interpolate the full dark histogram between scheduled dark anchors, subtract it bin-wise from the observed light histogram, clip negative bins to zero, renormalize, and compute moments.

This is not the physically correct model for additive random variables, but it is useful as a simple diagnostic. If bin-wise subtraction performs as well as deconvolution, the observed issue may be dominated by offset/tail contamination rather than true convolution structure.

### Method D: Regularized Histogram Deconvolution

Estimate the signal histogram `S` from observed light histogram `Y` and interpolated dark histogram `D` under `Y = S * D`.

Candidate algorithms:

- FFT deconvolution with floor regularization in the frequency domain.
- Wiener-style deconvolution with a tunable noise-to-signal parameter.
- Richardson-Lucy deconvolution with limited iterations, non-negativity, and support constraints.

All deconvolution variants must preserve non-negative mass, handle near-zero frequency bins, avoid unstable ringing, and emit diagnostic repair metrics.

### Method E: Bracketing-Dark Bound

Repeat Method D using the previous dark anchor and next dark anchor separately rather than the interpolated dark histogram. This gives an upper/lower sensitivity bound for dark interpolation error and helps distinguish deconvolution failure from dark-estimate drift.

## Metrics

### Stability Metrics

For each camera and method:

- Rolling standard deviation of corrected mean.
- Rolling standard deviation of corrected contrast.
- Rolling coefficient of variation for mean and contrast.
- Frame-to-frame derivative statistics.
- Robust steady-interval spread using median absolute deviation.

### Boundary Metrics

Around each scheduled dark anchor:

- Pre/post jump in corrected mean.
- Pre/post jump in corrected contrast.
- Recovery time after dark anchor.
- Presence of interpolation discontinuities.

### Temperature Metrics

For each camera and method:

- Correlation of corrected mean with temperature.
- Correlation of corrected contrast with temperature.
- Linear slope of corrected metrics versus temperature.
- Residual metric drift after removing a low-order time trend.

### Physical Plausibility Metrics

For each corrected histogram:

- Total mass before and after correction.
- Negative-mass or clipping repair rate.
- Fraction of frames with corrected variance at or below zero.
- Ringing score, based on alternating high-frequency residuals.
- Tail mass in low and high quantile ranges.
- Change in q01, q50, q99 versus baseline.

### Downstream Metrics

When calibration constants are available or existing fallbacks are sufficient:

- BFI stability.
- BVI stability.
- Cross-camera consistency.
- Pass/fail sensitivity against configured mean and contrast thresholds.

### Runtime Metrics

For each method:

- Mean and p95 correction time per frame per camera.
- Peak memory use for a representative chunk.
- Feasibility for offline-only use versus live processing.

## Analysis Workflow

1. **Streaming summarization**
   - Stream the raw CSV in chunks.
   - Reconstruct camera/frame coverage.
   - Record dark-anchor positions and light-frame counts.
   - Emit compact per-camera summary tables.

2. **Dark-shape diagnostics**
   - Compute per-dark-anchor histograms and moments.
   - Plot dark mean, variance, skew, kurtosis, q01, q50, q99, and tail mass over time.
   - Quantify whether camera 7 dark-shape drift exceeds cameras 4-6.

3. **Correction method implementation**
   - Implement Methods A-E behind one analysis interface.
   - Normalize all methods to produce comparable corrected mean, variance, contrast, and optional corrected histogram.
   - Record per-frame diagnostics for clipping, mass repair, and deconvolution instability.

4. **Model comparison**
   - Evaluate metrics by camera and by scan interval.
   - Highlight camera 7 improvements and any regressions in cameras 4-6.
   - Compare real-data behavior to the current pipeline output.

5. **Synthetic recovery test**
   - Use real dark histograms plus controlled signal histograms.
   - Convolve signal and dark distributions to generate simulated light histograms.
   - Test whether Methods B-E recover the known signal moments and shape.
   - Use this to interpret whether real-data failures come from algorithm instability or incorrect assumptions.

6. **Report**
   - Produce a concise Markdown report with summary tables and plots.
   - Include a recommendation: keep current correction, add optional deconvolution, apply deconvolution only to high-gain cameras, or collect more data.

## Decision Criteria

Recommend changing the production correction only if Method D or E:

- Reduces camera 7 steady-interval contrast or BFI variance by at least 20-30% versus current moment correction.
- Reduces residual temperature correlation for camera 7.
- Does not increase dark-anchor boundary artifacts.
- Does not materially degrade cameras 4-6.
- Has low artifact/repair rates.
- Has acceptable runtime for the intended use path.

If deconvolution improves only offline metrics but is too slow or fragile for live use, recommend an offline-only analysis option rather than changing the live pipeline.

If current moment correction performs similarly to deconvolution, keep the current method and document why moment subtraction is sufficient for the measured quantities.

## Deliverables

1. A reusable analysis script under `processing/` or `sandbox/`, depending on whether it is intended to become maintained tooling.
2. A report under `docs/` or `sandbox/` containing:
   - dataset summary,
   - method definitions,
   - per-camera comparison tables,
   - plots for dark drift and corrected metric stability,
   - runtime summary,
   - recommendation.
3. Optional small derived CSV/Parquet summaries for auditability. Do not commit the multi-GB raw CSV.

## Risks And Mitigations

**Risk:** Deconvolution amplifies noise and creates ringing.
**Mitigation:** Use regularization, iteration limits, non-negativity constraints, and explicit artifact metrics.

**Risk:** Dark interpolation error dominates the correction comparison.
**Mitigation:** Include bracketing-dark bounds and dark-shape drift diagnostics.

**Risk:** Current moment correction is already optimal for mean/contrast.
**Mitigation:** Treat this as a valid outcome and focus on evidence rather than algorithm novelty.

**Risk:** The target dataset is one phantom scan and may not generalize.
**Mitigation:** Frame the conclusion as conditional on this dataset unless additional scans are added later.

**Risk:** Full CSV loading is too memory-intensive.
**Mitigation:** Stream or chunk the CSV and cache compact derived summaries.

## Open Implementation Choices

The implementation plan should decide:

- Whether the first analysis script lives in `sandbox/dark_correction/` or `processing/`.
- Which deconvolution method is the first implementation target.
- Whether to produce static PNG plots, CSV summaries, an Excel workbook, or all three.
- Whether to run the full 3.6 GB dataset on every iteration or cache per-camera/per-frame summary artifacts.
