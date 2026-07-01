"""Resolve writable, user-data locations outside the (read-only) install dir.

When the app is installed to Program Files, its bundled files are read-only.
Runtime-writable state (config overrides, logs, scan data) lives under
%PROGRAMDATA%\\Openwater\\ instead. In a dev (non-frozen) run, everything stays
under the cwd so local development is unchanged.

Override the root with the OPENWATER_DATA_ROOT env var (used by tests and as a
power-user escape hatch).
"""
from pathlib import Path
import os
import sys

_APP_DIRNAME = "Openwater"


def writable_root(portable: bool = False) -> Path:
    """Return the writable data root, creating it if necessary.

    ``portable`` mirrors the shipped ``portableMode`` config flag: when set,
    a frozen build keeps everything next to the exe (the old un-installed
    behavior) instead of scattering it to %PROGRAMDATA%.
    """
    env = os.environ.get("OPENWATER_DATA_ROOT")
    if env:
        root = Path(env)
    elif getattr(sys, "frozen", False):
        if portable:
            root = Path(sys.executable).resolve().parent
        else:
            base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
            root = Path(base) / _APP_DIRNAME
    else:
        # dev: keep everything under the cwd, unchanged from before
        root = Path.cwd()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_config_path(portable: bool = False) -> Path:
    """Path to the writable config-overrides file."""
    return writable_root(portable) / "app_config.local.json"
