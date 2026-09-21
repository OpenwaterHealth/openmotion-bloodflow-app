import os
import sys
from pathlib import Path

import pytest

from utils import app_paths


def _override(monkeypatch, path):
    """Pin (or clear, with None) the in-process data-root override.

    The autouse ``_isolate_writable_root`` fixture already points it at a
    per-test tmp dir; these tests need to control it exactly.
    """
    monkeypatch.setattr(app_paths, "DATA_ROOT_OVERRIDE", path)


def _fake_home(monkeypatch, home):
    """Point the home-folder lookup at a temp dir.

    app_paths asks the shell / passwd database for the home folder rather
    than reading $HOME / %USERPROFILE%, so patching the helper is the only
    hook — and it holds on every platform the suite is collected on.
    """
    monkeypatch.setattr(app_paths, "_home_dir", lambda: home)


@pytest.mark.unit
def test_writable_root_honors_in_process_override(tmp_path, monkeypatch):
    _override(monkeypatch, tmp_path / "ow")
    root = app_paths.writable_root()
    assert root == tmp_path / "ow"
    assert root.is_dir()  # created on access


@pytest.mark.unit
def test_set_data_root_override_round_trips(tmp_path, monkeypatch):
    _override(monkeypatch, None)
    app_paths.set_data_root_override(str(tmp_path / "ow"))
    assert app_paths.writable_root() == tmp_path / "ow"
    app_paths.set_data_root_override(None)
    assert app_paths.DATA_ROOT_OVERRIDE is None


# --- derived portable mode (#546) ------------------------------------------
#
# The exe in the portable zip and the one inside the installer are
# byte-identical, so "installed vs portable" comes from the MSI's HKLM
# InstallDir marker, not from a config value.

@pytest.mark.unit
def test_portable_mode_override_wins(monkeypatch):
    monkeypatch.setattr(app_paths, "PORTABLE_MODE_OVERRIDE", True)
    assert app_paths.portable_mode() is True
    monkeypatch.setattr(app_paths, "PORTABLE_MODE_OVERRIDE", False)
    assert app_paths.portable_mode() is False


@pytest.mark.unit
def test_source_runs_are_never_portable(monkeypatch):
    monkeypatch.setattr(app_paths, "PORTABLE_MODE_OVERRIDE", None)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert app_paths.portable_mode() is False


@pytest.mark.unit
def test_frozen_exe_in_registered_dir_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(app_paths, "PORTABLE_MODE_OVERRIDE", None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    exe_dir = tmp_path / "Program Files" / "Openwater" / "Open-Motion"
    exe_dir.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "Open-Motion.exe"))
    monkeypatch.setattr(app_paths, "installed_dir", lambda: exe_dir)
    assert app_paths.is_installed_exe() is True
    assert app_paths.portable_mode() is False


@pytest.mark.unit
def test_frozen_exe_elsewhere_is_portable(tmp_path, monkeypatch):
    monkeypatch.setattr(app_paths, "PORTABLE_MODE_OVERRIDE", None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    zip_dir = tmp_path / "Downloads" / "Open-Motion"
    zip_dir.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(zip_dir / "Open-Motion.exe"))
    # a different install elsewhere on the same machine must not claim us
    monkeypatch.setattr(app_paths, "installed_dir", lambda: tmp_path / "Program Files" / "Open-Motion")
    assert app_paths.portable_mode() is True
    # ...and no registration at all is portable too
    monkeypatch.setattr(app_paths, "installed_dir", lambda: None)
    assert app_paths.portable_mode() is True


