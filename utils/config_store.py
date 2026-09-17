"""Resolve the effective app config from the compiled module plus saved
preferences, and persist preference changes (#546).

Layers, lowest to highest precedence:
  1. ``config.app_config.APP_CONFIG``, compiled into the executable. Values
     and tiers live there.
  2. Dev-only launch overrides (source runs only: --clinical / --research /
     --portable / --config-override). A frozen build drops them.
  3. Saved PREFERENCE / STATE keys from the ``settings`` table of scans.db
     (``utils.settings_store``). CONSTANT and SESSION keys are never read
     from storage, so nothing saved anywhere can move them.

There is no configuration file any more: neither a shipped JSON nor the
old writable ``app_config.local.json``. A leftover one from a pre-#546
install is ignored; nothing reads it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional, Tuple

from config import app_config as compiled

logger = logging.getLogger("openmotion.bloodflow-app.config")

_INT_KEYS = (
    "leftMask", "rightMask", "clinicalModeLeftMask", "clinicalModeRightMask",
)

def _coerce_ints(cfg: dict) -> dict:
    for key in _INT_KEYS:
        if cfg.get(key) is not None:
            cfg[key] = int(cfg[key])
    return cfg


def compiled_config() -> dict:
    """The compiled values, as a fresh dict the caller may mutate."""
    return _coerce_ints(compiled.compiled_config())


def tier_of(key: str) -> Optional[str]:
    return compiled.tier_of(key)


def is_persisted(key: str) -> bool:
    return key in compiled.PERSISTED_KEYS


def apply_dev_overrides(cfg: dict, overrides: Optional[dict]) -> set:
    """Apply source-run launch overrides in place; returns the keys applied.

    Unknown keys are logged and ignored (there is no whitelist to register
    them in any more: a key that is not compiled in does not exist).
    """
    applied = set()
    for key, value in (overrides or {}).items():
        if key not in cfg:
            logger.warning("--config-override: unknown config key %r ignored", key)
            continue
        cfg[key] = value
        applied.add(key)
    _coerce_ints(cfg)
    return applied


def apply_saved_preferences(cfg: dict, saved: Dict[str, Any]) -> set:
    """Overlay saved PREFERENCE / STATE values in place; returns keys used.

    Anything else in the table (a constant, a session key, a retired key)
    is ignored, so the settings table can never move a control.
    """
    used = set()
    for key, value in saved.items():
        if key in cfg and is_persisted(key):
            cfg[key] = value
            used.add(key)
        else:
            logger.info("settings table: ignoring non-preference key %r", key)
    _coerce_ints(cfg)
    return used


def persistable_diff(current: dict, baseline: dict) -> Tuple[dict, list]:
    """Split the persisted tier into (to_save, to_delete) against ``baseline``.

    A persisted key whose value differs from the compiled one is saved; one
    that is back at the compiled value is deleted so the table only ever
    holds real deviations (same diff-vs-baseline contract the overrides
    file had).
    """
    to_save: dict = {}
    to_delete: list = []
    for key in compiled.PERSISTED_KEYS:
        if key not in current:
            continue
        if current[key] != baseline.get(key):
            to_save[key] = current[key]
        else:
            to_delete.append(key)
    return _coerce_ints(to_save), to_delete


def refused_keys(keys: Iterable[str]) -> list:
    """The CONSTANT keys among ``keys`` (a runtime write to them is refused)."""
    return [k for k in keys if tier_of(k) == compiled.CONSTANT]
