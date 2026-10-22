"""Laser optimizer mode (#746, epic #745): the app-side helpers.

The SDK's ``LaserOptimizerWorkflow`` (openmotion-sdk#335) does the work: it
probes the TA pulse width and the camera gains until the near cameras read
the target dark-corrected mean (100 DN), runs the scan request the connector
built as its final scan, then restores the stock settings. What lives here
is the pure app-side part the connector and its tests share: who may start
it, how long the final scan runs, and how probes and outcomes read in the UI
and the scan notes. No Qt, no hardware.

Engineering experiments only (horse-hoof blood flow and the like): the run
widens the laser-safety FPGAs' limits while it is active, so it is never
offered in a clinical build.
"""

from __future__ import annotations

import importlib.util
import math
from typing import Any, Mapping, Optional

DURATION_DEFAULT_SEC = 10
# Settings → Engineering dropdown. Any whole second in [1, MAX] validates.
DURATION_CHOICES_SEC = (5, 10, 15, 20, 30, 60, 120, 300)
DURATION_MAX_SEC = 3600


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


def scan_duration_sec(app_config: Mapping[str, Any]) -> int:
    """The final scan's length from ``laserOptimizerScanDurationSec``; the
    default for a missing or malformed value."""
    value = app_config.get("laserOptimizerScanDurationSec")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DURATION_DEFAULT_SEC
    if not math.isfinite(value) or value != int(value):
        return DURATION_DEFAULT_SEC
    if not 1 <= int(value) <= DURATION_MAX_SEC:
        return DURATION_DEFAULT_SEC
    return int(value)


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
