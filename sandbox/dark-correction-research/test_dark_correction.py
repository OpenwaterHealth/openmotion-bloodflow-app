import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dark_correction import histogram_moments
from dark_correction import absolute_frame_ids, dark_anchor_mask, interpolate_dark_histograms
from dark_correction import correct_histogram, CorrectionDiagnostics
from dark_correction import convolve_histograms, deconvolve_histogram
from dark_correction import MetricSummary, summarize_series, temperature_correlation, boundary_jumps
from analyze_dark_correction import analyze_camera, production_cleanup_histograms, read_selected_rows

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


def test_read_selected_rows_maps_first_frame_id_one_to_absolute_zero_dark_anchor(tmp_path):
    csv_path = tmp_path / "starts_at_one.csv"
    bins = [str(i) for i in range(4)]
    header = ["cam_id", "frame_id", "timestamp_s", *bins, "temperature", "sum"]
    rows = [
        [7, 254, 0.0, 0, 10, 0, 0, 30.0, 10],
        [7, 255, 0.1, 0, 10, 0, 0, 30.1, 10],
        [7, 1, 0.2, 10, 0, 0, 0, 30.2, 10],
        [7, 2, 0.3, 0, 10, 0, 0, 30.3, 10],
    ]
    csv_path.write_text(
        ",".join(header) + "\n" + "\n".join(",".join(str(value) for value in row) for row in rows),
        encoding="utf-8",
    )

    selected = read_selected_rows(
        csv_path,
        cameras={7},
        histogram_bins=4,
        dark_interval=600,
        max_light_frames_per_camera=10,
        max_dark_anchors_per_camera=10,
        chunksize=2,
    )

    first = selected.iloc[0]
    assert int(first["frame_id"]) == 1
    assert int(first["absolute_frame"]) == 0
    assert bool(first["is_dark_anchor"])


def test_read_selected_rows_maps_frame_id_zero_after_rollover_to_index_255(tmp_path):
    csv_path = tmp_path / "rollover.csv"
    bins = [str(i) for i in range(4)]
    header = ["cam_id", "frame_id", "timestamp_s", *bins, "temperature", "sum"]
    rows = [
        [7, 1, 0.0, 10, 0, 0, 0, 30.0, 10],
        [7, 2, 0.1, 0, 10, 0, 0, 30.1, 10],
        [7, 255, 0.2, 0, 0, 10, 0, 30.2, 10],
        [7, 0, 0.3, 0, 0, 0, 10, 30.3, 10],
    ]
    csv_path.write_text(
        ",".join(header) + "\n" + "\n".join(",".join(str(value) for value in row) for row in rows),
        encoding="utf-8",
    )

    selected = read_selected_rows(
        csv_path,
        cameras={7},
        histogram_bins=4,
        dark_interval=600,
        max_light_frames_per_camera=10,
        max_dark_anchors_per_camera=10,
        chunksize=4,
    )

    rollover_row = selected[selected["frame_id"] == 0].iloc[0]
    assert int(rollover_row["absolute_frame"]) == 255


def test_production_cleanup_subtracts_bin_zero_and_zeroes_noisy_bins():
    hists = np.array([[20, 9, 10, 11], [8, 5, 15, 0]], dtype=float)

    cleaned = production_cleanup_histograms(hists, noisy_bin_min=10)

    np.testing.assert_allclose(cleaned, np.array([[14, 0, 10, 11], [0, 0, 15, 0]], dtype=float))


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


def test_analyze_camera_capped_sampling_excludes_dark_anchor_rows():
    import pandas as pd

    rows = []
    for frame in range(10):
        rows.append(
            {
                "cam_id": 7,
                "frame_id": frame,
                "timestamp_s": frame / 10.0,
                "0": 10 if frame % 3 == 0 else 0,
                "1": 0,
                "2": 10 if frame % 3 != 0 else 0,
                "3": 0,
                "temperature": 30.0 + frame / 10.0,
                "sum": 10,
            }
        )
    df = pd.DataFrame(rows)

    _, frame_metrics = analyze_camera(
        df,
        cam_id=7,
        dark_interval=3,
        histogram_bins=4,
        max_light_frames=4,
    )

    sampled_frames = set(frame_metrics["absolute_frame"].unique())
    assert sampled_frames <= {1, 2, 4, 5, 7, 8}
    assert sampled_frames.isdisjoint({0, 3, 6, 9})
    assert len(sampled_frames) == 4


def test_analyze_camera_current_uses_interpolated_dark_moments_not_histogram_moments():
    import pandas as pd

    def row(frame, hist, is_dark_anchor=False):
        return {
            "cam_id": 7,
            "frame_id": frame + 1,
            "absolute_frame": frame,
            "is_dark_anchor": is_dark_anchor,
            "timestamp_s": frame / 10.0,
            **{str(idx): value for idx, value in enumerate(hist)},
            "temperature": 30.0 + frame / 10.0,
            "sum": sum(hist),
        }

    dark_a = [0] * 24
    dark_a[2] = 100
    light = [0] * 24
    light[2] = 50
    light[22] = 50
    dark_b = [0] * 24
    dark_b[12] = 100
    df = pd.DataFrame(
        [
            row(0, dark_b, is_dark_anchor=True),
            row(9, dark_a),
            row(10, light),
            row(20, dark_b, is_dark_anchor=True),
        ]
    )

    _, frame_metrics = analyze_camera(
        df,
        cam_id=7,
        dark_interval=20,
        histogram_bins=24,
        max_light_frames=10,
        noisy_bin_min=0,
    )

    current_frame_10 = frame_metrics[
        (frame_metrics["method"] == "current") & (frame_metrics["absolute_frame"] == 10)
    ].iloc[0]
    old_interpolated_dark = interpolate_dark_histograms(
        np.array([0, 20]),
        np.array([dark_a, dark_b], dtype=float),
        np.array([10]),
    )[0]
    _, old_diagnostics = correct_histogram(np.array(light, dtype=float), old_interpolated_dark, method="current")

    assert current_frame_10["corrected_mean"] == pytest.approx(5.0)
    assert current_frame_10["corrected_contrast"] == pytest.approx(2.0)
    assert current_frame_10["corrected_contrast"] != pytest.approx(old_diagnostics.corrected_contrast)


