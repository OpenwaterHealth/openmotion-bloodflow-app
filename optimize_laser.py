"""Optimize Laser (docs/auto_exposure/HANDOFF.md): the app-side helpers.

The SDK's ``OptimizeLaserWorkflow`` does the work: it steps the camera gains,
the laser pulse width and the drive current until every camera's
dark-subtracted mean sits on the target, and leaves the device at those
settings for the scans that follow. What lives here is the pure app-side part
the connector and its tests share: who may start a run, the Settings form and
its validation, and how iterations, results and the scan notes read. No Qt,
no hardware.

Research builds with engineering mode unlocked only. While its settings are
active the run keeps the laser-safety FPGAs' pulse-width and rate limits
widened, so clinical builds leave this module out (openwater.spec,
scripts/build_nuitka.ps1) and the connector never imports it there. Console
firmware with the 1.8.2-rc.3 safety envelope is refused by the SDK before the
first write.
"""

from __future__ import annotations

import datetime
import functools
import itertools
import math
import os
from typing import Any, Callable, Mapping, Optional

from omotion.OptimizeLaserWorkflow import OptimizeLaserWorkflow, OptimizeResult
from omotion.exposure.actuator import LaserCameraActuator
from omotion.exposure.controller import CameraStatus, OptimizeStatus
from omotion.exposure.settings import (
    Bounds,
    GainMode,
    GainPriority,
    HardwareLimits,
    Knob,
    Method,
    OptimizeLaserSettings,
    StartPoint,
)

# The one PREFERENCE key: the Settings form's values that differ from the
# SDK defaults ({} = every default), so a default the SDK moves later is not
# pinned by an old save.
SETTINGS_KEY = "optimizeLaserSettings"

SIDES = ("left", "right")
LIMITS = HardwareLimits()

# Dropdown entries: (stored value, label).
METHODS = (("fit", "Fit (option 1)"), ("basic", "Basic (option 2)"))
START_POINTS = (("max", "Highest output"), ("current", "Current settings"))
GAIN_MODES = (("individual", "Individually"),
              ("row", "By row (1 & 8, 2 & 7, 3 & 6, 4 & 5)"))
GAIN_PRIORITIES = (("near", "Near (cameras 1 & 8)"),
                   ("far", "Far (cameras 4 & 5)"),
                   ("none", "None (all cameras)"))
_KNOB_LABELS = {"gain": "gain", "pulse_width": "pulse width", "current": "drive current"}
ORDERS = tuple(
    (",".join(p), ", ".join(f"{i + 1}. {_KNOB_LABELS[k]}" for i, k in enumerate(p)))
    for p in itertools.permutations(("gain", "pulse_width", "current")))

STATUS_TEXT = {
    OptimizeStatus.ON_TARGET: "Every camera is on target.",
    OptimizeStatus.PARTIAL: "The priority cameras are on target; others are out of range.",
    OptimizeStatus.TOO_DIM_AT_MAX: "Still too dim at the highest settings allowed.",
    OptimizeStatus.TOO_BRIGHT_AT_MIN: "Still too bright at the lowest settings allowed.",
    OptimizeStatus.SPREAD_TOO_WIDE: "The priority cameras need opposite changes.",
    OptimizeStatus.NOT_CONVERGED: "Did not converge.",
}
_CAMERA_STATUS_TEXT = {
    CameraStatus.TOO_DIM: "too dim at maximum gain",
    CameraStatus.TOO_BRIGHT: "too bright at minimum gain",
    CameraStatus.ADJUSTING: "out of range",
    CameraStatus.NO_DATA: "no usable frames",
}


def camera_label(side: int, position: int) -> str:
    """``(0, 0)`` -> ``"L1"``."""
    return f"{SIDES[side][0].upper()}{position + 1}"


def emitted_pulse_width_us(pulse_width_us: float) -> float:
    """What the TA driver emits for a commanded width (driver_control.v cuts
    the drive ~55 ticks early)."""
    return pulse_width_us - LIMITS.pulse_truncation_us


