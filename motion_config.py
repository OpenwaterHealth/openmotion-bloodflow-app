"""TEC-parameter helper.

Loads `config/tec_params.json` (the TEC voltage default). Extracted from
`motion_connector.py` to keep that file focused on the Qt connector.

The laser-power application and FPGA register map that used to live here now
live in the SDK (`omotion.laser` — bundled `laser_params.json` /
`fpga_model.json` and `apply_laser_power`), so the SDK is the single owner of
that config. The thermistor R-T lookup likewise lives in
`omotion.console_telemetry_conversions`.
"""

from __future__ import annotations

import enum
import json
import logging
import math
from pathlib import Path

from utils.resource_path import resource_path


logger = logging.getLogger("ow-testapp")


def load_tec_params(config_dir: str) -> float:
    """Load `tec_params.json` and return the TEC_VOLTAGE_DEFAULT value.

    Returns the float voltage from the file, or the hard-coded default on any
    error.
    """
    _TEC_VOLTAGE_DEFAULT = -0.07  # volts
    config_path = (
        resource_path("config", "tec_params.json")
        if config_dir == "config"
        else Path(config_dir) / "tec_params.json"
    )

    if not config_path.exists():
        logger.warning(
            f"[Connector] TEC parameter file not found: {config_path}, "
            f"using default value {_TEC_VOLTAGE_DEFAULT}V"
        )
        return _TEC_VOLTAGE_DEFAULT

    try:
        with open(config_path, "r") as f:
            params = json.load(f)
        voltage = params.get("TEC_VOLTAGE_DEFAULT", _TEC_VOLTAGE_DEFAULT)
        logger.info(
            f"[Connector] Loaded TEC voltage from {config_path}: {voltage}V"
        )
        return voltage
    except FileNotFoundError:
        logger.warning(
            f"[Connector] TEC parameter file not found: {config_path}, "
            f"using default value {_TEC_VOLTAGE_DEFAULT}V"
        )
        return _TEC_VOLTAGE_DEFAULT
    except json.JSONDecodeError as e:
        logger.error(
            f"[Connector] Invalid JSON in {config_path}: {e}, "
            f"using default value {_TEC_VOLTAGE_DEFAULT}V"
        )
        return _TEC_VOLTAGE_DEFAULT
    except Exception as e:
        logger.error(
            f"[Connector] Error loading TEC parameters: {e}, "
            f"using default value {_TEC_VOLTAGE_DEFAULT}V"
        )
        return _TEC_VOLTAGE_DEFAULT


# --- TEC over-temp trip (TEC_TRIP) -------------------------------------------
# Guard rails around the configured TEC_TRIP value (°C). These are NOT a
# precise safety envelope — they exist so a config typo can't disable the
# firmware over-temp trip (TEC_TRIP == 0 turns it off) or set a wildly wrong
# setpoint. Confirm the real safe envelope with firmware/SDK owners before
# tightening.
TEC_TRIP_MIN_C = 1.0
TEC_TRIP_MAX_C = 60.0


class TecTripOutcome(enum.Enum):
    """Result of ensure_tec_trip — caller decides how to log/surface each."""

    WROTE = "wrote"           # valid, differed, written OK
    UNCHANGED = "unchanged"   # valid, already matches device
    # absent/non-numeric/NaN/<=0/out-of-range:
    SKIPPED_INVALID = "skipped_invalid"
    FAILED = "failed"         # read_config or write_config failed/raised


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
