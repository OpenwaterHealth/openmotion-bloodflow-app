"""
Shared fixtures and helpers for OpenWater BloodFlow UI tests.

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
            f"Bloodflow app died — first noticed by '{_app_dead_after}'. "
            f"Subsequent tests cannot run. Inspect the bloodflow app log "
            f"(app-logs/ow-bloodflowapp-*.log) around that test for an "
            f"unhandled Python exception."
        )

    if not ensure_visible():
        _app_dead_after = request.node.nodeid
        pytest.fail(
            f"Bloodflow app window is gone — likely crashed during the "
            f"previous test. See app-logs/ow-bloodflowapp-*.log for "
            f"diagnostics. (First detected at '{_app_dead_after}'.)"
        )
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
# (commits ba05bd0 + e3a017a in tests/test_reducedmode.py).

_REPORT_SESSION_START: datetime | None = None


def _report_get_app_version() -> str:
    """Best-effort guess at the bundled bloodflow build version.

    Reads it from the running OpenWaterApp.exe path (the build script
    lays the version into the parent-directory name). Falls back to
    ``$OPENWATER_VERSION`` if no app process is detected.
    """
    try:
        for proc_name in ("OpenWaterApp.exe", "OpenWaterApp_console.exe"):
            try:
                result = subprocess.run(
                    ["wmic", "process", "where", f"name='{proc_name}'",
                     "get", "ExecutablePath", "/value"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.splitlines():
                    if "ExecutablePath=" in line:
                        path = line.split("=", 1)[1].strip()
                        if path:
                            return Path(path).parent.name
            except Exception:
                continue
    except Exception:
        pass
    return os.environ.get("OPENWATER_VERSION", "unknown")


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
        "report_title": "OpenWater BloodFlow — HIL Test Session Report",
        "purpose":      "Verification & validation evidence for the HIL "
                        "test suite.",
        "session_start": _REPORT_SESSION_START.isoformat(timespec="seconds")
                         if _REPORT_SESSION_START else "",
        "session_end":   datetime.now().isoformat(timespec="seconds"),
        "duration_sec":  round(duration, 1),
        "environment":   env,
        "summary":       summary,
        "test_results":  results,
    }
    json_path = log_dir / f"HIL_Report_{ts}.json"
    json_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")

    # ── Markdown ──
    lines = [
        f"# {report_data['report_title']}",
        "",
        f"**Purpose:** {report_data['purpose']}",
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
    md_path = log_dir / f"HIL_Report_{ts}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    log.info("HIL report written:")
    log.info(f"  JSON:     {json_path}")
    log.info(f"  Markdown: {md_path}")


@pytest.fixture(scope="session", autouse=True)
def _hil_report_session():
    """Capture session start time and arm the atexit-registered writer.

    Autouse + session-scope so it runs once for every pytest invocation
    that touches this conftest, with no per-test setup/teardown.
    """
    global _REPORT_SESSION_START
    _REPORT_SESSION_START = datetime.now()
    log.info(f"HIL report session started at {_REPORT_SESSION_START}")
    atexit.register(_write_hil_report)
    yield
