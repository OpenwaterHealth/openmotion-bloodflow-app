import os
import sys

import pytest

from utils import app_paths


@pytest.mark.unit
def test_writable_root_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "ow"))
    root = app_paths.writable_root()
    assert root == tmp_path / "ow"
    assert root.is_dir()  # created on access


@pytest.mark.unit
def test_local_config_path_under_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "ow"))
    expected = tmp_path / "ow" / "app_config.local.json"
    assert app_paths.local_config_path() == expected


@pytest.mark.unit
def test_dev_root_is_cwd_when_not_frozen(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    # not frozen in test → root is cwd, behavior unchanged for local dev
    assert app_paths.writable_root() == tmp_path


@pytest.mark.unit
def test_frozen_non_portable_uses_program_data(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")  # %PROGRAMDATA% is Windows-only
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))

    root = app_paths.writable_root(portable=False)

    assert root == tmp_path / "Openwater"
    assert root.is_dir()


@pytest.mark.unit
def test_frozen_portable_uses_exe_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")  # portable zips are Windows-only
    exe_dir = tmp_path / "install_dir"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "executable", str(exe_dir / "Open-Motion.exe"), raising=False)

    root = app_paths.writable_root(portable=True)

    assert root == exe_dir


def _fake_home(monkeypatch, home):
    """Point Path.home() at a temp dir, portably.

    Setting $HOME is not enough: on Windows Path.home() resolves via
    %USERPROFILE%, so a HOME-only stub leaks these darwin-simulating tests out
    into the developer's real home directory (and then fails there). Patching
    the method itself is what actually holds on every platform the suite is
    collected on.
    """
    monkeypatch.setattr(app_paths.Path, "home", classmethod(lambda cls: home))


@pytest.mark.unit
def test_frozen_macos_uses_application_support(tmp_path, monkeypatch):
    """A frozen macOS build has no %PROGRAMDATA%; it must land in the standard
    per-user data location, not a directory named after a Windows path."""
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    _fake_home(monkeypatch, tmp_path)

    root = app_paths.writable_root(portable=False)

    assert root == tmp_path / "Library" / "Application Support" / "Openwater"
    assert root.is_dir()


@pytest.mark.unit
def test_frozen_macos_never_yields_a_windows_path(tmp_path, monkeypatch):
    """Regression: PROGRAMDATA is unset off Windows, and the r'C:\\ProgramData'
    default was taken literally — creating a directory actually named
    'C:\\ProgramData' relative to the cwd (inside the .app bundle, or a hard
    failure when Finder launches with cwd='/').

    Asserted as "no ProgramData component" rather than "no 'C:' substring",
    because tmp_path is itself a C:\\... path when the suite runs on Windows.
    """
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    _fake_home(monkeypatch, tmp_path)

    for portable in (True, False):
        root = app_paths.writable_root(portable=portable)
        assert "ProgramData" not in str(root)
        assert root == tmp_path / "Library" / "Application Support" / "Openwater"


@pytest.mark.unit
def test_frozen_macos_portable_stays_outside_the_app_bundle(tmp_path, monkeypatch):
    """Writing inside Open-Motion.app invalidates its code signature, so the
    portable layout cannot apply on macOS."""
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    _fake_home(monkeypatch, tmp_path)
    bundle = tmp_path / "Open-Motion.app" / "Contents" / "MacOS"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(bundle / "Open-Motion"), raising=False)

    root = app_paths.writable_root(portable=True)

    assert ".app" not in str(root)


@pytest.mark.unit
def test_falls_back_to_documents_when_root_mkdir_denied(tmp_path, monkeypatch):
    """The unwritable-root fallback must survive mkdir itself being refused —
    on a read-only parent (Finder launch, cwd='/') mkdir raises before the
    os.access check is ever reached."""
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    _fake_home(monkeypatch, tmp_path)
    denied = tmp_path / "readonly_cwd"
    denied.mkdir()
    monkeypatch.chdir(denied)

    real_mkdir = app_paths.Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        if self == denied:
            raise PermissionError(13, "Read-only file system")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(app_paths.Path, "mkdir", fake_mkdir)

    root = app_paths.writable_root()

    assert root == tmp_path / "Documents" / "Open-Motion"
    assert root.is_dir()


@pytest.mark.unit
def test_falls_back_to_documents_when_root_unwritable(monkeypatch):
    """When the resolved root isn't writable (e.g. cwd is "/" on a macOS
    Finder launch) writable_root falls back to ~/Documents/Open-Motion."""
    monkeypatch.delenv("OPENWATER_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(os, "access", lambda path, mode: False)

    result = app_paths.writable_root()

    expected = os.path.expanduser(os.path.join("~", "Documents", "Open-Motion"))
    assert str(result) == expected
    assert "OpenWater" not in str(result)  # casing must be "Openwater"
