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
import math
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


# --- Alternative camera settings (issue #446) ---------------------------------
# OX02C1B register model (datasheet OX02C1S/OX02C1B a-CSP DS 1.0, table A-14;
# exposure math per OmniVision's OX02C10_CalcSheet_HTS-VTS-EXP.xlsx):
#
# - Coarse exposure {0x3501,0x3502} is in whole rows; one row (Tline) =
#   HTS / VT_PIXCLK. The deployed sensor-fw timing config
#   (X02C1B_Sensor_Config.h: HTS 0x01B0 = 432 px, VT_PIXCLK 48 MHz) gives
#   Tline = 9.0 us exactly — so the only VALID exposures are multiples of 9 us.
# - Analog ("real") gain is {0x3508[4:0],0x3509[7:4]} / 16, so writing
#   0x3508 = N with 0x3509 = 0 gives exactly N.0x. Digital gain (0x350A-C)
#   is deliberately never touched and stays at the firmware's 1.000x.
#
# Firmware defaults mirrored here (what the sensor firmware itself programs,
# and what app_config.json ships as the alternative-settings defaults):
# exposure 72 rows = 648 us (X02C1B_SENSOR_CONFIG), analog gain by array
# position 16/4/2/1/1/2/4/16 (X02C1B_configure_sensor's per-camera ladder).
CAMERA_EXPOSURE_ROW_US = 9.0
CAMERA_EXPOSURE_MIN_ROWS = 11    # 99 us  (~100 us)
CAMERA_EXPOSURE_MAX_ROWS = 244   # 2196 us (~2200 us)
CAMERA_GAIN_CHOICES = (1, 2, 4, 8, 16)

FW_DEFAULT_EXPOSURE_US = 648
FW_DEFAULT_CAMERA_GAINS = (16, 4, 2, 1, 1, 2, 4, 16)

# --- Alternative laser pulse width (experiments only) -------------------------
# The console trigger's LaserPulseWidthUsec gates the TA laser drive; the
# SDK's DEFAULT_TRIGGER_CONFIG (omotion/config.py) ships 500 µs. Bench facts
# (2026-07 drift campaign): the measured OPTICAL pulse is driver-shaped at
# ~494 µs regardless of longer gates — shorter gates CLIP it. The stock
# safety FPGA PULSE_WIDTH_UL interlock trips and LATCHES (until console
# power-cycle) above ~1000 µs; the experimenter must adjust the safety
# config before using wider gates. No restore path is needed here: the scan
# flow re-resolves the full trigger config from SDK defaults before every
# scan, so disabling the toggle is sufficient.
# The 20 µs floor is a HARDWARE limit, not a UI choice — see
# TA_PULSE_MIN_TICKS below: the TA driver's compare underflows below 55
# ticks (17.6 µs) and leaves the drive latched on. 20 µs = 63 ticks keeps
# 8 ticks of margin. Do NOT lower it without re-reading driver_control.v.
LASER_PULSE_WIDTH_MIN_US = 20
LASER_PULSE_WIDTH_MAX_US = 2200
DEFAULT_LASER_PULSE_WIDTH_US = 500

# The register that ACTUALLY shapes the optical pulse. The TA driver FPGA
# edge-detects the console's laser trigger and times the drive pulse itself
# (openmotion-ta-fpga driver_control.v: `pulse_count > pulse_width-55`), so
# the trigger config's LaserPulseWidthUsec is only a start edge — bench-
# proven 2026-08-10: 500/700/1100 µs gates → identical image means. The
# pulse_width register is 24-bit at I2C 0x41, mux 1 ch 4, offset 0,
# little-endian, 0.32 µs/tick (fpga_model.json "TA_PULSE_WIDTH");
# laser_params.json ships [27, 6, 0] = 1563 ticks ≈ 500.2 µs, which is the
# measured ~494 µs optical pulse. The seed stage runs CW (no width
# register), so the TA drive width alone bounds emission.
TA_PULSE_TICK_US = 0.32
TA_PULSE_WIDTH_BASELINE_TICKS = 1563

# driver_control.v drops the drive at `pulse_count > pulse_width - 55`, so
# the register is NOT the emitted width: the drive runs ~54 ticks
# (~17.3 µs) SHORTER than commanded. Irrelevant at 500 µs (3%), dominant at
# the short end — a commanded 20 µs is a ~3 µs optical pulse. Worse, that
# compare is 24-bit unsigned: below 55 ticks `pulse_width - 55` wraps to
# ~16.7M, the compare never fires, and nothing else in the state machine
# clears `pulse` — the TA drive stays latched ON. So 55 ticks is a hard
# floor for anything written to this register, enforced here as well as by
# LASER_PULSE_WIDTH_MIN_US, because this is the write that reaches the
# laser.
TA_PULSE_TRUNCATION_TICKS = 55
TA_PULSE_MIN_TICKS = TA_PULSE_TRUNCATION_TICKS

