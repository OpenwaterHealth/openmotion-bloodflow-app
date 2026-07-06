"""Fit cool-down curves from a thermal_logger.py CSV — #102 characterization.

Per (side, cam): fits T(t) = T_floor + (T0 - T_floor) * exp(-t / tau) by
linear regression on ln(T - T_floor). T_floor defaults to the per-camera
tail (last 10% of samples) minimum minus 0.5 C; override with --ambient
if the log was cut short of equilibrium (a short log biases tau low).

Reports per camera: T0 (hottest recorded), tau (minutes), and predicted
minutes from T0 down to each --targets temperature. These numbers feed the
cooldown-lockout model: lockout duration ~= tau * ln((T0-Tf)/(Ttarget-Tf)).

Usage:
    python cooldown_fit.py e2a_cooldown_powered.csv --targets 60,45
    python cooldown_fit.py e2b_cooldown_railoff.csv --ambient 24 --out fits.csv
"""
from __future__ import annotations

import argparse
import math
import sys

import numpy as np
import pandas as pd


def fit_one(t: np.ndarray, temp: np.ndarray, t_floor: float):
    """Return (tau_s, r2) for T = Tf + (T0-Tf)exp(-t/tau), or (nan, nan)."""
    mask = np.isfinite(temp) & (temp > t_floor + 2.0)
    if mask.sum() < 5:
        return float("nan"), float("nan")
    x = t[mask] - t[mask][0]
    y = np.log(temp[mask] - t_floor)
    slope, intercept = np.polyfit(x, y, 1)
    if slope >= 0:
        return float("nan"), float("nan")
    pred = slope * x + intercept
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return -1.0 / slope, r2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--ambient", type=float, default=None,
                    help="fixed T_floor (degC) for every camera; default = per-camera "
                         "tail minimum - 0.5")
    ap.add_argument("--targets", default="60,45",
                    help="comma list of target temps to predict time-to-reach (degC)")
    ap.add_argument("--out", default=None, help="optional output CSV of fits")
    args = ap.parse_args()
    targets = [float(x) for x in args.targets.split(",") if x.strip()]

    df = pd.read_csv(args.csv)
    df = df[df.cam != "imu"].copy()
    df["cam"] = df.cam.astype(int)
    df["temp_c"] = pd.to_numeric(df.temp_c, errors="coerce")

    rows = []
    for (side, cam), g in df.groupby(["side", "cam"]):
        g = g.sort_values("elapsed_s")
        t = g.elapsed_s.to_numpy(dtype=float)
        temp = g.temp_c.to_numpy(dtype=float)
        fin = np.isfinite(temp)
        if fin.sum() < 5:
            continue
        tail = temp[fin][-max(3, int(fin.sum() * 0.10)):]
        t_floor = args.ambient if args.ambient is not None \
            else float(np.min(tail)) - 0.5
        t0 = float(np.nanmax(temp))
        tau_s, r2 = fit_one(t, temp, t_floor)
        row = dict(side=side, cam=cam, T0_c=round(t0, 1),
                   T_floor_c=round(t_floor, 1),
                   tau_min=round(tau_s / 60.0, 2) if math.isfinite(tau_s) else float("nan"),
                   r2=round(r2, 3) if math.isfinite(r2) else float("nan"))
        for tgt in targets:
            key = f"min_to_{tgt:g}C"
            if math.isfinite(tau_s) and t0 > tgt > t_floor:
                row[key] = round(tau_s / 60.0 * math.log((t0 - t_floor) / (tgt - t_floor)), 1)
            else:
                row[key] = float("nan")
        rows.append(row)

    out = pd.DataFrame(rows).sort_values(["side", "cam"])
    print(out.to_string(index=False))
    if len(out):
        worst = out.tau_min.max()
        print(f"\nworst-case tau: {worst:.1f} min "
              f"(lockout model should use this camera)")
    if args.out:
        out.to_csv(args.out, index=False)
        print(f"saved -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