# ---- who may run it -------------------------------------------------------

def active_problem(app_config: Mapping[str, Any], *, clinical_build: bool = False) -> Optional[str]:
    """Why optimized settings may not be used right now, or None. A run that
    is already active is dropped (defaults restored) when this turns up."""
    if clinical_build or app_config.get("clinicalMode") is True:
        return "Optimize Laser is not available in clinical builds."
    if app_config.get("engineeringMode") is not True:
        return "Optimize Laser needs engineering mode."
    if app_config.get("forceLaserFail") is True:
        # Its writes would neutralize the staged fault (the sdk#252 class).
        return "Turn off Force laser fail first."
    return None


def refusal(app_config: Mapping[str, Any], *, clinical_build: bool = False) -> Optional[str]:
    """Why a run may not start right now, or None."""
    problem = active_problem(app_config, clinical_build=clinical_build)
    if problem:
        return problem
    if (app_config.get("altCameraSettingsEnabled") is True
            or app_config.get("altLaserPulseWidthEnabled") is True):
        return ("Turn off the alternative camera and laser settings first "
                "(Settings, Engineering): they write the same registers.")
    return None


# ---- the Settings form ----------------------------------------------------

def _value(v):
    return getattr(v, "value", v)


def to_form(settings: OptimizeLaserSettings) -> dict:
    """The Settings form's values (plain JSON types) for ``settings``."""
    return {
        "method": _value(settings.method),
        "targetMean": settings.target_mean,
        "passingRange": settings.passing_range,
        "startPoint": _value(settings.start_point),
        "order": ",".join(_value(k) for k in settings.order),
        "gainBounds": [[b.low, b.high] for b in settings.gain_bounds],
        "pulseWidthBounds": [settings.pulse_width_bounds.low, settings.pulse_width_bounds.high],
        "currentBounds": [settings.current_bounds.low, settings.current_bounds.high],
        "gainMode": _value(settings.gain_mode),
        "gainPriority": _value(settings.gain_priority),
        "framesPerIteration": settings.frames_per_iteration,
        "inRangePoints": settings.in_range_points,
    }


DEFAULT_FORM = to_form(OptimizeLaserSettings())

_LABELS = {
    "method": "method", "targetMean": "target mean", "passingRange": "passing range",
    "startPoint": "starting point", "order": "optimization order",
    "gainBounds": "gain bounds", "pulseWidthBounds": "pulse width bounds",
    "currentBounds": "drive current bounds", "gainMode": "gain adjustment",
    "gainPriority": "gain priority", "framesPerIteration": "frames per iteration",
    "inRangePoints": "in-range points to exit",
}


def _number(label, v, errors):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        errors.append(f"{label} must be a number, got {v!r}")
        return None
    return float(v)


def _whole(label, v, errors):
    n = _number(label, v, errors)
    if n is None:
        return None
    if n != int(n):
        errors.append(f"{label} must be a whole number, got {v!r}")
        return None
    return int(n)


def _bounds(label, v, errors):
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        errors.append(f"{label} must be a lower and an upper bound, got {v!r}")
        return None
    low, high = _number(f"{label} lower bound", v[0], errors), _number(f"{label} upper bound", v[1], errors)
    return None if low is None or high is None else Bounds(low, high)


def _choice(label, v, enum, errors):
    try:
        return enum(v)
    except ValueError:
        errors.append(f"{label} {v!r} is not one of {[m.value for m in enum]}")
        return None


