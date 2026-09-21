"""Resolve writable, user-data locations outside the (read-only) install dir.

When the app is installed to Program Files, its bundled files are read-only.
Runtime-writable state (config overrides, logs, scan data) lives under
the launching user's %LOCALAPPDATA%\\Openwater\\ instead — or next to the exe
when portableMode is set (see writable_root). In a dev (non-frozen) run,
everything stays under the cwd so local development is unchanged.

The installed root is per-user on purpose (#581). A Clinical build encrypts
scans.db and the SDK keeps the key in Windows Credential Manager, which is
per Windows user. While the root was the machine-wide %PROGRAMDATA%, the
first account to launch created the database and the key, and every other
account on that machine found an encrypted database it had no key for:
settings and the audit log failed to open at boot and every scan start
raised E-301. The database and the key that protects it must share one
owner. Data an older build left under %PROGRAMDATA%\\Openwater is not
migrated.

Nothing in this module reads the process environment. The resolved root
depends only on how the build was made (frozen / platform), whether the
installer registered this exe (portable_mode(), from the HKLM InstallDir
marker installer/app.wxs writes), and the in-process DATA_ROOT_OVERRIDE,
which ``python main.py --data-root`` (source runs only) and the unit-test
fixtures set explicitly. Even the LocalAppData and home folders are asked of
the Windows shell rather than read from %LOCALAPPDATA% / %USERPROFILE%, so a
packaged artifact starts the same way whatever env vars the host machine
carries.

Since #546 ``portableMode`` is derived, not configured: the exe in the
portable zip and the exe inside the installer are byte-identical (the
signed-installer workflow repacks the QA-validated zip), so the difference
has to come from the install itself. The MSI writes
``HKLM/Software/Openwater/Open-Motion/InstallDir`` (backslashes); a frozen Windows
build whose exe lives in that directory is "installed" (writable state
under %LOCALAPPDATA%), anything else is "portable" (next to the exe). Only
an administrator can write that key.

Two fixed children live under the writable root: LOGS_DIRNAME (this run's log
file) and DATA_DIRNAME (scans.db, scan CSVs, calibrations,
debug-bundles, downloaded updates).
"""
from pathlib import Path

from utils.frozen import executable_path, is_frozen
import os
import sys

_APP_DIRNAME = "Openwater"

LOGS_DIRNAME = "logs"
DATA_DIRNAME = "data"

# Explicit in-process override of the writable root, used as-is with no
# writability check. main() sets it from the ``--data-root`` dev flag (source
# runs only); the unit-test ``_isolate_writable_root`` fixture points it at
# tmp_path. ``None`` = resolve normally.
DATA_ROOT_OVERRIDE: Path | None = None

# Windows KNOWNFOLDERIDs (shlobj_core.h). Asking the shell for these is what
# lets the app ignore %LOCALAPPDATA% / %USERPROFILE%.
_FOLDERID_LOCAL_APP_DATA = "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}"
_FOLDERID_PROFILE = "{5E6C858F-0E22-4760-9AFE-EA3317B67173}"

# Written by installer/app.wxs (AppShortcut component) as [APPFOLDER].
_INSTALL_REG_KEY = r"Software\Openwater\Open-Motion"
_INSTALL_REG_VALUE = "InstallDir"

# Test / dev hook: None = derive from the registry + exe location.
PORTABLE_MODE_OVERRIDE: bool | None = None


def set_data_root_override(path) -> None:
    """Set (or clear, with a falsy ``path``) DATA_ROOT_OVERRIDE."""
    global DATA_ROOT_OVERRIDE
    DATA_ROOT_OVERRIDE = Path(path) if path else None


