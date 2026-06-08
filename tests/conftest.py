"""
Shared fixtures and helpers for Open-Motion UI tests.

Provides:
  - App launch/discovery as a session-scoped fixture
  - Window management utilities (coordinate clicks, UIA clicks)
  - Incremental test support (skip remaining tests in a class after first failure)
  - Per-machine panel button calibration (autouse)
  - Modal cleanup between test classes (autouse)
  - App-alive guard between tests (autouse)
  - Session-end JSON+Markdown test report (autouse) for V&V evidence
"""

import atexit
import getpass
import json
import platform
import socket
import time
import subprocess
import sys
import os
import glob as _glob
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pytest
import psutil

# The GUI-automation stack drives the HIL suite only, and pywinauto is
# Windows-only — importing it unconditionally made conftest unimportable on
# macOS, which took the platform-independent `unit` tests down with it. Import
# them optionally; every consumer lives inside a HIL fixture or helper, which
# a `unit`-marked run never reaches.
try:
    import pyautogui
    import pygetwindow as gw
    from pywinauto import Desktop as UiaDesktop
except ImportError:  # non-Windows dev machine — HIL tests can't run here anyway
    pyautogui = None
    gw = None
    UiaDesktop = None


# ─────────────────────────────────────────────
# QCoreApplication for unit tests
# ─────────────────────────────────────────────
# Unit tests that exercise pyqtSignal / QObject (e.g. ScanDataSource)
# need a QCoreApplication to exist before instantiating QObjects. HIL
# tests get a full QApplication via the bloodflow app launch; unit
# tests don't, so create a bare QCoreApplication once per session.
@pytest.fixture(scope="session", autouse=True)
def _qcoreapplication():
    from PyQt6.QtCore import QCoreApplication
    import sys
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication(sys.argv)
    yield app
    # Don't quit() — pytest-collected tests share this session-wide.


# ─────────────────────────────────────────────
# pyautogui defaults
# ─────────────────────────────────────────────
if pyautogui is not None:
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.5

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
APP_KEYWORDS = ["openmotion", "bloodflow", "openwater"]
SLEEP = 2  # seconds to wait after most UI actions

LOG_DIR = Path(__file__).parent / "test_logs"
LOG_DIR.mkdir(exist_ok=True)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

log = logging.getLogger("hil_tests")


# ─────────────────────────────────────────────
# Incremental test support
# ─────────────────────────────────────────────
# When a test in a class marked @pytest.mark.incremental fails,
# all subsequent tests in that class are marked as xfail.

def pytest_addoption(parser):
    parser.addoption(
        "--from-source",
        action="store_true",
        default=False,
        help=(
            "Launch the OpenWater app from source via 'python main.py' instead "
            "of discovering an installed Open-Motion.exe. Equivalent to setting "
            "$OPENWATER_FROM_SOURCE=1; the env var is honoured either way."
        ),
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "incremental: mark test class as incremental (stop on first failure)"
    )
    # Mirror --from-source onto $OPENWATER_FROM_SOURCE so the existing
    # _from_source_mode() helper picks it up without further plumbing.
    if config.getoption("--from-source"):
        os.environ["OPENWATER_FROM_SOURCE"] = "1"
    # Snapshot the tracked repo config before collection imports any test
    # module, so pytest_collection_finish can detect import-time writes.
    global _repo_app_config_snapshot
    try:
        _repo_app_config_snapshot = _REPO_APP_CONFIG.read_bytes()
    except OSError:
        _repo_app_config_snapshot = None


_class_failures = {}


def pytest_runtest_makereport(item, call):
    if call.when == "call" and call.excinfo is not None:
        cls = item.cls
        if cls is not None:
            _class_failures.setdefault(cls.__name__, item.name)


def pytest_runtest_setup(item):
    cls = item.cls
    if cls is not None:
        first_failure = _class_failures.get(cls.__name__)
        if first_failure and first_failure != item.name:
            pytest.xfail(f"previous test failed: {first_failure}")


# ─────────────────────────────────────────────
# App discovery
# ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────
# app_config.json hygiene + FORCE_APP_CONFIG
# ─────────────────────────────────────────────
# pytest imports EVERY test module during collection, even when `-m unit`
# deselects all of a module's tests — so module-level code that writes
# app_config.json fires on every run, and a module-scoped restore fixture
# never runs for a fully-deselected module. That combination used to leave
# the tracked config/app_config.json dirty (clinicalMode/engineeringMode
# flipped, file re-serialized) after plain `pytest -m unit` runs.
#
# The sanctioned replacement: a module that needs an app-config value forced
# on disk before the session `app` fixture launches declares
#
#     FORCE_APP_CONFIG = {"clinicalMode": False}
#
# at module level (a plain dict — no side effect at import). After
# collection and `-m` deselection, pytest_collection_finish applies the
# declarations of modules that actually have selected tests, targeting the
# same file hil_helpers._resolve_app_config_path() resolves (repo config in
# from-source mode, the exe's bundled copy otherwise). The file's original
# bytes are restored at session end, so even the json.dump re-serialization
# is undone.
_REPO_APP_CONFIG = PROJECT_ROOT / "config" / "app_config.json"
_repo_app_config_snapshot: bytes | None = None
_forced_config_target: Path | None = None
_forced_config_snapshot: bytes | None = None


def _read_repo_app_config() -> bytes | None:
    try:
        return _REPO_APP_CONFIG.read_bytes()
    except OSError:
        return None


