"""Thermal-shutdown tracker: extract camera death events from experiment data.

A camera "died" if its last frame arrives >10 s before the side's last frame.
For each death: time, die temp at death (median of last 2 s of frames), max
temp reached, temp slope into death, plus context (gain, scan type).
Cameras with zero frames are recorded as never_started (thermal latch from a
previous run persisting through configure).

Appends rows to data/thermal_deaths.csv (deduped by experiment+side+cam).

Usage:
    python deaths.py <expdir> [expdir2 ...]      # analyze + append
    python deaths.py --report                    # print the accumulated table
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

CAMPAIGN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEATHS_CSV = os.path.join(CAMPAIGN, "results", "thermal_deaths.csv")
GAIN = {0: 16, 1: 4, 2: 2, 3: 1, 4: 1, 5: 2, 6: 4, 7: 16}

COLS = ["experiment", "side", "cam", "gain_x", "event", "death_time_s",
        "scan_end_s", "temp_at_death_c", "max_temp_c",
        "temp_slope_c_per_min", "temp_frozen", "laser", "notes"]


def analyze_experiment(expdir: str) -> list[dict]:
    means = os.path.join(expdir, "means.csv")
    if not os.path.exists(means):
        return []
    name = os.path.basename(expdir.rstrip("\\/"))
    df = pd.read_csv(means)
    df = df[df["type"].isin(["light", "dark"])]
    laser = "off"
    try:
        with open(os.path.join(expdir, "manifest.json"), "r", encoding="utf-8") as fh:
            man = json.load(fh)
        tc = man.get("trigger_config") or {}
        laser = "off" if (man.get("disable_laser") or tc.get("TriggerStatus") == 1) \
            else "on"
    except Exception:
        pass

    rows: list[dict] = []
    for side, ssub in df.groupby("side"):
        side_end = ssub.timestamp_s.max()
        expected = set(range(8))
        seen = set(ssub.cam_id.unique())
        for cam in sorted(expected - seen):
            rows.append(dict(experiment=name, side=side, cam=cam,
                             gain_x=GAIN[cam], event="never_started",
                             death_time_s=0.0, scan_end_s=round(side_end, 1),
                             temp_at_death_c=np.nan, max_temp_c=np.nan,
                             temp_slope_c_per_min=np.nan, temp_frozen=False,
                             laser=laser,
                             notes="no frames; latch from prior run?"))
        for cam, csub in ssub.groupby("cam_id"):
            c = csub.sort_values("timestamp_s")
            last_t = c.timestamp_s.max()
            died = (side_end - last_t) > 10.0
            tail = c[c.timestamp_s >= last_t - 2.0]
            temp_death = float(tail.die_temp_c.median())
            max_temp = float(c.die_temp_c.max())
            sl = c[c.timestamp_s >= last_t - 60.0]
            slope = np.nan
            if len(sl) > 20 and sl.die_temp_c.notna().sum() > 20:
                m = sl.die_temp_c.notna()
                slope = float(np.polyfit(sl.timestamp_s[m], sl.die_temp_c[m], 1)[0] * 60)
            # Post-latch revived cameras stream frames with a FROZEN TPM
            # readout (zero variance over the scan) — their death temps are
            # stale pre-latch values, not real die temps.
            frozen = bool(c.die_temp_c.std() < 0.05)
            rows.append(dict(
                experiment=name, side=side, cam=int(cam), gain_x=GAIN[int(cam)],
                event="died" if died else "survived",
                death_time_s=round(float(last_t), 1) if died else np.nan,
                scan_end_s=round(float(side_end), 1),
                temp_at_death_c=round(temp_death, 2),
                max_temp_c=round(max_temp, 2),
                temp_slope_c_per_min=round(slope, 3) if slope == slope else np.nan,
                temp_frozen=frozen,
                laser=laser,
                notes="TPM frozen (stale temp)" if frozen else ""))
    return rows


def append_rows(rows: list[dict]) -> pd.DataFrame:
    new = pd.DataFrame(rows, columns=COLS)
    if os.path.exists(DEATHS_CSV):
        old = pd.read_csv(DEATHS_CSV)
        merged = pd.concat([old, new], ignore_index=True)
        merged = merged.drop_duplicates(subset=["experiment", "side", "cam"],
                                        keep="last")
    else:
        merged = new
    os.makedirs(os.path.dirname(DEATHS_CSV), exist_ok=True)
    merged.to_csv(DEATHS_CSV, index=False)
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("expdirs", nargs="*")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.expdirs:
        rows = []
        for d in args.expdirs:
            rows.extend(analyze_experiment(d))
        merged = append_rows(rows)
        print(f"{len(rows)} rows analyzed; {len(merged)} total in {DEATHS_CSV}")
    if args.report or not args.expdirs:
        if not os.path.exists(DEATHS_CSV):
            print("no deaths recorded yet")
            return 0
        df = pd.read_csv(DEATHS_CSV)
        events = df[df.event != "survived"]
        print("=== events (died / never_started) ===")
        print(events.to_string(index=False))
        surv = df[df.event == "survived"]
        if len(surv):
            print("\n=== survivors: max temp reached ===")
            top = surv.sort_values("max_temp_c", ascending=False).head(12)
            print(top[["experiment", "side", "cam", "gain_x",
                       "max_temp_c", "laser"]].to_string(index=False))
        d = df[df.event == "died"]
        if len(d):
            print(f"\ndeath temp stats: n={len(d)} "
                  f"min={d.temp_at_death_c.min():.1f} "
                  f"median={d.temp_at_death_c.median():.1f} "
                  f"max={d.temp_at_death_c.max():.1f} C")
            hot_survivors = surv[surv.max_temp_c > d.temp_at_death_c.min()]
            if len(hot_survivors):
                print(f"NOTE: {len(hot_survivors)} survivor-scans exceeded the "
                      f"coolest death temp — threshold is not a single number "
                      f"or is per-unit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
