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
    FRAME_ID_MAX,
    absolute_frame_ids,
    boundary_jumps,
    correct_histogram,
    dark_anchor_mask,
    interpolate_dark_histograms,
    summarize_series,
    temperature_correlation,
)


METHODS = ["raw", "current", "bin_subtract", "deconv_fft"]


def _empty_camera_state() -> dict:
    return {
        "last_frame_id": None,
        "rollovers": 0,
        "light_seen": 0,
        "light_rows": [],
        "dark_rows": [],
        "rng": np.random.default_rng(0),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare dark histogram correction methods.")
    parser.add_argument("--csv", required=True, help="Raw histogram CSV path.")
    parser.add_argument("--cameras", nargs="+", type=int, required=True, help="Camera ids to analyze.")
    parser.add_argument("--dark-interval", type=int, default=600)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--histogram-bins", type=int, default=1024)
    parser.add_argument("--max-light-frames-per-camera", type=int, default=5000)
    return parser.parse_args()


def read_selected_rows(
    csv_path: Path,
    cameras: set[int],
    histogram_bins: int,
    dark_interval: int = 600,
    max_light_frames_per_camera: int = 5000,
    chunksize: int = 25000,
) -> pd.DataFrame:
    usecols = ["cam_id", "frame_id", "timestamp_s", *[str(i) for i in range(histogram_bins)], "temperature", "sum"]
    states = {cam_id: _empty_camera_state() for cam_id in cameras}
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
            last_frame_id = state["last_frame_id"]
            if last_frame_id is not None and frame_id < last_frame_id:
                state["rollovers"] += 1
            state["last_frame_id"] = frame_id
            row["absolute_frame"] = (state["rollovers"] * FRAME_ID_MAX) + frame_id
            row["is_dark_anchor"] = (row["absolute_frame"] % dark_interval) == 0
            if row["is_dark_anchor"]:
                state["dark_rows"].append(row)
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
        selected_rows.extend(states[cam_id]["dark_rows"])
        selected_rows.extend(states[cam_id]["light_rows"])
    if not selected_rows:
        raise ValueError(f"no sampled rows found for cameras {sorted(cameras)}")
    selected = pd.DataFrame(selected_rows)
    return selected.sort_values(["cam_id", "absolute_frame"]).reset_index(drop=True)


def analyze_camera(
    df: pd.DataFrame,
    cam_id: int,
    dark_interval: int,
    histogram_bins: int,
    max_light_frames: int,
) -> tuple[list[dict], pd.DataFrame]:
    cam = df[df["cam_id"] == cam_id].copy()
    if "absolute_frame" not in cam.columns:
        cam["absolute_frame"] = absolute_frame_ids(cam["frame_id"].to_numpy(dtype=int))
    hist_cols = [str(i) for i in range(histogram_bins)]
    hists = cam[hist_cols].to_numpy(dtype=float)
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
    light_mask = ~dark_mask
    light_indices = np.flatnonzero(light_mask)
    if light_indices.size > max_light_frames:
        sample_positions = np.linspace(0, light_indices.size - 1, max_light_frames, dtype=int)
        light_indices = light_indices[sample_positions]
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