def from_form(form: Mapping[str, Any]) -> tuple[Optional[OptimizeLaserSettings], list[str]]:
    """Parse and validate Settings-form values (missing keys take the SDK
    default). Returns the settings and no errors, or None and every error.
    Nothing is coerced into range: a bad value is an error, never a clamp."""
    if not isinstance(form, Mapping):
        return None, [f"Optimize Laser settings must be a mapping, got {type(form).__name__}"]
    errors: list[str] = []
    unknown = sorted(set(form) - set(DEFAULT_FORM))
    if unknown:
        errors.append(f"unknown Optimize Laser settings: {unknown}")
    f = {**DEFAULT_FORM, **{k: v for k, v in form.items() if k in DEFAULT_FORM}}

    gain_bounds = f["gainBounds"]
    if not isinstance(gain_bounds, (list, tuple)) or len(gain_bounds) != 4:
        errors.append(f"gain bounds need 4 rows, got {gain_bounds!r}")
        rows = None
    else:
        rows = [_bounds(f"row {i + 1} gain", b, errors) for i, b in enumerate(gain_bounds)]
    order = f["order"]
    knobs = (tuple(_choice("optimization order", k.strip(), Knob, errors) for k in order.split(","))
             if isinstance(order, str) else None)
    if knobs is None:
        errors.append(f"optimization order must be text like 'gain,pulse_width,current', got {order!r}")

    parsed = dict(
        method=_choice("method", f["method"], Method, errors),
        target_mean=_number("target mean", f["targetMean"], errors),
        passing_range=_number("passing range", f["passingRange"], errors),
        start_point=_choice("starting point", f["startPoint"], StartPoint, errors),
        order=knobs,
        gain_bounds=tuple(rows) if rows else None,
        pulse_width_bounds=_bounds("pulse width", f["pulseWidthBounds"], errors),
        current_bounds=_bounds("drive current", f["currentBounds"], errors),
        gain_mode=_choice("gain adjustment", f["gainMode"], GainMode, errors),
        gain_priority=_choice("gain priority", f["gainPriority"], GainPriority, errors),
        frames_per_iteration=_whole("frames per iteration", f["framesPerIteration"], errors),
        in_range_points=_whole("in-range points to exit", f["inRangePoints"], errors),
    )
    if errors:
        return None, errors
    settings = OptimizeLaserSettings(**parsed)
    problems = settings.errors(LIMITS)
    return (None, problems) if problems else (settings, [])


def stored_form(app_config: Mapping[str, Any]) -> dict:
    """The saved form values over the defaults (not validated)."""
    saved = app_config.get(SETTINGS_KEY)
    return {**DEFAULT_FORM, **(saved if isinstance(saved, Mapping) else {})}


def stored_settings(app_config: Mapping[str, Any]) -> tuple[Optional[OptimizeLaserSettings], list[str]]:
    """The saved settings, validated; None and the errors when they don't
    validate (a run then refuses rather than quietly using the defaults)."""
    saved = app_config.get(SETTINGS_KEY)
    if saved is not None and not isinstance(saved, Mapping):
        return None, [f"saved Optimize Laser settings are not a mapping: {saved!r}"]
    return from_form(stored_form(app_config))


def storage_diff(settings: OptimizeLaserSettings) -> dict:
    """What to save under ``SETTINGS_KEY``: the values that differ from the
    defaults."""
    return {k: v for k, v in to_form(settings).items() if v != DEFAULT_FORM[k]}


def choices() -> dict:
    """Dropdown entries for the Settings form, plus the defaults and the
    hardware limits the bounds are checked against."""
    def entries(pairs):
        return [{"value": v, "label": label} for v, label in pairs]
    return {
        "method": entries(METHODS), "startPoint": entries(START_POINTS),
        "order": entries(ORDERS), "gainMode": entries(GAIN_MODES),
        "gainPriority": entries(GAIN_PRIORITIES), "defaults": dict(DEFAULT_FORM),
        "limits": {
            "gain": [LIMITS.gain_min, LIMITS.gain_max],
            "pulseWidthUs": [LIMITS.pulse_width_min_us, LIMITS.effective_pulse_width_max_us],
            "currentMa": [LIMITS.current_min_ma, LIMITS.effective_current_max_ma],
            "exposureUs": LIMITS.exposure_us,
        },
    }