def pytest_collection_finish(session):
    """Guard the tracked repo config, then apply FORCE_APP_CONFIG.

    Runs after collection + ``-m`` deselection but before any fixture, so
    forced values are on disk before the session ``app`` fixture launches
    the bloodflow app.
    """
    global _forced_config_target, _forced_config_snapshot

    # 1. Fail loudly if merely importing the test modules dirtied the
    #    tracked repo config — that's a module-level write sneaking back in.
    if _repo_app_config_snapshot is not None:
        current = _read_repo_app_config()
        if current != _repo_app_config_snapshot:
            _REPO_APP_CONFIG.write_bytes(_repo_app_config_snapshot)
            raise pytest.UsageError(
                "config/app_config.json was modified while importing test "
                "modules. A test module writes the app config at import time "
                "(e.g. a module-level force_app_config_value(...) call). That "
                "fires during collection of every run — including `-m unit` "
                "runs that never execute the module — and leaves the tracked "
                "file dirty. Declare FORCE_APP_CONFIG = {...} at module level "
                "instead; conftest applies it only when the module has "
                "selected tests. (Original file contents were restored.)"
            )

    if session.config.option.collectonly:
        return

    # 2. Gather FORCE_APP_CONFIG from modules with selected tests.
    forced: dict = {}
    forced_by: dict = {}
    for item in session.items:
        module = getattr(item, "module", None)
        declared = getattr(module, "FORCE_APP_CONFIG", None) or {}
        for key, value in declared.items():
            if key in forced and forced[key] != value:
                log.warning(
                    f"FORCE_APP_CONFIG conflict on {key!r}: "
                    f"{forced_by[key]} wants {forced[key]!r}, "
                    f"{module.__name__} wants {value!r}; using {value!r}"
                )
            forced[key] = value
            forced_by[key] = module.__name__
    if not forced:
        return

    # Deferred import — hil_helpers imports conftest, so importing it at
    # module level here would be circular.
    from hil_helpers import _resolve_app_config_path, force_app_config_value

    target = _resolve_app_config_path()
    try:
        _forced_config_snapshot = target.read_bytes()
    except OSError:
        _forced_config_snapshot = None
    _forced_config_target = target
    for key, value in forced.items():
        force_app_config_value(key, value)


def pytest_sessionfinish(session, exitstatus):
    """Byte-exact restore of the app config we force-wrote at collection."""
    if _forced_config_target is not None and _forced_config_snapshot is not None:
        try:
            _forced_config_target.write_bytes(_forced_config_snapshot)
        except OSError as e:
            log.warning(
                f"could not restore {_forced_config_target} after forcing "
                f"app-config values: {e}"
            )


@pytest.fixture(autouse=True)
def _guard_tracked_app_config(request):
    """Fail loudly if a unit test dirties the tracked config/app_config.json.

    Unit tests must point config IO at tmp_path (monkeypatch
    OPENWATER_DATA_ROOT / config_store.resource_path — see the pattern in
    tests/test_app_config_defaults.py). HIL tests are exempt: several
    legitimately write the on-disk config mid-session and restore it
    themselves.
    """
    if request.node.get_closest_marker("unit") is None:
        yield
        return
    before = _read_repo_app_config()
    yield
    after = _read_repo_app_config()
    if before != after:
        if before is not None:
            _REPO_APP_CONFIG.write_bytes(before)
        pytest.fail(
            "this test modified the tracked config/app_config.json — unit "
            "tests must redirect config reads/writes to tmp_path "
            "(monkeypatch OPENWATER_DATA_ROOT / config_store.resource_path). "
            "The original file contents were restored."
        )


def _from_source_mode() -> bool:
    """True when ``OPENWATER_FROM_SOURCE`` is set, i.e. launch via ``python main.py``."""
    return os.environ.get("OPENWATER_FROM_SOURCE", "").lower() in ("1", "true", "yes")


def _find_main_py() -> str:
    """Return path to main.py at the project root, or '' if missing."""
    p = PROJECT_ROOT / "main.py"
    return str(p) if p.exists() else ""


def _find_exe() -> str:
    """Locate the latest Open-Motion.exe, including pre-release builds.

    Collects all matches across every search pattern and returns the most
    recently modified file, so a newer pre-release build is always preferred
    over an older stable install.
    """
    env = os.environ.get("OPENWATER_EXE", "")
    if env and os.path.exists(env):
        return env
    patterns = [
        r"C:\Users\*\Documents\OpenMotion\**\Open-Motion.exe",
        r"C:\Users\*\Desktop\**\Open-Motion.exe",
        r"C:\Program Files\**\Open-Motion.exe",
        r"C:\Program Files (x86)\**\Open-Motion.exe",
    ]
    all_matches = []
    for pattern in patterns:
        all_matches.extend(_glob.glob(pattern, recursive=True))
    if all_matches:
        latest = max(all_matches, key=os.path.getmtime)
        log.info(f"  Found {len(all_matches)} Open-Motion.exe candidate(s) — using latest: {latest}")
        return latest
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Open-Motion.exe")
    if os.path.exists(local):
        return local
    return ""


# ─────────────────────────────────────────────
# Window helpers
# ─────────────────────────────────────────────
# Process names accepted as the bloodflow app. ``Open-Motion.exe`` is
# the packaged build; ``python.exe`` / ``pythonw.exe`` covers the
# from-source mode (verified by also checking ``main.py`` in the
# command line, since plenty of other things run under python.exe).
_APP_PROCESS_NAMES = ("open-motion.exe", "python.exe", "pythonw.exe")