# --- Dark-frame skip delay (clean darks at any exposure, #449) ----------------
# A scheduled dark frame isn't laser-off: the console firmware DISPLACES the
# pulse later by LaserPulseSkipDelayUsec so it lands outside the exposure
# window (console-fw trigger.c: long_lsync_arr = laserPulseDelayUsec +
# laserPulseWidthUsec - 1 + LaserPulseSkipDelayUsec). The displaced pulse
# therefore STARTS at laserPulseDelayUsec + LaserPulseSkipDelayUsec, width-
# independent. The SDK-default 1800 us displacement (→ pulse start 1900 us)
# was an uncalibrated guess that happened to clear the ~648 us stock exposure;
# any exposure past ~1800 us re-catches the displaced pulse and contaminates
# the dark reference (bench 2026-08-10: terminal laser-off dark clean at
# 128 DN, scheduled darks 143-185 DN in proportion to each camera's
# brightness; contamination onset between 1700 and 1800 us exposure).
#
# The app pins the skip to 2400 us unconditionally — every build, every
# trigger push. It rides MotionInterface's default_trigger_config (main.py),
# NOT a connector-side patch after resolve: ScanWorkflow re-sends the
# interface-resolved config immediately before start_trigger (fsync-counter
# reset), so anything patched in after resolution is silently reverted on the
# push that actually matters — which is exactly how the contamination above
# shipped despite setTrigger sending 2400. 2400 us (pulse start 2500 us)
# clears the whole alternative-exposure dropdown (max 2196 us); bench-verified
# 2026-08-10: darks clean at 2100 us exposure.
#
# HARD CONSOLE REQUIREMENT — RATE_LL: a dark frame shortens the following
# inter-pulse gap to (period - skip) = 25000 - 2400 = 22600 us at 40 Hz, and
# if that undercuts the console's EE/OPT_RATE_LL floor the safety FPGAs trip
# and LATCH until a console power-cycle. RATE_LL is NOT the stock
# laser_params.json 22500 — it lives per-console in the flash user config
# (read the connect log's "Override EE/OPT_RATE_LL raw=" lines for ground
# truth; raw x 0.32 = us). The bench console held 23125 us (raw 72266), which
# tripped instantly, and was provisioned to 22000 us (raw 68750) on
# 2026-08-10 — see HANDOFF-laser-safety-ceiling-override.md for the recipe.
# Every console running this build MUST have RATE_LL <= the constant below or
# its first scan latches the interlock.
DARK_RATE_LOWER_LIMIT_US = 22000      # max console EE/OPT_RATE_LL this skip tolerates
DARK_SKIP_BASELINE_US = 1800          # SDK DEFAULT_TRIGGER_CONFIG value (guess)
DARK_SKIP_DELAY_US = 2400             # what the app runs, everywhere

# Merged onto the SDK's DEFAULT_TRIGGER_CONFIG at MotionInterface
# construction, so every workflow's resolved trigger config carries it.
DEFAULT_TRIGGER_OVERRIDES = {"LaserPulseSkipDelayUsec": DARK_SKIP_DELAY_US}


def ta_pulse_width_write(width_us=None):
    """Build the I2C write that programs the TA driver's pulse_width.

    ``width_us=None`` means "the laser_params.json baseline" (restore path).
    Returns ``(write_kwargs, ticks)`` where ``write_kwargs`` feed
    ``console.write_i2c_packet``. Never raises: the register location and
    baseline resolve from the SDK's bundled fpga_model/laser_params when
    available and fall back to the constants above (same data, hardcoded)
    so a missing/renamed SDK entry can't break scan start.
    """
    entry = None
    try:
        from omotion.laser import FpgaMap
        entry = FpgaMap().get_entry_by_friendly_name("TA_PULSE_WIDTH")
    except Exception:
        entry = None

    if width_us is None:
        data = None
        try:
            from omotion.laser import load_laser_params
            for param in load_laser_params():
                if param.get("friendlyName") == "TA_PULSE_WIDTH":
                    data = bytearray(param["dataToSend"])
                    break
        except Exception:
            data = None
        if data is None:
            data = bytearray(
                TA_PULSE_WIDTH_BASELINE_TICKS.to_bytes(3, "little"))
        ticks = int.from_bytes(bytes(data), "little")
    else:
        ticks = max(TA_PULSE_MIN_TICKS,
                    int(round(float(width_us) / TA_PULSE_TICK_US)))
        data = bytearray(ticks.to_bytes(3, "little"))

    write_kwargs = {
        "mux_index": entry["mux_idx"] if entry else 1,
        "channel": entry["channel"] if entry else 4,
        "device_addr": entry["i2c_addr"] if entry else 0x41,
        "reg_addr": entry["start_address"] if entry else 0,
        "data": data,
    }
    return write_kwargs, ticks


@dataclasses.dataclass(frozen=True)
class CameraSettings:
    """A validated exposure + per-position analog gain set for one module."""

    exposure_rows: int      # coarse exposure register value (rows)
    gains: tuple            # 8 analog gains, index = camera position 0..7

    @property
    def exposure_us(self) -> float:
        return self.exposure_rows * CAMERA_EXPOSURE_ROW_US