@pytest.mark.unit
def test_installed_dir_finds_the_marker_in_either_registry_view(monkeypatch):
    """#577: the app MSI is a 32-bit package, so its InstallDir marker lives
    under WOW6432Node. Reading only the 64-bit view made every installed build
    think it was portable and crash writing logs\\ into Program Files."""
    import types

    target = r"C:\Program Files (x86)\Openwater\Open-Motion"

    def fake_winreg(present_in):
        mod = types.SimpleNamespace(
            HKEY_LOCAL_MACHINE=1, KEY_READ=0x1, REG_SZ=1,
            KEY_WOW64_64KEY=0x100, KEY_WOW64_32KEY=0x200, opened=[],
        )

        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def open_key(_root, _sub, _reserved, access):
            view = access & ~mod.KEY_READ
            mod.opened.append(view)
            if view not in present_in:
                raise FileNotFoundError(view)
            return _Key()

        mod.OpenKey = open_key
        mod.QueryValueEx = lambda _key, _name: (target, mod.REG_SZ)
        return mod

    monkeypatch.setattr(sys, "platform", "win32")
    only_32 = fake_winreg({0x200})
    monkeypatch.setitem(sys.modules, "winreg", only_32)
    assert app_paths.installed_dir() == Path(target)
    assert only_32.opened == [0x100, 0x200]  # 64-bit view first, explicitly

    only_64 = fake_winreg({0x100})
    monkeypatch.setitem(sys.modules, "winreg", only_64)
    assert app_paths.installed_dir() == Path(target)
    assert only_64.opened == [0x100]

    monkeypatch.setitem(sys.modules, "winreg", fake_winreg(set()))
    assert app_paths.installed_dir() is None


@pytest.mark.unit
def test_unwritable_root_falls_back_even_when_os_access_says_writable(tmp_path, monkeypatch):
    """#577: on Windows os.access(W_OK) ignores ACLs and says True for
    Program Files, so the ~/Documents fallback never fired. The root is now
    probed by actually creating a file."""
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    exe_dir = tmp_path / "Program Files (x86)" / "Open-Motion"
    exe_dir.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "Open-Motion.exe"))
    home = tmp_path / "home"
    _fake_home(monkeypatch, home)
    monkeypatch.setattr(os, "access", lambda *_a, **_k: True)  # the Windows lie
    monkeypatch.setattr(app_paths, "_can_write", lambda root: root != exe_dir.resolve())
    assert app_paths.writable_root(portable=True) == home / "Documents" / "Open-Motion"


@pytest.mark.unit
def test_can_write_probes_with_a_real_file(tmp_path):
    assert app_paths._can_write(tmp_path) is True
    assert list(tmp_path.iterdir()) == []  # the probe cleans up after itself
    assert app_paths._can_write(tmp_path / "does-not-exist") is False


@pytest.mark.unit
def test_env_vars_are_ignored(tmp_path, monkeypatch):
    """The retired OPENWATER_DATA_ROOT env var and %LOCALAPPDATA% must not steer
    the root any more: a packaged build has to start identically no matter
    what the host environment carries."""
    _override(monkeypatch, None)
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "from_env"))
    monkeypatch.chdir(tmp_path)
    assert app_paths.writable_root() == tmp_path
    assert not (tmp_path / "from_env").exists()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "env_localappdata"))
    monkeypatch.setattr(
        app_paths, "_local_app_data_dir", lambda: tmp_path / "shell_localappdata"
    )
    root = app_paths.writable_root(portable=False)
    assert root == tmp_path / "shell_localappdata" / "Openwater"
    assert not (tmp_path / "env_localappdata").exists()


@pytest.mark.unit
def test_dev_root_is_cwd_when_not_frozen(tmp_path, monkeypatch):
    _override(monkeypatch, None)
    monkeypatch.chdir(tmp_path)
    # not frozen in test → root is cwd, behavior unchanged for local dev
    assert app_paths.writable_root() == tmp_path


@pytest.mark.unit
def test_frozen_non_portable_uses_local_app_data(tmp_path, monkeypatch):
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")  # LocalAppData is Windows-only
    monkeypatch.setattr(app_paths, "_local_app_data_dir", lambda: tmp_path)

    root = app_paths.writable_root(portable=False)

    assert root == tmp_path / "Openwater"
    assert root.is_dir()


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32", reason="SHGetKnownFolderPath is Windows-only")
def test_local_app_data_dir_comes_from_the_shell(monkeypatch):
    """The known-folder lookup answers a real directory (normally
    <profile>\\AppData\\Local) even when the env var points somewhere else."""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\definitely\not\here")
    got = app_paths._local_app_data_dir()
    assert got.is_dir()
    assert got.name.lower() == "local"