def _window_process_name(w) -> str | None:
    """Return the lowercase exe name of the process owning window ``w``,
    or ``None`` if the lookup fails. Uses the Win32 HWND→PID→psutil
    chain — pygetwindow doesn't expose PID directly."""
    hwnd = getattr(w, "_hWnd", None)
    if hwnd is None:
        return None
    try:
        import ctypes
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(
            hwnd, ctypes.byref(pid)
        )
        if pid.value == 0:
            return None
        return psutil.Process(pid.value).name().lower()
    except Exception:
        return None


def _is_bloodflow_window(w) -> bool:
    """True if window ``w`` is owned by the bloodflow app process.

    Filtering on the OS-reported process name — rather than the title
    string — catches the case where the user has File Explorer open
    at ``C:\\Users\\...\\openmotion-bloodflow-app`` (title:
    ``openmotion-bloodflow-app``) which incidentally matches
    APP_KEYWORDS. ensure_visible would otherwise activate Explorer,
    every subsequent pixel-coord click would land inside it, and the
    user sees the test "bringing up another application and clicking
    around inside it".
    """
    title = (w.title or "").strip()
    if not title:
        return False
    proc_name = _window_process_name(w)
    if proc_name is None:
        # Process lookup failed; fall back to the old keyword check
        # rather than locking out tests on systems where the Win32
        # call is unavailable (CI containers etc).
        return any(k in title.lower() for k in APP_KEYWORDS)
    if proc_name not in _APP_PROCESS_NAMES:
        return False
    if proc_name in ("python.exe", "pythonw.exe"):
        # In from-source mode several python.exe windows can co-exist
        # (e.g. the pytest runner itself). Disambiguate by command line.
        try:
            hwnd = getattr(w, "_hWnd", None)
            if hwnd is None:
                return False
            import ctypes
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(
                hwnd, ctypes.byref(pid)
            )
            cmdline = " ".join(psutil.Process(pid.value).cmdline()).lower()
            return "main.py" in cmdline and "openmotion-bloodflow-app" in cmdline
        except Exception:
            return False
    return True


def ensure_visible():
    """Bring the bloodflow app window to the foreground.

    Identifies the right window by owning-process name (Open-Motion.exe
    or python main.py with the bloodflow project on the command line),
    so a sibling File Explorer window pointed at the project folder
    can't masquerade as the app. Otherwise every subsequent
    pixel-coord click would land inside Explorer.
    """
    for w in gw.getAllWindows():
        if not _is_bloodflow_window(w):
            continue
        try:
            if w.isMinimized:
                w.restore()
                time.sleep(2)
            w.activate()
            time.sleep(1)
        except Exception:
            pass
        return True
    return False


def uia_window(retries: int = 3):

    for attempt in range(retries):
        ensure_visible()
        desktop = UiaDesktop(backend="uia")
        try:
            # The window title differs per build variant since the #278
            # rename: "Open-Motion" (clinical) / "Open-Motion Research"
            # (research). Match both in one snapshot query — the old
            # exact title="Open-Motion" lookup silently missed every
            # research-mode run.
            hits = desktop.windows(
                title_re=r"(?i)^Open-Motion( Research)?$")
            if hits:
                return desktop.window(title=hits[0].window_text())
        except Exception as e:
            log.warning(f"  UIA exact-title lookup failed: {e}")
        # Fallback: match by keyword but require control_type=Window
        # to reduce ambiguity with File Explorer etc. Deliberately
        # OUTSIDE the except above — a clean not-found (no exception)
        # must reach it too.
        for kw in APP_KEYWORDS:
            try:
                hits = desktop.windows(title_re=f"(?i).*{kw}.*")
                for win in hits:
                    title = win.window_text()
                    # Skip File Explorer, browser tabs, etc.
                    if "File Explorer" in title or "Chrome" in title:
                        continue
                    if any(k in title.lower() for k in APP_KEYWORDS):
                        return desktop.window(title=title)
            except Exception:
                continue
        if attempt < retries - 1:
            log.warning(
                f"  UIA window not found (attempt {attempt + 1}/{retries}), retrying..."
            )
            time.sleep(2)
    raise RuntimeError("App window not found via UI Automation")


def get_app_window():
    """Return a pygetwindow Window object for the bloodflow app.

    Uses the same owning-process filter as ``ensure_visible`` so we
    never return a File Explorer / browser / shell window that
    incidentally matches APP_KEYWORDS.
    """
    for w in gw.getAllWindows():
        if _is_bloodflow_window(w):
            return w
    raise RuntimeError("App window not found")


def click_sidebar(rx: float, ry: float, label: str = ""):
    """Click a sidebar button using relative window coordinates."""
    ensure_visible()
    w = get_app_window()
    x = int(w.left + rx * w.width)
    y = int(w.top + ry * w.height)
    log.info(f"  click '{label}'  rel({rx:.3f}, {ry:.3f})  abs({x}, {y})")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click(x, y)
    time.sleep(SLEEP)


