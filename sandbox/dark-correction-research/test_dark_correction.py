import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dark_correction import histogram_moments
from dark_correction import absolute_frame_ids, dark_anchor_mask, interpolate_dark_histograms
from dark_correction import correct_histogram, CorrectionDiagnostics
from dark_correction import convolve_histograms, deconvolve_histogram

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
