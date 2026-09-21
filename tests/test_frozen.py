"""utils.frozen: one seam for "built executable?" and "which exe?" (#548).

PyInstaller sets ``sys.frozen`` / ``sys._MEIPASS``; Nuitka sets neither and
in onefile mode points ``sys.executable`` at the extracted interpreter. The
app's dev-flag drop, installed-vs-portable check, data root and updater
relaunch all go through this module so both bundlers answer the same way.
"""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils import frozen

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_source_run_is_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(frozen, "_compiled", lambda: None)
    assert frozen.is_frozen() is False
    assert frozen.bundler() == "source"
    assert frozen.executable_path() == Path(sys.executable)
    assert frozen.bundle_dir() == REPO_ROOT


def test_pyinstaller_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(frozen, "_compiled", lambda: None)
    assert frozen.is_frozen() is True
    assert frozen.bundler() == "pyinstaller"
    assert frozen.executable_path() == Path(sys.executable)
    assert frozen.bundle_dir() == tmp_path


def test_nuitka_onefile_shape(monkeypatch, tmp_path):
    """sys.frozen absent, sys.executable is the extracted python.exe, the
    launched exe is __compiled__.original_argv0."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    outer = tmp_path / "Program Files" / "Open-Motion" / "Open-Motion.exe"
    marker = SimpleNamespace(original_argv0=str(outer), containing_dir=str(outer.parent),
                             standalone=True, onefile=True)
    monkeypatch.setattr(frozen, "_compiled", lambda: marker)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "onefile_1_2_x" / "python.exe"))
    assert frozen.is_frozen() is True
    assert frozen.bundler() == "nuitka"
    assert frozen.executable_path() == outer
    # No _MEIPASS: resources are laid out relative to the module tree.
    assert frozen.bundle_dir() == REPO_ROOT


def test_nothing_in_app_code_reads_sys_frozen_directly():
    """Every frozen check goes through utils.frozen so a bundler change is a
    one-file change; ``sys.executable`` likewise, since Nuitka's is wrong."""
    offenders = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(("tests/", "dist/", "build/", "docs/", "scripts/", "sandbox/")):
            continue
        if rel in ("utils/frozen.py", "rthook_libusb_paths.py", "rthook_libusb_macos.py"):
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r'getattr\(sys,\s*"frozen"|sys\.frozen\b|sys\._MEIPASS|sys\.executable\b', src):
            offenders.append(rel)
    assert offenders == [], offenders


# ── vendored libusb registration (shared by the PyInstaller hook and main.py) ──

def test_libusb_candidate_dirs_cover_both_layouts(tmp_path):
    from utils import libusb_paths
    dirs = libusb_paths.candidate_dirs(tmp_path)
    flat = [d for d in dirs if "_internal" not in d]
    nested = [d for d in dirs if "_internal" in d]
    assert flat and nested
    assert any(d.endswith(str(Path("_vendor", "libusb", "windows", "x64"))) for d in flat)
    assert any(d.endswith(str(Path("omotion", "_vendor", "libusb", "windows", "x64"))) for d in nested)


def test_register_vendored_libusb_registers_only_existing_dirs(tmp_path, monkeypatch):
    from utils import libusb_paths
    if sys.platform != "win32":
        pytest.skip("DLL search path is Windows-only")
    (tmp_path / "_vendor" / "libusb" / "windows" / "x64").mkdir(parents=True)
    seen = []
    monkeypatch.setattr(libusb_paths.os, "add_dll_directory", lambda p: seen.append(p))
    registered = libusb_paths.register_vendored_libusb(tmp_path)
    assert registered == seen
    assert len(registered) == 1 and registered[0].endswith("x64")


def test_main_registers_libusb_dirs_before_importing_the_sdk():
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert src.index("register_vendored_libusb(bundle_dir())") < src.index("from omotion import")
    assert src.index("register_vendored_libusb(bundle_dir())") < src.index("from PyQt6")