def click_by_name(name: str, timeout: float = 8.0):
    """Find a UI element by its visible label via UIA, then click it.

    Polls for up to ``timeout`` seconds. The UIA tree can lag for a
    second or two after a foreground change (e.g. the matplotlib plot
    window closing and focus returning to the bloodflow app), so a
    one-shot lookup is too brittle — TestHistory.test_05 hit exactly
    that race when "Visualize Contrast/Mean" was queried right after
    Alt+F4'ing the BFI/BVI plot.

    Lookup order on each poll iteration:
      1. ``descendants(title=name, control_type="Button")`` — the
         common case. Filters out duplicate Text/Group nodes that may
         shadow the actual clickable button.
      2. ``descendants(title=name)`` — broader fallback for non-Button
         elements (TextField, Custom, etc.).
      3. ``child_window(title=name, control_type=...)`` walked across
         a handful of control types, with the bottom of the timeout
         budget — last-resort.

    Clicks via UIA's InvokePattern (``elem.click_input()`` style isn't
    used because it relies on the pixel position, which can be wrong
    for ScrollView-clipped items; ``elem.invoke()`` fires the button
    regardless of position). Falls back to a pixel click for elements
    that don't expose Invoke (TextField, Group, etc.).
    """
    ensure_visible()
    log.info(f"  find by name: '{name}' (timeout {timeout:.1f}s)")
    deadline = time.monotonic() + timeout
    last_err = None

    def _try_click(elem, source: str) -> bool:
        try:
            try:
                elem.invoke()  # UIA InvokePattern — coord-free
                log.info(f"     invoked via {source} (UIA)")
                time.sleep(SLEEP)
                return True
            except Exception:
                pass
            rect = elem.rectangle()
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            log.info(f"     {source}  click center=({cx}, {cy})")
            pyautogui.moveTo(cx, cy, duration=0.3)
            pyautogui.click(cx, cy)
            time.sleep(SLEEP)
            return True
        except Exception as e:
            log.warning(f"     click via {source} failed: {e}")
            return False

    while time.monotonic() < deadline:
        try:
            win = uia_window()
            try:
                matches = win.descendants(title=name, control_type="Button")
                if matches and _try_click(matches[0], "descendants(Button)"):
                    return
            except Exception as e:
                last_err = e

            try:
                matches = win.descendants(title=name)
                if matches and _try_click(matches[0], "descendants"):
                    return
            except Exception as e:
                last_err = e
        except Exception as e:
            last_err = e
        time.sleep(0.5)

    # Final last-resort: child_window walked across control types,
    # with a short per-CT timeout. Only reached if every poll above
    # failed — covers oddly-typed elements that descendants misses.
    try:
        win = uia_window()
        for ct in ("Button", "Custom", "Text", "Group", "ListItem", "Pane"):
            try:
                elem = win.child_window(title=name, control_type=ct)
                if elem.exists(timeout=1):
                    if _try_click(elem, f"child_window(ct={ct})"):
                        return
            except Exception:
                continue
    except Exception as e:
        last_err = e

    raise RuntimeError(
        f"Could not find '{name}' via UI Automation after {timeout:.1f}s "
        f"(last error: {last_err!r})"
    )


