"""
VisualizeBloodflow: compute and plot BFI/BVI from OpenWater histogram CSVs.

Usages:
  - As a module/class from Qt:
        viz = VisualizeBloodflow(left_csv, right_csv)  # right_csv optional
        viz.compute()
        bfi, bvi, camera_inds = viz.get_results()
        # (optionally) viz.plot(); viz.show()

  - CLI:
        python visualize_bloodflow.py --left path/to/left.csv --right path/to/right.csv \
            --t1 0.0 --t2 120 --save out.png
"""

from __future__ import annotations
import argparse
from dataclasses import dataclass, field
from typing import Optional, Tuple
import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


@dataclass
class VisualizeBloodflow:
    left_csv: str
    right_csv: Optional[str] = None

    # Display/time window
    t1: float = 0.0
    t2: float = 120.0

    # Acquisition constants
    frequency_hz: int = 40
    dark_interval: int = 600
    noisy_bin_min: int = 10

    # Calibration (8 cams per module)
    I_min: np.ndarray = field(
        default_factory=lambda: np.array(
            [[0, 0, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0, 0, 0]], dtype=float
        )
    )
    I_max: np.ndarray = field(
        default_factory=lambda: np.array(
            [
                [150, 300, 300, 300, 300, 300, 300, 150],
                [150, 300, 300, 300, 300, 300, 300, 150],
            ],
            dtype=float,
        )
    )
    C_min: np.ndarray = field(
        default_factory=lambda: np.array(
            [
                [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            ],
            dtype=float,
        )
    )
    C_max: np.ndarray = field(
        default_factory=lambda: np.array(
            [
                [0.4, 0.4, 0.45, 0.55, 0.55, 0.45, 0.4, 0.4],
                [0.4, 0.4, 0.45, 0.55, 0.55, 0.45, 0.4, 0.4],
            ],
            dtype=float,
        )
    )

    # Internals (populated after compute)
    _BFI: Optional[np.ndarray] = field(default=None, init=False)
    _BVI: Optional[np.ndarray] = field(default=None, init=False)
    _camera_inds: Optional[np.ndarray] = field(default=None, init=False)
    _contrast: Optional[np.ndarray] = field(default=None, init=False)
    _mean: Optional[np.ndarray] = field(default=None, init=False)
    _nmodules: int = field(default=0, init=False)

    # --------------------------
    # Public API
    # --------------------------
    def compute(self) -> None:
        """Load CSV(s), compute BFI/BVI, keep results in members."""
        # Don't display the first ~20 frames at 40Hz by default
        self.t1 = max(self.t1, 0.5)

        # Determine which files we have
        has_left = self.left_csv and self.left_csv.strip()
        has_right = self.right_csv and self.right_csv.strip()

        if not has_left and not has_right:
            raise ValueError("At least one CSV file (left or right) must be provided")

        # Read first available module
        if has_left:
            histos, camera_inds, timept, temperature = self._readdata(self.left_csv)
            sides = np.array(["left"] * len(camera_inds))
            nmodules = 1
        else:
            # Start with right if no left
            histos, camera_inds, timept, temperature = self._readdata(self.right_csv)
            sides = np.array(["right"] * len(camera_inds))
            nmodules = 1

        # Maybe read second module
        if has_left and has_right:
            histos2, camera_inds2, timept2, temperature2 = self._readdata(
                self.right_csv
            )
            histos = np.concatenate((histos, histos2), axis=0)
            camera_inds = np.concatenate((camera_inds, camera_inds2), axis=0)
            sides = np.concatenate((sides, np.array(["right"] * len(camera_inds2))))
            # temperature/timept not used beyond alignment
            nmodules = 2

        self._sides = sides

        # baseline adjust & noise floor
        histos = histos.astype(float, copy=False)
        histos[:, :, 0] -= 6
        histos[histos < self.noisy_bin_min] = 0

        short_scan = histos.shape[1] < self.dark_interval
        dark_corrected = True

        if short_scan:
            ntimepts = histos.shape[1]
            if ntimepts < 3:
                raise ValueError(
                    f"Scan too short ({ntimepts} frames): need at least 3 frames "
                    "(dark, illuminated…, dark)."
                )
            frame_totals = histos.sum(axis=(0, 2))  # (ntimepts,) – summed over cams & bins
            illum_median = float(np.median(frame_totals[1:-1]))
            if illum_median > 0 and frame_totals[-1] > 0.3 * illum_median:
                import warnings
                warnings.warn(
                    f"Short scan ({ntimepts} frames): final frame does not appear to be a "
                    f"dark frame (last-frame counts {frame_totals[-1]:.0f} vs illuminated "
                    f"median {illum_median:.0f}). Showing uncorrected data.",
                    stacklevel=2,
                )
                print(
                    f"WARNING: Short scan ({ntimepts} frames): final frame does not appear "
                    f"to be a dark frame ({frame_totals[-1]:.0f} vs median {illum_median:.0f}). "
                    "Showing uncorrected data."
                )
                dark_corrected = False
            inds_dark = np.array([0, ntimepts - 1])
        else:
            # crop data so that final frame is dark
            ntimepts = int(
                self.dark_interval * np.floor(histos.shape[1] / self.dark_interval) + 1
            )
            histos = histos[:, :ntimepts, :]
            inds_dark = np.arange(0, ntimepts, self.dark_interval)

        ndark = len(inds_dark)
        histos_dark = np.zeros((len(camera_inds), ndark, 1024), dtype=float)
        for i in range(ndark):
            histos_dark[:, i, :] = histos[:, int(inds_dark[i]), :]

        # replace the 0th dark histogram with the first "good" dark histogram frame
        first_good_dark_frame_index = min(9, ntimepts - 2)
        histos_dark[:, 0, :] = histos[:, first_good_dark_frame_index, :]

        # dark stats
        bins = np.expand_dims(np.arange(1024, dtype=float), axis=0)
        temp1 = self._moments(bins, histos_dark, 1)
        temp2 = self._moments(bins, histos_dark, 2)
        tempv = temp2 - temp1**2  # variance of dark histograms

        u1_dark = np.zeros((len(camera_inds), ntimepts), dtype=float)
        var_dark = np.zeros((len(camera_inds), ntimepts), dtype=float)

        # interpolate dark stats across frames for ALL cams/modules
        for i in range(ndark - 1):
            ind = int(inds_dark[i])
            interval = inds_dark[i + 1] - ind
            ramp = np.arange(interval) / (interval - 1) if interval > 1 else np.zeros(1)
            for j in range(len(camera_inds)):
                u1_dark[j, ind : (ind + interval)] = (
                    temp1[j, i] + (temp1[j, i + 1] - temp1[j, i]) * ramp
                )
                var_dark[j, ind : (ind + interval)] = (
                    tempv[j, i] + (tempv[j, i + 1] - tempv[j, i]) * ramp
                )

        u1_dark[:, -1] = temp1[:, -1]
        var_dark[:, -1] = tempv[:, -1]

        if not dark_corrected:
            u1_dark[:] = 0
            var_dark[:] = 0

        # laser stats
        u1 = self._moments(bins, histos, 1)
        u2 = self._moments(bins, histos, 2)
        mean = u1 - u1_dark
        var = (u2 - u1**2) - var_dark
        std = np.sqrt(np.clip(var, 0.0, None))

        # Safe contrast
        contrast = np.divide(std, mean, out=np.zeros_like(std), where=mean > 0)

        # quadratic interpolation to fill in dark frames
        for i in range(1, ndark - 1):
            mean[:, inds_dark[i]] = (
                (-1 / 6) * mean[:, inds_dark[i] - 2]
                + (2 / 3) * mean[:, inds_dark[i] - 1]
                + (2 / 3) * mean[:, inds_dark[i] + 1]
                + (-1 / 6) * mean[:, inds_dark[i] + 2]
            )
            contrast[:, inds_dark[i]] = (
                (-1 / 6) * contrast[:, inds_dark[i] - 2]
                + (2 / 3) * contrast[:, inds_dark[i] - 1]
                + (2 / 3) * contrast[:, inds_dark[i] + 1]
                + (-1 / 6) * contrast[:, inds_dark[i] + 2]
            )

        # remove first and last (dark) frames
        if dark_corrected:
            mean = mean[:, 1:-1]
            contrast = contrast[:, 1:-1]

        # compute BFI/BVI
        BFI = np.zeros(contrast.shape, dtype=float)
        BVI = np.zeros(mean.shape, dtype=float)

        # Handle cameras by their actual IDs and module assignments
        for ind, cam_id in enumerate(camera_inds):
            # Determine which module this camera belongs to based on the _sides array
            if hasattr(self, "_sides") and ind < len(self._sides):
                module_idx = 0 if self._sides[ind] == "left" else 1
            else:
                # Fallback: assume first half are left, second half are right
                module_idx = 0 if ind < len(camera_inds) // 2 else 1

            # Ensure module index is valid
            module_idx = min(module_idx, nmodules - 1) if nmodules > 0 else 0

            # Use camera ID for calibration lookup (mod 8 to handle camera positions 0-7)
            cam_pos = int(cam_id) % 8

            # Ensure camera position is within calibration array bounds
            if cam_pos < self.C_max.shape[1] and module_idx < self.C_max.shape[0]:
                cden = (
                    self.C_max[module_idx, cam_pos] - self.C_min[module_idx, cam_pos]
                ) or 1.0
                iden = (
                    self.I_max[module_idx, cam_pos] - self.I_min[module_idx, cam_pos]
                ) or 1.0
                BFI[ind, :] = (
                    1 - (contrast[ind, :] - self.C_min[module_idx, cam_pos]) / cden
                ) * 10.0
                BVI[ind, :] = (
                    1 - (mean[ind, :] - self.I_min[module_idx, cam_pos]) / iden
                ) * 10.0
            else:
                # Default calculation for out-of-bounds cameras
                BFI[ind, :] = contrast[ind, :] * 10.0  # Simple fallback
                BVI[ind, :] = mean[ind, :] * 10.0  # Simple fallback

        # stash results
        self._BFI = BFI
        self._BVI = BVI
        self._camera_inds = camera_inds
        self._contrast = contrast
        self._mean = mean
        self._nmodules = nmodules

    def get_results(
        self,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return (BFI, BVI, camera_inds). Call after compute()."""
        if self._BFI is None:
            raise RuntimeError("Call compute() before get_results().")
        return self._BFI, self._BVI, self._camera_inds, self._contrast, self._mean

    def plot(self, legend: Tuple[str, str] = ("BFI", "BVI")) -> plt.Figure:
        """Create the Birmingham-style plot. Returns the matplotlib Figure."""
        if self._BFI is None or self._BVI is None or self._camera_inds is None:
            raise RuntimeError("Call compute() before plot().")

        x = self._BFI
        y = self._BVI
        camera_inds = self._camera_inds
        nmodules = self._nmodules
        if legend[0] == "contrast":
            x = self._contrast
        if legend[1] == "mean":
            y = self._mean

        t = np.arange(x.shape[1], dtype=float) / self.frequency_hz
        # sanitize t2
        t1, t2 = self.t1, (t[-1] if self.t2 <= self.t1 or self.t2 == 0 else self.t2)
        ind1 = int(self.frequency_hz * t1)
        ind2 = int(self.frequency_hz * t2)

        # Determine which modules we actually have
        has_left = any(self._sides[i] == "left" for i in range(len(camera_inds)))
        has_right = any(self._sides[i] == "right" for i in range(len(camera_inds)))

        # Adjust the number of rows and columns based on the number of cameras active
        left_cams = [
            (ind, int(camera_inds[ind]))
            for ind in range(len(camera_inds))
            if self._sides[ind] == "left"
        ]
        right_cams = [
            (ind, int(camera_inds[ind]))
            for ind in range(len(camera_inds))
            if self._sides[ind] == "right"
        ]
        n_left, n_right = len(left_cams), len(right_cams)

        if len(camera_inds) == 16:  # dual sensor, 8 cams each
            nrows = 8
            ncols = 2
        elif has_left ^ has_right and len(camera_inds) == 8:  # single sensor, 8 cams
            nrows = 8
            ncols = 1
        elif (
            has_left and has_right and len(camera_inds) == 8
        ):  # dual sensor, 4 cams each
            nrows = 4
            ncols = 2
        elif has_left ^ has_right and len(camera_inds) == 4:  # single sensor, 4 cams
            nrows = 4
            ncols = 1
        elif (
            has_left and has_right
        ):  # dual sensor, arbitrary channels (e.g. 2+2 for Third Row)
            nrows = max(n_left, n_right)
            ncols = 2
        else:  # single sensor, arbitrary channels (e.g. 2 for Third Row)
            nrows = len(camera_inds)
            ncols = 1

        # Create grid with appropriate number of columns
        fig, ax = plt.subplots(
            nrows=nrows, ncols=ncols, figsize=(6 * ncols, 8), squeeze=False
        )

        # Birmingham mapping: camera position to subplot row (fallback to cam_position for arbitrary counts)
        position_to_row = {0: 0, 1: 1, 2: 2, 3: 3}  # Far sensor on top

        # Initialize all subplots as empty placeholders
        for row in range(nrows):
            for col in range(ncols):
                ax[row, col].text(
                    0.5,
                    0.5,
                    "No Data",
                    ha="center",
                    va="center",
                    transform=ax[row, col].transAxes,
                    fontsize=12,
                    alpha=0.5,
                )
                ax[row, col].set_ylabel(f"Camera Position {row + 1}")
                # Hide ticks for empty plots
                ax[row, col].set_xticks([])
                ax[row, col].set_yticks([])

        # Plot actual camera data (left_cams, right_cams already computed above) - iterate through all cameras using _sides array
        for ind_cam in range(len(camera_inds)):
            # Determine module (column) from _sides array
            is_left = self._sides[ind_cam] == "left"

            # Map to column index based on what modules exist
            if has_left and has_right:
                module_col = 0 if is_left else 1
            elif has_left:
                module_col = 0
            else:  # has_right only
                module_col = 0

            if module_col >= ncols:
                continue

            cam_id = int(camera_inds[ind_cam])

            # Map camera to sequential position within its module (0-3)
            if is_left:
                cam_position = next(
                    i for i, (idx, _) in enumerate(left_cams) if idx == ind_cam
                )
            else:
                cam_position = next(
                    i for i, (idx, _) in enumerate(right_cams) if idx == ind_cam
                )

            # Map position to subplot row using Birmingham mapping
            subplot_row = position_to_row.get(cam_position, cam_position)

            ax_mj = ax[subplot_row, module_col]

            # Clear the placeholder text
            ax_mj.clear()

            # Plot the actual data
            line1 = ax_mj.plot(
                t[ind1:ind2], x[ind_cam, ind1:ind2], "k", linewidth=2, label=legend[0]
            )
            ax2 = ax_mj.twinx()
            line2 = ax2.plot(
                t[ind1:ind2], y[ind_cam, ind1:ind2], "r", linewidth=1, label=legend[1]
            )
            ax2.tick_params(axis="y", colors="red")

            # keep legacy inversion condition if someone passes ('contrast','mean')
            if legend[0] == "contrast":
                ax_mj.invert_yaxis()
            if legend[1] == "mean":
                ax2.invert_yaxis()

            lines = line1 + line2
            labels = [l.get_label() for l in lines]
            ax_mj.legend(lines, labels)
            ax_mj.set_ylabel(f"Camera {cam_id + 1}")

        # Titles/labels based on what modules we have
        if has_left and has_right:
            ax[0, 0].set_title("Left")
            ax[0, 1].set_title("Right")
            ax[-1, 0].set_xlabel("Time (s)")
            ax[-1, 1].set_xlabel("Time (s)")
        elif has_left:
            ax[0, 0].set_title("Left")
            ax[-1, 0].set_xlabel("Time (s)")
        else:  # has_right only
            ax[0, 0].set_title("Right")
            ax[-1, 0].set_xlabel("Time (s)")

        fig.tight_layout()
        return fig

    def show(self) -> None:
        """Show the current matplotlib figure."""
        plt.show()

    def save_results_csv(self, path: str) -> None:
        """Save BFI and BVI results to CSV."""
        if self._BFI is None or self._BVI is None or self._camera_inds is None:
            raise RuntimeError("Call compute() before saving results.")

        nframes = self._BFI.shape[1]
        time_s = np.arange(nframes, dtype=float) / self.frequency_hz

        rows = []
        for cam_idx, cam_id in enumerate(self._camera_inds):
            side = self._sides[cam_idx]
            for frame_idx in range(nframes):
                rows.append(
                    {
                        "camera": int(cam_id),
                        "side": side,
                        "time_s": time_s[frame_idx],
                        "BFI": self._BFI[cam_idx, frame_idx],
                        "BVI": self._BVI[cam_idx, frame_idx],
                    }
                )

        df = pd.DataFrame(rows)

        # Sort by time then camera ID
        df.sort_values(by=["time_s", "camera"], inplace=True, ignore_index=True)

        # Remove file if it already exists
        if os.path.exists(path):
            os.remove(path)

        df.to_csv(path, index=False)
        print(f"Saved results CSV: {path}")

    # --------------------------
    # Internals
    # --------------------------
    @staticmethod
    def _moments(bins: np.ndarray, histos: np.ndarray, power: int) -> np.ndarray:
        """
        bins: shape (1, 1024) or (1024,)
        histos: shape (cams, time, 1024)
        returns: (cams, time)
        """
        s = histos.shape
        m = np.zeros((s[0], s[1]), dtype=float)
        w = bins**power
        for i in range(s[0]):
            numer = np.sum(w * histos[i, :, :], axis=1)
            denom = np.sum(histos[i, :, :], axis=1)
            m[i, :] = np.divide(
                numer, denom, out=np.zeros_like(numer, dtype=float), where=denom > 0
            )
        return m

    @staticmethod
    def _readdata(
        csv_path: str,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        FRAME_ID_MAX = 256

        df = pd.read_csv(csv_path)

        # New format: cam_id, frame_id, timestamp_s, 0..1023, temperature, sum, ...
        # Old format: camera, frame_id, 0..1023, temperature, ...
        if "timestamp_s" in df.columns:
            camera      = df["cam_id"].values.astype(float)
            timept_raw  = df["frame_id"].values.astype(float)
            temperature = df["temperature"].values.astype(float)
            histo_cols  = [str(i) for i in range(1024)]
            histos      = df[histo_cols].values.astype(float)
        else:
            x           = df.values
            camera      = x[:, 0]
            timept_raw  = x[:, 1]
            temperature = x[:, 1026]
            histos      = x[:, 2:1026]

        # Find first frame_id == 1
        ind1 = np.where(timept_raw == 1)[0][0]
        camera      = camera[ind1:]
        timept_raw  = timept_raw[ind1:]
        temperature = temperature[ind1:]
        histos      = histos[ind1:]

        camera_inds = np.unique(camera)
        ncameras    = len(camera_inds)
        rollovers   = np.insert(np.cumsum((np.diff(timept_raw) < 0)), 0, 0)
        timept      = rollovers * FRAME_ID_MAX + timept_raw
        ntimepts    = int(np.amax(timept))

        data = np.zeros((ncameras, ntimepts, 1024), dtype=float)
        for i in range(len(camera)):
            idx = np.where(camera_inds == camera[i])[0][0]
            data[idx, int(timept[i]) - 1, :] = histos[i]

        return data, camera_inds, timept, temperature


# --------------------------
# CLI
# --------------------------
def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Compute and visualize BFI/BVI from histogram CSVs."
    )
    p.add_argument(
        "--left",
        help="Left module CSV file (at least one of --left or --right required)",
    )
    p.add_argument(
        "--right",
        help="Right module CSV file (at least one of --left or --right required)",
    )
    p.add_argument("--t1", type=float, default=0.0, help="Start time (s), default 0.0")
    p.add_argument(
        "--t2",
        type=float,
        default=120.0,
        help="End time (s). If <= t1 or 0, uses full duration",
    )
    p.add_argument("--freq", type=int, default=40, help="Frame rate Hz (default 40)")
    p.add_argument(
        "--dark-interval",
        type=int,
        default=600,
        help="Dark interval (frames) (default 600)",
    )
    p.add_argument(
        "--noisy-bin-min",
        type=int,
        default=10,
        help="Bins below this are zeroed (default 10)",
    )
    p.add_argument(
        "--save",
        help="Save figure to this path (e.g. out.png). If omitted, just shows window",
    )
    p.add_argument(
        "--no-show",
        action="store_true",
        help="Do not display the window (use with --save)",
    )
    return p


def main():
    args = _build_argparser().parse_args()

    # Validate that at least one file is provided
    if not args.left and not args.right:
        print("Error: At least one of --left or --right must be provided")
        return 1

    viz = VisualizeBloodflow(
        left_csv=args.left,
        right_csv=args.right,
        t1=args.t1,
        t2=args.t2,
        frequency_hz=args.freq,
        dark_interval=args.dark_interval,
        noisy_bin_min=args.noisy_bin_min,
    )
    viz.compute()

    if args.save:
        csv_path = args.save.rsplit(".", 1)[0] + "_results.csv"
        viz.save_results_csv(csv_path)

    # Plot
    fig = viz.plot(legend=("BFI", "BVI"))

    # Save/Show
    if args.save:
        fig.savefig(args.save, dpi=150, bbox_inches="tight")
        print(f"Saved figure: {args.save}")
    if not args.no_show:
        viz.show()


if __name__ == "__main__":
    main()
