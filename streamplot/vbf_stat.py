"""
This vbf_stat.py module contains code for profiling operation used for blood flow acquisition , plotting and stoing.

All the profiling code is placed into the main_profiling function.

To start MongoDB installed locally: 
C:\MongoDB\bin\mongod.exe --config C:\MongoDB\mongod.conf


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
from typing import Optional, Tuple, List
import os
import numpy as np  
import pandas as pd
import matplotlib.pyplot as plt
import fastplotlib as fplt
print(fplt.__version__)
import tqdm
from tqdm import trange
import io
import sys
from contextlib import redirect_stdout

#custom modules:
import profile_hist
from profile_hist import ExecutionTimer
import hist_db

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
    I_min: np.ndarray = field(default_factory=lambda: np.array(
        [[0, 0, 0, 0, 0, 0, 0, 0],
         [0, 0, 0, 0, 0, 0, 0, 0]], dtype=float))
    I_max: np.ndarray = field(default_factory=lambda: np.array(
        [[150, 300, 300, 300, 300, 300, 300, 150],
         [150, 300, 300, 300, 300, 300, 300, 150]], dtype=float))
    C_min: np.ndarray = field(default_factory=lambda: np.array(
        [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
         [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]], dtype=float))
    C_max: np.ndarray = field(default_factory=lambda: np.array(
        [[0.4, 0.4, 0.45, 0.55, 0.55, 0.45, 0.4, 0.4],
         [0.4, 0.4, 0.45, 0.55, 0.55, 0.45, 0.4, 0.4]], dtype=float))

    # Internals (populated after compute)
    _BFI: Optional[np.ndarray] = field(default=None, init=False)
    _BVI: Optional[np.ndarray] = field(default=None, init=False)
    _camera_inds: Optional[np.ndarray] = field(default=None, init=False)
    _contrast: Optional[np.ndarray] = field(default=None, init=False)
    _mean: Optional[np.ndarray] = field(default=None, init=False)
    _nmodules: int = field(default=0, init=False)
    #
    fig = None
    ffig = None
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
            histos2, camera_inds2, timept2, temperature2 = self._readdata(self.right_csv)
            histos = np.concatenate((histos, histos2), axis=0)
            camera_inds = np.concatenate((camera_inds, camera_inds2), axis=0)
            sides = np.concatenate((sides, np.array(["right"] * len(camera_inds2))))
            # temperature/timept not used beyond alignment
            nmodules = 2

        self._sides = sides


        # raise an error if the number of points acquired in either histogram is less than the dark_interval
        if histos.shape[1] < self.dark_interval:
            raise ValueError("The number of points acquired in either histogram is less than the dark_interval")
        
        # baseline adjust & noise floor
        histos = histos.astype(float, copy=False)
        histos[:, :, 0] -= 6
        histos[histos < self.noisy_bin_min] = 0

        # crop data so that final frame is dark
        ntimepts = int(self.dark_interval * np.floor(histos.shape[1] / self.dark_interval) + 1)
        histos = histos[:, :ntimepts, :]

        # get dark histograms (every dark_interval frames)
        inds_dark = np.arange(0, ntimepts, self.dark_interval)
        ndark = len(inds_dark)
        histos_dark = np.zeros((len(camera_inds), ndark, 1024), dtype=float)
        for i in range(ndark):
            histos_dark[:, i, :] = histos[:, int(inds_dark[i]), :]

        # dark stats
        bins = np.expand_dims(np.arange(1024, dtype=float), axis=0)
        temp1 = self._moments(bins, histos_dark, 1)
        temp2 = self._moments(bins, histos_dark, 2)
        tempv = temp2 - temp1 ** 2 # variance of dark histograms

        u1_dark = np.zeros((len(camera_inds), ntimepts), dtype=float)
        var_dark = np.zeros((len(camera_inds), ntimepts), dtype=float)

        # interpolate dark stats across frames for ALL cams/modules
        for i in range(ndark - 1):
            ind = int(inds_dark[i])
            interval = inds_dark[i + 1] - ind
            ramp = np.arange(interval) / (interval - 1) if interval > 1 else np.zeros(1)
            for j in range(len(camera_inds)):
                u1_dark[j, ind:(ind + interval)] = temp1[j, i] + (temp1[j, i + 1] - temp1[j, i]) * ramp
                var_dark[j, ind:(ind + interval)] = tempv[j, i] + (tempv[j, i + 1] - tempv[j, i]) * ramp

        u1_dark[:, -1] = temp1[:, -1]
        var_dark[:, -1] = tempv[:, -1]

        # laser stats
        u1 = self._moments(bins, histos, 1)
        u2 = self._moments(bins, histos, 2)
        mean = u1 - u1_dark
        var = (u2 - u1 ** 2) - var_dark
        std = np.sqrt(np.clip(var, 0.0, None))

        # Safe contrast
        contrast = np.divide(std, mean, out=np.zeros_like(std), where=mean > 0)

        # quadratic interpolation to fill in dark frames
        for i in range(1, ndark - 1):
            mean[:, inds_dark[i]] = (-1/6) * mean[:, inds_dark[i] - 2] + (2/3) * mean[:, inds_dark[i] - 1] \
                                    + (2/3) * mean[:, inds_dark[i] + 1] + (-1/6) * mean[:, inds_dark[i] + 2]
            contrast[:, inds_dark[i]] = (-1/6) * contrast[:, inds_dark[i] - 2] + (2/3) * contrast[:, inds_dark[i] - 1] \
                                        + (2/3) * contrast[:, inds_dark[i] + 1] + (-1/6) * contrast[:, inds_dark[i] + 2]

        # remove first and last (dark) frames
        mean = mean[:, 1:-1]
        contrast = contrast[:, 1:-1]

        # compute BFI/BVI
        BFI = np.zeros(contrast.shape, dtype=float)
        BVI = np.zeros(mean.shape, dtype=float)
        
        # Handle cameras by their actual IDs and module assignments
        for ind, cam_id in enumerate(camera_inds):
            # Determine which module this camera belongs to based on the _sides array
            if hasattr(self, '_sides') and ind < len(self._sides):
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
                cden = (self.C_max[module_idx, cam_pos] - self.C_min[module_idx, cam_pos]) or 1.0
                iden = (self.I_max[module_idx, cam_pos] - self.I_min[module_idx, cam_pos]) or 1.0
                BFI[ind, :] = (1 - (contrast[ind, :] - self.C_min[module_idx, cam_pos]) / cden) * 10.0
                BVI[ind, :] = (1 - (mean[ind, :] - self.I_min[module_idx, cam_pos]) / iden) * 10.0
            else:
                # Default calculation for out-of-bounds cameras
                BFI[ind, :] = contrast[ind, :] * 10.0  # Simple fallback
                BVI[ind, :] = mean[ind, :] * 10.0      # Simple fallback

        # stash results
        self._BFI = BFI
        self._BVI = BVI
        self._camera_inds = camera_inds
        self._contrast = contrast
        self._mean = mean
        self._nmodules = nmodules

    def get_results(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return (BFI, BVI, camera_inds). Call after compute()."""
        if self._BFI is None:
            raise RuntimeError("Call compute() before get_results().")
        return self._BFI, self._BVI, self._camera_inds, self._contrast, self._mean

    def plot(self, legend: Tuple[str, str] = ('BFI', 'BVI')) -> plt.Figure:
        """Create the Birmingham-style plot. Returns the matplotlib Figure."""
        if self._BFI is None or self._BVI is None or self._camera_inds is None:
            raise RuntimeError("Call compute() before plot().")

        x = self._BFI
        y = self._BVI
        camera_inds = self._camera_inds
        nmodules = self._nmodules
        if legend[0] == 'contrast':
            x = self._contrast
        if legend[1] == 'mean':
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
        if len(camera_inds) == 16: # this is dual camera 8 cams
            nrows = 8
            ncols = 2
        elif has_left ^ has_right and len(camera_inds) == 8: # this is single camera 8 cams
            nrows = 8
            ncols = 1
        elif has_left and has_right and len(camera_inds) == 8: # this is dual camera 4 cams
            nrows = 4
            ncols = 2
        elif has_left ^ has_right and len(camera_inds) == 4: # this is single camera 4 cams
            nrows = 4
            ncols = 1

        # Create grid with appropriate number of columns
        fig, ax = plt.subplots(nrows=nrows, ncols=ncols, figsize=(6 * ncols, 8), squeeze=False)

        # Birmingham mapping: camera position to subplot row
        position_to_row = {0: 0, 1: 1, 2: 2, 3: 3}  # Far sensor on top
        
        # Initialize all subplots as empty placeholders
        for row in range(4):
            for col in range(ncols):
                ax[row, col].text(0.5, 0.5, 'No Data', 
                                ha='center', va='center', 
                                transform=ax[row, col].transAxes,
                                fontsize=12, alpha=0.5)
                ax[row, col].set_ylabel(f'Camera Position {row + 1}')
                # Hide ticks for empty plots
                ax[row, col].set_xticks([])
                ax[row, col].set_yticks([])

        # Group cameras by module to get sequential positions per module
        left_cams = [(ind, int(camera_inds[ind])) for ind in range(len(camera_inds)) if self._sides[ind] == "left"]
        right_cams = [(ind, int(camera_inds[ind])) for ind in range(len(camera_inds)) if self._sides[ind] == "right"]
        
        # Plot actual camera data - iterate through all cameras using _sides array
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
                cam_position = next(i for i, (idx, _) in enumerate(left_cams) if idx == ind_cam)
            else:
                cam_position = next(i for i, (idx, _) in enumerate(right_cams) if idx == ind_cam)
            
            # Map position to subplot row using Birmingham mapping
            subplot_row = position_to_row.get(cam_position, cam_position)
            
            ax_mj = ax[subplot_row, module_col]
            
            # Clear the placeholder text
            ax_mj.clear()
            
            # Plot the actual data
            line1 = ax_mj.plot(t[ind1:ind2], x[ind_cam, ind1:ind2], 'k', linewidth=2, label=legend[0])
            ax2 = ax_mj.twinx()
            line2 = ax2.plot(t[ind1:ind2], y[ind_cam, ind1:ind2], 'r', linewidth=1, label=legend[1])
            ax2.tick_params(axis='y', colors='red')

            # keep legacy inversion condition if someone passes ('contrast','mean')
            if legend[0] == 'contrast':
                ax_mj.invert_yaxis()
            if legend[1] == 'mean':
                ax2.invert_yaxis()

            lines = line1 + line2
            labels = [l.get_label() for l in lines]
            ax_mj.legend(lines, labels)
            ax_mj.set_ylabel(f'Camera {cam_id + 1}')

        # Titles/labels based on what modules we have
        if has_left and has_right:
            ax[0, 0].set_title('Left')
            ax[0, 1].set_title('Right')
            ax[-1, 0].set_xlabel('Time (s)')
            ax[-1, 1].set_xlabel('Time (s)')
        elif has_left:
            ax[0, 0].set_title('Left')
            ax[-1, 0].set_xlabel('Time (s)')
        else:  # has_right only
            ax[0, 0].set_title('Right')
            ax[-1, 0].set_xlabel('Time (s)')

        fig.tight_layout()
        return fig


    def custom_zoom(self, event):

        # event contains 'dy', 'x', 'y', 'modifiers'
        zoom_speed = 0.9

        dy = event.dy

        z = 0.9 if dy < 0.0 else 1.1


        p = self.fig[0,0].camera.get_state()
        sy = p["scale"]#[1]
        sy[1] *= z
        p["scale"] = sy
        #self.fig[0,0].camera.set_state(p)
        event.stop_propagation()
        return
        
        c = self.fig[0,0].controller
        r = c.get_y_range()
        #p = event.subplot
        ylim = fig[0,0]
        current_xlim = self.fig[0,0].get_axes_limits('x')
        # Calculate new limits based on scroll direction (dy)
        if event['dy'] < 0: # Zoom in
            new_xlim = [lim * (1 - zoom_speed) for lim in current_xlim]
        else: # Zoom out
            new_xlim = [lim * (1 + zoom_speed) for lim in current_xlim]
        # Apply new limits
        self.fig[0,0].set_axes_limits(x=new_xlim)

    def fplot(self, legend: Tuple[str, str] = ('BFI', 'BVI')) -> fplt.Figure:
        """Create the Birmingham-style plot. Returns the matplotlib Figure."""
        if self._BFI is None or self._BVI is None or self._camera_inds is None:
            raise RuntimeError("Call compute() before plot().")

        x = self._BFI
        y = self._BVI
        camera_inds = self._camera_inds
        nmodules = self._nmodules
        if legend[0] == 'contrast':
            x = self._contrast
        if legend[1] == 'mean':
            y = self._mean

        l1 = len(self._BFI[0])
        l2 = len(self._BVI)
        l3 = len(self._contrast)
        l4 = len(self._mean)

        t = np.arange(x.shape[1], dtype=float) / self.frequency_hz

        lt = len(t)

        # sanitize t2
        t1, t2 = self.t1, (t[-1] if self.t2 <= self.t1 or self.t2 == 0 else self.t2)
        ind1 = int(self.frequency_hz * t1)
        ind2 = int(self.frequency_hz * t2)

        # Determine which modules we actually have
        has_left = any(self._sides[i] == "left" for i in range(len(camera_inds)))
        has_right = any(self._sides[i] == "right" for i in range(len(camera_inds)))
        
        
        # Adjust the number of rows and columns based on the number of cameras active
        if len(camera_inds) == 16: # this is dual camera 8 cams
            nrows = 8
            ncols = 2
        elif has_left ^ has_right and len(camera_inds) == 8: # this is single camera 8 cams
            nrows = 8
            ncols = 1
        elif has_left and has_right and len(camera_inds) == 8: # this is dual camera 4 cams
            nrows = 4
            ncols = 2
        elif has_left ^ has_right and len(camera_inds) == 4: # this is single camera 4 cams
            nrows = 4
            ncols = 1

        # Create grid with appropriate number of columns
        #self.fig = fplt.Figure(shape=(nrows, ncols), controller_ids="sync", size=(1000, 800))
        self.ffig = fplt.Figure(shape=(nrows, ncols), size=(1000, 800))


        self.ffig[0,0].auto_scale(maintain_aspect=False)
        self.ffig[1,0].auto_scale(maintain_aspect=False)
        self.ffig[2,0].auto_scale(maintain_aspect=False)
        self.ffig[3,0].auto_scale(maintain_aspect=False)

        r00 = x[0,:]
        b00 = y[0,:]
        min00 = r00.min()
        max00 = r00.max()
        plot00x = self.ffig[0, 0].add_line(x[0,:],colors="red")
        #plot00y = fig[0, 0].add_line(y[0,:],colors="blue")

        #fig[0,0].auto_scale()#.camera..set_range(y=(min00,max00))

        r10 = x[1,:]
        b10 = y[1,:]
        min10 = r10.min()
        max10 = r10.max()
        plot10x = self.ffig[1, 0].add_line(x[1,:],colors="red")
        #plot10y = fig[1, 0].add_line(y[1,:],colors="blue")

        #fig[1,0].camera.set_range(y=(min10,max10))

        r20 = x[2,:]
        b20 = y[2,:]
        min20 = r20.min()
        max20 = r20.max()
        plot20x = self.ffig[2, 0].add_line(x[2,:],colors="red")
        #plot20y = fig[2, 0].add_line(y[2,:],colors="blue")
        
        #fig[2,0].controller..camera..world..scale_y.set_range(y=(min20,max20))

        r30 = x[3,:]
        b30 = y[3,:]
        min30 = r30.min()
        max30 = r30.max()
        plot30x = self.ffig[3, 0].add_line(x[3,:],colors="red")
        #plot30y = fig[3, 0].add_line(y[3,:],colors="blue")

        #fig[3,0].camera.set_range(y=(min30,max30))

