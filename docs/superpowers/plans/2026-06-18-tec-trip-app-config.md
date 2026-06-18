# Configurable Console TEC_TRIP from app_config.json — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the clinical bloodflow app set the console's `TEC_TRIP` over-temperature trip (°C) from `config/app_config.json`, pushing it to the device on every console connect.

**Architecture:** A pure, unit-testable helper `ensure_tec_trip(console, temp_c)` in `motion_config.py` validates the value and does a read-modify-write of the console user config (touching only `TEC_TRIP`, preserving the calibration block + OPT/EE safety keys). The connector calls it once on console connect — alongside the existing laser-power push — under `_console_mutex`. A new `tecTripTempC` config key is registered in both the shipped JSON and `main.py`'s in-code defaults whitelist.

**Tech Stack:** Python 3.13, PyQt6, `omotion` SDK (`MotionConfig`, `MotionConsole.read_config`/`write_config`), pytest (`@pytest.mark.unit`), flake8.

---

## Background the engineer needs

- The console **user config** is a JSON blob in console flash holding five safety values — `TEC_TRIP`, `OPT_GAIN`, `OPT_THRESH`, `EE_GAIN`, `EE_THRESH` — **plus** a `calibration` block written by the bloodflow calibration flow. Firmware default: `{"TEC_TRIP": 40, "OPT_THRESH": 7143, "EE_THRESH": 5000, "EE_GAIN": 1.86, "OPT_GAIN": 1.86}`.
- Firmware reads `TEC_TRIP` as a **°C setpoint** and guards the trip with `TEC_TRIP_VALUE != 0.0`. **A `0`/missing `TEC_TRIP` silently DISABLES the over-temp trip.** So a write must be read-modify-write (never clobber calibration/OPT/EE) and must never push `0`/garbage.
- SDK primitives (already exist, do not modify): `console.read_config() -> Optional[MotionConfig]` (returns `None` on device error, raises `ValueError` if not connected), `console.write_config(cfg) -> Optional[MotionConfig]` (returns `None` on device error), `MotionConfig.get(key, default)`, `MotionConfig.set(key, value)`, `MotionConfig(json_data={...})`. `MotionConfig` is importable as `from omotion import MotionConfig`.
- **Config whitelist gotcha:** `main._load_app_config()` (`main.py:67`) keeps only keys present in its in-code `defaults` dict (`main.py:158-161`). A key added to `config/app_config.json` but **not** to that dict is silently dropped on load. Both must be updated.
- **Editing `config/app_config.json`:** use the Edit/Write tools (UTF-8, no BOM). NEVER `Set-Content -Encoding utf8` — it adds a BOM that makes the app's `json.load` fail silently and fall back to defaults.
- The connect-time hook is `_on_handle_state_changed_impl` in `motion_connector.py` (the `if name == "console": ... if is_now_connected:` block, ~line 1330), which already pushes `tec_voltage`, fan speed, and `set_laser_power_from_config`.
- `_console_mutex = QRecursiveMutex()` exists (`motion_connector.py:812`) but is currently unused; we use it to serialize the config read-modify-write pair against any future console-config writer that also takes it.

---

## File structure