def wait_with_log(total_seconds: int, label: str):
    """Wait total_seconds, logging progress every 60s."""
    log.info(f"  Waiting {total_seconds}s -- {label}")
    elapsed = 0
    while elapsed < total_seconds:
        chunk = min(60, total_seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
        remaining = total_seconds - elapsed
        log.info(
            f"     {elapsed}/{total_seconds}s elapsed"
            + (f"  ({remaining}s remaining)" if remaining > 0 else "  -- done")
        )


def require_focus():
    """Ensure the app window has foreground focus. Fails the test if it can't."""
    if not ensure_visible():
        raise RuntimeError("App window not found -- cannot ensure focus")
    # Verify the app actually has foreground focus
    w = get_app_window()
    try:
        if not w.isActive:
            w.activate()
            time.sleep(1)
            if not w.isActive:
                raise RuntimeError(
                    f"App window '{w.title}' is not the active foreground window"
                )
    except AttributeError:
        pass  # pygetwindow version without isActive -- best-effort


def read_combobox_values():
    """Return a list of text values for all ComboBox controls in the app window."""
    ensure_visible()
    time.sleep(1)  # let QML animations settle before querying UIA tree
    win = uia_window()
    results = []
    try:
        for cb in win.descendants(control_type="ComboBox"):
            text = cb.window_text().strip()
            results.append(text)
    except Exception as e:
        log.warning(f"  read_combobox_values failed: {e}")
    log.info(f"  ComboBox values: {results}")
    return results


def wait_for_combobox(idx: int, timeout: float = 15.0):
    """Poll UIA up to ``timeout`` seconds for at least ``idx + 1``
    ComboBoxes to appear in the app window. Returns the matching
    ComboBox element on success, or ``None`` on timeout.

    Same pattern as ``test_history._wait_for_combobox`` — promoted
    here so every test that opens Scan Settings can share it. The
    Qt accessibility bridge can take a couple of seconds to expose
    modal contents on the self-hosted runner; a one-shot
    ``descendants(control_type="ComboBox")`` query right after
    ``click_panel("Scan\\nSettings")`` returns ``[]`` and the calling
    test bails with "Expected at least 1 ComboBox(es), found 0".
    """
    deadline = time.monotonic() + timeout
    last_count = -1
    while time.monotonic() < deadline:
        ensure_visible()
        try:
            cbs = uia_window().descendants(control_type="ComboBox")
        except Exception:
            cbs = []
        if len(cbs) > idx:
            return cbs[idx]
        if len(cbs) != last_count:
            log.info(
                f"  waiting for ComboBox[{idx}]... currently {len(cbs)} visible"
            )
            last_count = len(cbs)
        time.sleep(0.5)

    # Diagnostics: on timeout, dump what the UIA tree DOES expose so
    # the failing test report can distinguish "modal didn't open" from
    # "modal opened but UIA can't see ComboBoxes". Prior runs hit this
    # path silently and we burned an iteration debugging the wrong
    # hypothesis.
    try:
        win = uia_window()
        sample: list[str] = []
        for elem in win.descendants():
            try:
                t = (elem.window_text() or "").strip()
            except Exception:
                continue
            if t and len(t) < 80:
                sample.append(t)
                if len(sample) >= 30:
                    break
        log.warning(
            f"  wait_for_combobox({idx}) timed out — UIA texts visible "
            f"in window (first 30): {sample}"
        )
    except Exception as e:
        log.warning(
            f"  wait_for_combobox({idx}) timed out and UIA dump itself "
            f"failed: {e}"
        )
    return None


def get_clipboard() -> str:
    """Read clipboard text via PowerShell. Returns '' on failure."""
    try:
        return subprocess.check_output(
            ["powershell", "-command", "Get-Clipboard"], text=True
        ).strip()
    except Exception:
        return ""


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────
@pytest.fixture(scope="session")
def app():
    """Launch or connect to the Open-Motion app. Session-scoped — runs once.

    Set ``OPENWATER_FROM_SOURCE=1`` to run the in-tree dev branch via
    ``python main.py`` instead of discovering an installed ``Open-Motion.exe``.
    """
    from_source = _from_source_mode()

    # Check if already running
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if from_source:
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if "python" in name and "main.py" in cmdline and "openmotion-bloodflow-app" in cmdline:
                    log.info("App (from source) already running.")
                    time.sleep(SLEEP)
                    ensure_visible()
                    return True
            else:
                if "openwater" in name:
                    log.info("App already running.")
                    time.sleep(SLEEP)
                    ensure_visible()
                    return True
        except Exception:
            pass

    # Try to launch
    if from_source:
        main_py = _find_main_py()
        if main_py:
            log.info(f"Launching from source: {sys.executable} {main_py}")
            subprocess.Popen([sys.executable, main_py], cwd=str(PROJECT_ROOT))
            time.sleep(SLEEP * 8)  # python+QML startup is slower than packaged exe
            ensure_visible()
            return True
        pytest.skip(f"main.py not found at {PROJECT_ROOT}")

    exe = _find_exe()
    if exe and os.path.exists(exe):
        log.info(f"Launching: {exe}")
        subprocess.Popen([exe])
        time.sleep(SLEEP * 5)  # give it time to launch and settle
        ensure_visible()
        return True

    pytest.skip(
        "Open-Motion.exe not found -- set OPENWATER_EXE, or set "
        "OPENWATER_FROM_SOURCE=1 to launch via python main.py"
    )


def _all_collected_are_unit(session) -> bool:
    """True iff every collected test in the session carries the
    ``unit`` marker. Used by the app-dependent autouse fixtures to
    skip launching the bloodflow app when the operator only asked
    for unit tests."""
    items = getattr(session, "items", None) or []
    if not items:
        return False
    return all(item.get_closest_marker("unit") is not None for item in items)


@pytest.fixture(scope="session", autouse=True)
def _calibrate_panel_buttons_once(request):
    """Run UIA-based panel button calibration once after the app
    launches. Sets a per-session cache that ``click_panel(label)`` and
    ``click_panel_button(label, fallback=...)`` both consult, so panel
    button click coordinates are correct regardless of the runner's
    DPI scale or window size.

    Autouse so every test session gets calibration without each test
    needing to request the fixture explicitly. If the app fixture
    skipped (no exe / no main.py), we skip silently here too.

    Skipped entirely when every collected test is unit-marked — those
    tests don't drive the UI, so launching the app would just cost a
    cold-start with nothing to use it.
    """
    if _all_collected_are_unit(request.session):
        yield
        return
    request.getfixturevalue("app")  # ensure the app launched
    try:
        from hil_helpers import calibrate_panel_buttons
        calibrate_panel_buttons()
    except Exception as e:
        log.warning(f"  panel button calibration failed at session start: {e}")
    yield


# Track the *first* test that finds the app gone, so subsequent tests
# fail with a pinpointed message instead of a generic
# "App window not found" cascade.
_app_dead_after: str | None = None


def _dismiss_modals_if_any(max_presses: int = 3) -> int:
    """Press Escape repeatedly to dismiss any modal currently on top.

    Returns the number of presses sent. Idempotent — pressing Escape
    when no modal is open is harmless. Used by the autouse class
    cleanup fixture to keep stale Session-Notes modals from a prior
    test class from being the topmost UIA target when a later class
    looks for its own modal contents.
    """
    if not ensure_visible():
        return 0
    for i in range(max_presses):
        try:
            import pyautogui as _pg
            _pg.press("escape")
        except Exception:
            return i
        time.sleep(0.3)
    time.sleep(SLEEP)
    return max_presses


@pytest.fixture(scope="class", autouse=True)
def _dismiss_leftover_modals_per_class(request):
    """Dismiss any leftover modal at the start of every test class.

    Background: test_connection_redesign.test_03_power_cycle_during_scan
    starts a scan, then yanks power. The SDK auto-stops the scan and
    the Session Notes modal opens automatically, but the test never
    dismisses it. The modal stays on top through subsequent test
    classes, and UIA only exposes the *topmost* modal's contents —
    so test_scan_settings, test_notes, etc. all start with their
    target modal hidden behind the stale Session Notes overlay.

    Class-scoped autouse + 3 Escape presses is idempotent and cheap;
    pressing Escape with no modal open is harmless.

    Unit-marked tests skip — no modals to dismiss without an app.
    """
    if request.node.get_closest_marker("unit") is not None:
        yield
        return
    request.getfixturevalue("app")
    n = _dismiss_modals_if_any(max_presses=3)
    if n:
        log.info(f"  pre-class modal cleanup: sent {n} Escape press(es)")
    yield


@pytest.fixture(autouse=True)
def _check_app_alive(request):
    """Function-scoped guard that runs before every test.

    Detects the case where the bloodflow app has crashed between
    tests (e.g. an SDK-side unhandled exception during a previous
    test's teardown), and fails the current test with a clear,
    pointing message rather than letting the cascade of "App window
    not found" errors bury the actual culprit.

    Once the app is dead, every subsequent test fails with the same
    message naming the test that *first* detected the death — this
    is almost always either the test that triggered the crash, or
    the test immediately after it.

    Unit-marked tests skip — there's no app to be alive.
    """
    if request.node.get_closest_marker("unit") is not None:
        yield
        return
    request.getfixturevalue("app")
    global _app_dead_after
    if _app_dead_after is not None:
        pytest.fail(
            f"Open-Motion app died — first noticed by '{_app_dead_after}'. "
            f"Subsequent tests cannot run. Inspect the app log "
            f"(logs/open-motion-*.log) around that test for an "
            f"unhandled Python exception."
        )

    if not ensure_visible():
        _app_dead_after = request.node.nodeid
        pytest.fail(
            f"Open-Motion app window is gone — likely crashed during the "
            f"previous test. See logs/open-motion-*.log for "
            f"diagnostics. (First detected at '{_app_dead_after}'.)"
        )
    yield


@pytest.fixture(autouse=True)
def _isolate_writable_root(request, tmp_path, monkeypatch):
    """Route utils.app_paths.writable_root() to a per-test tmp dir for
    every unit test.

    Without this, a non-frozen (dev-mode) writable_root() falls through to
    cwd — the repo root when pytest runs from there. Any unit test that
    triggers a config save (e.g. via setConfig/_save_app_config) without
    its own OPENWATER_DATA_ROOT override then writes a real
    app_config.local.json into the checked-out worktree, which persists
    across test runs and corrupts later tests that load the shipped
    config expecting no overrides present (seen: test_app_config_defaults
    failures that changed shape between runs depending on prior state).

    A test that needs its own root (e.g. to assert on the exact path)
    still wins — monkeypatch.setenv/delenv in the test body simply
    overrides this fixture's value, same monkeypatch instance either way.
    """
    if request.node.get_closest_marker("unit") is None:
        yield
        return
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path / "_app_paths_root"))
    yield


