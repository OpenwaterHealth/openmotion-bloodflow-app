#!/usr/bin/env python3
import pandas as pd
import matplotlib.pyplot as plt
import argparse
import numpy as np
from typing import Tuple

NUM_BINS = 1024
FRAME_ID_MAX = 256
CAMERA_ORDER = [3, 4, 2, 5, 1, 6, 0, 7]


def parse_args():
    p = argparse.ArgumentParser(
        description="Visualize histogram CSV (μ & σ) with optional temperature."
    )
    p.add_argument(
        "--csv",
        nargs="+",
        required=True,
        help="One or more CSV files (e.g. left and right). They will be concatenated.",
    )
    p.add_argument(
        "--skip-first",
        type=int,
        default=0,
        help="Skip first N frames per camera (after logical indexing).",
    )
    p.add_argument(
        "--t1",
        type=float,
        default=None,
        help="Start time index (logical frames) to plot. If omitted, start at 0.",
    )
    p.add_argument(
        "--t2",
        type=float,
        default=None,
        help="End time index (logical frames). If omitted, use full length.",
    )
    p.add_argument(
        "--ignore-last-bin",
        action="store_true",
        help="Zero-out the last histogram bin (1023).",
    )
    p.add_argument(
        "--save",
        type=str,
        default=None,
        help="Path to save the figure (PNG). If not set, shows window.",
    )
    return p.parse_args()


def logical_frame_index(series: pd.Series) -> pd.Series:
    rollovers = (series.diff() < 0).cumsum()
    return rollovers * FRAME_ID_MAX + series


def cam_stats(
    cam_df: pd.DataFrame, ignore_last_bin: bool
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    cam_df = cam_df.copy()
    cam_df["logical_frame_index"] = logical_frame_index(cam_df["frame_id"])
    cam_df.sort_values("logical_frame_index", inplace=True)

    histo = cam_df.iloc[:, 2 : 2 + NUM_BINS].to_numpy()
    if ignore_last_bin:
        histo[:, -1] = 0

    sums = histo.sum(axis=1)
    bins = np.arange(NUM_BINS)

    # μ
    mu = np.divide(histo @ bins, sums, out=np.zeros_like(sums, float), where=sums != 0)
    # σ
    second_moment = np.divide(
        histo @ (bins**2), sums, out=np.zeros_like(sums, float), where=sums != 0
    )
    sigma = np.sqrt(np.clip(second_moment - mu**2, 0, None))

    temp = cam_df["temperature"].to_numpy() if "temperature" in cam_df.columns else None
    frame_ids = cam_df["logical_frame_index"].to_numpy()
    return frame_ids, mu, sigma, temp


def main():
    args = parse_args()
    print("Reading CSV(s):", ", ".join(args.csv))

    # Concatenate multiple CSVs if provided (left/right, etc.)
    dfs = [pd.read_csv(path) for path in args.csv]
    df = pd.concat(dfs, ignore_index=True)

    # Basic sanity columns
    required = ["cam_id", "frame_id"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing required column '{col}' in CSV.")

    fig, axes = plt.subplots(nrows=4, ncols=2, figsize=(12, 10), sharex=False)
    axes = axes.flatten()

    for ax, cam_id in zip(axes, CAMERA_ORDER):
        cam_df = df[df["cam_id"] == cam_id]
        ax.set_title(f"Camera {cam_id}")

        if cam_df.empty:
            ax.text(0.5, 0.5, "No data", ha="center", va="center")
            ax.set_axis_off()
            continue

        frame_ids, mu, sigma, temp = cam_stats(cam_df, args.ignore_last_bin)

        # Optional trimming
        # First, skip-first N frames (after logical index)
        if args.skip_first > 0:
            keep = frame_ids >= (frame_ids.min() + args.skip_first)
            frame_ids, mu, sigma = frame_ids[keep], mu[keep], sigma[keep]
            if temp is not None:
                temp = temp[keep]

        # Then t1/t2 window
        if args.t1 is not None:
            keep = frame_ids >= args.t1
            frame_ids, mu, sigma = frame_ids[keep], mu[keep], sigma[keep]
            if temp is not None:
                temp = temp[keep]
        if args.t2 is not None:
            keep = frame_ids <= args.t2
            frame_ids, mu, sigma = frame_ids[keep], mu[keep], sigma[keep]
            if temp is not None:
                temp = temp[keep]

        (ln_mu,) = ax.plot(frame_ids, mu, "o-", markersize=3, label="μ (weighted avg)")
        (ln_sg,) = ax.plot(frame_ids, sigma, "s--", markersize=3, label="σ (std dev)")
        ax.set_ylabel("Bin index")
        ax.grid(True)

        lines = [ln_mu, ln_sg]
        labels = [ln_mu.get_label(), ln_sg.get_label()]

        if temp is not None:
            ax2 = ax.twinx()
            (ln_tp,) = ax2.plot(
                frame_ids, temp, "d-.", markersize=3, color="tab:red", label="Temp (°C)"
            )
            ax2.set_ylabel("Temperature (°C)", color="tab:red")
            ax2.tick_params(axis="y", labelcolor="tab:red")
            lines.append(ln_tp)
            labels.append(ln_tp.get_label())

        ax.legend(lines, labels, fontsize="x-small", loc="best")

    plt.tight_layout()
    if args.save:
        plt.savefig(args.save, dpi=150)
        print(f"Saved figure to {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
