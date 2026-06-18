"""Load app config from layered sources and persist runtime overrides.

Layers (lowest to highest precedence):
  1. code defaults (passed in by the caller)
  2. shipped, read-only config/app_config.json (PyInstaller bundle)
  3. writable overrides: %PROGRAMDATA%\\OpenWater\\app_config.local.json

Runtime changes are written as a *diff* against layers 1+2 so future changes to
shipped defaults still reach keys the operator never touched.
"""
import json
import logging

from utils.resource_path import resource_path
from utils import app_paths

logger = logging.getLogger(__name__)

_INT_KEYS = ("leftMask", "rightMask", "reducedModeLeftMask", "reducedModeRightMask")


def _coerce_ints(cfg: dict) -> dict:
    for key in _INT_KEYS:
        if cfg.get(key) is not None:
            cfg[key] = int(cfg[key])
    return cfg


def shipped_baseline(defaults: dict) -> dict:
    """defaults merged with the read-only bundled app_config.json."""
    base = dict(defaults)
    path = resource_path("config", "app_config.json")
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            base.update({k: v for k, v in loaded.items() if k in defaults})
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load %s: %s; using defaults", path, e)
    return _coerce_ints(base)


def load_overrides() -> dict:
    """Read the writable overrides file (empty dict if absent/invalid)."""
    path = app_paths.local_config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load overrides %s: %s", path, e)
        return {}


def load_app_config(defaults: dict):
    """Return (baseline, merged); merged = baseline + writable overrides."""
    baseline = shipped_baseline(defaults)
    overrides = load_overrides()
    merged = {
        **baseline,
        **{k: v for k, v in overrides.items() if k in baseline},
    }
    return baseline, _coerce_ints(merged)


def save_overrides(current: dict, baseline: dict) -> None:
    """Persist only keys whose value differs from the baseline."""
    diff = _coerce_ints({k: v for k, v in current.items() if baseline.get(k) != v})
    path = app_paths.local_config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(diff, f, indent=2)
    except OSError as e:
        logger.warning("Could not write overrides %s: %s", path, e)
