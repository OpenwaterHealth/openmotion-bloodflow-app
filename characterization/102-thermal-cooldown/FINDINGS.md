# #102 characterization — findings log

## Bench log (2026-07-06)

| Field | Value |
|---|---|
| Operator | Ethan + Claude (headless) |
| Console FW | 1.8.0, hw_id 23004c00065133333735383300000000 |
| LEFT sensor FW | **1.8.1-rc.3-dirty**, hw_id 28004f00065133333735383300000000 |
| RIGHT sensor FW | **1.8.1-rc.3-dirty**, hw_id 24004600035133333639353500000000 |
| Camera chip IDs | 0x5802 × 16 (all reachable once rails powered) |
| Cold idle temps | ~15–30 °C first-read after power-on (see E0 caveat) |
| IMU temps (idle) | left ~27.5 °C, right ~32.6 °C — stable |
| Attribution note | "-dirty" local firmware build — confirm branch/commit with Ethan before crediting hardware differences |

## E0 — idle TPM validity (RESULT: plain idle reads are UNUSABLE)

1. **Camera rails are OFF at sensor boot** — all 16 cameras I2C-unreachable
   until `enable_camera_power(0xFF)`. (Feature implication: fresh-boot app
   sees all-NaN → fail-open path, which is correct-by-coincidence.)
2. **Plain TPM reads on powered-but-never-streamed cameras are garbage.**
   3-min static-bench log: per-cam ranges up to −60.7…+57.2 °C, std 5–23 °C.
   The TPM conversion does not run in standby. A gate polling these would
   randomly false-lock (a +57 junk read) and false-unlock (a −60 junk read).
3. **`--capture-before-read` returns live, consistent values** (smooth
   cross-camera gradient, mids hottest) **but activates the sensor**: cameras
   heated ~45→72 °C in 90 s during the capture-first log (≈16 °C/min = full
   active self-heat), and a 16-cam sweep takes ~80 s. NOT viable at 5 s
   cadence; viable as sparse checkpoints in rails-off mode (each cam active
   only ~5 s during its slot; rail drop kills the leftover active state).
4. **IMU temps are stable and plausible at idle** (zero perturbation, work
   regardless of camera rail state). Module-level only — die↔IMU correlation
   to be extracted from E1/E2.
5. **Open question → E2a:** are plain standby reads live in the POST-SCAN
   state (sensor configured + recently streaming)? That's the state the
   feature's lockout actually polls in. If yes, PR #329's mechanism stands
   with a boot-state guard; if no, switch the gate to IMU-temp + timer.

**PR #329 impact (regardless of E2a):** the gate must not trust plain reads
on never-streamed cameras — needs either consecutive-sample agreement, a
"recently streamed" qualifier, or the IMU-proxy mechanism.

## E2b (accidental preheat variant) — rails-off cooldown from ~70 °C

Started 11:1x from the E0 capture-first preheat (left 55–73 °C, right
48–70 °C). Protocol: rails off between samples; every ~2 min rails on →
capture-first sweep → rails off. Exit gate: all cams ≤ 35 °C (feeds E1a
cold start). Data: `e2b_railoff_cooldown_preheat.csv`. → τ fits pending.

## E1a — pending (cold 20-min all-cam laser-on heat run)

Plan: Shelly power-cycle first (resets fw#73 stale temp-poll deadline so
STREAMED temps are live), then `run_experiment.py experiments/E1a_heat20_cold/manifest.json --configure`.