FW_DEFAULT_CAMERA_SETTINGS = CameraSettings(
    exposure_rows=int(FW_DEFAULT_EXPOSURE_US / CAMERA_EXPOSURE_ROW_US),
    gains=FW_DEFAULT_CAMERA_GAINS,
)


def camera_settings_from_config(exposure_us, gains):
    """Validate config values into a CameraSettings.

    Returns ``(settings, "")`` or ``(None, reason)``; never raises and never
    logs (the caller owns logging). Fail direction is "don't write": a
    malformed hand-edited config must not guess at laser-scan camera state.

    ``exposure_us`` is snapped to the nearest whole row (so 650 from a
    hand-edit still lands on the valid 648) but rejected outside the
    [MIN_ROWS, MAX_ROWS] window. ``gains`` must be exactly 8 values, each an
    integral number from CAMERA_GAIN_CHOICES (bools rejected).
    """
    try:
        rows = round(float(exposure_us) / CAMERA_EXPOSURE_ROW_US)
    except (TypeError, ValueError):
        return None, f"exposure {exposure_us!r} is not a number"
    if not (CAMERA_EXPOSURE_MIN_ROWS <= rows <= CAMERA_EXPOSURE_MAX_ROWS):
        return None, (
            f"exposure {exposure_us!r} us is outside the valid "
            f"{CAMERA_EXPOSURE_MIN_ROWS * CAMERA_EXPOSURE_ROW_US:g}-"
            f"{CAMERA_EXPOSURE_MAX_ROWS * CAMERA_EXPOSURE_ROW_US:g} us window"
        )

    if not isinstance(gains, (list, tuple)) or len(gains) != 8:
        return None, f"gains {gains!r} is not an 8-element list"
    clean = []
    for i, g in enumerate(gains):
        # bool is an int subclass; `true` in a hand-edited JSON must not
        # become gain 1.
        if isinstance(g, bool) or not isinstance(g, (int, float)) or g != int(g):
            return None, f"camera {i + 1} gain {g!r} is not a valid gain"
        if int(g) not in CAMERA_GAIN_CHOICES:
            return None, (
                f"camera {i + 1} gain {g!r} is not one of "
                f"{list(CAMERA_GAIN_CHOICES)}"
            )
        clean.append(int(g))
    return CameraSettings(exposure_rows=int(rows), gains=tuple(clean)), ""


def laser_pulse_width_from_config(value):
    """Validate the alternative laser pulse width (µs) from app config.

    Returns ``(width, "")`` or ``(None, reason)``; never raises and never
    logs. Any whole-µs value in [MIN, MAX] is valid — the console trigger
    takes raw microseconds (no row quantization like camera exposure);
    the UI dropdown's 100 µs steps are just a usability subset, so a
    hand-edited in-between value (e.g. 750) still validates and applies.
    """
    if isinstance(value, bool):
        return None, f"pulse width {value!r} is not a number"
    try:
        width = float(value)
    except (TypeError, ValueError):
        return None, f"pulse width {value!r} is not a number"
    if not math.isfinite(width) or width != int(width):
        return None, f"pulse width {value!r} is not a whole number of µs"
    width = int(width)
    if not (LASER_PULSE_WIDTH_MIN_US <= width <= LASER_PULSE_WIDTH_MAX_US):
        return None, (
            f"pulse width {value!r} µs is outside the valid "
            f"{LASER_PULSE_WIDTH_MIN_US}-{LASER_PULSE_WIDTH_MAX_US} µs window"
        )
    return width, ""


def apply_camera_settings(sensor, camera_mask: int, settings: CameraSettings):
    """Write exposure + analog gain to every camera in ``camera_mask``.

    Uses the SDK's I2C passthrough (mux switch + register writes) — the same
    path the firmware's own configure uses, so values land exactly like the
    firmware defaults do. Writes happen while the trigger is off; the
    pipeline's warmup-frame discard absorbs the sensor's N+2-frame register
    latch when streaming resumes.

    Returns a list of failure strings (empty = all cameras written). Never
    raises; a failed camera is recorded and the rest still get written.
    """
    from omotion.i2c_packet import I2C_Packet

    failures = []
    exp_hi = (settings.exposure_rows >> 8) & 0xFF
    exp_lo = settings.exposure_rows & 0xFF
    for cam_id in range(8):
        if not (camera_mask & (1 << cam_id)):
            continue
        regs = (
            (0x3501, exp_hi),                   # coarse exposure MSB (rows)
            (0x3502, exp_lo),                   # coarse exposure LSB
            (0x3508, settings.gains[cam_id]),   # real gain = code/16, MSB
            (0x3509, 0x00),                     # real gain LSB (fractional)
        )
        try:
            # Mux reads clear the selection, so always re-switch per camera.
            sensor.switch_camera(cam_id)
            for reg, value in regs:
                ok = sensor.camera_i2c_write(
                    I2C_Packet(
                        device_address=0x36, register_address=reg, data=value
                    )
                )
                if ok is False:
                    failures.append(f"camera {cam_id + 1} reg 0x{reg:04X}")
        except Exception as e:
            failures.append(f"camera {cam_id + 1}: {e}")
    return failures