#        plots=[
#            plot00x,plot00y,
#            plot10x,plot10y,
#            plot20x,plot20y,
#            plot30x,plot30y,
#            ]
        return self.ffig


    def show(self) -> None:
        """Show the current figure."""
        fplt.loop.run()

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
                rows.append({
                    "camera": int(cam_id),
                    "side": side,
                    "time_s": time_s[frame_idx],
                    "BFI": self._BFI[cam_idx, frame_idx],
                    "BVI": self._BVI[cam_idx, frame_idx],
                })

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
        w = (bins ** power)
        for i in range(s[0]):
            numer = np.sum(w * histos[i, :, :], axis=1)
            denom = np.sum(histos[i, :, :], axis=1)
            m[i, :] = np.divide(numer, denom, out=np.zeros_like(numer, dtype=float), where=denom > 0)
        return m

    @staticmethod
    def _readdata(csv_path: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        FRAME_ID_MAX = 256

        x = np.array(pd.read_csv(csv_path))
        ind1 = np.where(x[:, 1] == 1)[0][0]  # 1st line in csv with good data
        x = x[ind1:, :]
        camera = x[:, 0]
        timept = x[:, 1]
        temperature = x[:, 1026]

        camera_inds = np.unique(camera)
        ncameras = len(camera_inds)
        rollovers = np.insert(np.cumsum((np.diff(timept) < 0)), 0, 0)

        timept = rollovers * FRAME_ID_MAX + timept
        ntimepts = int(np.amax(timept))

        data = np.zeros((ncameras, ntimepts, 1024), dtype=float)
        for i in range(len(camera)):
            idx = np.where(camera_inds == camera[i])[0][0]
            data[idx, int(timept[i]) - 1, :] = x[i, 2:1026]

        return data, camera_inds, timept, temperature


# --------------------------
# CLI
# --------------------------
def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Compute and visualize BFI/BVI from histogram CSVs.")
    p.add_argument("--left", help="Left module CSV file (at least one of --left or --right required)")
    p.add_argument("--right", help="Right module CSV file (at least one of --left or --right required)")
    p.add_argument("--t1", type=float, default=0.0, help="Start time (s), default 0.0")
    p.add_argument("--t2", type=float, default=120.0, help="End time (s). If <= t1 or 0, uses full duration")
    p.add_argument("--freq", type=int, default=40, help="Frame rate Hz (default 40)")
    p.add_argument("--dark-interval", type=int, default=600, help="Dark interval (frames) (default 600)")
    p.add_argument("--noisy-bin-min", type=int, default=10, help="Bins below this are zeroed (default 10)")
    p.add_argument("--save", help="Save figure to this path (e.g. out.png). If omitted, just shows window")
    p.add_argument("--no-show", action="store_true", help="Do not display the window (use with --save)")
    return p
""" """
def main_profile():
    args = _build_argparser().parse_args()
    csv_path = "C:/Users/ethan/Projects/ow-bloodflow-app/scan_data/scan_owTM2GXS_20260214_125021_left_mask99.csv"
    
    ncycles = 100 #!!! number of execution cyckles for profiling
    print_distribution = False#True#False #Print the estimated profiled time distribution

    try:
        sqlite_connection=hist_db.init_sqlite_db("s.db")

        is_keyframe=1
        sensor_id=1
        ts=111
        #
        csv_data_in = hist_db.read_csv_file( csv_path )
        #
        levels=[6,3]
        for level in levels:
            print(f"Compress zlib level {level} =================")
            extimer = ExecutionTimer()
            for i in range(ncycles):
                blob_compressed_zlib = extimer.time_function(hist_db.compress_zlib, csv_data_in, level)
            extimer.hist.save_out(f"../../../tmp/compress_zlib_{level}_hist.txt", print_distribution)
            print(f"From {len(csv_data_in)} to {len(blob_compressed_zlib)}  level {level}")
        pass
        #
        for level in levels:
            print(f"Compress zstd level {level} =================")
            extimer = ExecutionTimer()
            for i in range(ncycles):
                blob_compressed_zstd = extimer.time_function(hist_db.compress_zstd, csv_data_in, level)
            extimer.hist.save_out(f"../../../tmp/compress_zstd_{level}_hist.txt", print_distribution)
            print(f"From {len(csv_data_in)} to {len(blob_compressed_zstd)}  level {level}")
        pass
        #
        print("Insert blob to SQlite=====================")
        extimer = ExecutionTimer()
        for i in range(ncycles):
            extimer.time_function(hist_db.insert_blob_sqlite, sqlite_connection, ts, sensor_id, is_keyframe, blob_compressed_zstd )
        extimer.hist.save_out("../../../tmp/insert_blob_sql_hist.txt", print_distribution)
        pass
        #
        print("Retrieve blob from SQLite====================")
        extimer = ExecutionTimer()
        for i in range(ncycles):
            blob_out = extimer.time_function(hist_db.retrieve_blob_sqlite, sqlite_connection, ts, sensor_id)
        extimer.hist.save_out("../../../tmp/retrieve_blob_sql_hist.txt", print_distribution)
        pass
        #
        print("Decompress CSV zlib=====================")
        extimer = ExecutionTimer()
        for i in range(ncycles):
            csv_data_out_zlib = extimer.time_function(hist_db.decompress_csv_zlib, blob_compressed_zlib)
        extimer.hist.save_out("../../../tmp/decompress_csv_hist.txt", print_distribution)
        pass
        #
        print("Insert blob to SQl=====================")
        extimer = ExecutionTimer()
        for i in range(ncycles):
            extimer.time_function(hist_db.insert_csv_data_sqlite, sqlite_connection, ts, sensor_id, is_keyframe, blob_compressed_zstd )
        extimer.hist.save_out("../../../tmp/insert_blob_sql_hist.txt", print_distribution)
        pass
        #
        print("Retrieve blob from SQl=====================")
        extimer = ExecutionTimer()
        row = None
        for i in range(ncycles):
            row = extimer.time_function(hist_db.retrieve_csv_data_sqlite, sqlite_connection, ts, sensor_id )
        extimer.hist.save_out("../../../tmp/retrieve_blob_sql_hist.txt", print_distribution)
        pass
        #
        print("Write CSV to file=====================")
        extimer = ExecutionTimer()
        path = "csv_file.csv"
        for i in range(ncycles):
            extimer.time_function(hist_db.write_csv_file, path, csv_data_in  )
        extimer.hist.save_out("../../../tmp/write_csv_hist.txt", print_distribution)
        pass
        #
        print("Read CSV from file=====================")
        extimer = ExecutionTimer()
        path = "csv_file.csv"
        for i in range(ncycles):
            read_csv_data = extimer.time_function(hist_db.read_csv_file, path )
        extimer.hist.save_out("../../../tmp/read_csv_hist.txt", print_distribution)
        pass
        # C:\MongoDB\bin\mongod.exe --config C:\MongoDB\mongod.conf
        collection = hist_db.init_mongodb()
        #
        print("Insert blob to MongoDB=====================")
        extimer = ExecutionTimer()
        path = "csv_file.csv"
        for i in range(ncycles):
            read_csv_data = extimer.time_function(hist_db.store_csv_blob_to_mongo, collection, ts, sensor_id, is_keyframe, blob_compressed_zstd)
        extimer.hist.save_out("../../../tmp/insert_blob_to_mongo_hist.txt", print_distribution)
        pass
        #
        print("Retrieve blob from MongoDB=====================")
        extimer = ExecutionTimer()
        path = "csv_file.csv"
        for i in range(ncycles):
            blob_out = extimer.time_function(hist_db.retrieve_csv_blob_from_mongo, collection, ts, sensor_id)
        extimer.hist.save_out("../../../tmp/retrieve_blob_from_mongo_hist.txt", print_distribution)
        pass
        #
        viz = VisualizeBloodflow(
            left_csv=args.left,
            right_csv=args.right,
            t1=args.t1,
            t2=args.t2,
            frequency_hz=args.freq,
            dark_interval=args.dark_interval,
            noisy_bin_min=args.noisy_bin_min,
        )
         #
        print("Compute=====================")
        extimer = ExecutionTimer()
        for i in range(ncycles):
            extimer.time_function(viz.compute)
        extimer.hist.save_out("../../../tmp/compute_hist.txt", print_distribution)
        pass
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if 'connection' in locals():
            sqlite_connection.close()

""" """
if __name__ == "__main__":
    if True:
        main_profile()
    else:
        buffer = io.StringIO()
        # Redirect stdout to the buffer
        with redirect_stdout(buffer):
            main_profile()
        captured_output = buffer.getvalue()
        print(captured_output)
        with open("../../../tmp/profile.txt", "w") as f:
            f.write(captured_output)
    pass