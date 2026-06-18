# TEC_TRIP configurable from app_config.json — design

**Date:** 2026-06-18
**Repo:** openmotion-bloodflow-app
**Status:** Draft for review

## Summary

Let the clinical bloodflow app set the console's `TEC_TRIP` over-temperature trip
temperature from `config/app_config.json`. On every console connect, the app reads
the configured value, validates it, and — only if it is valid and differs from
what the device already has — writes it into the console user config via a
read-modify-write that preserves the rest of that config (calibration block,
OPT/EE safety thresholds).

## Background / current state

- `TEC_TRIP` is one of five safety values stored in the **console user config**
  (a JSON blob in console flash): `TEC_TRIP`, `OPT_GAIN`, `OPT_THRESH`,
  `EE_GAIN`, `EE_THRESH`. The firmware default is
  `{"TEC_TRIP": 40, "OPT_THRESH": 7143, "EE_THRESH": 5000, "EE_GAIN": 1.86, "OPT_GAIN": 1.86}`.
- Firmware reads `TEC_TRIP` as a **°C setpoint**, converts it to a resistance →
  voltage threshold, and trips when `TEC_TRIP_VALUE != 0.0 && tec_volts > TEC_TRIP_VALUE`.
  **A missing or `0` `TEC_TRIP` silently DISABLES the over-temp trip.**
- The **clinical bloodflow app does not currently touch the console user config
  at all** — no `read_config`/`write_config` calls in `motion_connector.py`. Today
  `TEC_TRIP` is only managed by the engineering **test-app**.
- The same console config also holds the **BFI/BVI calibration block**, written by
  the bloodflow calibration flow (SDK `write_calibration`, which itself does a
  read-modify-write). Therefore any `TEC_TRIP` write **must** be read-modify-write
  — a blind whole-config write would clobber calibration and the other safety keys.
  (This is the same failure mode recorded for the HIL `test_console.py` config
  round-trip tests, which have wiped `TEC_TRIP` to 0 on real hardware.)
- Natural hook point: the console-connect block in
  `motion_connector.py` (`_on_handle_state_changed_impl`, around line 1330) already
  pushes `tec_voltage`, fan speed, and **laser power "from config"** on every
  connect. `TEC_TRIP` belongs right alongside laser power.
- A `_console_mutex` (`QRecursiveMutex`) already exists on the connector
  (`motion_connector.py:812`).

## Decisions (resolved during brainstorming)

| Question | Decision |
|---|---|
| Scope | **`TEC_TRIP` only.** Leave `OPT_*`/`EE_*` exactly as they are on the device. |
| Bad/absent value handling | **Validate + write-if-different; never write a bad value.** Absent / non-numeric / out-of-range → leave the device's existing trip untouched. |
| Surfacing of failures | **Log-only (ERROR).** Do not block scanning, do not show a popup. The device keeps its prior valid trip. |
| Safe range | **`1.0`–`60.0` °C** guard rails (around the 40 °C default). Exact envelope to be confirmed with firmware owners — see Open Questions. |

## Architecture

Approach: a **pure helper in `motion_config.py`** plus a **thin call from the
connector** on console connect. This keeps the 4031-line `motion_connector.py`
from growing, mirrors the existing `motion_config.py` laser-param helpers, and —
since there is no working hardware mock mode — makes the logic unit-testable with a
fake console.

### Unit 1 — `motion_config.py`: `ensure_tec_trip`

```python
TEC_TRIP_MIN_C = 1.0    # guard rails against a typo disabling/mis-setting the
TEC_TRIP_MAX_C = 60.0   # trip — NOT a precise safety envelope (confirm w/ FW)

class TecTripOutcome(enum.Enum):
    WROTE = "wrote"            # value valid, differed, written OK
    UNCHANGED = "unchanged"    # value valid, already matches device
    SKIPPED_INVALID = "skipped_invalid"  # absent / non-numeric / out of range
    FAILED = "failed"          # read_config or write_config failed/raised

def ensure_tec_trip(console, temp_c) -> TecTripOutcome:
    ...
```

**Behavior:**
1. Coerce `temp_c` to `float`. If `None`, non-numeric, `NaN`, `<= 0`, or outside
   `[TEC_TRIP_MIN_C, TEC_TRIP_MAX_C]` → return `SKIPPED_INVALID` (no device I/O,
   nothing written).
2. `cfg = console.read_config()`. If it returns `None` or raises → return `FAILED`.
3. Read existing `cfg.get("TEC_TRIP")`. If it equals the configured value
   (compared as numbers) → return `UNCHANGED` (no write).
4. `cfg.set("TEC_TRIP", value)` then `console.write_config(cfg)`. If the write
   returns `None` or raises → return `FAILED`. Otherwise → `WROTE`.

The helper does **no logging** and raises nothing to the caller — it returns an
outcome enum. (The connector owns logging; the helper stays pure/testable.)