- `motion_config.py` (modify) — add `TEC_TRIP_MIN_C`, `TEC_TRIP_MAX_C`, `TecTripOutcome`, `ensure_tec_trip`. (This file is the app's TEC-parameter helper home; the helper does no logging and no Qt — pure and testable.)
- `tests/test_tec_trip_config.py` (create) — unit tests for `ensure_tec_trip` with a fake console.
- `main.py` (modify) — register `"tecTripTempC": 40` in the `_load_app_config` defaults whitelist.
- `config/app_config.json` (modify) — add `"tecTripTempC": 40`.
- `tests/test_app_config_defaults.py` (modify) — pin that `tecTripTempC` survives load + ships in the config.
- `motion_connector.py` (modify) — import the helper; add `apply_tec_trip_from_config`; call it on console connect.
- `CLAUDE.md` (modify) — document the new flag in the Notable config flags table.

---

## Task 1: `ensure_tec_trip` helper + unit tests

**Files:**
- Create: `tests/test_tec_trip_config.py`
- Modify: `motion_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tec_trip_config.py`:

```python
"""Unit tests for ensure_tec_trip (motion_config) — TEC_TRIP push from app config.

ensure_tec_trip does a read-modify-write of the console user config, touching
ONLY the TEC_TRIP key so the calibration block and OPT_*/EE_* safety thresholds
survive. A 0/garbage value must never reach the device, because the firmware
treats TEC_TRIP == 0 as "over-temp trip disabled".
"""

import math

import pytest
from omotion import MotionConfig

from motion_config import (
    TEC_TRIP_MAX_C,
    TEC_TRIP_MIN_C,
    TecTripOutcome,
    ensure_tec_trip,
)

pytestmark = pytest.mark.unit


def _device_config(tec_trip=40):
    """Representative device user config: TEC_TRIP + OPT/EE safety keys + a
    calibration block. ensure_tec_trip must preserve every key but TEC_TRIP."""
    return MotionConfig(json_data={
        "TEC_TRIP": tec_trip,
        "OPT_THRESH": 7143,
        "EE_THRESH": 5000,
        "EE_GAIN": 1.86,
        "OPT_GAIN": 1.86,
        "calibration": {"version": 1, "bfi_scale": [1.0, 2.0]},
    })


class FakeConsole:
    """Records read/write of the user config for assertions.

    read_result: MotionConfig returned by read_config, or None, or an Exception
    instance to simulate read failure. write_returns_none / write_raises
    simulate a failed write.
    """

    def __init__(self, read_result, write_returns_none=False, write_raises=False):
        self._read_result = read_result
        self._write_returns_none = write_returns_none
        self._write_raises = write_raises
        self.written = None
        self.write_calls = 0

    def read_config(self):
        if isinstance(self._read_result, Exception):
            raise self._read_result
        return self._read_result

    def write_config(self, config):
        self.write_calls += 1
        if self._write_raises:
            raise RuntimeError("simulated write failure")
        if self._write_returns_none:
            return None
        self.written = config
        return config


def test_valid_and_different_writes_and_preserves_other_keys():
    console = FakeConsole(_device_config(tec_trip=40))
    outcome = ensure_tec_trip(console, 45)
    assert outcome is TecTripOutcome.WROTE
    assert console.write_calls == 1
    written = console.written.json_data
    assert float(written["TEC_TRIP"]) == 45.0
    # Calibration + OPT/EE keys must ride through the read-modify-write.
    assert written["OPT_THRESH"] == 7143
    assert written["EE_THRESH"] == 5000
    assert written["EE_GAIN"] == 1.86
    assert written["OPT_GAIN"] == 1.86
    assert written["calibration"] == {"version": 1, "bfi_scale": [1.0, 2.0]}


def test_valid_and_equal_skips_write():
    console = FakeConsole(_device_config(tec_trip=40))
    assert ensure_tec_trip(console, 40) is TecTripOutcome.UNCHANGED
    assert console.write_calls == 0


def test_valid_and_equal_across_int_float_skips_write():
    console = FakeConsole(_device_config(tec_trip=40))
    assert ensure_tec_trip(console, 40.0) is TecTripOutcome.UNCHANGED
    assert console.write_calls == 0


@pytest.mark.parametrize(
    "bad",
    [0, -5, math.nan, None, "oops", TEC_TRIP_MAX_C + 1, TEC_TRIP_MIN_C - 0.5],
)
def test_invalid_values_never_write(bad):
    console = FakeConsole(_device_config(tec_trip=40))
    assert ensure_tec_trip(console, bad) is TecTripOutcome.SKIPPED_INVALID
    assert console.write_calls == 0


def test_read_config_none_is_failed():
    console = FakeConsole(None)
    assert ensure_tec_trip(console, 45) is TecTripOutcome.FAILED
    assert console.write_calls == 0


def test_read_config_raises_is_failed():
    console = FakeConsole(ValueError("not connected"))
    assert ensure_tec_trip(console, 45) is TecTripOutcome.FAILED
    assert console.write_calls == 0


def test_write_config_none_is_failed():
    console = FakeConsole(_device_config(tec_trip=40), write_returns_none=True)
    assert ensure_tec_trip(console, 45) is TecTripOutcome.FAILED
    assert console.write_calls == 1


def test_write_config_raises_is_failed():
    console = FakeConsole(_device_config(tec_trip=40), write_raises=True)
    assert ensure_tec_trip(console, 45) is TecTripOutcome.FAILED
    assert console.write_calls == 1


def test_device_missing_tec_trip_is_treated_as_differing():
    cfg = _device_config()
    del cfg.json_data["TEC_TRIP"]
    console = FakeConsole(cfg)
    assert ensure_tec_trip(console, 45) is TecTripOutcome.WROTE
    assert console.write_calls == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_tec_trip_config.py -v`
Expected: collection/import error — `ImportError: cannot import name 'ensure_tec_trip' from 'motion_config'`.

- [ ] **Step 3: Implement the helper**

Add to the top of `motion_config.py`, after the existing `from pathlib import Path` import block (alongside the other module-level imports):

```python
import enum
import math
```

Then append to the end of `motion_config.py`:

```python
# --- TEC over-temp trip (TEC_TRIP) -------------------------------------------
# Guard rails around the configured TEC_TRIP value (°C). These are NOT a precise
# safety envelope — they exist so a config typo can't disable the firmware
# over-temp trip (TEC_TRIP == 0 turns it off) or set a wildly wrong setpoint.
# Confirm the real safe envelope with firmware/SDK owners before tightening.
TEC_TRIP_MIN_C = 1.0
TEC_TRIP_MAX_C = 60.0


class TecTripOutcome(enum.Enum):
    """Result of ensure_tec_trip — the caller decides how to log/surface each."""

    WROTE = "wrote"                      # valid, differed, written OK
    UNCHANGED = "unchanged"              # valid, already matches device
    SKIPPED_INVALID = "skipped_invalid"  # absent/non-numeric/NaN/<=0/out of range
    FAILED = "failed"                    # read_config or write_config failed/raised


def ensure_tec_trip(console, temp_c) -> TecTripOutcome:
    """Ensure the console's TEC_TRIP over-temp trip matches ``temp_c`` (°C).

    Read-modify-write that touches ONLY the TEC_TRIP key, so the calibration
    block and the OPT_*/EE_* safety thresholds in the console user config are
    preserved. Returns an outcome; never raises and never logs (the caller
    owns logging).

    - SKIPPED_INVALID: ``temp_c`` is None/non-numeric/NaN/<=0/outside
      [TEC_TRIP_MIN_C, TEC_TRIP_MAX_C]. Nothing is read or written, so a bad
      config value can never disable the firmware over-temp trip.
    - FAILED: read_config or write_config returned None or raised.
    - UNCHANGED: the device already holds this value (no write performed).
    - WROTE: the value was valid, differed from the device, and was written.
    """
    # 1. Validate — never let a bad value reach the device.
    try:
        value = float(temp_c)
    except (TypeError, ValueError):
        return TecTripOutcome.SKIPPED_INVALID
    if math.isnan(value) or value <= 0.0:
        return TecTripOutcome.SKIPPED_INVALID
    if not (TEC_TRIP_MIN_C <= value <= TEC_TRIP_MAX_C):
        return TecTripOutcome.SKIPPED_INVALID

    # 2. Read the current config (the base for the read-modify-write).
    try:
        cfg = console.read_config()
    except Exception:
        return TecTripOutcome.FAILED
    if cfg is None:
        return TecTripOutcome.FAILED

    # 3. Skip the flash write if the device already matches.
    try:
        if float(cfg.get("TEC_TRIP")) == value:
            return TecTripOutcome.UNCHANGED
    except (TypeError, ValueError):
        pass  # missing/garbage on device -> treat as differing, write it

    # 4. Write only TEC_TRIP; calibration + OPT_*/EE_* ride along unchanged.
    try:
        cfg.set("TEC_TRIP", value)
        result = console.write_config(cfg)
    except Exception:
        return TecTripOutcome.FAILED
    if result is None:
        return TecTripOutcome.FAILED
    return TecTripOutcome.WROTE
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_tec_trip_config.py -v`
Expected: PASS (all cases, including the 7 parametrized invalid values).

- [ ] **Step 5: Lint**

Run: `python -m flake8 motion_config.py tests/test_tec_trip_config.py`
Expected: no output (clean).

- [ ] **Step 6: Commit**

```bash
git add motion_config.py tests/test_tec_trip_config.py
git commit -m "feat: add ensure_tec_trip helper for console over-temp trip"
```

---

## Task 2: Register `tecTripTempC` config key (defaults whitelist + shipped JSON)

**Files:**
- Modify: `tests/test_app_config_defaults.py`
- Modify: `main.py:69-150` (the `defaults` dict)
- Modify: `config/app_config.json`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app_config_defaults.py`:

```python
def test_tec_trip_temp_default_is_registered(tmp_path, monkeypatch):
    """tecTripTempC must be in the in-code defaults whitelist, or
    _load_app_config silently drops it and the connector never pushes the
    configured over-temp trip."""
    # Present in the pure in-code defaults (no file on disk).
    _patch_config_path(monkeypatch, tmp_path / "app_config.json")
    cfg = app_main._load_app_config()
    assert cfg["tecTripTempC"] == 40

    # A value supplied in the file survives the whitelist filter.
    config_path = tmp_path / "app_config.json"
    config_path.write_text(json.dumps({"tecTripTempC": 42}), encoding="utf-8")
    _patch_config_path(monkeypatch, config_path)
    cfg = app_main._load_app_config()
    assert cfg["tecTripTempC"] == 42


def test_tec_trip_temp_present_in_shipped_config():
    """The shipped config must carry tecTripTempC so field installs push a
    trip on connect rather than leaving whatever the device last had."""
    shipped = app_main.resource_path("config", "app_config.json")
    with open(shipped, "r", encoding="utf-8") as f:
        assert "tecTripTempC" in json.load(f)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_app_config_defaults.py -v -k tec_trip`
Expected: both FAIL — `KeyError: 'tecTripTempC'` (defaults) and `assert 'tecTripTempC' in {...}` (shipped).

- [ ] **Step 3: Register the key in the defaults whitelist**

In `main.py`, in the `defaults` dict, add the line immediately after `"cameraTempAlertThresholdC": 105,` (line 71):

```python
        "cameraTempAlertThresholdC": 105,
        # Console over-temp trip (°C) pushed to the console user config on
        # connect. 0/missing disables the firmware trip, so this is validated
        # (1-60 °C) before any write; see motion_config.ensure_tec_trip.
        "tecTripTempC": 40,
```

- [ ] **Step 4: Add the key to the shipped config**

In `config/app_config.json`, add `"tecTripTempC": 40,` next to the existing `"cameraTempAlertThresholdC"` line. Use the Edit tool (UTF-8, no BOM) — do NOT use `Set-Content -Encoding utf8`.

Example (match the file's existing formatting/value for `cameraTempAlertThresholdC`):

```json
    "cameraTempAlertThresholdC": 110,
    "tecTripTempC": 40,
```

- [ ] **Step 5: Verify the JSON still loads (no BOM, valid)**

Run: `python -c "import json; print(json.load(open('config/app_config.json'))['tecTripTempC'])"`
Expected: `40`

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_app_config_defaults.py -v`
Expected: PASS (the new tec_trip tests plus all existing tests, including the `autoConfigureOnStartup` tombstone scan).

- [ ] **Step 7: Commit**

```bash
git add main.py config/app_config.json tests/test_app_config_defaults.py
git commit -m "feat: register tecTripTempC app config key (default 40 C)"
```

---

## Task 3: Wire the connect-time TEC_TRIP push into the connector

**Files:**
- Modify: `motion_connector.py:38-40` (import), add method near `:3302`, call in connect block near `:1354`

- [ ] **Step 1: Extend the motion_config import**

In `motion_connector.py`, replace the existing import block (lines 38-40):

```python
from motion_config import (
    load_tec_params,
)
```

with:

```python
from motion_config import (
    TEC_TRIP_MAX_C,
    TEC_TRIP_MIN_C,
    TecTripOutcome,
    ensure_tec_trip,
    load_tec_params,
)
```

- [ ] **Step 2: Add the `apply_tec_trip_from_config` method**

In `motion_connector.py`, immediately after the `set_laser_power_from_config` method (it ends at line 3307 with `return interface.apply_laser_power(...)`), add:

```python
    def apply_tec_trip_from_config(self, interface) -> TecTripOutcome:
        """Push the configured TEC over-temp trip (tecTripTempC, °C) into the
        console user config. Read-modify-write under _console_mutex so the
        read+write pair can't interleave with another console-config writer;
        only the TEC_TRIP key is touched (calibration + OPT_*/EE_* preserved).

        Non-fatal: a bad config value or a device read/write failure is logged
        at ERROR and the device keeps its existing trip — never blocks scanning
        and never writes 0 (which would disable the firmware over-temp trip)."""
        temp_c = self._app_config.get("tecTripTempC", 40)
        self._console_mutex.lock()
        try:
            outcome = ensure_tec_trip(interface.console, temp_c)
        finally:
            self._console_mutex.unlock()

        if outcome is TecTripOutcome.WROTE:
            logger.info(f"Console TEC_TRIP set to {temp_c} °C")
        elif outcome is TecTripOutcome.UNCHANGED:
            logger.info(f"Console TEC_TRIP already {temp_c} °C, skipping write")
        elif outcome is TecTripOutcome.SKIPPED_INVALID:
            logger.error(
                f"Invalid tecTripTempC={temp_c!r}; leaving console over-temp "
                f"trip untouched (expected a number in "
                f"{TEC_TRIP_MIN_C}-{TEC_TRIP_MAX_C} °C)"
            )
        else:  # TecTripOutcome.FAILED
            logger.error(f"Failed to apply TEC_TRIP={temp_c} °C to console")
        return outcome
```

- [ ] **Step 3: Call it on console connect**

In `motion_connector.py`, in the console-connect block, insert the call after the laser-power push and before the `except Exception as e:`. Replace:

```python
                        self._raise_critical(
                            "E-103", detail="laser power params not applied")
                except Exception as e:
```

with:

```python
                        self._raise_critical(
                            "E-103", detail="laser power params not applied")
                    # Push the configured TEC over-temp trip (tecTripTempC, °C)
                    # into the console user config. Read-modify-write that
                    # preserves calibration + OPT/EE keys; non-fatal on a bad
                    # value or write failure (device keeps its existing trip).
                    self.apply_tec_trip_from_config(self._interface)
                except Exception as e:
```

- [ ] **Step 4: Syntax + lint check**

Run: `python -c "import ast; ast.parse(open('motion_connector.py', encoding='utf-8').read()); print('ok')"`
Expected: `ok`

Run: `python -m flake8 motion_connector.py`
Expected: no output (clean). If flake8 reports a pre-existing issue unrelated to these lines, leave it; only the new lines must be clean.

- [ ] **Step 5: Commit**

```bash
git add motion_connector.py
git commit -m "feat: push configured TEC_TRIP to console on connect"
```

---

## Task 4: Document the flag + full-suite verification

**Files:**
- Modify: `CLAUDE.md` (Notable config flags table)

- [ ] **Step 1: Add a row to the Notable config flags table**

In `CLAUDE.md`, in the `## Notable config flags (config/app_config.json)` table, add a row (place it after the `histoCmp` row or wherever it reads naturally):

```markdown
| `tecTripTempC` | `40` | Console over-temp trip (°C) pushed to the console user config on connect via `motion_config.ensure_tec_trip` (read-modify-write, preserves calibration + OPT/EE keys). Validated to 1–60 °C; absent/invalid values leave the device's existing trip untouched (never writes `0`, which would disable the firmware trip). |
```

- [ ] **Step 2: Run the full unit-marked suite**

Run: `python -m pytest -m unit -q`
Expected: PASS, no regressions. (Per the SDK/app test notes: do not pipe pytest through PowerShell `Select-Object`; run it plain.)

- [ ] **Step 3: Lint the changed Python files together**

Run: `python -m flake8 motion_config.py motion_connector.py main.py tests/test_tec_trip_config.py tests/test_app_config_defaults.py`
Expected: no new output for the changed lines.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document tecTripTempC config flag"
```

---

## Verification (whole feature)

- [ ] `python -m pytest tests/test_tec_trip_config.py tests/test_app_config_defaults.py -v` → all PASS.
- [ ] `python -m pytest -m unit -q` → no regressions.
- [ ] `python -c "import json; print(json.load(open('config/app_config.json'))['tecTripTempC'])"` → `40` (config still valid, no BOM).
- [ ] Manual reasoning / hardware check (no mock mode exists): on a real console connect, the app log shows one of `Console TEC_TRIP set to 40 °C` / `... already 40 °C, skipping write`. Verify against `<dataDirectory>/app-logs/ow-bloodflowapp-*.log`.

## Notes / open items

- **Safe range (1–60 °C) is a placeholder guard rail** — confirm the real envelope with firmware/SDK owners and adjust `TEC_TRIP_MIN_C`/`TEC_TRIP_MAX_C` in `motion_config.py` if needed. This does not block implementation.
- No hardware mock mode exists, so the connect-time wiring (Task 3) is not unit-tested directly; its logic is fully covered by the `ensure_tec_trip` unit tests, and the glue mirrors the existing untested `set_laser_power_from_config` pattern.
- Branch: work continues on `claude/goofy-kare-60bd45` (current worktree). Verify `git branch --show-current` before each commit — worktrees in this repo can share state.