# ─────────────────────────────────────────────────────────────────────────
# Test report generation — V&V evidence at session end
# ─────────────────────────────────────────────────────────────────────────
# Captures every collected pytest result and writes a structured report
# at session end suitable for verification & validation evidence.
# Output paths (relative to this conftest):
#   tests/test_logs/HIL_Report_<timestamp>.json
#   tests/test_logs/HIL_Report_<timestamp>.md
#
# Implementation details:
#   - The report is built by parsing pytest's JUnit XML
#     (tests/test_logs/results.xml, written by pytest.ini's --junitxml).
#   - We register the writer via atexit rather than a fixture finalizer
#     so it fires AFTER pytest has written results.xml. A fixture
#     teardown would race the file write and capture stale data.
#   - Generic across all test files — no per-file class filter.
#
# Originally adapted from a per-file version on origin/Varun-Test
# (commits ba05bd0 + e3a017a in tests/test_clinicalmode.py).

_REPORT_SESSION_START: datetime | None = None
# Resolved at session start so we don't depend on the app process
# still being alive when the atexit-registered _write_hil_report runs
# (test cleanup or a crash may have killed it by then).
_REPORT_APP_VERSION: str | None = None


def _running_app_exe_path() -> str:
    """Find the .exe path of the bloodflow process that's currently running.

    Returns the path of the FIRST live process whose name matches
    one of the bloodflow executable names. Falls back to '' if no
    matching process is alive.

    This is the source of truth for "which app is being tested" —
    much more reliable than ``_find_exe()`` (which picks the newest
    installed .exe by mtime, possibly a different install).
    """
    # Old (≤1.3.x) and new (Open-Motion rename on next) packaged exe names.
    target_names = {"openwaterapp.exe", "openwaterapp_console.exe",
                    "open-motion.exe", "open-motion_console.exe"}
    try:
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name in target_names:
                    exe = proc.info.get("exe") or ""
                    if exe and os.path.exists(exe):
                        return exe
            except Exception:
                continue
    except Exception as e:
        log.warning(f"  _running_app_exe_path failed: {e}")
    return ""


