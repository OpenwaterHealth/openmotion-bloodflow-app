#!/usr/bin/env python3
"""
Plot BFI and BVI from a _corrected.csv file produced by the OpenMOTION SDK.

Supports two CSV formats:

**Normal mode** — per-camera columns (bfi_l1..bfi_l8, bfi_r1..bfi_r8, etc.).
The subplot grid mirrors the physical camera layout described in
docs/CameraArrangement.md:

    Col 0  Col 1  |  Col 2  Col 3
    ─────────────────────────────
    L:C1   L:C8   │  R:C1   R:C8   ← row 0 (top)
    L:C2   L:C7   │  R:C2   R:C7
    L:C3   L:C6   │  R:C3   R:C6
    L:C4   L:C5   │  R:C4   R:C5   ← row 3 (bottom)

Inactive cameras are omitted entirely.  Empty rows and columns that result
from cameras being inactive are collapsed so no whitespace is wasted.

**Reduced mode** — averaged per-side columns (bfi_left, bfi_right,
bvi_left, bvi_right).  Each side is shown in a single subplot.

Each subplot shows:
    Left  y-axis  — BFI (solid black, lw=2)
    Right y-axis  — BVI (solid red,   lw=1)

Optional secondary figure (--show-signal) adds mean, std, and contrast
(normal mode only — reduced mode CSVs do not contain these columns).

Usage
-----
    python plot_corrected_scan.py --csv path/to/_corrected.csv
    python plot_corrected_scan.py --csv scan.csv --show-signal --save
"""

import argparse
import os
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Camera layout constants  (from docs/CameraArrangement.md)
# ---------------------------------------------------------------------------

# Camera number (1-indexed) → (grid_row, sensor_col) within one sensor's 4×2 grid
CAMERA_GRID_POS = {
    1: (0, 0),
    2: (1, 0),
    3: (2, 0),
    4: (3, 0),
    5: (3, 1),
    6: (2, 1),
    7: (1, 1),
    8: (0, 1),
}

# Sensor side → offset added to sensor_col to get the full plot-grid column
SENSOR_COL_OFFSET = {"left": 0, "right": 2}

SIDES = ("left", "right")

# Reduced-mode column names (per-side averaged)
REDUCED_BFI = {"left": "bfi_left", "right": "bfi_right"}
REDUCED_BVI = {"left": "bvi_left", "right": "bvi_right"}


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def _is_reduced_mode(df: pd.DataFrame) -> bool:
    """Return True if the CSV uses reduced-mode (per-side averaged) columns."""
    return "bfi_left" in df.columns or "bfi_right" in df.columns


# ---------------------------------------------------------------------------
# Column name helpers
# ---------------------------------------------------------------------------

def _bfi(side, cam):      return f"bfi_{side[0]}{cam}"
def _bvi(side, cam):      return f"bvi_{side[0]}{cam}"
def _mean(side, cam):     return f"mean_{side[0]}{cam}"
def _std(side, cam):      return f"std_{side[0]}{cam}"
def _contrast(side, cam): return f"contrast_{side[0]}{cam}"
def _temp(side, cam):     return f"temp_{side[0]}{cam}"


# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------

def _active_cells(df: pd.DataFrame, sides: list[str]) -> list[tuple]:
    """
    Return (grid_row, plot_col, side, cam) for every camera that has data,
    in the order they appear in the physical layout.
    """
    cells = []
    for side in sides:
        for cam in range(1, 9):
            col = _bfi(side, cam)
            if col in df.columns and df[col].notna().any():
                grid_row, sensor_col = CAMERA_GRID_POS[cam]
                plot_col = sensor_col + SENSOR_COL_OFFSET[side]
                cells.append((grid_row, plot_col, side, cam))
    return cells


def _collapse(cells: list[tuple]) -> tuple[dict, dict, int, int]:
    """
    Collapse empty rows/columns and return
    (row_map, col_map, n_subplot_rows, n_subplot_cols).
    """
    active_rows = sorted({c[0] for c in cells})
    active_cols = sorted({c[1] for c in cells})
    row_map = {r: i for i, r in enumerate(active_rows)}
    col_map = {c: i for i, c in enumerate(active_cols)}
    return row_map, col_map, len(active_rows), len(active_cols)


