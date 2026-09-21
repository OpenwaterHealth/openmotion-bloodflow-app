"""Startup diagnostics report (issue #527).

Logged once per launch, after the file log handler exists: build variant,
install mode, SDK and Qt identity, where preferences are persisted, and the
effective config with every key that is not at its compiled value marked.

Since #546 there is no configuration file to fingerprint: the shipped values
compile into the executable (``config.app_config``) and the only persisted
layer is the ``settings`` table in scans.db. The SDK's laser/FPGA data are
compiled into the SDK the same way (openmotion-sdk#278), so the SDK version
line is their provenance.
"""
from __future__ import annotations

import hashlib
import json
import sys

from utils.frozen import bundler, is_frozen
from pathlib import Path

from config import app_config as compiled

# Provenance markers for the merged-config dump.
MARK_SAVED = "[saved]"        # differs from compiled: saved preference (scans.db)
MARK_DEV = "[dev-flag]"       # forced by a source-run launch flag


def compiled_fingerprint() -> str:
    """sha256 over the compiled values, so support can match a log to a
    build even when the module source is not on disk (frozen build)."""
    payload = json.dumps(compiled.APP_CONFIG, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def config_provenance(
    merged: dict, baseline: dict, dev_keys=frozenset(), saved_keys=frozenset(),
) -> dict:
    """Map each merged key to its source marker ("" = compiled value)."""
    marks = {}
    for key in merged:
        if key in dev_keys:
            marks[key] = MARK_DEV
        elif key in saved_keys or merged[key] != baseline.get(key):
            marks[key] = MARK_SAVED
        else:
            marks[key] = ""
    return marks


def _fmt_value(value) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return repr(value)


def merged_config_block(
    merged: dict, baseline: dict, dev_keys=frozenset(), saved_keys=frozenset(),
) -> str:
    """The merged config as one aligned multi-line block, markers and tiers applied."""
    marks = config_provenance(merged, baseline, dev_keys, saved_keys)
    width = max((len(k) for k in merged), default=0)
    lines = []
    for key in sorted(merged):
        tier = compiled.tier_of(key) or "?"
        mark = marks.get(key, "")
        lines.append(
            f"  {key:<{width}} = {_fmt_value(merged[key])}  <{tier}>"
            + (f"  {mark}" if mark else "")
        )
    return "\n".join(lines)


def log_startup_report(
    log, merged: dict, baseline: dict, *, dev_keys=frozenset(),
    saved_keys=frozenset(), settings_path=None, settings_enabled=None,
) -> None:
    """Emit the whole startup report. Never raises — logging must not take
    down the launch.

    ``baseline`` is the compiled config; ``dev_keys`` the keys a source-run
    launch flag forced; ``saved_keys`` those loaded from the settings table.
    """
    try:
        clinical = bool(merged.get("clinicalMode", False))
        portable = bool(merged.get("portableMode", False))
        frozen = is_frozen()
        if not frozen:
            mode = "dev (running from source)"
        elif sys.platform == "darwin":
            mode = "installed (macOS Application Support; portableMode ignored)"
        elif portable:
            mode = "portable (writable state next to exe)"
        else:
            mode = "installed (writable state under %LOCALAPPDATA%, per user)"
        log.info("Build variant:  %s", "Clinical" if clinical else "Research")
        log.info("Install mode:   %s (portableMode=%s, frozen=%s, bundler=%s)",
                 mode, portable, frozen, bundler())
        log.info("Config:         compiled (config/app_config.py), "
                 "%d keys, values sha256=%s",
                 len(compiled.APP_CONFIG), compiled_fingerprint())

        # SDK identity. __version__ is an install-time metadata stamp — on an
        # editable install it goes stale the moment the checkout moves, so the
        # resolved package path is what actually says which SDK this process
        # imported (editable checkout vs bundled wheel, and which checkout).
        try:
            import omotion
            log.info("SDK:            %s (%s)",
                     getattr(omotion, "__version__", "unknown"),
                     Path(omotion.__file__).resolve().parent)
        except Exception as e:
            log.info("SDK:            unresolvable (%s)", e)

        try:
            from PyQt6.QtCore import PYQT_VERSION_STR, qVersion
            log.info("Qt runtime:     Qt %s / PyQt6 %s",
                     qVersion(), PYQT_VERSION_STR)
        except Exception as e:
            log.info("Qt runtime:     unresolvable (%s)", e)

        if settings_enabled is None:
            state = "unknown"
        elif settings_enabled:
            state = f"open, {len(saved_keys)} saved preference(s)"
        else:
            state = "UNAVAILABLE, preferences are session-only"
        log.info("Settings store: %s (%s)", settings_path or "none", state)

        log.info(
            "Effective app config (%s=saved preference, %s=dev launch flag; "
            "<tier> per key):\n%s",
            MARK_SAVED, MARK_DEV,
            merged_config_block(merged, baseline, dev_keys, saved_keys),
        )
    except Exception:
        log.warning("Startup report failed", exc_info=True)
