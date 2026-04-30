"""
Shared fixtures and helpers for OpenWater BloodFlow UI tests.

Provides:
  - App launch/discovery as a session-scoped fixture
  - Window management utilities (coordinate clicks, UIA clicks)
  - Incremental test support (skip remaining tests in a class after first failure)
"""

import time
import subprocess
import sys
import os
import glob as _glob
import logging
from pathlib import Path

import pytest
import pyautogui
import psutil
import pygetwindow as gw
from pywinauto import Desktop as UiaDesktop

# ─────────────────────────────────────────────
# pyautogui defaults
# ─────────────────────────────────────────────
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
            "of discovering an installed OpenWaterApp.exe. Equivalent to setting "
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


def _from_source_mode() -> bool:
    """True when ``OPENWATER_FROM_SOURCE`` is set, i.e. launch via ``python main.py``."""
    return os.environ.get("OPENWATER_FROM_SOURCE", "").lower() in ("1", "true", "yes")


def _find_main_py() -> str:
    """Return path to main.py at the project root, or '' if missing."""
    p = PROJECT_ROOT / "main.py"
    return str(p) if p.exists() else ""


def _find_exe() -> str:
    """Locate the latest OpenWaterApp.exe, including pre-release builds.

    Collects all matches across every search pattern and returns the most
    recently modified file, so a newer pre-release build is always preferred
    over an older stable install.
    """
    env = os.environ.get("OPENWATER_EXE", "")
    if env and os.path.exists(env):
        return env
    patterns = [
        r"C:\Users\*\Documents\OpenMotion\**\OpenWaterApp.exe",
        r"C:\Users\*\Desktop\**\OpenWaterApp.exe",
        r"C:\Program Files\**\OpenWaterApp.exe",
        r"C:\Program Files (x86)\**\OpenWaterApp.exe",
    ]
    all_matches = []
    for pattern in patterns:
        all_matches.extend(_glob.glob(pattern, recursive=True))
    if all_matches:
        latest = max(all_matches, key=os.path.getmtime)
        log.info(f"  Found {len(all_matches)} OpenWaterApp.exe candidate(s) — using latest: {latest}")
        return latest
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "OpenWaterApp.exe")
    if os.path.exists(local):
        return local
    return ""


# ─────────────────────────────────────────────
# Window helpers
# ─────────────────────────────────────────────
def ensure_visible():
    """Bring the app window to the foreground."""
    for w in gw.getAllWindows():
        if any(k in w.title.lower() for k in APP_KEYWORDS):
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
            spec = desktop.window(title="OpenWater Bloodflow")
            if spec.exists(timeout=5):
                return spec
        except Exception as e:
            log.warning(f"  UIA exact-title lookup failed: {e}")
            # Fallback: match by keyword but require control_type=Window
            # to reduce ambiguity with File Explorer etc.
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
    """Return a pygetwindow Window object for the app."""
    for w in gw.getAllWindows():
        if any(k in w.title.lower() for k in APP_KEYWORDS):
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


def click_by_name(name: str):
    """Find a UI element by its visible label via UIA, then click its center."""
    ensure_visible()
    win = uia_window()
    log.info(f"  find by name: '{name}'")

    # Search entire tree first (finds disabled buttons too)
    try:
        matches = win.descendants(title=name)
        if matches:
            elem = matches[0]
            rect = elem.rectangle()
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            log.info(f"     found via descendants  center=({cx}, {cy})")
            pyautogui.moveTo(cx, cy, duration=0.3)
            pyautogui.click(cx, cy)
            time.sleep(SLEEP)
            return
    except Exception as e:
        log.warning(f"     descendants search failed: {e}")

    # Fallback: child_window per control type
    for ct in ["Button", "Custom", "Text", "Group", "ListItem", "Pane"]:
        try:
            elem = win.child_window(title=name, control_type=ct)
            if elem.exists(timeout=2):
                rect = elem.rectangle()
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                log.info(f"     found control_type='{ct}'  center=({cx}, {cy})")
                pyautogui.moveTo(cx, cy, duration=0.3)
                pyautogui.click(cx, cy)
                time.sleep(SLEEP)
                return
        except Exception:
            continue
    raise RuntimeError(f"Could not find '{name}' via UI Automation")


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
    """Launch or connect to the OpenWater app. Session-scoped — runs once.

    Set ``OPENWATER_FROM_SOURCE=1`` to run the in-tree dev branch via
    ``python main.py`` instead of discovering an installed ``OpenWaterApp.exe``.
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
        "OpenWaterApp.exe not found -- set OPENWATER_EXE, or set "
        "OPENWATER_FROM_SOURCE=1 to launch via python main.py"
    )