@pytest.mark.unit
def test_local_app_data_dir_falls_back_when_shell_lookup_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(app_paths, "_known_folder", lambda folder_id: None)
    monkeypatch.setattr(app_paths, "_home_dir", lambda: tmp_path)
    assert app_paths._local_app_data_dir() == tmp_path / "AppData" / "Local"


@pytest.mark.unit
def test_installed_root_is_per_user_not_machine_wide(tmp_path, monkeypatch):
    """#581: a Clinical scans.db is encrypted with a key held in the launching
    user's Credential Manager, so a root shared by every account on the
    machine locks out all but the first one (E-301 at every scan start). Two
    Windows users must resolve to two different roots, neither of them under
    ProgramData."""
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(app_paths, "_known_folder", lambda folder_id: None)

    roots = []
    for user in ("alice", "bob"):
        monkeypatch.setattr(app_paths, "_home_dir", lambda u=user: tmp_path / u)
        roots.append(app_paths.writable_root(portable=False))

    assert roots[0] == tmp_path / "alice" / "AppData" / "Local" / "Openwater"
    assert roots[1] == tmp_path / "bob" / "AppData" / "Local" / "Openwater"
    assert not hasattr(app_paths, "_program_data_dir")


@pytest.mark.unit
def test_home_dir_ignores_profile_env_vars(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "env_home"))
    monkeypatch.setenv("HOME", str(tmp_path / "env_home"))
    monkeypatch.setattr(
        app_paths, "_known_folder", lambda folder_id: tmp_path / "shell_home"
    )
    assert app_paths._home_dir() == tmp_path / "shell_home"


@pytest.mark.unit
def test_frozen_portable_uses_exe_folder(tmp_path, monkeypatch):
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")  # portable zips are Windows-only
    exe_dir = tmp_path / "install_dir"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "executable", str(exe_dir / "Open-Motion.exe"), raising=False)

    root = app_paths.writable_root(portable=True)

    assert root == exe_dir


@pytest.mark.unit
def test_frozen_macos_uses_application_support(tmp_path, monkeypatch):
    """A frozen macOS build has no %PROGRAMDATA%; it must land in the standard
    per-user data location, not a directory named after a Windows path."""
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    _fake_home(monkeypatch, tmp_path)

    root = app_paths.writable_root(portable=False)

    assert root == tmp_path / "Library" / "Application Support" / "Openwater"
    assert root.is_dir()


@pytest.mark.unit
def test_frozen_macos_never_yields_a_windows_path(tmp_path, monkeypatch):
    """Regression: the r'C:\\ProgramData' default was once taken literally off
    Windows — creating a directory actually named 'C:\\ProgramData' relative
    to the cwd (inside the .app bundle, or a hard failure when Finder launches
    with cwd='/').

    Asserted as "no ProgramData component" rather than "no 'C:' substring",
    because tmp_path is itself a C:\\... path when the suite runs on Windows.
    """
    _override(monkeypatch, None)
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
    _override(monkeypatch, None)
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
    _override(monkeypatch, None)
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
def test_falls_back_to_documents_when_root_unwritable(tmp_path, monkeypatch):
    """When the resolved root isn't writable (e.g. cwd is "/" on a macOS
    Finder launch) writable_root falls back to ~/Documents/Open-Motion."""
    _override(monkeypatch, None)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    _fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(app_paths, "_can_write", lambda root: False)

    result = app_paths.writable_root()

    assert result == tmp_path / "Documents" / "Open-Motion"
    assert "OpenWater" not in str(result)  # casing must be "Openwater"
