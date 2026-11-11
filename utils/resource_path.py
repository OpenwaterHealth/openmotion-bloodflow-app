# utils/resource_path.py
from pathlib import Path

from utils.frozen import bundle_dir


def resource_path(*relative_parts: str) -> Path:
    """
    Build a resource path with fallbacks:
    - directly under the bundle root (dev, Nuitka, PyInstaller onefile)
    - inside _internal (PyInstaller one-folder layouts)

    Resolution never consults the process environment (the old
    OPENWATER_CONFIG_DIR override is gone), so a bundled resource is found
    in exactly the same place on every machine. Tests that need to redirect
    a resource monkeypatch this function at its import site instead.
    """
    base = bundle_dir()
    direct = base.joinpath(*relative_parts)
    if not direct.exists():
        internal = base.joinpath("_internal", *relative_parts)
        if internal.exists():
            return internal
    return direct  # also when missing everywhere, so the error names this path
