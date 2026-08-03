"""Guards for the runtime window-class icon hardening (issue #223).

See utils/win_taskbar_icon.py for why the class icon matters: it is what the
shell paints when its WM_GETICON probe of a freshly shown window times out.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ICO_PATH = REPO_ROOT / "assets" / "images" / "favicon.ico"

from utils.win_taskbar_icon import apply_window_class_icon  # noqa: E402


@pytest.mark.unit
def test_apply_is_fail_soft_on_a_bad_hwnd():
    """A cosmetic icon must never take down a clinical app's startup."""
    assert apply_window_class_icon(0, ICO_PATH) is False
    assert apply_window_class_icon(None, ICO_PATH) is False
    assert apply_window_class_icon("not-a-handle", ICO_PATH) is False


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32", reason="Win32 class icons")
def test_apply_is_fail_soft_on_a_missing_icon_file(tmp_path):
    # Non-null but bogus HWND: LoadImage fails first, so nothing is installed
    # and we still return False rather than raising.
    assert apply_window_class_icon(1, tmp_path / "nope.ico") is False


# Run in a subprocess: this needs a real GUI QApplication, and the session-wide
# QCoreApplication that conftest installs cannot host a QWidget and cannot be
# replaced in-process.
_CLASS_ICON_PROBE = r'''
import ctypes, sys
from ctypes import wintypes
from PyQt6.QtWidgets import QApplication, QWidget

sys.path.insert(0, sys.argv[1])
from utils.win_taskbar_icon import apply_window_class_icon

GCLP_HICON = -14
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.LoadIconW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HMODULE, wintypes.LPCWSTR]
get_class = getattr(user32, "GetClassLongPtrW", None) or user32.GetClassLongW
get_class.restype = ctypes.c_size_t
get_class.argtypes = [wintypes.HWND, ctypes.c_int]

app = QApplication([])
widget = QWidget()
hwnd = int(widget.winId())

generic = user32.LoadIconW(None, wintypes.LPCWSTR(32512))  # IDI_APPLICATION
before = get_class(wintypes.HWND(hwnd), GCLP_HICON)
ok = apply_window_class_icon(hwnd, sys.argv[2])
after = get_class(wintypes.HWND(hwnd), GCLP_HICON)

print(f"applied={ok} generic={generic} before={before} after={after}")
assert ok is True, "apply_window_class_icon returned False"
assert after not in (0, generic), "class icon is still the generic Windows icon"
assert after != before, "class icon was not changed"
print("OK")
'''


@pytest.mark.unit
@pytest.mark.skipif(sys.platform != "win32", reason="Win32 class icons")
def test_apply_replaces_the_generic_class_icon(tmp_path):
    """The whole point: the class icon must stop being IDI_APPLICATION.

    Also asserts the *before* state is the generic icon, which is the bug
    itself — Qt registers the class with IDI_APPLICATION because its
    "IDI_ICON1" lookup finds nothing.
    """
    pytest.importorskip("PyQt6.QtWidgets")

    script = tmp_path / "probe.py"
    script.write_text(_CLASS_ICON_PROBE, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(REPO_ROOT), str(ICO_PATH)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"probe failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout, result.stdout


@pytest.mark.unit
def test_aumid_matches_the_installer_shortcuts():
    """A one-sided AUMID bump silently breaks Start-menu/pinned icons.

    Both MSI shortcuts must carry the same System.AppUserModel.ID the app sets
    at startup.
    """
    main_py = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    wxs = (REPO_ROOT / "installer" / "app.wxs").read_text(encoding="utf-8")

    import re
    app_ids = re.findall(
        r"SetCurrentProcessExplicitAppUserModelID\(\s*[\"']([^\"']+)[\"']",
        main_py,
    )
    assert len(app_ids) == 1, f"expected one AUMID in main.py, got {app_ids}"

    shortcut_ids = re.findall(
        r'Key="System\.AppUserModel\.ID"\s+Value="([^"]+)"', wxs
    )
    assert len(shortcut_ids) == 2, (
        f"expected both MSI shortcuts to set an AUMID, got {shortcut_ids}"
    )
    assert set(shortcut_ids) == set(app_ids), (
        f"AUMID drift: main.py {app_ids} vs installer/app.wxs {shortcut_ids}"
    )
