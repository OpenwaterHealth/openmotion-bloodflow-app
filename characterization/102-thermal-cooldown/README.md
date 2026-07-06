# #102 Thermal cooldown characterization — runbook

| | |
|---|---|
| **Goal** | Characterize heat-up, latch, and cool-down on the **newest hardware rev** to parameterize the 8-camera cooldown/lockout feature ([bloodflow-app #102](https://github.com/OpenwaterHealth/openmotion-bloodflow-app/issues/102)) |
| **Prepared** | 2026-07-05 (characterization scheduled next day) |
| **Prior data** | `camera-drift-campaign/data/thermal_deaths.csv`, `camera-drift-campaign/issue_tpm.md`, `investigations/246_camera_thermal_dropout/` |
| **Related issues** | app #84 (deaths "below 110°C" — explained: stale TPM), app #207, sensor-fw #73 (temp-poll freeze, OPEN), sensor-fw #68/#69/#71 (starvation dropouts, FIXED+merged) |

## Branch layout note (feature/330-thermal-characterization-kit)

On this branch the kit is **self-contained**: the drift-campaign harness
(`owcam.py`, `run_experiment.py`, `drift_collector.py`, `deaths.py`) is
vendored into `tools/`, imports resolve script-relative (no `OWCAM_TOOLS`
needed), and `deaths.py` writes to `results/thermal_deaths.csv` (the repo
gitignores any `data/` dir). Per-unit outputs go under `results/<unit>/` and
are committed back to this branch. **Second-machine instructions:
[SECOND_UNIT.md](SECOND_UNIT.md).** Tracking: issue #330 (milestone 1.5.0).

## What we already know (don't re-derive tomorrow)

- **True thermal latch: 112.7–117.5 °C die temp** (median ~114.3, n=10, live
  telemetry only). "Deaths below 110 °C" were stale-telemetry readings
  (fw #73) or non-thermal starvation dropouts (fw #68, now fixed).
- **Latch behavior:** PCB regulator latches; firmware rails the camera off
  10 s then re-powers with FPGA held in reset — **no in-scan recovery**, and a
  post-latch camera is I2C-unreachable while latched and streams a **frozen
  TPM value + flaky registers after revival until a true power cycle**.
- **Start temp dominates survival** (old rev: same camera died at 477 s
  cold-started vs 72 s warm-started). Mid-slot cameras run 20–40 °C hotter
  than outer ones. Mechanical clamp/pad seating shifts idle temps by ~40 °C
  (the drift-campaign right-module regression) — characterization is
  per-assembly until E5 says otherwise.
- **No cooldown data exists anywhere.** Temps were only ever recorded while
  streaming. E2 is genuinely new ground.

## ⚠ Attribution trap (the reason this runbook exists)

"New rev barely drops out" is confounded: firmware with the #69/#71
starvation fixes eliminates most historical dropouts **regardless of
hardware**, and stale TPM values hide how hot cameras actually run.
So tomorrow:

1. **Record versions first** (bench log below): sensor FW, console FW, SDK,
   app build if used, plus sensor serials/hw_id and per-camera chip IDs
   (`owcam.survey`).
2. **Judge the hardware by measured temperatures, not by dropout counts.**
   The question is "where do die temps sit vs the ~113–117 °C latch band",
   not "did anything drop out".
3. If time allows (E5b): flash **old** sensor FW (1.7.1) on the same new-rev
   unit and repeat one heat run — cleanly splits hardware vs firmware credit.

## Bench log (fill in before E0)

| Field | Value |
|---|---|
| Date / operator | |
| Unit / hw rev identifiers | |
| Sensor serials (L/R) + hw_id | |
| Sensor FW version | |
| Console FW version | |
| SDK version (`pip show omotion`) | |
| Ambient temp / room conditions | |
| Fixture (phantom, clamp state — note any re-seat!) | |

## Environment

- **Python:** base miniconda python (the `pylib` env is hollow). SDK installed
  editable from `openmotion-sdk`.
- **Close the bloodflow app first** — it holds the USB interfaces.
- Campaign tools are imported from
  `C:\Users\ethan\Projects\camera-drift-campaign\tools` (override:
  `OWCAM_TOOLS`). Scan runs use the campaign's `run_experiment.py`
  (manifest-driven; writes `means.csv` with per-frame `die_temp_c` next to
  the manifest) and `deaths.py` (death/latch extraction + frozen-TPM flag).
- New tools in this kit (`tools/`):
  - `thermal_logger.py` — **idle** per-camera TPM polling (direct 0x4D2A/2B
    reads). Modes: `powered` and `rail-off`; gates: `--exit-below/--exit-above`.
    Also logs the per-module IMU temp (proxy-candidate for an app-side gate
    with zero firmware changes — keep it in every log).
  - `cooldown_fit.py` — exponential fits → per-camera τ and time-to-target.
- **Never run `thermal_logger.py` during a scan** (I2C bus contention with the
  firmware's per-frame temp poll). During scans the temps come from
  `means.csv`.
- **Power-cycle the sensors (Shelly) between heat runs.** This clears any
  latch AND resets the fw#73 stale temp-poll deadline so the *streamed* temps
  stay live for the next scan. Check `deaths.py` output: any `temp_frozen=True`
  row means that scan's streamed temps are garbage — rerun after power cycle.

## Run order

### E0 — Idle TPM validity (~20 min)

Does the die-temp register update while powered-but-not-streaming?

1. From cold, `python tools\thermal_logger.py e0_idle.csv --interval 5 --duration 300 --power-on`
   → values should sit near ambient and move if you warm a camera (finger on
   the housing works).
2. Repeat 2 min with `--capture-before-read`. If plain reads are stale but
   capture-first reads are live, the production idle-poll needs the
   capture-first path — note it, it changes the feature's SDK surface.
3. Sanity: start a 60 s scan (any manifest, edited duration) and compare
   `means.csv` die_temp_c against the last idle reads.

**Decides:** the mechanism the app's cooldown gate can use at idle.

### E1 — Heat-up curves (2 × 20 min + analysis)

1. Confirm cold: `thermal_logger.py e1a_pre.csv --interval 10 --exit-below 35`
2. `python ..\camera-drift-campaign\tools\run_experiment.py experiments\E1a_heat20_cold\manifest.json --configure`
3. Immediately extract: `python ..\camera-drift-campaign\tools\deaths.py experiments\E1a_heat20_cold`
   and eyeball `means.csv` temp trajectories.
4. **Straight into E2a below — the cooldown curve starts the second the scan ends.**
5. After E2a: Shelly power-cycle, re-cool, repeat as E1b (then E2b).
6. If time: E1c (laser-off variant) — separates streaming self-heat from
   laser heating.

**Deliverables:** per-slot steady-state temp (or latch time+temp), heating
τ, confirmation of the latch band on the new rev.

**The regime question this answers:**
- **Regime A** — hottest slot plateaus ≤ ~105 °C: scans of any length are
  thermally safe once started cool; the lockout only enforces a start-temp
  ceiling (margin + drift quality).
- **Regime B** — hottest slot exceeds the latch band: there's a hard
  time-above-start-temp budget; the 10-min guarantee comes entirely from the
  start-temp ceiling `T_start_max` (E3).

### E2 — Cool-down curves (runs inside E1's gaps)

- **E2a (powered-idle, after E1a):** the moment the scan completes:
  `python tools\thermal_logger.py e2a_cooldown_powered.csv --interval 5 --duration 2700`
- **E2b (rails-off, after E1b):**
  `python tools\thermal_logger.py e2b_cooldown_railoff.csv --mode rail-off --interval 60 --duration 3600`
- Fit both: `python tools\cooldown_fit.py e2a_cooldown_powered.csv --targets 60,45`

**Deliverables:** τ_cool powered vs rails-off → whether the feature should
power cameras down post-scan, the lockout-duration model, and the re-arm
threshold. Also check IMU-vs-camera temp correlation in the logs (cheap
app-side gate candidate).

### E3 — Start-temp boundary for the 10-min guarantee (2–4 trials)

Only meaningful under Regime B (under Regime A, run one confirmation trial).

Per trial: copy `experiments\E3_scan10_TEMPLATE` → `E3_scan10_start<T>`, set
`name`, then:

```powershell
# cooling to target from above:
python tools\thermal_logger.py e3_gate.csv --interval 10 --exit-below <T>
# (preheat instead: run a short scan, then --exit-below <T> on the way down)
python ..\camera-drift-campaign\tools\run_experiment.py experiments\E3_scan10_start<T>\manifest.json
python ..\camera-drift-campaign\tools\deaths.py experiments\E3_scan10_start<T>
```

Bisect start temps (suggest first pair: 45 °C and 70 °C). PASS = zero
died/never_started rows. **Deliverable: `T_start_max` = highest passing start
temp − ≥5 °C margin.**

### E4 — Duty-cycle soak (later / overnight)

Loop: gated cooldown to `T_start_max` → 10-min scan → repeat, ≥6 cycles.
Expected zero dropouts; the measured gate-wait is the clinical
scan-every-X-minutes number. (Script it with `--exit-below` in a PS loop;
power-cycle between cycles is NOT allowed here — the feature won't have one,
so fw#73 frozen-temp scans must be tolerated in analysis or fw#73 fixed first.)

### E5 — Attribution & spread (day 2 / as available)

- **E5a:** repeat E1a+E2a on a second new-rev unit (or after a deliberate
  re-clamp) → margin the gate needs against assembly variation.
- **E5b:** old sensor FW (1.7.1) on the same unit, one heat run → hardware
  vs firmware attribution for the "dropouts vanished" observation.

## What feeds the feature spec

| Parameter | Source |
|---|---|
| Latch band on new rev | E1 (deaths.py `temp_at_death_c`, live rows only) |
| Regime A vs B | E1 steady-state vs latch band |
| `T_start_max` (Start-button gate) | E3 (or Regime-A margin choice) |
| Cooldown model (τ, powered vs off) | E2 → `cooldown_fit.py` |
| Idle temp mechanism (reg read / capture-first / IMU proxy) | E0 + E2 IMU correlation |
| Assembly margin | E5a |
| fw#73 fix priority | E4 feasibility without power cycles |
