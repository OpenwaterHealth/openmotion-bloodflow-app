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

## E2b attempt 1 (rails-off w/ 2-min capture checkpoints) — PROTOCOL ARTIFACT, retained as duty-cycle data

3-hour run (990 rows, `e2b_railoff_cooldown_preheat.csv`): temps **plateaued**
at left 48–54 °C / right 39–46 °C and never approached the 35 °C exit gate.
Cause: each capture-first sweep takes ~80 s and each captured camera stays
ACTIVE until the rail drop at end-of-burst → at interval 120 the protocol
holds ~30–40 % active duty, and the assembly equilibrates against the
measurement's own heat. Lessons:

1. **Rails-off checkpoints must be sparse** (≥ 25 min apart → ≤ ~5 % duty).
2. Useful accidental datum: ~35 % active duty ⇒ equilibrium ≈ 20 °C above
   ambient on the hotter module — relevant to duty-cycled operation ideas.
3. **USB fragility around rail cycling:** 2 min into the run, errno-32 pipe
   errors dropped BOTH sensors + console simultaneously (auto-recovered);
   plausibly fallout from cutting rails under cameras the E0 captures had
   left active. The run later died hard (exit 255, no traceback) at t≈3 h.
   Quiesce cameras before rail-off; power-cycle when in doubt.
4. Left module consistently ~9 °C hotter than right at equal duty — same
   left-hot asymmetry as the drift-campaign bench.

Shelly power-cycled after the crash (recovers USB; boots with camera rails
OFF ⇒ passive cooling with zero measurement; resets the fw#73 stale
temp-poll deadline for E1a's streamed temps).

## First cooling numbers (salvaged from attempt 1's pre-equilibrium segment)

Between burst 1 (t=81 s) and burst 2 (t=283 s) — mostly rails-off time —
the hottest left camera fell 71.6 → 54.0 °C. Against ~28 °C ambient that is
**τ_cool(rails-off) ≈ 6 min** (380 s; slight over-estimate since the 80 s
sweep re-heated). Implication: 54 → 35 °C ≈ 8 min; a full post-scan
cooldown from ~70 °C to a 45 °C gate ≈ 6–7 min rails-off. To be refined by
E2a/E2b proper.

**IMU proxy check:** module IMU tracked the cooldown (left 43.9→39.7 °C,
right 33.2→29.2 °C) with dies sitting ~+12 °C (left) / ~+15 °C (right)
above their module IMU at ~35 % duty. Same-ballpark offsets, not identical
across modules — usable as a gate proxy with a per-module margin if
post-scan die reads turn out stale (E2a decides).

## E2b attempt 2 — blocked (app holds the bench); superseded by unit-B data

Attempt 2 never connected — the Open-Motion app was launched on this
machine at 14:18 and owns the USB. Unit-A E1/E2 remain pending (now as the
assembly-spread check, E5a); everything the feature needed came from unit B.

## Unit-B (DVT-1B, sensors 1.8.1-rc.3 clean) — the decisive dataset

Full results on the kit branch under `results/unit-B-hwid-3450/`; analysis
2026-07-06:

- **E1a/E1b heat runs:** hottest slot (left 6) steady-state **96.9 °C**,
  repeatable ~1 °C across runs; zero latches; equilibrium reached inside
  20 min. → **Regime A** at ~21 °C room: steady-state below the latch band
  (~113–117 °C), so any start temp below steady-state cannot latch. Gate's
  role = margin for hot rooms / degraded assemblies + no scan stacking.
- **E2a (post-scan, rails powered, PLAIN 5 s reads): reads are LIVE ≥45 min**
  — smooth decays, max step 0.8–3.6 °C. Mechanism verdict: TPM polling works
  exactly in the window the lockout needs → armed-only gate.
- **Cooling constants:** worst τ = 8.5 min (r² 0.83–0.97), powered-idle
  floors 24.6–31.8 °C. From 97 °C: ~6–7 min to 60 °C, ~12 min to 45 °C.
- **Rails-off true floor ≈ ambient** (their manual 5-min-off single-read:
  20.7–27.6 °C) — rail-cycle sampling artifact confirmed independently and
  root-caused (sampling inside the re-power transient).
- Unit-B also hit one unexplained hard tool hang cleared by Shelly cycle —
  same USB-fragility family as unit-A's errno-32 collapse. Watch it.

## Unit-A run (2026-07-06 late) — the Regime-B counterexample

Serials: console QWW04Q10003, **LEFT QWW24Q10011 (production), RIGHT
EVT2SN82 (EVT2)** — mixed-rev bench. Cold-start E1a:

- **LEFT latched cameras 3+4 at 501/600 s** (113.4/115.6 °C, live temps) and
  its hottest survivor steady-states at **114.3 °C — inside the latch band.**
  TSI 1.022. No start-temp gate can guarantee this module 10 minutes; it
  needs mechanical service. Exactly the historical field failure, reproduced
  on demand with live telemetry.
- RIGHT (EVT2!) clean: 105.4 °C, TSI 0.908, margin 7.3 °C.
- **Two-phase cooling discovered:** die readings collapse ~25–30 °C within
  ~1 min of streaming stop (die dissipation term), then assembly τ 9–13 min.
  TTR60 is minutes; TTR45 is ~16 min on the hot module → supports gate=60.
- **Latched cameras read live again from cooldown-log start** — teardown's
  rail re-power restores I2C, so the armed gate sees the full module.

Full metric definitions + 4-module table: `MODULE_METRICS.md` +
`results/module_metrics.csv`.

## Applied to PR #329 (commit 16efefd)

Armed-only gate (scan-end → release); `cooldownStartTempC` 60,
`cooldownTauSec` 510, `cooldownAmbientC` 29. 22 unit tests / 470 sweep green.
Remaining: unit-A spread run (E5a), bench visual check, optional E4 soak.

## E1a — pending (cold 20-min all-cam laser-on heat run)

Plan: Shelly power-cycle first (resets fw#73 stale temp-poll deadline so
STREAMED temps are live), then `run_experiment.py experiments/E1a_heat20_cold/manifest.json --configure`.
