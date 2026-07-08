"""TEC-parameter helper.

Loads `config/tec_params.json` (the TEC DAC setpoints) and picks the one
matching the connected console's hardware revision. Extracted from
`motion_connector.py` to keep that file focused on the Qt connector.

The laser-power application and FPGA register map that used to live here now
live in the SDK (`omotion.laser` — bundled `laser_params.json` /
`fpga_model.json` and `apply_laser_power`), so the SDK is the single owner of
that config. The thermistor R-T lookup likewise lives in
`omotion.console_telemetry_conversions`.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
from pathlib import Path

from utils.resource_path import resource_path


logger = logging.getLogger("openmotion.bloodflow-app.motion_config")


# --- TEC DAC setpoint (issue #269) --------------------------------------------
# EVT2 units need +1.16 V on the TEC DAC to hold the lasers at 25 C; DVT1a
# changed the voltage divider around the DAC input, so DVT-and-beyond use the
# TEC_VOLTAGE_DEFAULT from tec_params.json (-0.07 V).
#
# The unit revision comes from the console's 3-bit BRD_V0..V2 hardware strap
# (SDK `console.read_board_id()`, OW_CTRL_BOARDID): EVT2 straps 1; the DVT1a
# Unified Console Board (700-00010 Rev 0.4 / Rev 1) straps 2. The SDK returns
# 0 on a transport error, so 0 must never be listed as an EVT2 id.
TEC_VOLTAGE_DEFAULT = -0.07       # volts — DVT1a and beyond
TEC_VOLTAGE_EVT2_DEFAULT = 1.16   # volts — EVT2 units
EVT2_BOARD_IDS_DEFAULT = (1,)     # board-ID strap values that mean "EVT2"


@dataclasses.dataclass(frozen=True)
class TecVoltageParams:
    """TEC DAC setpoints from tec_params.json, by console hardware revision."""

    default_v: float = TEC_VOLTAGE_DEFAULT
    evt2_v: float = TEC_VOLTAGE_EVT2_DEFAULT
    evt2_board_ids: tuple = EVT2_BOARD_IDS_DEFAULT


def load_tec_voltage_params(config_dir: str) -> TecVoltageParams:
    """Load `tec_params.json` and return the TEC DAC setpoint parameters.

    Every field falls back to its hard-coded default independently, so a
    pre-#269 file (TEC_VOLTAGE_DEFAULT only) keeps working and a malformed
    EVT2 entry can never break the DVT path.
    """
    config_path = (
        resource_path("config", "tec_params.json")
        if config_dir == "config"
        else Path(config_dir) / "tec_params.json"
    )

    try:
        with open(config_path, "r") as f:
            raw = json.load(f)
    except FileNotFoundError:
        logger.warning(
            "TEC parameter file not found: %s, using defaults %r",
            config_path, TecVoltageParams(),
        )
        return TecVoltageParams()
    except json.JSONDecodeError as e:
        logger.error(
            "Invalid JSON in %s: %s, using defaults %r",
            config_path, e, TecVoltageParams(),
        )
        return TecVoltageParams()
    except Exception as e:
        logger.error(
            "Error loading TEC parameters: %s, using defaults %r",
            e, TecVoltageParams(),
        )
        return TecVoltageParams()

    def _volts(key, fallback):
        value = raw.get(key, fallback)
        try:
            return float(value)
        except (TypeError, ValueError):
            logger.error(
                "Invalid %s=%r in %s, using default %sV",
                key, value, config_path, fallback,
            )
            return fallback

    default_v = _volts("TEC_VOLTAGE_DEFAULT", TEC_VOLTAGE_DEFAULT)
    evt2_v = _volts("TEC_VOLTAGE_EVT2", TEC_VOLTAGE_EVT2_DEFAULT)

    ids_raw = raw.get("EVT2_BOARD_IDS", list(EVT2_BOARD_IDS_DEFAULT))
    if isinstance(ids_raw, list):
        # ints only — bools (True == 1) and strings must never widen
        # EVT2 detection via a config typo.
        evt2_board_ids = tuple(x for x in ids_raw if type(x) is int)
        if len(evt2_board_ids) != len(ids_raw):
            logger.error(
                "Dropped non-integer entries from EVT2_BOARD_IDS=%r in %s",
                ids_raw, config_path,
            )
    else:
        logger.error(
            "Invalid EVT2_BOARD_IDS=%r in %s (expected a list), using "
            "default %r", ids_raw, config_path, EVT2_BOARD_IDS_DEFAULT,
        )
        evt2_board_ids = EVT2_BOARD_IDS_DEFAULT

    params = TecVoltageParams(
        default_v=default_v, evt2_v=evt2_v, evt2_board_ids=evt2_board_ids,
    )
    logger.info("Loaded TEC voltage params from %s: %r", config_path, params)
    return params


def select_tec_voltage(console, params: TecVoltageParams):
    """Pick the TEC DAC setpoint for the connected console.

    Reads the console's board-ID strap and returns ``(voltage, reason)``;
    ``reason`` is a short human-readable string for the caller's log line.
    Never raises and never logs (the caller owns logging).

    Fail-safe direction: any read failure or unknown/error board id falls
    back to ``params.default_v`` — exactly the pre-#269 behavior for every
    unit. EVT2 is the only special case.
    """
    try:
        board_id = console.read_board_id()
    except Exception as e:
        return params.default_v, f"board id read failed: {e}"
    # `type is int` (not isinstance) — SDK demo mode returns True, and a
    # bool must never satisfy EVT2 detection.
    if type(board_id) is int and board_id in params.evt2_board_ids:
        return params.evt2_v, f"EVT2 console (board id {board_id})"
    return params.default_v, f"board id {board_id!r}"


# --- TEC over-temp trip (TEC_TRIP) -------------------------------------------
# Hardcoded guard rails (°C) for the configured TEC_TRIP. A value outside this
# range is rejected so a config typo can't disable the firmware over-temp trip
# (TEC_TRIP == 0 turns it off) or set a wildly wrong setpoint.
TEC_TRIP_MIN_C = 1.0
TEC_TRIP_MAX_C = 60.0


class TecTripOutcome(enum.Enum):
    """Result of ensure_tec_trip — caller decides how to log/surface each."""

    WROTE = "wrote"           # valid, differed, written OK
    UNCHANGED = "unchanged"   # valid, already matches device
    # non-numeric or outside [TEC_TRIP_MIN_C, TEC_TRIP_MAX_C]:
    SKIPPED_INVALID = "skipped_invalid"
    FAILED = "failed"         # read_config or write_config failed/raised


def ensure_tec_trip(console, temp_c) -> TecTripOutcome:
    """Ensure the console's TEC_TRIP over-temp trip matches ``temp_c`` (°C).

    Read-modify-write that touches ONLY the TEC_TRIP key, so the calibration
    block and the OPT_*/EE_* safety thresholds in the console user config are
    preserved. Returns an outcome; never raises and never logs (the caller
    owns logging).

    - SKIPPED_INVALID: ``temp_c`` is non-numeric or outside
      [TEC_TRIP_MIN_C, TEC_TRIP_MAX_C] (so 0, negative, and NaN are all
      rejected). Nothing is read or written, so a bad config value can never
      disable the firmware over-temp trip.
    - FAILED: read_config or write_config returned None or raised.
    - UNCHANGED: the device already holds this value (no write performed).
    - WROTE: the value was valid, differed from the device, and was written.
    """
    # 1. Validate — never let a bad value reach the device. The range check
    #    alone rejects 0/negative/NaN/out-of-range (NaN fails any comparison).
    try:
        value = float(temp_c)
    except (TypeError, ValueError):
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
