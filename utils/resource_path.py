# utils/resource_path.py
from pathlib import Path

from utils.frozen import bundle_dir


def app_base_dir() -> Path:
    """
    Returns a directory that contains bundled resources:
    - Dev: project root (the parent of utils/)
    - PyInstaller: the extraction dir it records on sys (one-folder: _internal)
    - Nuitka (#548): the directory the modules were unpacked into, which is
      the parent of utils/ exactly as in a source tree (no _MEIPASS exists)
    All three cases are answered by utils.frozen.bundle_dir().
    """
    return bundle_dir()


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
    base = app_base_dir()

    # 1) Try directly under base
    p = base.joinpath(*relative_parts)
    if p.exists():
        return p

    # 2) Try under _internal (PyInstaller one-folder places datas there)
    p2 = base.joinpath("_internal", *relative_parts)
    if p2.exists():
        return p2

    # 3) As a last resort, return the direct under-base path (even if missing)
    return p
