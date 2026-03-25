# utils/resource_path.py
from pathlib import Path
import sys
import os


def app_base_dir() -> Path:
    """
    Returns a directory that contains bundled resources:
    - Dev: project root (folder with this file / your entry point)
    - One-Folder: dist/<AppName> (next to exe)
    - One-File: sys._MEIPASS (PyInstaller temp extract dir)
    """
    if getattr(sys, "frozen", False):
        # PyInstaller
        # one-file: _MEIPASS points to the extracted temp dir (often where _internal lives)
        # one-folder: _MEIPASS may not exist; resources are next to the exe
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        # running from source
        base = (
            Path(__file__).resolve().parent.parent
        )  # adjust if you prefer a different root
    return base


def resource_path(*relative_parts: str) -> Path:
    """
    Build a resource path with fallbacks:
    - ENV override: OPENWATER_CONFIG_DIR (only when asking for 'config/...')
    - next to exe (one-folder)
    - inside _MEIPASS (one-file)
    - inside _internal (newer PyInstaller layouts)
    """
    # If asking for config/* and env override is present, honor it
    parts = Path(*relative_parts)
    if len(parts.parts) >= 1 and parts.parts[0] == "config":
        env_dir = os.environ.get("OPENWATER_CONFIG_DIR")
        if env_dir:
            p = Path(env_dir).joinpath(*parts.parts[1:])
            if p.exists():
                return p

    base = app_base_dir()

    # 1) Try directly under base (dev & one-folder)
    p = base.joinpath(*relative_parts)
    if p.exists():
        return p

    # 2) Try under _internal (PyInstaller may place datas there)
    p2 = base.joinpath("_internal", *relative_parts)
    if p2.exists():
        return p2

    # 3) As a last resort, return the direct under-base path (even if missing)
    return p
