"""Laser optimizer mode (#746, epic #745): the app-side helpers.

The SDK's ``LaserOptimizerWorkflow`` (openmotion-sdk#335) does the work: it
probes the TA pulse width and the camera gains until the near cameras read
the operator's setpoint (dark-corrected mean, 100 DN by default), runs the
scan request the connector built as its final scan, then restores the stock
settings. What lives here is the pure app-side part the connector and its
tests share: who may start it, the operator settings and their validation,
and how probes and outcomes read in the UI and the scan notes. No Qt, no
hardware.

Engineering experiments only (horse-hoof blood flow and the like): the run
widens the laser-safety FPGAs' limits while it is active, so it is never
offered in a clinical build.
"""

from __future__ import annotations

import importlib.util
import math
from typing import Any, Mapping, Optional

# Operator settings (Settings → Laser Optimizer card), each a PREFERENCE key:
# config key -> (QML name, default, dropdown choices, valid range). A
# hand-set whole number inside the range is used even between dropdown
# steps; anything else falls back to the default.
SETTINGS = {
    # Dark-corrected mean (DN) the near cameras (4 and 5) should land on.
    "laserOptimizerTargetMeanDn": (
        "targetMeanDn", 100, (50, 75, 100, 125, 150, 200, 250, 300), (20, 400)),
    # Accepted band around it, +/- percent.
    "laserOptimizerTolerancePct": (
        "tolerancePct", 15, (5, 10, 15, 20, 25), (2, 50)),
    # Where every run starts: the near cameras' analog gain ...
    "laserOptimizerStartGain": (
        "startGain", 16, (1, 2, 4, 8, 16), (1, 16)),
    # ... and the TA pulse width. 2000 µs is the most the 2196 µs exposure
    # covers after the 100 µs laser delay.
    "laserOptimizerStartPulseUs": (
        "startPulseUs", 2000, (250, 500, 750, 1000, 1250, 1500, 1750, 2000), (100, 2000)),
    # Length of the scan it runs once tuned.
    "laserOptimizerScanDurationSec": (
        "scanDurationSec", 10, (5, 10, 15, 20, 30, 60, 120, 300), (1, 3600)),
}
_GAIN_STEPS = (1, 2, 4, 8, 16)

DURATION_DEFAULT_SEC = SETTINGS["laserOptimizerScanDurationSec"][1]
DURATION_CHOICES_SEC = SETTINGS["laserOptimizerScanDurationSec"][2]


def sdk_supports_laser_optimizer() -> bool:
    """True when the installed SDK ships the optimizer (sdk#335)."""
    try:
        return importlib.util.find_spec("omotion.LaserOptimizerWorkflow") is not None
    except (ImportError, ValueError):
        return False


def refusal(app_config: Mapping[str, Any], *, sdk_supported: bool) -> Optional[str]:
    """Why the optimizer may not start right now, or None."""
    if app_config.get("clinicalMode") is True:
        return "Laser optimizer mode is not available in clinical builds."
    if app_config.get("engineeringMode") is not True:
        return "Laser optimizer mode needs engineering mode."
    if not sdk_supported:
        return "The installed SDK has no laser optimizer (needs openmotion-sdk#335)."
    if app_config.get("forceLaserFail") is True:
        # The optimizer's restore reloads the stock laser parameters, which
        # would silently disarm the staged fault (the sdk#252 class).
        return "Turn off Force laser fail before running the laser optimizer."
    return None


def setting(app_config: Mapping[str, Any], key: str) -> int:
    """The validated value of one ``SETTINGS`` key: a whole number inside its
    range (a power of two for the gain), else its default."""
    _, default, _, (lo, hi) = SETTINGS[key]
    value = app_config.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value) or value != int(value) or not lo <= value <= hi:
        return default
    if key == "laserOptimizerStartGain" and int(value) not in _GAIN_STEPS:
        return default
    return int(value)


