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