def _resolve_app_version() -> str:
    """Resolve the version of the *running* bloodflow app.

    Priority — match the version of what's actually being tested,
    NOT the source tree's git state:

      0. **Find the .exe path of the currently-running OpenWaterApp
         process** via psutil. This guarantees the version we report
         is for the app actually under test, not the newest install
         on disk (which is what ``_find_exe()`` would pick).
         Falls back to ``_find_exe()`` if no process is alive (e.g.
         resolver called before the ``app`` fixture launches it).
      1. Embedded ``ProductVersion`` / ``FileVersion`` from the .exe
         metadata.
      2. Walk UP from the .exe through parent / grandparent folders
         looking for a name that contains a version pattern. Install
         layouts vary:
           ``…\\OpenWaterApp-1.0.2-pre-0-g8027c14\\OpenWaterApp.exe``
                                ^ version in parent
           ``…\\OpenMotion-Bloodflow-1.2.0-dev.1_RUO\\OpenWaterApp\\OpenWaterApp.exe``
                                ^ version in grandparent (with _RUO suffix)
      3. ``version.get_version()`` from the source tree — git-describe
         based; used only when no .exe is discoverable.
      4. ``$OPENWATER_VERSION`` env override.
      5. ``"unknown"``.
    """
    import re

    version_re = re.compile(r"^\d+(\.\d+)+")
    folder_version_re = re.compile(
        r"\d+\.\d+(?:\.\d+)*(?:[-_+][A-Za-z0-9._+-]*)?"
    )

    # 0. Prefer the path of the actually-running process.
    exe_path = _running_app_exe_path()
    if exe_path:
        log.info(f"  resolving version from running process: {exe_path}")
    else:
        log.info("  no running OpenWaterApp process — falling back to _find_exe()")
        try:
            exe_path = _find_exe()
        except Exception as e:
            log.warning(f"  _find_exe() failed: {e}")
            exe_path = ""

    # 1. .exe ProductVersion / FileVersion metadata.
    try:
        if exe_path:
            ps_cmd = (
                f"$v = (Get-Item '{exe_path}').VersionInfo; "
                "Write-Output $v.FileVersion; "
                "Write-Output $v.ProductVersion; "
                "Write-Output $v.FileVersionRaw; "
                "Write-Output $v.ProductVersionRaw"
            )
            try:
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=10,
                )
                candidates = [ln.strip() for ln in (result.stdout or "").splitlines()
                              if ln.strip()]
                log.info(f"  app version metadata candidates: {candidates}")
                for c in candidates:
                    if version_re.match(c):
                        log.info(f"  resolved app version (from .exe): {c}")
                        return c
            except Exception as e:
                log.warning(f"  PowerShell VersionInfo read failed: {e}")

            # 2. Walk up from the .exe — first ancestor folder whose
            #    name contains a version-shaped substring wins.
            for ancestor in Path(exe_path).parents:
                folder_name = ancestor.name
                # Don't walk past the user's home directory layout.
                if folder_name.lower() in ("users", "documents", "openmotion", ""):
                    if folder_name.lower() in ("users", ""):
                        break
                    # OpenMotion / Documents themselves don't have a
                    # version in them but their CHILDREN do — keep
                    # going, just don't try to match this folder.
                    continue
                m = folder_version_re.search(folder_name)
                if m:
                    version = m.group(0)
                    log.info(
                        f"  resolved app version (from path '{folder_name}'): "
                        f"{version}"
                    )
                    return version

            # Final fallback: the immediate parent folder name, even
            # without a version pattern — at least it tells you which
            # install was picked.
            parent_name = Path(exe_path).parent.name
            log.info(f"  resolved app version (parent folder): {parent_name}")
            return parent_name
    except Exception as e:
        log.warning(f"  _resolve_app_version: .exe path resolution failed: {e}")

    # 3. No .exe discoverable — running from source. Use git describe.
    try:
        repo_root = Path(__file__).resolve().parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        import version as _project_version    # noqa: PLC0415
        v = (_project_version.get_version() or "").strip()
        if v:
            log.info(f"  resolved app version (from source git describe): {v}")
            return v
    except Exception as e:
        log.warning(f"  version.get_version() unavailable: {e}")

    # 4. Env override / 5. unknown.
    return os.environ.get("OPENWATER_VERSION", "unknown")


def _report_get_app_version() -> str:
    """Return the version of the running bloodflow app.

    Resolves on each call so we always pick up the currently-running
    process's path via ``_running_app_exe_path``. Once a live answer
    has been resolved we cache it so a later call (after the app may
    have been killed by test cleanup) still has the right value.
    """
    global _REPORT_APP_VERSION
    v = _resolve_app_version()
    if v and v != "unknown":
        _REPORT_APP_VERSION = v
    # If the live resolve failed, fall back to the last good cached
    # value (e.g. the app died during cleanup, but it was alive earlier).
    return v if (v and v != "unknown") else (_REPORT_APP_VERSION or "unknown")


def _report_get_environment() -> dict:
    """Environment snapshot for the report header."""
    return {
        "tester":         os.environ.get("TESTER_NAME", getpass.getuser()),
        "hostname":       socket.gethostname(),
        "os":             f"{platform.system()} {platform.release()} "
                          f"({platform.version()})",
        "python_version": sys.version.split()[0],
        "app_version":    _report_get_app_version(),
    }


def _parse_junit_xml(xml_path: Path) -> list[dict]:
    """Parse pytest's JUnit XML into a list of per-test result dicts.

    Includes every testcase regardless of file or class — this is a
    suite-wide report, not a per-module one.
    """
    if not xml_path.exists():
        log.warning(f"  JUnit XML not found at {xml_path}; report skipped")
        return []
    try:
        tree = ET.parse(xml_path)
    except Exception as e:
        log.warning(f"  Failed to parse {xml_path}: {e}")
        return []

    out: list[dict] = []
    for testcase in tree.iter("testcase"):
        classname = testcase.get("classname", "")
        test_class = classname.split(".")[-1] if classname else "<module>"
        test_id = testcase.get("name", "")
        duration = float(testcase.get("time", "0") or 0.0)

        if testcase.find("failure") is not None:
            status = "FAIL"
            details = (testcase.find("failure").get("message", "") or "")[:300]
        elif testcase.find("error") is not None:
            status = "ERROR"
            details = (testcase.find("error").get("message", "") or "")[:300]
        elif testcase.find("skipped") is not None:
            status = "SKIP"
            details = (testcase.find("skipped").get("message", "") or "")[:300]
        else:
            status = "PASS"
            details = ""

        out.append({
            "test_id":      test_id,
            "test_class":   test_class,
            "test_module":  classname.rsplit(".", 1)[0] if "." in classname else "",
            "status":       status,
            "duration_sec": round(duration, 2),
            "details":      details,
        })
    return out


