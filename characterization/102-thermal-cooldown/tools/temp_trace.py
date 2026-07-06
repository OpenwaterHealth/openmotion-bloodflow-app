"""Extract a committable per-camera temperature trace from a means.csv.

A 20-min 16-cam means.csv is 50-100 MB — do NOT commit it. This reduces it
to a ~1 MB 1 Hz die-temp trace (median per second per camera), which is all
the cooldown characterization needs from a heat run.

Usage:
    python temp_trace.py experiments/E1a_heat20_cold/means.csv results/<unit>/E1a_temps_1hz.csv
"""
from __future__ import annotations

import sys

import pandas as pd


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    df = pd.read_csv(src, usecols=["side", "cam_id", "timestamp_s",
                                   "type", "die_temp_c"])
    df = df[df["type"].isin(["light", "dark"])]
    df["t_s"] = df.timestamp_s.round(0).astype(int)
    out = (df.groupby(["side", "cam_id", "t_s"]).die_temp_c.median()
             .reset_index())
    out.to_csv(dst, index=False)
    print(f"{len(out)} rows -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