def _requested_sides(df: pd.DataFrame, requested: str) -> list[str]:
    candidates = SIDES if requested == "both" else (requested,)
    if _is_reduced_mode(df):
        return [
            s for s in candidates
            if REDUCED_BFI[s] in df.columns and df[REDUCED_BFI[s]].notna().any()
        ]
    return [
        s for s in candidates
        if any(
            _bfi(s, cam) in df.columns and df[_bfi(s, cam)].notna().any()
            for cam in range(1, 9)
        )
    ]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Plot OpenMOTION corrected scan CSV")
    p.add_argument("--csv", required=True, help="Path to the _corrected.csv file")
    p.add_argument(
        "--sides", choices=["left", "right", "both"], default="both",
        help="Which sensor side(s) to plot (default: both)",
    )
    p.add_argument(
        "--show-signal", action="store_true",
        help="Also show a figure with corrected mean, std, and contrast",
    )
    p.add_argument(
        "--save", action="store_true",
        help="Save figures as PNG files next to the CSV instead of displaying",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Reduced-mode figure builder
# ---------------------------------------------------------------------------

def _make_reduced_figure(
    df: pd.DataFrame,
    active_sides: list[str],
) -> plt.Figure:
    """
    Build a figure for reduced-mode CSVs.  One subplot per active side,
    showing averaged BFI (left y-axis) and BVI (right y-axis).
    """
    ts = df["timestamp_s"].to_numpy()
    n_cols = len(active_sides)

    fig, axes = plt.subplots(
        nrows=1, ncols=n_cols,
        figsize=(7 * n_cols, 5),
        squeeze=False,
    )

    for i, side in enumerate(active_sides):
        ax = axes[0, i]
        label = side.capitalize()
        ax.set_title(f"{label} — Averaged BFI / BVI", fontsize=14)

        bfi_col = REDUCED_BFI[side]
        bvi_col = REDUCED_BVI[side]

        bfi_vals = df[bfi_col].to_numpy(dtype=float) if bfi_col in df.columns else None
        bvi_vals = df[bvi_col].to_numpy(dtype=float) if bvi_col in df.columns else None

        if bfi_vals is not None:
            ln1, = ax.plot(ts, bfi_vals, "k", lw=2, label="BFI")
            ax.set_ylabel("BFI")
        if bvi_vals is not None:
            ax2 = ax.twinx()
            ln2, = ax2.plot(ts, bvi_vals, "r", lw=1, label="BVI")
            ax2.tick_params(axis="y", colors="red")

        lines, labels = [], []
        if bfi_vals is not None:
            lines.append(ln1); labels.append("BFI")
        if bvi_vals is not None:
            lines.append(ln2); labels.append("BVI")
        if lines:
            ax.legend(lines, labels)

        ax.set_xlabel("Time (s)")

    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# Figure builder
# ---------------------------------------------------------------------------

def _make_figure(
    df: pd.DataFrame,
    cells: list[tuple],
    row_map: dict,
    col_map: dict,
    n_rows: int,
    n_cols: int,
    *,
    mode: str,       # "bfi" or "signal"
) -> plt.Figure:
    """
    Build and return one figure.  mode="bfi" plots BFI/BVI;
    mode="signal" plots mean/std/contrast.
    """
    ts = df["timestamp_s"].to_numpy()

    fig, axes = plt.subplots(
        nrows=n_rows, ncols=n_cols,
        figsize=(6 * n_cols, 8),
        squeeze=False,
    )

    # Hide every subplot; we'll re-enable only the active ones.
    for ax in axes.flat:
        ax.set_visible(False)

    for grid_row, plot_col, side, cam in cells:
        sr = row_map[grid_row]
        sc = col_map[plot_col]
        ax = axes[sr, sc]
        ax.set_visible(True)

        label = f"{'L' if side == 'left' else 'R'}:  Cam {cam}"
        ax.set_title(label)

        if mode == "bfi":
            bfi_vals = df[_bfi(side, cam)].to_numpy(dtype=float)
            bvi_vals = df[_bvi(side, cam)].to_numpy(dtype=float)
            ln1, = ax.plot(ts, bfi_vals, "k", lw=2, label="BFI")
            ax.set_ylabel("BFI")

            ax2 = ax.twinx()
            ln2, = ax2.plot(ts, bvi_vals, "r", lw=1, label="BVI")
            ax2.tick_params(axis="y", colors="red")

            lines, labels = [ln1, ln2], ["BFI", "BVI"]

        else:  # signal
            contrast_vals = df[_contrast(side, cam)].to_numpy(dtype=float)
            mean_vals     = df[_mean(side, cam)].to_numpy(dtype=float)
            ln1, = ax.plot(ts, contrast_vals, "k", lw=2, label="Contrast")
            ax.set_ylabel("Contrast")
            ax.invert_yaxis()

            ax2 = ax.twinx()
            ln2, = ax2.plot(ts, mean_vals, "r", lw=1, label="Mean")
            ax2.tick_params(axis="y", colors="red")
            ax2.invert_yaxis()

            lines, labels = [ln1, ln2], ["Contrast", "Mean"]

        ax.legend(lines, labels)

    # x-axis labels on bottom row only
    for sc in range(n_cols):
        axes[n_rows - 1, sc].set_xlabel("Time (s)")

    fig.tight_layout()

    return fig


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if not os.path.isfile(args.csv):
        print(f"ERROR: file not found: {args.csv}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading: {args.csv}")
    df = pd.read_csv(args.csv)
    print(f"  {len(df)} rows, {len(df.columns)} columns")

    if "timestamp_s" not in df.columns:
        print("ERROR: 'timestamp_s' column not found — is this a _corrected.csv?",
              file=sys.stderr)
        sys.exit(1)

    active_sides = _requested_sides(df, args.sides)
    if not active_sides:
        print(f"ERROR: no data found for requested side(s): {args.sides}", file=sys.stderr)
        sys.exit(1)

    reduced = _is_reduced_mode(df)

    if reduced:
        print("  Reduced-mode CSV detected (per-side averaged data)")
        for side in active_sides:
            print(f"  {side.capitalize()} side: averaged")

        fig_bfi = _make_reduced_figure(df, active_sides)

        if args.save:
            out = os.path.splitext(args.csv)[0] + "_bfi.png"
            fig_bfi.savefig(out, dpi=150, bbox_inches="tight")
            print(f"  Saved: {out}")

        if args.show_signal:
            print("  Note: --show-signal ignored for reduced-mode CSVs "
                  "(no contrast/mean columns)", file=sys.stderr)

    else:
        for side in active_sides:
            cams = [c for c in range(1, 9) if _bfi(side, c) in df.columns
                    and df[_bfi(side, c)].notna().any()]
            print(f"  {side.capitalize()} side: cameras {cams}")

        cells = _active_cells(df, active_sides)
        row_map, col_map, n_rows, n_cols = _collapse(cells)
        print(f"  Grid: {n_rows} row(s) × {n_cols} col(s)")

        kwargs = dict(
            cells=cells, row_map=row_map, col_map=col_map,
            n_rows=n_rows, n_cols=n_cols,
        )

        fig_bfi = _make_figure(df, mode="bfi", **kwargs)

        if args.save:
            out = os.path.splitext(args.csv)[0] + "_bfi.png"
            fig_bfi.savefig(out, dpi=150, bbox_inches="tight")
            print(f"  Saved: {out}")

        if args.show_signal:
            fig_sig = _make_figure(df, mode="signal", **kwargs)
            if args.save:
                out = os.path.splitext(args.csv)[0] + "_signal.png"
                fig_sig.savefig(out, dpi=150, bbox_inches="tight")
                print(f"  Saved: {out}")

    if not args.save:
        plt.show()


if __name__ == "__main__":
    main()