def _write_hil_report() -> None:
    """Build and write the JSON + Markdown report files. Called via
    atexit so it runs after pytest's results.xml is finalised."""
    log_dir = LOG_DIR  # tests/test_logs from the top of this file
    junit_xml = log_dir / "results.xml"

    # results.xml is written when pytest finalises the session, which
    # may be slightly after our atexit runs in some plugin orderings.
    # Poll briefly so we don't miss it on race-y exits.
    for _ in range(10):
        if junit_xml.exists() and junit_xml.stat().st_size > 0:
            break
        time.sleep(0.5)

    results = _parse_junit_xml(junit_xml)
    if not results:
        log.warning("HIL report: no test results captured; skipping.")
        return

    log_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Which test scripts ran this session (sorted, deduplicated).
    test_modules = sorted({r["test_module"] for r in results if r["test_module"]})
    # Filename slug: single script → that script's name; multiple → a
    # generic 'suite_<N>scripts' tag so the file name stays readable.
    if len(test_modules) == 1:
        module_slug = test_modules[0]
    elif len(test_modules) > 1:
        module_slug = f"suite_{len(test_modules)}scripts"
    else:
        module_slug = "unknown"

    env = _report_get_environment()
    duration = (
        (datetime.now() - _REPORT_SESSION_START).total_seconds()
        if _REPORT_SESSION_START else 0
    )
    summary = {
        "total":   len(results),
        "passed":  sum(1 for r in results if r["status"] == "PASS"),
        "failed":  sum(1 for r in results if r["status"] == "FAIL"),
        "errored": sum(1 for r in results if r["status"] == "ERROR"),
        "skipped": sum(1 for r in results if r["status"] == "SKIP"),
    }

    # ── JSON ──
    report_data = {
        "report_title": "Open-Motion — HIL Test Session Report",
        "purpose":      "Verification & validation evidence for the HIL "
                        "test suite.",
        "test_scripts":  test_modules,
        "session_start": _REPORT_SESSION_START.isoformat(timespec="seconds")
                         if _REPORT_SESSION_START else "",
        "session_end":   datetime.now().isoformat(timespec="seconds"),
        "duration_sec":  round(duration, 1),
        "environment":   env,
        "summary":       summary,
        "test_results":  results,
    }
    json_path = log_dir / f"HIL_Report_{module_slug}_{ts}.json"
    json_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")

    # ── Markdown ──
    lines = [
        f"# {report_data['report_title']}",
        "",
        f"**Purpose:** {report_data['purpose']}",
        "",
        "## Test Scripts",
        "",
    ]
    if test_modules:
        for mod in test_modules:
            lines.append(f"- `{mod}.py`")
    else:
        lines.append("- _n/a_")
    lines += [
        "",
        "## Session",
        "",
        f"- **Session Start:** {report_data['session_start']}",
        f"- **Session End:**   {report_data['session_end']}",
        f"- **Duration:**      {report_data['duration_sec']} s",
        "",
        "## Environment",
        "",
        f"- **Tester:** {env['tester']}",
        f"- **Hostname:** {env['hostname']}",
        f"- **OS:** {env['os']}",
        f"- **Python:** {env['python_version']}",
        f"- **App version:** {env['app_version']}",
        "",
        "## Summary",
        "",
        "| Metric  | Count |",
        "|---------|-------|",
        f"| Total   | {summary['total']} |",
        f"| Passed  | {summary['passed']} |",
        f"| Failed  | {summary['failed']} |",
        f"| Errored | {summary['errored']} |",
        f"| Skipped | {summary['skipped']} |",
        "",
        "## Test Results",
        "",
        "| # | Test | Class | Status | Duration (s) |",
        "|---|------|-------|--------|--------------|",
    ]
    for i, r in enumerate(results, 1):
        lines.append(
            f"| {i} | `{r['test_id']}` | {r['test_class']} | "
            f"**{r['status']}** | {r['duration_sec']} |"
        )

    failures = [r for r in results if r["status"] in ("FAIL", "ERROR")]
    if failures:
        lines += ["", "## Failure Details", ""]
        for r in failures:
            lines += [
                f"### `{r['test_class']}::{r['test_id']}`",
                "",
                f"- **Status:** {r['status']}",
                f"- **Module:** {r['test_module']}",
                f"- **Error:** `{r['details'] or 'no message'}`",
                "",
            ]

    lines += [
        "",
        "## Sign-Off",
        "",
        "| Role | Name | Signature | Date |",
        "|------|------|-----------|------|",
        f"| Tester | {env['tester']} | _______________ | _______________ |",
        "| QA Reviewer | _______________ | _______________ | _______________ |",
        "| Technical Lead | _______________ | _______________ | _______________ |",
        "",
        "---",
        f"_Report generated automatically at "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
    ]
    md_path = log_dir / f"HIL_Report_{module_slug}_{ts}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    log.info("HIL report written:")
    log.info(f"  JSON:     {json_path}")
    log.info(f"  Markdown: {md_path}")


@pytest.fixture(scope="session", autouse=True)
def _hil_report_session():
    """Capture session start time + app version, and arm the atexit-
    registered report writer.

    Autouse + session-scope so it runs once for every pytest invocation
    that touches this conftest, with no per-test setup/teardown.

    Version resolution happens lazily in ``_write_hil_report`` (NOT
    at session start) because this fixture runs BEFORE the ``app``
    fixture launches the bloodflow app — querying ``psutil`` at
    session start would never find a running process, defeating the
    "use the actually-running app's path" priority. The lazy resolver
    runs at atexit, when the app is most likely still alive.
    """
    global _REPORT_SESSION_START
    _REPORT_SESSION_START = datetime.now()
    log.info(f"HIL report session started at {_REPORT_SESSION_START}")
    atexit.register(_write_hil_report)
    yield
