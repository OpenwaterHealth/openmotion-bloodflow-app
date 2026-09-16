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
old writable ``app_config.local.json``. The legacy overrides file, if one
is found from a pre-#546 install, is imported once (preference/state keys
only) and deleted.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from config import app_config as compiled

logger = logging.getLogger("openmotion.bloodflow-app.config")

_INT_KEYS = (
    "leftMask", "rightMask", "clinicalModeLeftMask", "clinicalModeRightMask",
)

# Legacy pre-#546 overrides file name; only ever read by the one-time import.
LEGACY_OVERRIDES_NAME = "app_config.local.json"


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


def legacy_overrides_path(root: Path) -> Path:
    return Path(root) / LEGACY_OVERRIDES_NAME


def import_legacy_overrides(cfg: dict, path: Path) -> Optional[dict]:
    """One-time import of a pre-#546 ``app_config.local.json``.

    Applies the file's PREFERENCE / STATE keys to ``cfg`` in place and
    returns them (possibly an empty dict for a readable file that held
    nothing worth keeping). Returns None when there is no file, or when
    the file cannot be parsed — that one is left alone and reported.
    Everything else in the file is dropped, engineering mode included.

    The file is NOT deleted here: the caller removes it with
    ``remove_legacy_overrides`` once the imported values are durable in the
    settings table, so a launch whose store is unavailable leaves the file
    for the next launch instead of losing the operator's preferences.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        logger.warning("Legacy overrides %s unreadable (%s); left in place", path, e)
        return None
    imported = {}
    for key, value in raw.items():
        if key in cfg and is_persisted(key):
            cfg[key] = value
            imported[key] = value
    _coerce_ints(cfg)
    logger.warning(
        "Legacy %s: importing %d preference(s) %s; dropping %s",
        path, len(imported), sorted(imported), sorted(set(raw) - set(imported)),
    )
    return imported


def remove_legacy_overrides(path: Path) -> bool:
    """Delete the legacy overrides file after its values are persisted."""
    path = Path(path)
    try:
        os.remove(path)
        logger.warning("Deleted legacy %s; it is no longer read", path)
        return True
    except OSError as e:
        logger.warning("Could not delete legacy %s (%s); it is no longer read", path, e)
        return False


def refused_keys(keys: Iterable[str]) -> list:
    """The CONSTANT keys among ``keys`` (a runtime write to them is refused)."""
    return [k for k in keys if tier_of(k) == compiled.CONSTANT]