def test_analyze_camera_excludes_light_frames_after_last_dark_anchor():
    import pandas as pd

    rows = []
    for frame in [0, 1, 2, 5, 6, 7]:
        hist = [0] * 8
        hist[frame % 8] = 100
        rows.append(
            {
                "cam_id": 7,
                "frame_id": frame + 1,
                "absolute_frame": frame,
                "is_dark_anchor": frame in {0, 5},
                "timestamp_s": frame / 10.0,
                **{str(idx): value for idx, value in enumerate(hist)},
                "temperature": 30.0 + frame / 10.0,
                "sum": 100,
            }
        )
    df = pd.DataFrame(rows)

    _, frame_metrics = analyze_camera(
        df,
        cam_id=7,
        dark_interval=5,
        histogram_bins=8,
        max_light_frames=10,
        noisy_bin_min=0,
    )

    sampled_frames = set(frame_metrics["absolute_frame"].unique())
    assert sampled_frames == {1, 2}
    assert sampled_frames.isdisjoint({6, 7})


def test_analyze_camera_replaces_first_dark_anchor_with_first_good_frame_baseline():
    import pandas as pd

    rows = []
    for frame, peak_bin in [(0, 0), (1, 5), (4, 2), (5, 4)]:
        hist = [0] * 6
        hist[peak_bin] = 100
        rows.append(
            {
                "cam_id": 7,
                "frame_id": frame + 1,
                "absolute_frame": frame,
                "is_dark_anchor": frame in {0, 5},
                "timestamp_s": frame / 10.0,
                **{str(idx): value for idx, value in enumerate(hist)},
                "temperature": 30.0 + frame / 10.0,
                "sum": 100,
            }
        )
    df = pd.DataFrame(rows)

    metrics, frame_metrics = analyze_camera(
        df,
        cam_id=7,
        dark_interval=5,
        histogram_bins=6,
        max_light_frames=10,
        noisy_bin_min=0,
    )

    current_frame_1 = frame_metrics[
        (frame_metrics["method"] == "current") & (frame_metrics["absolute_frame"] == 1)
    ].iloc[0]
    assert current_frame_1["corrected_mean"] == pytest.approx(2.6)
    assert metrics[0]["first_dark_replacement_target"] == 4
    assert metrics[0]["first_dark_replacement_frame"] == 4
    assert metrics[0]["first_dark_replacement_exact"] is True


def test_read_selected_rows_bounds_light_and_dark_rows_to_sample_caps(tmp_path):
    csv_path = tmp_path / "many_light_rows.csv"
    bins = [str(i) for i in range(4)]
    header = ["cam_id", "frame_id", "timestamp_s", *bins, "temperature", "sum"]
    rows = []
    for frame in range(1, 21):
        absolute_frame = frame - 1
        rows.append([7, frame, frame / 10.0, 10 if absolute_frame % 5 == 0 else 0, 0, 10, 0, 30.0, 10])
    csv_path.write_text(
        ",".join(header) + "\n" + "\n".join(",".join(str(value) for value in row) for row in rows),
        encoding="utf-8",
    )

    selected = read_selected_rows(
        csv_path,
        cameras={7},
        histogram_bins=4,
        dark_interval=5,
        max_light_frames_per_camera=3,
        max_dark_anchors_per_camera=2,
        chunksize=6,
    )

    dark_frames = set(selected.loc[selected["is_dark_anchor"], "absolute_frame"])
    light_frames = set(selected.loc[~selected["is_dark_anchor"], "absolute_frame"])
    assert len(dark_frames) <= 2
    assert dark_frames <= {0, 5, 10, 15}
    assert len(light_frames) >= 3
    assert light_frames.isdisjoint(dark_frames)
    assert {1, 2, 3, 4, 6, 7, 8, 9} & light_frames


def test_cli_writes_report_for_tiny_csv(tmp_path):
    csv_path = tmp_path / "tiny_raw.csv"
    bins = [str(i) for i in range(8)]
    header = ["cam_id", "frame_id", "timestamp_s", *bins, "temperature", "sum", "tcm", "tcl", "pdc"]
    rows = [
        [7, 1, 0.0, 10, 0, 0, 0, 0, 0, 0, 0, 30.0, 10, 1, 1, 1],
        [7, 2, 0.1, 0, 0, 15, 15, 0, 0, 0, 0, 30.1, 30, 1, 1, 1],
        [7, 3, 0.2, 0, 0, 14, 16, 0, 0, 0, 0, 30.2, 30, 1, 1, 1],
        [7, 4, 0.3, 0, 20, 0, 0, 0, 0, 0, 0, 30.3, 20, 1, 1, 1],
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
