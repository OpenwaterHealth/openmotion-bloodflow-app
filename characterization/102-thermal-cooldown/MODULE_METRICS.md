# Per-module thermal metrics — #330 characterization

Computed by `tools/unit_metrics.py` from cold-start 20-min all-camera 40 Hz
laser-on heat runs + immediate post-scan powered-idle cooldown logs.
Machine-readable rows: `results/module_metrics.csv`.

## Metric definitions

**Thermal shutdown behavior:**
- **T_ss_hot (°C)** — hottest slot's steady-state die temp (median, final 2 min).
- **LM — latch margin (°C)** = 112.7 − T_ss_hot (112.7 = bottom of the measured
  regulator-latch band, 112.7–117.5 °C, n=10, live telemetry). LM ≤ 0 ⇒ the
  module latches on long scans.
- **TSI — Thermal Stress Index** = (T_ss_hot − T_start)/(112.7 − T_start),
  dimensionless. **TSI < 1 = Regime A** (cannot latch at this ambient,
  any scan length); **TSI ≥ 1 = Regime B** (latches; scan time is a budget).
  Proposed as the fleet service diagnostic: trend it per module; service the
  clamp/pad when it approaches 1.
- **HR5 (°C/min)** — hottest camera's heating rate over the first 5 min.
- **SSS (°C)** — steady-state spread across the module's 8 cameras.
- **n_latch / t_latch (s)** — thermal latches in the run / earliest.

**Cooldown behavior (post-scan, rails powered, plain reads):**
- **τ_cool (min)** — worst-camera exponential constant of the slow
  (assembly) cooling phase. Note: the first ~1 min after streaming stops
  shows a fast ~25–30 °C die-dissipation collapse that the single τ does
  not model; τ_cool describes the tail that dominates ETA.
- **T_floor (°C)** — hottest powered-idle asymptote.
- **TTR60 / TTR45 (min)** — measured time until every camera reads ≤60 / ≤45 °C
  (from cooldown-log start ≈ scan end + ~1 min).

## Results (2026-07-06/07)

| Unit | Module serial | FW | T_ss_hot | LM | **TSI** | HR5 | SSS | n_latch (t) | τ_cool | T_floor | TTR60 | TTR45 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | **QWW24Q10011** (left) | 1.8.1-rc.3-dirty | **114.3** | **−1.6** | **1.022** | 10.6 | 30.0 | **2 (501 s)** | 12.8 | 38.7 | 2.7 | 15.6 |
| A | **EVT2SN82** (right) | 1.8.1-rc.3-dirty | 105.4 | 7.3 | 0.908 | 11.3 | 49.6 | 0 | 9.1 | 31.6 | 0.6 | 2.9 |
| B | unprog. (hw 43004e…, left) | 1.8.1-rc.3 | 96.8 | 16.0 | 0.801 | 10.1 | 44.7 | 0 | 7.9 | 31.8 | 0.3* | 1.6* |
| B | unprog. (hw 2a0052…, right) | 1.8.1-rc.3 | 90.0 | 22.7 | 0.714 | 9.3 | 37.1 | 0 | 8.5 | 30.3 | 0.2* | 1.0* |

\* unit-B cooldown log started minutes after scan end → TTR under-measured.
Unit-A console: QWW04Q10003. Unit-B: DVT-1B, console serial unprogrammed
(hw 2f0043…3450). Heating rates are uniform (~10 °C/min) — the differentiator
is where the module *equilibrates*, i.e. its thermal path to ambient.

## Reading

- **TSI cleanly rank-orders the fleet sample** and the one module with
  TSI > 1 (QWW24Q10011) is exactly the one that latched — at 8.4 min from a
  cold start. No start-temp gate can give that module a guaranteed 10-min
  scan; it needs mechanical service (clamp/pad — same class as the drift
  bench's regression). Everything with TSI < 1 completed 20 min untouched.
- **Latched cameras return I2C-readable during scan teardown** (rail
  re-power) — the post-scan cooldown gate sees the whole module.
- **Cooling is two-phase:** ~25–30 °C of die self-heat collapses within
  ~1 min of streaming stop; the assembly then cools with τ ≈ 8–13 min.
  Consequence: a 60 °C gate releases in ~1–3 min on all measured modules,
  while a 45 °C gate would cost up to ~16 min on hot ones — supporting
  `cooldownStartTempC = 60` as the shipped default.
