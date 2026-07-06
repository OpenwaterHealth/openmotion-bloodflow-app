# Running the #330 thermal characterization on a second unit

Step-by-step for characterizing another unit (console + 2 sensor modules) on
a different Windows machine. Everything you need is in this directory — the
tools vendor their own SDK-harness copies and find them automatically.
Tracking: issue [#330](https://github.com/OpenwaterHealth/openmotion-bloodflow-app/issues/330)
(milestone 1.5.0) → feeds the Camera Cooldown feature
([#102](https://github.com/OpenwaterHealth/openmotion-bloodflow-app/issues/102) / PR #329).

## 1. One-time machine setup

1. **Python 3.12+** on PATH, then:
   ```powershell
   pip install --upgrade omotion pandas numpy
   ```
   (Latest PyPI `omotion` is fine; an editable SDK checkout also works.)
2. **Sensor USB driver**: sensors need WinUSB (Zadig) — if the unit already
   runs the Open-Motion app on this machine, drivers are already right.
3. **Close the Open-Motion app** (fully — check Task Manager for python
   processes). Only one process can own the USB interfaces.
4. Console + BOTH sensors connected and powered. The tools refuse to start
   until all three enumerate.

## 2. Get the kit

```powershell
git clone https://github.com/OpenwaterHealth/openmotion-bloodflow-app.git   # or use an existing clone
cd openmotion-bloodflow-app
git fetch origin
git checkout feature/330-thermal-characterization-kit
cd characterization\102-thermal-cooldown
```

## 3. Bench log first (attribution — do not skip)

```powershell
mkdir results\unit-B-<yourname>
python tools\bench_survey.py --power-on > results\unit-B-<yourname>\BENCH_LOG.txt
type results\unit-B-<yourname>\BENCH_LOG.txt
```

Then append to that file by hand: **which hardware rev this unit is**, room
temp, fixture (phantom? clamp state?), and anything odd. The firmware
version lines it prints are the attribution record — if the sensors run a
different firmware than unit A (1.8.1-rc.3-dirty), say so loudly.

Expect all 16 cameras readable with chip_id 0x5802. If cameras show
`chip_id=0x-001 / nan`, the rails didn't power — re-run with `--power-on`
or power-cycle the sensors.

## 4. E0 spot-check (~10 min) — confirm unit-A's telemetry findings

```powershell
python tools\thermal_logger.py results\unit-B-<yourname>\e0_idle.csv --interval 5 --duration 180 --power-on
```

Unit-A result: idle values are **garbage** (±50 °C swings on a static
bench) because the TPM doesn't convert in standby. Expect the same; if your
unit's idle reads are *stable*, that's a finding — note it.

## 5. E1a — cold heat run (20 min) + E2a — post-scan cooldown (45 min)

**Cold gate** (all cams ≤ 35 °C; rails off between checks so waiting doesn't
heat anything):

```powershell
python tools\thermal_logger.py results\unit-B-<yourname>\e1a_coldgate.csv --mode rail-off --capture-before-read --interval 120 --exit-below 35
```

**Power-cycle the sensors** when it exits (unplug/replug sensor power or
outlet). This clears the firmware's stale temp-poll deadline (sensor-fw#73)
so the scan's *streamed* temps are live, and clears any prior thermal latch.
Wait ~20 s for re-enumeration.

**Heat run** (20-min all-camera 40 Hz laser-on — normal scan, standard laser
safety applies):

```powershell
python tools\run_experiment.py experiments\E1a_heat20_cold\manifest.json --configure
```

**The moment it finishes**, start the cooldown log (this doubles as the
gate-mechanism test — plain reads in the post-scan state):

```powershell
python tools\thermal_logger.py results\unit-B-<yourname>\e2a_cooldown_powered.csv --interval 5 --duration 2700
```

Then reduce + extract (both outputs are committable):

```powershell
python tools\deaths.py experiments\E1a_heat20_cold
python tools\temp_trace.py experiments\E1a_heat20_cold\means.csv results\unit-B-<yourname>\E1a_temps_1hz.csv
python tools\cooldown_fit.py results\unit-B-<yourname>\e2a_cooldown_powered.csv --targets 60,45
```

⚠ `means.csv` is 50–100 MB and gitignored — commit the 1 Hz trace, not the raw.
⚠ Never run `thermal_logger.py` while a scan is running (I2C bus contention
with the firmware's per-frame temp poll).
⚠ If a camera thermally latches during the run (it stops posting; deaths.py
will show `died`), that's data, not a failure — but power-cycle the sensors
before the next experiment (a post-latch camera is flaky until true power
cycle).

## 6. E1b/E2b — repeat + rails-off cooldown (as time allows)

Same as step 5 with `experiments\E1b_heat20_cold_repeat\manifest.json`, but
after the scan run the **rails-off** cooldown variant instead:

```powershell
python tools\thermal_logger.py results\unit-B-<yourname>\e2b_cooldown_railoff.csv --mode rail-off --capture-before-read --interval 120 --duration 3600
```

## 7. Sync results back

```powershell
git add characterization\102-thermal-cooldown\results\unit-B-<yourname> characterization\102-thermal-cooldown\results\thermal_deaths.csv
git commit -m "data: unit-B <yourname> thermal characterization results (Refs #330)"
git pull --rebase origin feature/330-thermal-characterization-kit
git push origin feature/330-thermal-characterization-kit
```

That's it — analysis (τ fits, start-temp ceiling, cross-unit spread) happens
centrally from the results directories. Questions / anomalies: comment on
#330 with your BENCH_LOG.txt attached.