def settings(app_config: Mapping[str, Any]) -> dict:
    """Every operator setting, validated, by QML name (the Settings card and
    the progress modal read these)."""
    return {name: setting(app_config, key) for key, (name, *_) in SETTINGS.items()}


def choices() -> dict:
    """Dropdown entries by QML name, plus ``defaults`` (for the "(default)" tag)."""
    out = {name: list(steps) for name, _, steps, _ in SETTINGS.values()}
    out["defaults"] = {name: default for name, default, _, _ in SETTINGS.values()}
    return out


def scan_duration_sec(app_config: Mapping[str, Any]) -> int:
    """The final scan's length (``laserOptimizerScanDurationSec``)."""
    return setting(app_config, "laserOptimizerScanDurationSec")


def optimizer_config(app_config: Mapping[str, Any]):
    """The SDK ``OptimizerConfig`` for a run: the operator's setpoint, band
    and starting point; every other number stays the SDK's agreed default."""
    from omotion.LaserOptimizerWorkflow import OptimizerConfig
    return OptimizerConfig(
        target_mean_dn=float(setting(app_config, "laserOptimizerTargetMeanDn")),
        tolerance=setting(app_config, "laserOptimizerTolerancePct") / 100.0,
        max_gain=setting(app_config, "laserOptimizerStartGain"),
        max_pulse_width_us=setting(app_config, "laserOptimizerStartPulseUs"),
    )


def _num(v: float, nd: int = 1) -> float:
    """QML-friendly number: NaN becomes -1 (QVariantMap carries no NaN check)."""
    return round(float(v), nd) if isinstance(v, (int, float)) and math.isfinite(v) else -1.0


def probe_row(probe: Any, decision: Any) -> dict:
    """One row of the progress modal's probe table, as a plain dict."""
    control = probe.control
    nxt = decision.setting
    return {
        "probe": int(probe.index),     # 1-based ("index" is a ListModel built-in)
        "setting": probe.setting.describe(),
        "nearGain": int(probe.setting.near_gain),
        "pulseWidthUs": int(probe.setting.pulse_width_us),
        "meanDn": _num(probe.mean_dn),
        "clippedPct": _num(probe.clipped_fraction * 100.0, 2)
        if math.isfinite(probe.clipped_fraction) else -1.0,
        "clipped": bool(probe.clipped),
        "camera": (f"{control.side.capitalize()} {control.cam_id + 1}"
                   if control is not None else ""),
        "darkDn": _num(control.dark_dn) if control is not None else -1.0,
        "action": str(getattr(decision.action, "value", decision.action)),
        "next": nxt.describe() if nxt is not None else "",
        "reason": str(decision.reason),
    }


# Headline per SDK OptimizerOutcome value; the SDK message is the detail.
_HEADLINES = {
    "completed": "Scan complete",
    "scan_stopped": "Scan stopped",
    "insufficient_signal": "Not enough light",
    "too_bright": "Too bright",
    "no_convergence": "No setting found",
    "canceled": "Stopped",
    "error": "Laser optimizer failed",
}


def outcome_text(outcome: str, message: str) -> str:
    """User-facing line for an outcome, e.g. for the modal and the log."""
    head = _HEADLINES.get(outcome, outcome)
    if outcome == "insufficient_signal":
        return (f"{head}: {message}. Reposition the sensor so it sits firmly "
                "on the tissue, then press Retry.")
    return f"{head}: {message}" if message else head


def notes_block(probes: list, setting: Any, config: Any = None) -> str:
    """The scan-notes block for a final scan at ``setting``: the chosen
    settings and every probe (SDK ``summary_text``)."""
    from omotion.LaserOptimizerWorkflow import (
        LaserOptimizerResult, OptimizerConfig, OptimizerOutcome, summary_text,
    )
    result = LaserOptimizerResult(
        outcome=OptimizerOutcome.COMPLETED, message="", probes=list(probes),
        setting=setting,
    )
    return summary_text(result, config or OptimizerConfig())