# ---- running it -----------------------------------------------------------

def make_workflow(interface, *, force_fault: Callable[[], bool]) -> OptimizeLaserWorkflow:
    """The workflow, with restores going through ``apply_laser_power`` with
    the app's Force laser fail flag, like every other laser load in the app."""
    from omotion.laser import apply_laser_power

    def restore_laser(console, lock=None):
        return apply_laser_power(console, force_fault=bool(force_fault()), lock=lock)

    return OptimizeLaserWorkflow(
        interface,
        actuator_factory=functools.partial(LaserCameraActuator, restore_laser=restore_laser))


def csv_path(data_root: str, now: datetime.datetime) -> str:
    return os.path.join(data_root, "optimize-laser", f"optimize-{now:%Y%m%d_%H%M%S}.csv")


def _num(v, digits=1):
    if v is None or not math.isfinite(v):
        return None
    return round(float(v), digits)


def _camera_rows(cameras) -> list[dict]:
    return [{
        "camera": camera_label(side, pos), "side": SIDES[side], "position": pos + 1,
        "gain": c.gain, "gainCode": c.gain_code, "mean": _num(c.mean_raw),
        "signal": _num(c.signal), "dark": _num(c.dark_dn), "sat": _num(c.sat_frac, 4),
        "onTarget": bool(c.on_target), "status": _value(c.status),
    } for (side, pos), c in sorted(cameras.items())]


def iteration_row(record) -> dict:
    """One iteration, for the progress modal."""
    s, n = record.settings, record.next_settings
    return {
        "iteration": record.iteration,
        "pulseWidthUs": _num(s.pulse_width_us),
        "emittedPulseWidthUs": _num(emitted_pulse_width_us(s.pulse_width_us)),
        "currentMa": _num(s.current_ma),
        "done": bool(record.done),
        "status": _value(record.status) if record.status else "",
        "cameras": _camera_rows(record.cameras),
        "next": {"pulseWidthUs": _num(n.pulse_width_us), "currentMa": _num(n.current_ma)},
    }


def camera_alerts(result: OptimizeResult) -> list[str]:
    """One line per kind of camera that ended off target, e.g.
    "L3, L4: too bright at minimum gain"."""
    if not result.records:
        return []
    groups: dict = {}
    for (side, pos), c in sorted(result.records[-1].cameras.items()):
        if c.on_target:
            continue
        text = _CAMERA_STATUS_TEXT.get(c.status, _value(c.status))
        groups.setdefault(text, []).append(camera_label(side, pos))
    return [f"{', '.join(cams)}: {text}" for text, cams in groups.items()]


def message(result: OptimizeResult) -> str:
    """The headline for a finished run."""
    if result.error:
        tail = " Defaults restored." if result.restored else ""
        return f"Optimize Laser stopped: {result.error}.{tail}"
    if result.canceled:
        return "Optimize Laser canceled. Defaults restored."
    text = STATUS_TEXT.get(result.status, str(_value(result.status)))
    if not result.passed:
        return f"{text} Defaults restored."
    if result.status is OptimizeStatus.ON_TARGET:
        return f"{text} Scans use these settings until you restore the defaults."
    return f"{text} Scans can go ahead with these settings."


def summary(result: OptimizeResult) -> dict:
    """A finished run, for the modal and the Settings card."""
    final = result.settings
    last = result.records[-1] if result.records else None
    shown = final or (last.settings if last else None)
    return {
        "passed": result.passed,
        "status": _value(result.status) if result.status else "",
        "message": message(result),
        "alerts": camera_alerts(result) if result.passed else [],
        "iterations": result.iterations,
        "pulseWidthUs": _num(shown.pulse_width_us) if shown else None,
        "emittedPulseWidthUs": _num(emitted_pulse_width_us(shown.pulse_width_us)) if shown else None,
        "currentMa": _num(shown.current_ma) if shown else None,
        "exposureUs": result.exposure_us,
        "gains": ({camera_label(s, p): g for (s, p), g in sorted(final.gains.items())}
                  if final else {}),
        "cameras": _camera_rows(last.cameras) if last else [],
        "csvPath": result.csv_path or "",
        "csvName": os.path.basename(result.csv_path) if result.csv_path else "",
        "error": result.error or "",
        "canceled": result.canceled,
        "restored": result.restored,
        "restoreProblems": list(result.restore_problems),
    }


