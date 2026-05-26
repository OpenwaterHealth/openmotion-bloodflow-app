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