Note: step 4 mutates only the `TEC_TRIP` key on the object returned by
`read_config()`, so calibration + `OPT_*`/`EE_*` keys are carried through
unchanged. This is the read-modify-write that protects calibration.

### Unit 2 — `motion_connector.py`: connect-time call

In `_on_handle_state_changed_impl`, inside the existing console-connect `try`
block, immediately after `set_laser_power_from_config(...)`:

```python
with QMutexLocker(self._console_mutex):
    temp_c = self._app_config.get("tecTripTempC", 40)
    outcome = ensure_tec_trip(self._interface.console, temp_c)
# log per outcome (see below)
```

- The `_console_mutex` is held across the whole helper call so the read+write
  pair is **atomic** with respect to the telemetry poller and any other console
  access (otherwise a concurrent writer between our read and write would lose
  data). `_console_mutex` is a `QRecursiveMutex`, so re-entrancy is safe.
- Logging by outcome:
  - `WROTE` → `INFO`  "TEC_TRIP set to {value} °C"
  - `UNCHANGED` → `INFO`/`DEBUG`  "TEC_TRIP already {value} °C, skipping write"
  - `SKIPPED_INVALID` → `ERROR`  "Invalid tecTripTempC={raw}; leaving device over-temp trip untouched"
  - `FAILED` → `ERROR`  "Failed to apply TEC_TRIP={value} °C to console"
- **Non-fatal in all cases:** never raises a critical `E-code`, never gates
  scanning. (Contrast with laser power, which raises `E-103` on failure — there,
  a failure means no scan at all; here, the device's previous valid over-temp
  trip remains in force.)

### Unit 3 — config

Add to `config/app_config.json`, next to `cameraTempAlertThresholdC`:

```json
"tecTripTempC": 40,
```

(`40` = firmware default.) Edit the file with a UTF-8-clean writer — **never**
`Set-Content -Encoding utf8` (adds a BOM that breaks the app's `json.load`).

## Data flow

```
console CONNECTED event
  → _on_handle_state_changed_impl (Qt slot)
      → [existing] log_console_info, tec_voltage, set_fan_speed, set_laser_power_from_config
      → [new] with _console_mutex:
            tecTripTempC = app_config.get("tecTripTempC", 40)
            ensure_tec_trip(console, tecTripTempC):
                validate → read_config → compare → (set + write_config)
      → log outcome
```

## Error handling

| Situation | Outcome | App behavior |
|---|---|---|
| `tecTripTempC` absent from config | valid → `WROTE`/`UNCHANGED` | `.get("tecTripTempC", 40)` supplies the 40 °C default, which is valid and gets applied normally. |
| `tecTripTempC` explicitly `null` | `SKIPPED_INVALID` | `.get` returns `None`; helper rejects it. ERROR logged; device trip left as-is; **no write**. |
| Value `0`, negative, non-numeric, NaN, or outside 1–60 | `SKIPPED_INVALID` | ERROR logged; device trip left as-is; **no write**. |
| `read_config()` returns None / raises | `FAILED` | ERROR logged; no write; app continues. |
| `write_config()` returns None / raises | `FAILED` | ERROR logged; app continues. |
| Console disconnects mid-call | Exception caught by the existing connect-block `try/except` | Already logged as "connect-time setup interrupted". |

Nothing here blocks scanning or raises a critical error (per the Surfacing
decision).

## Testing

No working hardware mock mode exists, so test the seam with a fake console.

`tests/test_tec_trip_config.py` (`@pytest.mark.unit`):
- **valid + different** → `WROTE`; `write_config` called once; `TEC_TRIP` updated;
  calibration block + `OPT_*`/`EE_*` keys preserved on the written config.
- **valid + equal** → `UNCHANGED`; `write_config` **not** called.
- **absent / `0` / negative / NaN / above max / below min / non-numeric** →
  `SKIPPED_INVALID`; `write_config` **not** called.
- **`read_config` returns None** and **`read_config` raises** → `FAILED`; no crash.
- **`write_config` returns None / raises** → `FAILED`.

`tests/test_app_config_defaults.py`: add an assertion that `tecTripTempC` is
present and is a number (update the expected-keys set if that test enumerates keys).

## Out of scope (YAGNI)

- No UI to view/edit `TEC_TRIP` in the clinical app (the test-app already has one).
- No management of `OPT_*`/`EE_*` from app config.
- No change to firmware or SDK (`read_config`/`write_config`/`MotionConfig.get/set`
  already provide everything needed).
- No re-apply on a timer or per-scan — connect-time only, mirroring laser power.

## Open questions

- **Exact safe range.** `1.0`–`60.0` °C are placeholder guard rails. Confirm the
  real safe envelope for the TEC over-temp trip with firmware/SDK owners and adjust
  `TEC_TRIP_MIN_C`/`TEC_TRIP_MAX_C` before/at implementation.
