# Optimize Laser bench checklist

For epic #745 (app #774, SDK openmotion-sdk#353). Research build, engineering
mode, console firmware **1.8.1**. Firmware 1.8.2-rc.3 and later is refused by
design (step D1).

Two passes. Part A to C run headless through the SDK
(`openmotion-sdk/scripts/bench_optimize_laser.py`, which prints PASS/FAIL per
step). Part D needs the app's window.

## Before you start

- Sensor on a phantom. A run starts at maximum output by default: 5000 mA,
  2096 µs pulse, 16x gain.
- Close every app instance. USB access is exclusive.
- Console powered and showing no safety fault. A latched fault (E-202) needs a
  console power-cycle.

"Stock" below means the register values read right after connect: TA pulse
width about 500 µs, `PULSE_WIDTH_UL` and `RATE_LL` at the console's own
values, cameras at 72 rows and gains 16/4/2/1/1/2/4/16.

## A. Normal scans still work (`start_scan` changed)

| # | Step | Pass when |
|---|---|---|
| A1 | Stock scan, 5 s, before any Optimize Laser run | Starts, completes, every masked camera delivers light frames, no safety fault |
| A2 | Registers after A1 | Unchanged from stock |

## B. Optimize Laser run and scans at its settings

| # | Step | Pass when |
|---|---|---|
| B1 | Run with the study defaults (target 100 ± 10 DN, 20 in-range points, start at highest output) | Ends on target or partial; iteration count and per-camera signal recorded |
| B2 | Registers after B1 | `PULSE_WIDTH_UL` 2200 µs, `RATE_LL` 22400 µs, TA pulse width and current at the run's result, no safety fault |
| B3 | Scan, 10 s, saved to the scan DB | Starts with no extra call from the caller; priority cameras read within the passing range; `session_meta.optimize_laser` is in the DB record; no safety fault |
| B4 | Reload stock laser power (what a console reconnect does), then scan | The scan re-applies the settings itself: TA pulse width is back at the run's result, no safety fault |
| B5 | Scan on fewer cameras than the run (half the mask) | Starts and completes; no camera-write failure |
| B6 | Contact-quality check with settings active | Its scan runs under the 2400 µs dark skip; no safety fault (the verdict itself is informational: thresholds are for stock settings) |

## C. Getting back to stock

| # | Step | Pass when |
|---|---|---|
| C1 | Gate says the settings may not be used (app: engineering mode off), then scan | Scan runs; settings dropped; registers and cameras stock |
| C2 | New run, canceled after 2 iterations | Reports canceled and restored; registers and cameras stock; no safety fault |
| C3 | New run to a pass, then Restore default laser settings | No restore problems; nothing active; registers and cameras stock |
| C4 | Stock scan, 5 s | Same as A1 |

## D. In the app (not covered by the script)

| # | Step | Pass when |
|---|---|---|
| D1 | Console on firmware 1.8.2-rc.3 or later, press Optimize Laser | Refused before any write, with the firmware reason |
| D2 | Optimize Laser button | Shown only in a research build with engineering mode unlocked |
| D3 | Run from the button | Modal shows a row per iteration and the result; button highlighted while settings are active |
| D4 | Start a scan after the run | Scan notes open with the Optimize Laser block; no live contact-quality warnings |
| D5 | "Start a scan after optimizing" on | A good run closes the modal, shows the toast and scans for the set duration |
| D6 | Run that hits the iteration limit (narrow passing range) | Modal asks keep iterating / accept / stop; each answer does what it says |
| D7 | Stop during a run | Canceled, defaults restored, Start works afterwards |
| D8 | Turn engineering mode off with settings active, then Start | Toast says the settings were cleared; scan runs at stock |
| D9 | Settings → Restore default laser settings | Success toast; button no longer highlighted |
| D10 | Quit with settings active, relaunch, scan | Quit takes a few seconds (restore); next scan is stock with no fault |
| D11 | Clinical build | No button, no Settings card |
