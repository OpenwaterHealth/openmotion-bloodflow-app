"""Idle per-camera die-temp logger — #102 thermal cooldown characterization.

Polls every camera's TPM die temperature (0x4D2A/2B, direct I2C register
read via the drift campaign's owcam helper) while the system is IDLE.
This is the tool the drift campaign never needed: temps have only ever
been recorded while streaming; cooldown between scans is unmeasured.

Used for:
  E0 - idle TPM validity (does the reading update while powered-not-streaming?)
  E2 - cool-down curves (powered-idle vs rails-off sampling)
  E3/E4 - start-temp gating for scan trials (--exit-below / --exit-above)

DO NOT run this during an active scan: the firmware polls camera temps
over the same I2C bus per frame (see owcam.py header). During scans use
the streamed die_temp_c column in the campaign collector's means.csv.

CSV schema: t_wall_s, elapsed_s, side, cam, temp_c
  cam is 0-7, or "imu" for the module IMU temperature row (side-level).
A latched / unpowered / unreachable camera logs NaN — never aborts.
A sidecar <out>.meta.json records the arguments and run window.

Usage examples:
  # E0 / E2a — powered-idle cooldown curve, sample every 5 s for 45 min:
  python thermal_logger.py e2a_cooldown_powered.csv --interval 5 --duration 2700

  # E2b — rails-off cooldown: power off, re-power briefly to sample each minute:
  python thermal_logger.py e2b_cooldown_railoff.csv --mode rail-off --interval 60 --duration 3600

  # E3 gate — block until every camera is at/below 45 C, then exit 0:
  python thermal_logger.py e3_gate.csv --interval 10 --exit-below 45

  # Preheat gate — exit as soon as any camera reaches 90 C:
  python thermal_logger.py preheat.csv --interval 10 --exit-above 90

Requires the camera-drift-campaign tools on disk (owcam.py). Override the
location with OWCAM_TOOLS if it moves.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time

CAMPAIGN_TOOLS = os.environ.get(
    "OWCAM_TOOLS", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CAMPAIGN_TOOLS)

import owcam  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("thermal_logger")

ALL_MASK = 0xFF


def read_all_temps(iface, sides, capture_first: bool) -> dict[str, dict]:
    """One sample burst. Returns {side: {"cams": {cam: temp|nan}, "imu": temp|nan}}."""
    out: dict[str, dict] = {}
    for side in sides:
        entry = {"cams": {}, "imu": float("nan")}
        try:
            sensor = owcam.sensor_for(iface, side)
        except Exception:
            for cam in range(8):
                entry["cams"][cam] = float("nan")
            out[side] = entry
            continue
        try:
            entry["imu"] = float(sensor.imu_get_temperature())
        except Exception:
            pass
        for cam in range(8):
            try:
                if capture_first:
                    # Force a fresh conversion through the firmware capture
                    # path in case the TPM turns out to hold stale values at
                    # idle (E0 decides whether this is needed).
                    try:
                        sensor.camera_capture_histogram(1 << cam)
                    except Exception:
                        pass
                entry["cams"][cam] = float(owcam.read_die_temp_c(sensor, cam))
            except Exception:
                entry["cams"][cam] = float("nan")
        out[side] = entry
    return out


def set_power(iface, sides, on: bool) -> None:
    for side in sides:
        try:
            sensor = owcam.sensor_for(iface, side)
            fn = sensor.enable_camera_power if on else sensor.disable_camera_power
            if not fn(ALL_MASK):
                logger.warning("%s camera power %s returned False",
                               side, "on" if on else "off")
        except Exception as e:
            logger.warning("%s camera power %s failed: %s",
                           side, "on" if on else "off", e)


def finite(vals):
    return [v for v in vals if isinstance(v, float) and math.isfinite(v)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out_csv")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="seconds between sample bursts (default 5; use >=30 for rail-off)")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after N seconds (0 = run until Ctrl+C or exit condition)")
    ap.add_argument("--mode", choices=("powered", "rail-off"), default="powered",
                    help="powered: leave rails as-is and poll. rail-off: rails off between "
                         "samples, re-power briefly to read (simulates post-scan power-down)")
    ap.add_argument("--power-on", action="store_true",
                    help="powered mode: force camera rails ON at start (use if all reads NaN)")
    ap.add_argument("--capture-before-read", action="store_true",
                    help="trigger a single-frame capture before each TPM read (E0 fallback "
                         "if idle TPM values turn out stale)")
    ap.add_argument("--settle", type=float, default=1.0,
                    help="rail-off mode: seconds after re-power before reading (default 1.0)")
    ap.add_argument("--sides", default="left,right")
    ap.add_argument("--exit-below", type=float, default=None, metavar="T",
                    help="exit 0 once EVERY finite camera temp is <= T degC (cooldown gate)")
    ap.add_argument("--exit-above", type=float, default=None, metavar="T",
                    help="exit 0 once ANY camera temp is >= T degC (preheat gate)")
    ap.add_argument("--note", default="", help="free-text note saved to the meta sidecar")
    args = ap.parse_args()

    sides = [s.strip() for s in args.sides.split(",") if s.strip()]
    iface = owcam.connect()
    meta = {
        "args": vars(args),
        "started_utc": time.time(),
        "owcam_tools": CAMPAIGN_TOOLS,
    }

    if args.mode == "rail-off":
        logger.info("rail-off mode: powering camera rails OFF between samples")
        set_power(iface, sides, on=False)
    elif args.power_on:
        set_power(iface, sides, on=True)
        time.sleep(args.settle)

    t0 = time.time()
    exit_reason = "user/duration"
    try:
        with open(args.out_csv, "w", encoding="utf-8", newline="") as fh:
            fh.write("t_wall_s,elapsed_s,side,cam,temp_c\n")
            while True:
                if args.mode == "rail-off":
                    set_power(iface, sides, on=True)
                    time.sleep(args.settle)
                burst = read_all_temps(iface, sides, args.capture_before_read)
                if args.mode == "rail-off":
                    set_power(iface, sides, on=False)

                now = time.time()
                elapsed = now - t0
                all_temps: list[float] = []
                for side, entry in burst.items():
                    if math.isfinite(entry["imu"]):
                        fh.write(f"{now:.1f},{elapsed:.1f},{side},imu,{entry['imu']:.2f}\n")
                    for cam in range(8):
                        t = entry["cams"][cam]
                        tstr = f"{t:.2f}" if math.isfinite(t) else "nan"
                        fh.write(f"{now:.1f},{elapsed:.1f},{side},{cam},{tstr}\n")
                        if math.isfinite(t):
                            all_temps.append(t)
                fh.flush()

                if all_temps:
                    per_side = []
                    for side, entry in burst.items():
                        fv = finite(list(entry["cams"].values()))
                        if fv:
                            per_side.append(f"{side} {min(fv):.1f}-{max(fv):.1f}C")
                        else:
                            per_side.append(f"{side} n/a")
                    logger.info("t=%5.0fs  %s  (%d/16 readable)",
                                elapsed, "  ".join(per_side), len(all_temps))
                else:
                    logger.warning("t=%5.0fs  no readable cameras "
                                   "(rails off? try --power-on)", elapsed)

                if args.exit_below is not None and all_temps and \
                        max(all_temps) <= args.exit_below:
                    exit_reason = f"all cams <= {args.exit_below}C"
                    logger.info("exit condition met: %s", exit_reason)
                    break
                if args.exit_above is not None and all_temps and \
                        max(all_temps) >= args.exit_above:
                    exit_reason = f"a cam >= {args.exit_above}C"
                    logger.info("exit condition met: %s", exit_reason)
                    break
                if args.duration and elapsed >= args.duration:
                    break
                time.sleep(max(0.0, args.interval - (time.time() - now)))
    except KeyboardInterrupt:
        exit_reason = "KeyboardInterrupt"
        logger.info("stopped by user")
    finally:
        if args.mode == "rail-off":
            # Leave the bench in the normal state: rails back on.
            set_power(iface, sides, on=True)
        meta["finished_utc"] = time.time()
        meta["exit_reason"] = exit_reason
        with open(args.out_csv + ".meta.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        try:
            iface.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
