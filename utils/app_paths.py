"""Resolve writable, user-data locations outside the (read-only) install dir.

When the app is installed to Program Files, its bundled files are read-only.
Runtime-writable state (config overrides, logs, scan data) lives under
%PROGRAMDATA%\\Openwater\\ instead — or next to the exe when portableMode is
set (see writable_root). In a dev (non-frozen) run, everything stays under
the cwd so local development is unchanged.

Override the root with the OPENWATER_DATA_ROOT env var (used by tests and as a
power-user escape hatch).

Two fixed children live under the writable root: LOGS_DIRNAME (this run's log
file) and DATA_DIRNAME (scans.db, scan CSVs, calibrations,
debug-bundles, downloaded updates).
"""
from pathlib import Path
import os
import sys

_APP_DIRNAME = "Openwater"

LOGS_DIRNAME = "logs"
DATA_DIRNAME = "data"


def writable_root(portable: bool = False) -> Path:
    """Return the writable data root, creating it if necessary.

    ``portable`` mirrors the shipped ``portableMode`` config flag: when set,
    a frozen build keeps everything next to the exe (the old un-installed
    behavior) instead of scattering it to %PROGRAMDATA%. An explicit
    OPENWATER_DATA_ROOT override is used as-is, no writability check. The
    other branches fall back to ~/Documents/Open-Motion if the resolved
    root isn't writable (e.g. cwd is "/" on a macOS Finder launch).
    """
    env = os.environ.get("OPENWATER_DATA_ROOT")
    if env:
        root = Path(env)
        root.mkdir(parents=True, exist_ok=True)
        return root

    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            # macOS has no %PROGRAMDATA% (the literal r"C:\ProgramData" default
            # got taken at face value, creating a directory actually *named*
            # that), and the portable layout can't apply either: writing inside
            # Open-Motion.app invalidates its code signature. Both variants use
            # the standard per-user data location.
            root = Path.home() / "Library" / "Application Support" / _APP_DIRNAME
        elif portable:
            root = Path(sys.executable).resolve().parent
        else:
            base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
            root = Path(base) / _APP_DIRNAME
    else:
        root = Path.cwd()

    # A read-only parent (Finder launches the app with cwd="/") makes mkdir
    # itself raise, before the os.access check below could ever redirect us.
    try:
        root.mkdir(parents=True, exist_ok=True)
        writable = os.access(root, os.W_OK)
    except OSError:
        writable = False

    if not writable:
        root = Path.home() / "Documents" / "Open-Motion"
        root.mkdir(parents=True, exist_ok=True)
    return root


def local_config_path(portable: bool = False) -> Path:
    """Path to the writable config-overrides file."""
    return writable_root(portable) / "app_config.local.json"