def _known_folder(folder_id: str) -> Path | None:
    """SHGetKnownFolderPath(folder_id) on Windows; None if unavailable.

    The known-folder table is registry-backed, so unlike the matching env
    vars it cannot be scrubbed or redirected by whatever launched us.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
        guid = GUID()
        if ole32.CLSIDFromString(folder_id, ctypes.byref(guid)) != 0:
            return None
        out = ctypes.c_wchar_p()
        hr = shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(out)
        )
        try:
            if hr != 0 or not out.value:
                return None
            return Path(out.value)
        finally:
            ole32.CoTaskMemFree(out)
    except Exception:
        return None


def installed_dir() -> Path | None:
    """The directory the MSI installed the app to, per HKLM, or None.

    Both registry views are read, 64-bit first, each one explicitly so the
    answer never depends on the bitness of this process. The app MSI is built
    without ``-arch``, which makes it a 32-bit package: Windows Installer
    writes its HKLM values under WOW6432Node and installs to
    ``Program Files (x86)``. Reading only the 64-bit view (as #546 shipped)
    never found the marker, so every installed build took itself for a
    portable copy and died creating ``logs\`` next to the exe (#577).
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, _INSTALL_REG_KEY, 0,
                winreg.KEY_READ | view,
            ) as key:
                value, kind = winreg.QueryValueEx(key, _INSTALL_REG_VALUE)
        except OSError:
            continue
        if kind == winreg.REG_SZ and value:
            return Path(value)
    return None


def is_installed_exe() -> bool:
    """True when this frozen exe is the one the installer registered."""
    if not is_frozen() or sys.platform != "win32":
        return False
    target = installed_dir()
    if target is None:
        return False
    try:
        exe_dir = executable_path().resolve().parent
        return exe_dir == target.resolve()
    except OSError:
        return False


def _can_write(root: Path) -> bool:
    """True when a file can actually be created in ``root``.

    ``os.access(root, os.W_OK)`` is not that test on Windows: it looks at the
    read-only attribute and ignores ACLs, so it answers True for
    ``C:\Program Files`` and the ~/Documents fallback below never triggered
    (#577).
    """
    probe = root / f".open-motion-write-test-{os.getpid()}"
    try:
        with open(probe, "wb"):
            pass
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def portable_mode() -> bool:
    """Derived portableMode: a frozen Windows build that the installer did
    not register keeps its writable state next to the exe. Source runs and
    macOS are never portable (cwd / Application Support respectively)."""
    if PORTABLE_MODE_OVERRIDE is not None:
        return bool(PORTABLE_MODE_OVERRIDE)
    if not is_frozen() or sys.platform != "win32":
        return False
    return not is_installed_exe()


def _home_dir() -> Path:
    """The user's home folder without consulting HOME / USERPROFILE.

    Windows: the shell's profile folder. POSIX: the passwd entry for the
    real uid. ``Path.home()`` is only the last resort.
    """
    if sys.platform == "win32":
        known = _known_folder(_FOLDERID_PROFILE)
        if known is not None:
            return known
    else:
        try:
            import pwd

            return Path(pwd.getpwuid(os.getuid()).pw_dir)
        except Exception:
            pass
    return Path.home()


def _local_app_data_dir() -> Path:
    """This user's LocalAppData folder (stock location under the profile if
    the shell call fails). Per-user, never the machine-wide ProgramData —
    see the module docstring (#581)."""
    return _known_folder(_FOLDERID_LOCAL_APP_DATA) or (
        _home_dir() / "AppData" / "Local"
    )


def writable_root(portable: bool | None = None) -> Path:
    """Return the writable data root, creating it if necessary.

    ``portable``: keep everything next to the exe (the un-installed layout)
    instead of scattering it to %LOCALAPPDATA%; ``None`` derives it with
    portable_mode(). An explicit DATA_ROOT_OVERRIDE is used as-is, no
    writability check. The other branches fall back to
    ~/Documents/Open-Motion if the resolved root isn't writable (e.g. cwd
    is "/" on a macOS Finder launch).
    """
    if portable is None:
        portable = portable_mode()
    if DATA_ROOT_OVERRIDE is not None:
        root = Path(DATA_ROOT_OVERRIDE)
        root.mkdir(parents=True, exist_ok=True)
        return root

    if is_frozen():
        if sys.platform == "darwin":
            # macOS has no %LOCALAPPDATA%, and the portable layout can't apply
            # either: writing inside Open-Motion.app invalidates its code
            # signature. Both variants use the standard per-user data location.
            root = _home_dir() / "Library" / "Application Support" / _APP_DIRNAME
        elif portable:
            # The launched exe (utils.frozen resolves Nuitka's outer exe; the
            # interpreter path there is the extracted copy, #548).
            root = executable_path().resolve().parent
        else:
            root = _local_app_data_dir() / _APP_DIRNAME
    else:
        root = Path.cwd()

    # A read-only parent (Finder launches the app with cwd="/") makes mkdir
    # itself raise, before the write probe below could ever redirect us.
    try:
        root.mkdir(parents=True, exist_ok=True)
        writable = _can_write(root)
    except OSError:
        writable = False

    if not writable:
        root = _home_dir() / "Documents" / "Open-Motion"
        root.mkdir(parents=True, exist_ok=True)
    return root

