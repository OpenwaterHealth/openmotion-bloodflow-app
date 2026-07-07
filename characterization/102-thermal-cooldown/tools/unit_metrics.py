"""Per-module thermal metrics for the #330 characterization.

Reduces one unit's heat run (E1 1 Hz trace) + post-scan cooldown log (E2a)
+ deaths table into a small set of defined metrics, keyed by the sensor
module's serial number. Append rows to a shared CSV so units/modules are
directly comparable.

METRIC DEFINITIONS (per sensor module)
--------------------------------------
Thermal shutdown behavior (from a cold-start 20-min all-cam 40 Hz laser-on run):
  T_amb_C     ambient proxy: coolest camera's median die temp in the first
              10 s of the run (cold start makes t=0 ~ ambient).
  T_ss_hot_C  hottest slot's steady state: max over cameras of the median
              die temp in the final 120 s.
  LM_C        latch margin = 112.7 - T_ss_hot_C. The distance between the
              hottest steady state and the bottom of the measured latch
              band (112.7-117.5 degC, n=10). Positive => the module cannot
              thermally latch at this ambient, no matter the scan length.
  TSI         Thermal Stress Index = (T_ss_hot_C - T_amb_C) /
              (112.7 - T_amb_C). Dimensionless fraction of the way from
              ambient to the latch band; < 1.0 is Regime A, and headroom
              (1 - TSI) is ambient-portable (a hotter room shifts both
              numerator and denominator).
  HR5_C_min   initial heating rate of the eventually-hottest camera over
              the first 5 min (degC/min) — how quickly margin is consumed.
  SSS_C       steady-state spread = max - min across the module's 8
              cameras' final-120 s medians. Assembly-uniformity indicator
              (a degraded pad shows up here first).
  n_latch     cameras that died or never started (deaths.py events).
  t_latch_s   earliest death time, if any.

Cooldown behavior (from the post-scan powered-idle plain-read log):
  tau_cool_min   worst per-camera exponential cooling constant (min),
                 T(t) = T_floor + (T0-T_floor)exp(-t/tau); r2_min reported.
  T_floor_C      hottest fitted powered-idle floor (degC).
  TTR60_min      measured time-to-ready: wall minutes from log start until
                 EVERY camera on the module is <= 60 degC (the gate value).
                 0.0 = already below at log start.
  TTR45_min      same for a 45 degC gate.

Usage:
  python unit_metrics.py --unit unit-A --experiment E1a_unitA_heat20_cold \
      --e1 results/unit-A/E1a_temps_1hz.csv --e2a results/unit-A/e2a.csv \
      --deaths results/thermal_deaths.csv \
      --serial left=QWW24Q10011 --serial right=EVT2SN82 \
      --fw 1.8.1-rc.3-dirty --out results/module_metrics.csv
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

LATCH_LOW_C = 112.7   # bottom of the measured latch band (n=10, live TPM)


def fit_cool(t: np.ndarray, temp: np.ndarray):
    """(tau_s, floor_c, r2) exponential fit; NaNs if not fittable."""
    fin = np.isfinite(temp)
    if fin.sum() < 10:
        return float("nan"), float("nan"), float("nan")
    tail = temp[fin][-max(3, int(fin.sum() * 0.10)):]
    floor = float(np.min(tail)) - 0.5
    mask = fin & (temp > floor + 2.0)
    if mask.sum() < 5:
        return float("nan"), floor, float("nan")
    x = t[mask] - t[mask][0]
    y = np.log(temp[mask] - floor)
    slope, intercept = np.polyfit(x, y, 1)
    if slope >= 0:
        return float("nan"), floor, float("nan")
    pred = slope * x + intercept
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - float(np.sum((y - pred) ** 2)) / ss_tot if ss_tot > 0 else float("nan")
    return -1.0 / slope, floor, r2


def ttr(cams: pd.DataFrame, gate_c: float) -> float:
    """Minutes from log start until every camera is <= gate_c (per burst)."""
    ok_t = None
    for t, g in cams.groupby("elapsed_s"):
        vals = g.temp_c[np.isfinite(g.temp_c)]
        if len(vals) and vals.max() <= gate_c:
            if ok_t is None:
                ok_t = t
        else:
            ok_t = None
    return round(ok_t / 60.0, 1) if ok_t is not None else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", required=True)
    ap.add_argument("--experiment", required=True,
                    help="deaths.csv experiment name for the heat run")
    ap.add_argument("--e1", required=True, help="heat-run 1 Hz temps CSV")
    ap.add_argument("--e2a", required=True, help="post-scan cooldown log CSV")
    ap.add_argument("--deaths", required=True)
    ap.add_argument("--serial", action="append", default=[],
                    metavar="side=SERIAL", help="module serial per side")
    ap.add_argument("--fw", default="?")
    ap.add_argument("--out", default=None, help="append rows to this CSV")
    args = ap.parse_args()
    serials = dict(s.split("=", 1) for s in args.serial)

    e1 = pd.read_csv(args.e1)
    end = e1.t_s.max()
    deaths = pd.read_csv(args.deaths)
    deaths = deaths[deaths.experiment == args.experiment]

    e2 = pd.read_csv(args.e2a)
    e2 = e2[e2.cam != "imu"].copy()
    e2["temp_c"] = pd.to_numeric(e2.temp_c, errors="coerce")

    rows = []
    for side, g in e1.groupby("side"):
        first = g[g.t_s <= 10].groupby("cam_id").die_temp_c.median()
        last = g[g.t_s > end - 120].groupby("cam_id").die_temp_c.median()
        t_amb = float(first.min())
        t_ss = float(last.max())
        hot_cam = int(last.idxmax())
        hg = g[g.cam_id == hot_cam]
        t0 = float(hg[hg.t_s <= 10].die_temp_c.median())
        t5 = float(hg[(hg.t_s >= 290) & (hg.t_s <= 310)].die_temp_c.median())
        dside = deaths[deaths.side == side]
        died = dside[dside.event != "survived"]

        sg = e2[e2.side == side]
        taus, floors, r2s = [], [], []
        for cam, cg in sg.groupby("cam"):
            cg = cg.sort_values("elapsed_s")
            tau, floor, r2 = fit_cool(cg.elapsed_s.to_numpy(dtype=float),
                                      cg.temp_c.to_numpy(dtype=float))
            if math.isfinite(tau):
                taus.append(tau); r2s.append(r2)
            if math.isfinite(floor):
                floors.append(floor)

        rows.append({
            "unit": args.unit, "side": side,
            "serial": serials.get(side, "?"), "fw": args.fw,
            "experiment": args.experiment,
            "T_amb_C": round(t_amb, 1),
            "T_ss_hot_C": round(t_ss, 1),
            "LM_C": round(LATCH_LOW_C - t_ss, 1),
            "TSI": round((t_ss - t_amb) / (LATCH_LOW_C - t_amb), 3),
            "HR5_C_min": round((t5 - t0) / 5.0, 2),
            "SSS_C": round(float(last.max() - last.min()), 1),
            "n_latch": int(len(died)),
            "t_latch_s": (round(float(died.death_time_s.min()), 1)
                          if len(died) else float("nan")),
            "tau_cool_min": round(max(taus) / 60.0, 2) if taus else float("nan"),
            "r2_min": round(min(r2s), 3) if r2s else float("nan"),
            "T_floor_C": round(max(floors), 1) if floors else float("nan"),
            "TTR60_min": ttr(sg, 60.0),
            "TTR45_min": ttr(sg, 45.0),
        })

    out = pd.DataFrame(rows)
    print(out.to_string(index=False))
    if args.out:
        if os.path.exists(args.out):
            old = pd.read_csv(args.out)
            merged = pd.concat([old, out], ignore_index=True)
            merged = merged.drop_duplicates(
                subset=["unit", "side", "experiment"], keep="last")
        else:
            merged = out
        merged.to_csv(args.out, index=False)
        print(f"appended -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