def unoptimized_cameras(result: OptimizeResult, left_mask: int, right_mask: int) -> list[str]:
    """Cameras a scan uses that the run did not optimize (they run at the
    sensor firmware's settings)."""
    done = set(result.cameras)
    return [camera_label(side, pos)
            for side, mask in enumerate((left_mask, right_mask))
            for pos in range(8) if mask & (1 << pos) and (side, pos) not in done]


def notes_block(result: OptimizeResult, *, left_mask: int, right_mask: int,
                finished_at: datetime.datetime) -> str:
    """The fixed-format block every scan at optimized settings carries in
    its session notes (HANDOFF: all laser and camera settings in the notes)."""
    s, cfg = result.settings, result.config
    gains = {camera_label(side, pos): g for (side, pos), g in s.gains.items()}

    def side_line(side):
        cams = [f"{pos + 1}: {gains[camera_label(side, pos)]:g}x"
                for pos in range(8) if camera_label(side, pos) in gains]
        return f"Camera gains ({SIDES[side]}): " + ", ".join(cams) if cams else None

    lines = [
        "--- Optimize Laser settings (research, engineering mode) ---",
        f"Optimized {finished_at:%Y-%m-%d %H:%M:%S}: {_value(result.status)}, "
        f"{result.iterations} iteration(s), method {_value(cfg.method)}",
        f"Target: {cfg.target_mean:g} +/- {cfg.passing_range:g} DN dark-subtracted mean",
        f"Exposure: {result.exposure_us:g} us",
        f"Laser pulse width: {s.pulse_width_us:g} us commanded, "
        f"~{emitted_pulse_width_us(s.pulse_width_us):.0f} us emitted",
        f"Laser drive current: {s.current_ma:.0f} mA",
        *(line for line in (side_line(0), side_line(1)) if line),
        "Dark-frame skip: 2400 us; laser-safety pulse-width and rate limits widened "
        "while these settings are active",
        f"BVI I_max: {2 * cfg.target_mean:g} DN (2 x target). BFI uses the stock "
        "calibration, so it is not comparable with scans at stock settings.",
    ]
    if result.csv_path:
        lines.append(f"Optimize Laser log: {os.path.basename(result.csv_path)}")
    lines += [f"Not on target: {alert}" for alert in camera_alerts(result)]
    missing = unoptimized_cameras(result, left_mask, right_mask)
    if missing:
        lines.append(f"Not optimized (sensor firmware settings): {', '.join(missing)}")
    # No closing rule: the scan's footer opens with its own "---".
    return "\n".join(lines)


def restore_message(problems: list[str]) -> tuple[bool, str]:
    """(ok, message) for a Restore default laser settings result. A laser or
    trigger-config problem fails it (a held 2400 us skip under the restored
    RATE_LL would latch E-202 on a direct trigger start). Camera write
    failures alone still count as restored: every Start and Check
    reconfigures the cameras first."""
    laser = [p for p in problems
             if p.startswith(("laser power restore", "trigger config reset"))]
    if laser:
        return False, ("Could not restore the laser defaults: " + "; ".join(laser)
                       + ". Power-cycle the console to be sure.")
    if problems:
        return True, ("Laser defaults restored. The cameras could not be written now; "
                      "they are reconfigured at the next Start or Check.")
    return True, "Laser and camera defaults restored."
