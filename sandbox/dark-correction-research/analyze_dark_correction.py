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
    CorrectionDiagnostics,
    FRAME_ID_MAX,
    absolute_frame_ids,
    boundary_jumps,
    correct_histogram,
    dark_anchor_mask,
    histogram_moments,
    interpolate_dark_histograms,
    summarize_series,
    temperature_correlation,
)


METHODS = ["raw", "current", "bin_subtract", "deconv_fft"]


def _empty_camera_state() -> dict:
    return {
        "started": False,
        "last_frame_id": None,
        "rollovers": 0,
        "light_seen": 0,
        "dark_seen": 0,
        "light_rows": [],
        "first_dark_row": None,
        "latest_dark_row": None,
        "internal_dark_seen": 0,
        "internal_dark_rows": [],
        "replacement_candidate_rows": [],
        "rng": np.random.default_rng(0),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare dark histogram correction methods.")
    parser.add_argument("--csv", required=True, help="Raw histogram CSV path.")
    parser.add_argument("--cameras", nargs="+", type=int, required=True, help="Camera ids to analyze.")
    parser.add_argument("--dark-interval", type=int, default=600)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--histogram-bins", type=int, default=1024)
    parser.add_argument("--noisy-bin-min", type=int, default=10)
    parser.add_argument("--max-light-frames-per-camera", type=int, default=5000)
    parser.add_argument("--max-dark-anchors-per-camera", type=int, default=5000)
    return parser.parse_args()


def production_cleanup_histograms(hists: np.ndarray, noisy_bin_min: int = 10) -> np.ndarray:
    cleaned = np.asarray(hists, dtype=float).copy()
    if cleaned.shape[-1] > 0:
        cleaned[..., 0] -= 6
    cleaned[cleaned < noisy_bin_min] = 0
    return cleaned


def production_interval_interpolate(
    anchor_frames: np.ndarray,
    anchor_values: np.ndarray,
    target_frames: np.ndarray,
) -> np.ndarray:
    anchors = np.asarray(anchor_frames, dtype=float)
    values = np.asarray(anchor_values, dtype=float)
    targets = np.asarray(target_frames, dtype=float)
    if anchors.ndim != 1:
        raise ValueError("anchor_frames must be 1-D")
    if values.shape[0] != anchors.size:
        raise ValueError("anchor_values row count must match anchor_frames")
    if anchors.size == 0:
        raise ValueError("at least one dark anchor is required")
    if np.any(np.diff(anchors) < 0):
        raise ValueError("anchor_frames must be sorted")
    if anchors.size == 1:
        return np.repeat(values[:1], targets.size, axis=0)

    output = np.empty((targets.size, *values.shape[1:]), dtype=float)
    interval_indices = np.searchsorted(anchors, targets, side="right") - 1
    for output_idx, interval_idx in enumerate(interval_indices):
        if interval_idx < 0:
            output[output_idx] = values[0]
            continue
        if interval_idx >= anchors.size - 1:
            output[output_idx] = values[-1]
            continue
        start_frame = anchors[interval_idx]
        next_frame = anchors[interval_idx + 1]
        denominator = max(next_frame - start_frame - 1.0, 1.0)
        weight = float(np.clip((targets[output_idx] - start_frame) / denominator, 0.0, 1.0))
        output[output_idx] = values[interval_idx] + (values[interval_idx + 1] - values[interval_idx]) * weight
    return output


def read_selected_rows(
    csv_path: Path,
    cameras: set[int],
    histogram_bins: int,
    dark_interval: int = 600,
    max_light_frames_per_camera: int = 5000,
    max_dark_anchors_per_camera: int = 5000,
    chunksize: int = 25000,
) -> pd.DataFrame:
    if max_dark_anchors_per_camera < 2:
        raise ValueError("max_dark_anchors_per_camera must be at least 2 to preserve structural dark anchors")
    usecols = ["cam_id", "frame_id", "timestamp_s", *[str(i) for i in range(histogram_bins)], "temperature", "sum"]
    states = {cam_id: _empty_camera_state() for cam_id in cameras}
    max_internal_dark_anchors = max_dark_anchors_per_camera - 2
    found_rows = False
    for chunk in pd.read_csv(csv_path, usecols=usecols, chunksize=chunksize):
        chunk = chunk[chunk["cam_id"].isin(cameras)]
        if chunk.empty:
            continue
        found_rows = True
        for row in chunk.to_dict("records"):
            cam_id = int(row["cam_id"])
            state = states[cam_id]
            frame_id = int(row["frame_id"])
            if not state["started"]:
                if frame_id != 1:
                    continue
                state["started"] = True

            last_frame_id = state["last_frame_id"]
            if last_frame_id is not None and frame_id < last_frame_id:
                state["rollovers"] += 1
            state["last_frame_id"] = frame_id
            row["absolute_frame"] = (state["rollovers"] * FRAME_ID_MAX) + frame_id - 1
            row["is_dark_anchor"] = (row["absolute_frame"] % dark_interval) == 0
            if 0 < row["absolute_frame"] <= 9 and not row["is_dark_anchor"]:
                state["replacement_candidate_rows"].append(row.copy())
            if row["is_dark_anchor"]:
                state["dark_seen"] += 1
                if state["first_dark_row"] is None:
                    state["first_dark_row"] = row
                    state["latest_dark_row"] = row
                    continue

                previous_latest = state["latest_dark_row"]
                if (
                    previous_latest is not None
                    and previous_latest["absolute_frame"] != state["first_dark_row"]["absolute_frame"]
                ):
                    state["internal_dark_seen"] += 1
                    if len(state["internal_dark_rows"]) < max_internal_dark_anchors:
                        state["internal_dark_rows"].append(previous_latest)
                    elif max_internal_dark_anchors:
                        replacement_index = int(state["rng"].integers(0, state["internal_dark_seen"]))
                        if replacement_index < max_internal_dark_anchors:
                            state["internal_dark_rows"][replacement_index] = previous_latest
                state["latest_dark_row"] = row
                continue

            state["light_seen"] += 1
            if len(state["light_rows"]) < max_light_frames_per_camera:
                state["light_rows"].append(row)
                continue

            replacement_index = int(state["rng"].integers(0, state["light_seen"]))
            if replacement_index < max_light_frames_per_camera:
                state["light_rows"][replacement_index] = row
    if not found_rows:
        raise ValueError(f"no rows found for cameras {sorted(cameras)}")
    selected_rows = []
    for cam_id in sorted(states):
        first_dark_row = states[cam_id]["first_dark_row"]
        latest_dark_row = states[cam_id]["latest_dark_row"]
        if first_dark_row is not None:
            selected_rows.append(first_dark_row)
        selected_rows.extend(states[cam_id]["internal_dark_rows"])
        if (
            latest_dark_row is not None
            and first_dark_row is not None
            and latest_dark_row["absolute_frame"] != first_dark_row["absolute_frame"]
        ):
            selected_rows.append(latest_dark_row)
        selected_rows.extend(states[cam_id]["light_rows"])
        selected_rows.extend(states[cam_id]["replacement_candidate_rows"])
    if not selected_rows:
        raise ValueError(f"no sampled rows found for cameras {sorted(cameras)}")
    selected = pd.DataFrame(selected_rows)
    selected = selected.drop_duplicates(subset=["cam_id", "absolute_frame"], keep="first")
    return selected.sort_values(["cam_id", "absolute_frame"]).reset_index(drop=True)


def analyze_camera(
    df: pd.DataFrame,
    cam_id: int,
    dark_interval: int,
    histogram_bins: int,
    max_light_frames: int,
    noisy_bin_min: int = 10,
) -> tuple[list[dict], pd.DataFrame]:
    cam = df[df["cam_id"] == cam_id].copy()
    if "absolute_frame" not in cam.columns:
        frame_ids = cam["frame_id"].to_numpy(dtype=int)
        start_positions = np.flatnonzero(frame_ids == 1)
        if start_positions.size == 0:
            raise ValueError(f"camera {cam_id} has no frame_id 1 start row")
        cam = cam.iloc[int(start_positions[0]) :].copy()
        cam["absolute_frame"] = absolute_frame_ids(cam["frame_id"].to_numpy(dtype=int)) - 1
    hist_cols = [str(i) for i in range(histogram_bins)]
    hists = production_cleanup_histograms(cam[hist_cols].to_numpy(dtype=float), noisy_bin_min=noisy_bin_min)
    frames = cam["absolute_frame"].to_numpy(dtype=int)
    temperatures = cam["temperature"].to_numpy(dtype=float)
    if "is_dark_anchor" in cam.columns:
        dark_mask = cam["is_dark_anchor"].to_numpy(dtype=bool)
    else:
        dark_mask = dark_anchor_mask(frames, dark_interval=dark_interval)
    if dark_mask.sum() < 1:
        raise ValueError(f"camera {cam_id} has no dark anchors")
    dark_frames = frames[dark_mask]
    dark_hists = hists[dark_mask]
    max_frame = int(frames.max())
    first_dark_replacement_target = int(min(9, max(max_frame - 1, 0)))
    first_dark_replacement_frame = int(dark_frames[0])
    first_dark_replacement_exact = False
    replacement_candidates = np.arange(frames.size)
    if replacement_candidates.size:
        nearest_pos = int(np.argmin(np.abs(frames[replacement_candidates] - first_dark_replacement_target)))
        replacement_idx = int(replacement_candidates[nearest_pos])
        dark_hists = dark_hists.copy()
        dark_hists[0] = hists[replacement_idx]
        first_dark_replacement_frame = int(frames[replacement_idx])
        first_dark_replacement_exact = first_dark_replacement_frame == first_dark_replacement_target
    max_dark_frame = int(dark_frames.max())
    light_mask = (~dark_mask) & (frames <= max_dark_frame)
    light_indices = np.flatnonzero(light_mask)
    if light_indices.size > max_light_frames:
        sample_positions = np.linspace(0, light_indices.size - 1, max_light_frames, dtype=int)
        light_indices = light_indices[sample_positions]
    light_frames = frames[light_indices]
    interpolated_dark = interpolate_dark_histograms(dark_frames, dark_hists, light_frames)
    dark_moments = [histogram_moments(dark_hist) for dark_hist in dark_hists]
    interpolated_dark_means = production_interval_interpolate(
        dark_frames,
        np.array([moment.mean for moment in dark_moments], dtype=float),
        light_frames,
    )
    interpolated_dark_variances = production_interval_interpolate(
        dark_frames,
        np.array([moment.variance for moment in dark_moments], dtype=float),
        light_frames,
    )
    interpolated_dark_totals = production_interval_interpolate(
        dark_frames,
        np.array([moment.total for moment in dark_moments], dtype=float),
        light_frames,
    )

    metric_rows = []
    frame_rows = []
    for method in METHODS:
        means = []
        contrasts = []
        clipped = []
        ringing = []
        start = time.perf_counter()
        if method == "current":
            for row_idx, dark_mean, dark_variance, dark_total in zip(
                light_indices,
                interpolated_dark_means,
                interpolated_dark_variances,
                interpolated_dark_totals,
            ):
                light_moment = histogram_moments(hists[row_idx])
                corrected_mean = light_moment.mean - float(dark_mean)
                corrected_variance = max(light_moment.variance - float(dark_variance), 0.0)
                corrected_std = float(np.sqrt(corrected_variance))
                corrected_contrast = corrected_std / corrected_mean if corrected_mean > 0 else 0.0
                diagnostics = CorrectionDiagnostics(
                    method=method,
                    input_mass=light_moment.total,
                    dark_mass=float(dark_total),
                    output_mass=light_moment.total,
                    clipped_mass=0.0,
                    corrected_mean=float(corrected_mean),
                    corrected_variance=float(corrected_variance),
                    corrected_contrast=float(corrected_contrast),
                )
                means.append(diagnostics.corrected_mean)
                contrasts.append(diagnostics.corrected_contrast)
                clipped.append(diagnostics.clipped_mass)
                ringing.append(diagnostics.ringing_score)
        else:
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
        metric_rows.append(
            {
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
                "first_dark_replacement_target": first_dark_replacement_target,
                "first_dark_replacement_frame": first_dark_replacement_frame,
                "first_dark_replacement_exact": first_dark_replacement_exact,
            }
        )
        frame_rows.extend(
            {
                "cam_id": cam_id,
                "method": method,
                "absolute_frame": int(frame),
                "temperature": float(temp),
                "corrected_mean": float(mean),
                "corrected_contrast": float(contrast),
            }
            for frame, temp, mean, contrast in zip(light_frames, temp_arr, means_arr, contrast_arr)
        )
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
    df = read_selected_rows(
        csv_path,
        set(args.cameras),
        args.histogram_bins,
        args.dark_interval,
        args.max_light_frames_per_camera,
        args.max_dark_anchors_per_camera,
    )
    all_metrics = []
    all_frames = []
    for cam_id in args.cameras:
        rows, frame_df = analyze_camera(
            df,
            cam_id,
            args.dark_interval,
            args.histogram_bins,
            args.max_light_frames_per_camera,
            args.noisy_bin_min,
        )
        all_metrics.extend(rows)
        all_frames.append(frame_df)
    metrics = pd.DataFrame(all_metrics)
    frame_metrics = pd.concat(all_frames, ignore_index=True)
    write_report(output_dir, metrics, frame_metrics)
    print(f"Wrote report: {output_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
