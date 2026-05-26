import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dark_correction import histogram_moments
from dark_correction import absolute_frame_ids, dark_anchor_mask, interpolate_dark_histograms

pytestmark = pytest.mark.unit


def test_histogram_moments_returns_weighted_mean_variance_and_contrast():
    hist = np.array([0, 1, 2, 1], dtype=float)

    moments = histogram_moments(hist)

    assert moments.total == 4.0
    assert moments.mean == pytest.approx(2.0)
    assert moments.variance == pytest.approx(0.5)
    assert moments.std == pytest.approx(np.sqrt(0.5))
    assert moments.contrast == pytest.approx(np.sqrt(0.5) / 2.0)


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
