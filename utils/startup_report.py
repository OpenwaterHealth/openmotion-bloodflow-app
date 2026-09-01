"""Startup diagnostics for the app log (issue #527).

Logged once per launch so support can reconstruct what build, mode, and
configuration a log came from without asking the operator:

* build variant (Clinical/Research) and install mode (portable/installed/dev)
* an inventory line per deployed config/data file — present/valid + SHA-256
* the merged app config, with every overridden key's source marked

Must be called AFTER the log-file handler is attached: everything logged
during ``_load_app_config`` (which runs first, because the log file's
location depends on the config) reaches only the console.

Validity here means "the real loader would accept it": files are decoded
as strict UTF-8 before parsing, exactly like ``config_store`` and the
SDK's ``omotion.laser`` do, so a BOM'd file (the PowerShell
``Set-Content -Encoding utf8`` trap) reports INVALID here just as it
silently falls back to defaults there.
"""
from pathlib import Path
import hashlib
import json
import os
import sys

from utils import app_paths
from utils.resource_path import resource_path

# Provenance markers for the merged-config dump.
MARK_SHIPPED = "[shipped]"    # set by the bundled config/app_config.json
MARK_LOCAL = "[local]"        # set by the writable app_config.local.json
MARK_ENV = "[env]"            # dev env override (OPENMOTION_CLINICAL/PORTABLE)


def inventory_files() -> list:
    """(name, path) for every deployed config/data file worth fingerprinting.

    app_config.json + tec_params.json ship with the app; the laser/FPGA
    register files are bundled inside the SDK package (``omotion/data``).
    """
    from omotion import laser as sdk_laser

    sdk_data = getattr(sdk_laser, "_DATA_DIR", None)
    if sdk_data is None:  # private layout changed — derive from the module
        sdk_data = Path(sdk_laser.__file__).resolve().parent / "data"
    sdk_data = Path(sdk_data)
    return [
        ("app_config.json", resource_path("config", "app_config.json")),
        ("tec_params.json", resource_path("config", "tec_params.json")),
        ("laser_params.json", sdk_data / "laser_params.json"),
        ("laser_params_fault.json", sdk_data / "laser_params_fault.json"),
        ("fpga_model.json", sdk_data / "fpga_model.json"),
    ]


def inspect_file(path) -> dict:
    """Presence/validity/checksum for one JSON config file.

    Returns {present, size, sha256, valid, error}; never raises.
    """
    info = {"present": False, "size": None, "sha256": None,
            "valid": False, "error": None}
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        info["error"] = "missing"
        return info
    except OSError as e:
        info["error"] = str(e)
        return info
    info["present"] = True
    info["size"] = len(data)
    info["sha256"] = hashlib.sha256(data).hexdigest()
    try:
        # Strict UTF-8, like the real loaders (open(encoding="utf-8")) —
        # a BOM must fail here because it fails there.
        json.loads(data.decode("utf-8"))
        info["valid"] = True
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        info["error"] = f"invalid JSON: {e}"
    return info


def describe_file(name: str, path) -> str:
    """One log line: name, status, size, sha256, path."""
    info = inspect_file(path)
    if not info["present"]:
        return f"  {name:<24} MISSING  ({path}: {info['error']})"
    status = "OK     " if info["valid"] else "INVALID"
    line = (f"  {name:<24} {status}  {info['size']:>6} B  "
            f"sha256={info['sha256']}  ({path})")
    if not info["valid"]:
        line += f"  [{info['error']}]"
    return line


def config_provenance(merged: dict, baseline: dict, defaults: dict) -> dict:
    """Map each merged key to its source marker ("" = code default).

    baseline = defaults + shipped app_config.json; merged = baseline +
    local overrides. The two build-time keys may instead have been forced
    by their dev env vars (main mutates baseline AND merged, so they can
    only be recognized by the environment itself).
    """
    env_keys = set()
    if "OPENMOTION_CLINICAL" in os.environ:
        env_keys.add("clinicalMode")
    if os.environ.get("OPENMOTION_PORTABLE") == "1":
        env_keys.add("portableMode")

    marks = {}
    for key, value in merged.items():
        if key in env_keys:
            marks[key] = MARK_ENV
        elif value != baseline.get(key):
            marks[key] = MARK_LOCAL
        elif baseline.get(key) != defaults.get(key):
            marks[key] = MARK_SHIPPED
        else:
            marks[key] = ""
    return marks


def _fmt_value(value) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return repr(value)


def merged_config_block(merged: dict, baseline: dict, defaults: dict) -> str:
    """The merged config as one aligned multi-line block, markers applied."""
    marks = config_provenance(merged, baseline, defaults)
    width = max((len(k) for k in merged), default=0)
    lines = []
    for key in sorted(merged):
        mark = marks.get(key, "")
        lines.append(
            f"  {key:<{width}} = {_fmt_value(merged[key])}"
            + (f"  {mark}" if mark else "")
        )
    return "\n".join(lines)


def log_startup_report(log, merged: dict, baseline: dict, defaults: dict) -> None:
    """Emit the whole startup report. Never raises — logging must not
    take down the launch."""
    try:
        clinical = bool(merged.get("clinicalMode", False))
        portable = bool(merged.get("portableMode", False))
        frozen = bool(getattr(sys, "frozen", False))
        if not frozen:
            mode = "dev (running from source)"
        elif sys.platform == "darwin":
            mode = "installed (macOS Application Support; portableMode ignored)"
        elif portable:
            mode = "portable (writable state next to exe)"
        else:
            mode = "installed (writable state under %PROGRAMDATA%)"
        log.info("Build variant:  %s", "Clinical" if clinical else "Research")
        log.info("Install mode:   %s (portableMode=%s, frozen=%s)",
                 mode, portable, frozen)

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

        overrides_path = app_paths.local_config_path(portable)
        log.info("Config overrides file: %s (%s)", overrides_path,
                 "present" if overrides_path.exists() else "absent")

        log.info("Config/data file inventory:")
        for name, path in inventory_files():
            log.info("%s", describe_file(name, path))

        log.info(
            "Merged app config (%s=app_config.json, %s=app_config.local.json, "
            "%s=env override):\n%s",
            MARK_SHIPPED, MARK_LOCAL, MARK_ENV,
            merged_config_block(merged, baseline, defaults),
        )
    except Exception:
        log.warning("Startup report failed", exc_info=True)
