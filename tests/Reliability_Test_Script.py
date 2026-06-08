"""
Reliability Test Script — overnight life test in Reduced Mode (FDA mode).

"""

import atexit
import getpass
import glob
import json
import logging
import os
import platform
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil
import pyautogui
import pygetwindow as gw
import pytest
from pywinauto import Desktop as UiaDesktop

import shelly


pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.5

log = logging.getLogger("hil_tests")

pytestmark = pytest.mark.release


# ─── Configuration ──────────────────────────────────────────────────
CYCLE_COUNT          = 12       # number of (5-scan + power-cycle) cycles
SCANS_PER_CYCLE      = 5
SCAN_DURATION_MIN    = 10
QUALITY_CHECK_TIMEOUT = 180     # max wait for "Good signal quality" modal
STOP_SETTLE_SEC      = 15       # wait after Stop for data to flush
POWER_CYCLE_OFF_SEC  = 5.0
RECONNECT_TIMEOUT    = 60
INTER_SCAN_PAUSE_SEC = 5

SCAN_DURATION_SEC = SCAN_DURATION_MIN * 60
SLEEP             = 2

# App window identification + paths.
APP_KEYWORDS = ("openmotion", "bloodflow", "openwater")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_CONFIG_PATH = PROJECT_ROOT / "config" / "app_config.json"

# Fallback Start-Scan button position (relative to the app window),
# used when UIA can't find the modal's button element.
_START_SCAN_BUTTON_REL = (0.58, 0.78)

# SDK logs one transition line per state change.
RE_CONNECTED = re.compile(r"state \S+ -> CONNECTED")

# Sidebar Start/Stop position (relative to the app window).
SIDEBAR_START = (0.019, 0.115)


# ─── Module state (populated by fixtures, consumed by the report) ───
_INITIAL_REDUCED_MODE: bool | None = None
_REPORT_SESSION_START: datetime | None = None
_REPORT_APP_VERSION:   str | None = None
_CYCLE_RESULTS: list[dict] = []


# ═══════════════════════════════════════════════════════════════════
# Window / UIA helpers (inlined so this script has no conftest dep)
# ═══════════════════════════════════════════════════════════════════
def get_app_window():
    """Return a pygetwindow Window object for the bloodflow app."""
    for w in gw.getAllWindows():
        if any(k in w.title.lower() for k in APP_KEYWORDS):
            return w
    raise RuntimeError("App window not found")


def is_app_alive() -> bool:
    """True if the app window still exists."""
    for w in gw.getAllWindows():
        if any(k in w.title.lower() for k in APP_KEYWORDS):
            return True
    return False


def ensure_visible() -> bool:
    """Bring the app window to the foreground. True if found."""
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


def require_focus() -> None:
    """Ensure the app window is foreground; fail if it can't be found."""
    if not ensure_visible():
        raise RuntimeError("App window not found — cannot ensure focus")


def move_window_on_screen() -> None:
    """Move the app window onto the primary screen if it is off-screen."""
    try:
        w = get_app_window()
        screen_w, screen_h = pyautogui.size()
        if w.left < 0 or w.top < 0 or w.left > screen_w or w.top > screen_h:
            log.warning(
                f"  Window is off-screen at ({w.left}, {w.top}) — "
                f"moving to primary display"
            )
            w.moveTo(50, 50)
            time.sleep(1)
            log.info(f"  Window moved to ({w.left}, {w.top})")
    except Exception as e:
        log.warning(f"  move_window_on_screen failed: {e}")


def uia_window(retries: int = 3):
    """Return the bloodflow app's UIA window spec."""
    for attempt in range(retries):
        ensure_visible()
        desktop = UiaDesktop(backend="uia")
        try:
            spec = desktop.window(title="OpenWater Bloodflow")
            if spec.exists(timeout=5):
                return spec
        except Exception:
            for kw in APP_KEYWORDS:
                try:
                    for win in desktop.windows(title_re=f"(?i).*{kw}.*"):
                        title = win.window_text()
                        if "File Explorer" in title or "Chrome" in title:
                            continue
                        if any(k in title.lower() for k in APP_KEYWORDS):
                            return desktop.window(title=title)
                except Exception:
                    continue
        if attempt < retries - 1:
            time.sleep(2)
    raise RuntimeError("App window not found via UI Automation")


def click_sidebar(rx: float, ry: float, label: str = "") -> None:
    """Click a sidebar button using window-relative coordinates."""
    ensure_visible()
    w = get_app_window()
    x = int(w.left + rx * w.width)
    y = int(w.top + ry * w.height)
    log.info(f"  click '{label}' rel({rx:.3f}, {ry:.3f}) abs({x}, {y})")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click(x, y)
    time.sleep(SLEEP)


# ═══════════════════════════════════════════════════════════════════
# App config helpers (read/write reducedMode)
# ═══════════════════════════════════════════════════════════════════
def read_app_config_value(key, default=None):
    try:
        with APP_CONFIG_PATH.open(encoding="utf-8") as fh:
            return json.load(fh).get(key, default)
    except Exception:
        return default


def write_app_config_value(key, value) -> None:
    try:
        with APP_CONFIG_PATH.open(encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg[key] = value
        with APP_CONFIG_PATH.open("w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception as e:
        log.warning(f"  Failed to persist {key}={value}: {e}")


def force_app_config_value(key, value):
    """Snapshot + force ``key=value``; return original so caller can restore."""
    initial = read_app_config_value(key)
    if initial != value:
        write_app_config_value(key, value)
        log.warning(
            f"  app_config.json {key} was {initial!r}; forced to {value!r} "
            f"for this run — relaunch the app if it was already running."
        )
    return initial


# ═══════════════════════════════════════════════════════════════════
# App-log tailing (used to verify console reconnect after power cycle)
# ═══════════════════════════════════════════════════════════════════
def find_app_log() -> Path | None:
    """Locate the most recently modified bloodflow app log."""
    home = Path.home()
    roots = [
        Path.cwd(),
        PROJECT_ROOT,
        home / "Documents" / "OpenWater Bloodflow",
        home / "Documents" / "OpenMotion",
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        candidates.extend(root.glob("**/app-logs/ow-bloodflowapp-*.log"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def wait_for_pattern(
    pattern: re.Pattern, log_path: Path, start_offset: int, timeout: float
) -> str | None:
    """Tail ``log_path`` from ``start_offset``; return first match within timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(start_offset)
                for line in f:
                    if pattern.search(line):
                        return line.strip()
        except OSError:
            pass
        time.sleep(0.5)
    return None


# ═══════════════════════════════════════════════════════════════════
# App version resolver (independent — uses psutil + .exe metadata)
# ═══════════════════════════════════════════════════════════════════
def _running_app_exe_path() -> str:
    """Path of the currently-running bloodflow process, or '' if none."""
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name in ("openwaterapp.exe", "openwaterapp_console.exe"):
                exe = proc.info.get("exe") or ""
                if exe and os.path.exists(exe):
                    return exe
        except Exception:
            continue
    return ""


def _find_installed_exe() -> str:
    """Find an installed OpenWaterApp.exe (newest by mtime)."""
    env = os.environ.get("OPENWATER_EXE", "")
    if env and os.path.exists(env):
        return env
    patterns = (
        r"C:\Users\*\Documents\OpenMotion\**\OpenWaterApp.exe",
        r"C:\Users\*\Desktop\**\OpenWaterApp.exe",
        r"C:\Program Files\**\OpenWaterApp.exe",
        r"C:\Program Files (x86)\**\OpenWaterApp.exe",
    )
    matches: list[str] = []
    for p in patterns:
        matches.extend(glob.glob(p, recursive=True))
    return max(matches, key=os.path.getmtime) if matches else ""


def resolve_app_version() -> str:
    """Resolve the version of the running/installed bloodflow app.
    """
    exe_path = _running_app_exe_path() or _find_installed_exe()
    if not exe_path:
        return os.environ.get("OPENWATER_VERSION", "unknown")

    # 1. ProductVersion / FileVersion from .exe metadata.
    try:
        ps_cmd = (
            f"$v = (Get-Item '{exe_path}').VersionInfo; "
            "Write-Output $v.FileVersion; "
            "Write-Output $v.ProductVersion; "
            "Write-Output $v.FileVersionRaw; "
            "Write-Output $v.ProductVersionRaw"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
        )
        version_re = re.compile(r"^\d+(\.\d+)+")
        for ln in (result.stdout or "").splitlines():
            c = ln.strip()
            if c and version_re.match(c):
                return c
    except Exception:
        pass

    # 2. Walk up the path looking for a folder name with a version pattern.
    folder_version_re = re.compile(
        r"\d+\.\d+(?:\.\d+)*(?:[-_+][A-Za-z0-9._+-]*)?"
    )
    for ancestor in Path(exe_path).parents:
        name = ancestor.name
        if name.lower() in ("users", ""):
            break
        if name.lower() in ("documents", "openmotion"):
            continue
        m = folder_version_re.search(name)
        if m:
            return m.group(0)

    return Path(exe_path).parent.name


# ═══════════════════════════════════════════════════════════════════
# Scan lifecycle helpers
# ═══════════════════════════════════════════════════════════════════
def _click_start_scan_button() -> bool:
    """Click the modal's 'Start Scan' button. UIA first, coord fallback."""
    try:
        for elem in uia_window().descendants(title="Start Scan", control_type="Button"):
            r = elem.rectangle()
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            if r.right > r.left and r.bottom > r.top and cx > 0 and cy > 0:
                log.info(f"  click 'Start Scan' at ({cx}, {cy})")
                pyautogui.moveTo(cx, cy, duration=0.15)
                time.sleep(0.15)
                pyautogui.click(cx, cy)
                return True
    except Exception as e:
        log.warning(f"  UIA Start Scan lookup failed: {e}")

    w = get_app_window()
    cx = int(w.left + _START_SCAN_BUTTON_REL[0] * w.width)
    cy = int(w.top + _START_SCAN_BUTTON_REL[1] * w.height)
    log.info(f"  coord-fallback 'Start Scan' at ({cx}, {cy})")
    pyautogui.moveTo(cx, cy, duration=0.15)
    time.sleep(0.15)
    pyautogui.click(cx, cy)
    return True


def _quality_dialog_visible() -> bool:
    """True if the 'Good signal quality' dialog is up and clickable."""
    try:
        win = uia_window()
        if win.descendants(title="Start Scan", control_type="Button"):
            return True
        for title in ("Good signal quality", "Dismiss", "Retest"):
            if win.descendants(title=title):
                return True
    except Exception:
        pass
    return False


def _wait_quality_then_start(timeout: int = QUALITY_CHECK_TIMEOUT) -> None:
    """Poll until the 'Good signal quality' dialog appears, then click Start Scan."""
    log.info(f"  waiting up to {timeout}s for signal quality dialog…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        if not is_app_alive():
            pytest.fail("APP CLOSED while waiting for signal-quality dialog.")
        if _quality_dialog_visible():
            time.sleep(0.5)             # let modal finish animating in
            move_window_on_screen()
            _click_start_scan_button()
            time.sleep(SLEEP)
            return
    pytest.fail(f"Signal-quality dialog did not appear within {timeout}s.")


def _sleep_with_alive_check(label: str, seconds: int) -> None:
    """Sleep ``seconds``, checking every 30 s that the app is still alive."""
    log.info(f"  {label}: scanning for {seconds}s ({seconds // 60} min)…")
    elapsed = 0
    while elapsed < seconds:
        nap = min(30, seconds - elapsed)
        time.sleep(nap)
        elapsed += nap
        if not is_app_alive():
            pytest.fail(f"APP CLOSED during {label} after {elapsed}s.")
        if elapsed % 60 == 0:
            log.info(f"  {label}: {elapsed}/{seconds}s")


def _run_single_scan(cycle_idx: int, scan_idx: int) -> None:
    """One scan: Start → quality check → Start Scan → 10 min → Stop."""
    label = f"cycle {cycle_idx}/{CYCLE_COUNT} scan {scan_idx}/{SCANS_PER_CYCLE}"
    log.info(f"─── ▶ {label} ───")

    move_window_on_screen()
    ensure_visible()
    require_focus()
    click_sidebar(*SIDEBAR_START, f"{label}: Start")

    _wait_quality_then_start()
    _sleep_with_alive_check(label, SCAN_DURATION_SEC)

    move_window_on_screen()
    ensure_visible()
    require_focus()
    log.info(f"  {label}: clicking Stop")
    click_sidebar(*SIDEBAR_START, f"{label}: Stop")
    time.sleep(STOP_SETTLE_SEC)
    pyautogui.press("escape")              # dismiss any post-scan modal
    time.sleep(SLEEP)
    log.info(f"  ✓ {label} done")


def _power_cycle_and_wait_reconnect(outlet, cycle_idx: int) -> None:
    """Power-cycle the console; wait for the app log's CONNECTED line."""
    log.info(f"  ⟲ cycle {cycle_idx}: power-cycling console")
    log_path = find_app_log()
    assert log_path, "Cannot find bloodflow app log."
    offset = log_size(log_path)
    outlet.power_cycle(off_time=POWER_CYCLE_OFF_SEC)
    line = wait_for_pattern(RE_CONNECTED, log_path, offset, RECONNECT_TIMEOUT)
    assert line, (
        f"Console did not reconnect within {RECONNECT_TIMEOUT}s after "
        f"cycle {cycle_idx} power cycle."
    )
    log.info(f"  ✓ reconnected: {line.strip()}")
    move_window_on_screen()


# ═══════════════════════════════════════════════════════════════════
# Report writer
# ═══════════════════════════════════════════════════════════════════
def _write_report() -> None:
    """Write Reliability_Test_Report_<ts>.json + .md to tests/test_logs/."""
    global _REPORT_APP_VERSION
    v = resolve_app_version()
    if v and v != "unknown":
        _REPORT_APP_VERSION = v
    app_version = _REPORT_APP_VERSION or "unknown"

    log_dir = Path(__file__).resolve().parent / "test_logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    env = {
        "tester":         os.environ.get("TESTER_NAME", getpass.getuser()),
        "hostname":       socket.gethostname(),
        "os":             f"{platform.system()} {platform.release()} ({platform.version()})",
        "python_version": sys.version.split()[0],
        "app_version":    app_version,
    }
    start = _REPORT_SESSION_START
    end   = datetime.now()
    duration = (end - start).total_seconds() if start else 0
    passed = sum(1 for r in _CYCLE_RESULTS if r["status"] == "PASS")
    failed = len(_CYCLE_RESULTS) - passed
    summary = {
        "cycles_planned":  CYCLE_COUNT,
        "cycles_run":      len(_CYCLE_RESULTS),
        "cycles_passed":   passed,
        "cycles_failed":   failed,
        "scans_per_cycle": SCANS_PER_CYCLE,
        "scans_completed": passed * SCANS_PER_CYCLE,
    }
    report = {
        "report_title":  "OpenWater BloodFlow — Reliability Test Report",
        "test_script":   Path(__file__).name,
        "session_start": start.isoformat(timespec="seconds") if start else "",
        "session_end":   end.isoformat(timespec="seconds"),
        "duration_sec":  round(duration, 1),
        "configuration": {
            "cycle_count":           CYCLE_COUNT,
            "scans_per_cycle":       SCANS_PER_CYCLE,
            "scan_duration_min":     SCAN_DURATION_MIN,
            "power_cycle_off_sec":   POWER_CYCLE_OFF_SEC,
            "quality_check_timeout": QUALITY_CHECK_TIMEOUT,
        },
        "environment":   env,
        "summary":       summary,
        "cycle_results": _CYCLE_RESULTS,
    }

    json_path = log_dir / f"Reliability_Test_Report_{ts}.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    rows = "\n".join(
        f"| {r['cycle']} | {r['scans_planned']} | {r['scan_duration_min']} | "
        f"**{r['status']}** | {r['finished_at']} |"
        for r in _CYCLE_RESULTS
    ) or "| _no cycles recorded_ |"
    md = f"""\
# {report['report_title']}

**Test Script:** `{report['test_script']}`

## Session

- **Session Start:** {report['session_start']}
- **Session End:**   {report['session_end']}
- **Duration:**      {report['duration_sec']} s ({report['duration_sec'] / 3600:.2f} h)

## Environment

- **Tester:** {env['tester']}
- **Hostname:** {env['hostname']}
- **OS:** {env['os']}
- **Python:** {env['python_version']}
- **App version:** {env['app_version']}

## Configuration

- **Cycles planned:** {CYCLE_COUNT}
- **Scans per cycle:** {SCANS_PER_CYCLE}
- **Scan duration:** {SCAN_DURATION_MIN} min
- **Power cycle off-time:** {POWER_CYCLE_OFF_SEC} s
- **Quality-check timeout:** {QUALITY_CHECK_TIMEOUT} s

## Summary

| Metric          | Count |
|-----------------|-------|
| Cycles planned  | {summary['cycles_planned']} |
| Cycles run      | {summary['cycles_run']} |
| Cycles passed   | {summary['cycles_passed']} |
| Cycles failed   | {summary['cycles_failed']} |
| Scans completed | {summary['scans_completed']} |

## Per-Cycle Results

| Cycle | Scans planned | Scan duration (min) | Status | Finished At |
|-------|---------------|---------------------|--------|-------------|
{rows}

## Sign-Off

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Tester | {env['tester']} | _______________ | _______________ |
| QA Reviewer | _______________ | _______________ | _______________ |
| Technical Lead | _______________ | _______________ | _______________ |

---
_Report generated automatically by `{Path(__file__).name}` on \
{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_
"""
    md_path = log_dir / f"Reliability_Test_Report_{ts}.md"
    md_path.write_text(md, encoding="utf-8")
    log.info(f"Reliability Test Report written: {json_path}")
    log.info(f"                                 {md_path}")


# ═══════════════════════════════════════════════════════════════════
# Module-level setup — force Reduced Mode before app launches
# ═══════════════════════════════════════════════════════════════════
# Pytest's ``app`` fixture (from conftest, when present) launches the
# app AFTER module import, so writing reducedMode=True here is enough
# for a fresh launch to boot into FDA mode. Restored on teardown.
_INITIAL_REDUCED_MODE = force_app_config_value("reducedMode", True)


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module", autouse=True)
def _restore_reduced_mode_on_module_teardown():
    yield
    write_app_config_value("reducedMode", _INITIAL_REDUCED_MODE)


@pytest.fixture(scope="module", autouse=True)
def _report_lifecycle():
    """Snapshot start time; register the atexit report writer."""
    global _REPORT_SESSION_START
    _REPORT_SESSION_START = datetime.now()
    log.info(f"Reliability report started at {_REPORT_SESSION_START}")
    atexit.register(_write_report)
    yield


@pytest.fixture(scope="module")
def outlet():
    """Resolve the Shelly outlet; skip the module if unreachable."""
    try:
        out = shelly.default_outlet()
        log.info(
            f"  Shelly outlet: host={getattr(out, 'host', '?')} "
            f"relay={'ON' if out.is_on() else 'OFF'}"
        )
    except Exception as e:
        pytest.skip(f"Shelly outlet not reachable: {e}")
    yield out
    try:
        out.on()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.incremental
class TestReliabilityTestScript:
    """Overnight reliability test — CYCLE_COUNT cycles of
    (SCANS_PER_CYCLE × 10-min scans + power cycle)."""

    @pytest.mark.parametrize(
        "cycle_idx",
        range(1, CYCLE_COUNT + 1),
        ids=[f"cycle-{i:02d}" for i in range(1, CYCLE_COUNT + 1)],
    )
    def test_cycle(self, outlet, app, cycle_idx):
        for scan_idx in range(1, SCANS_PER_CYCLE + 1):
            _run_single_scan(cycle_idx, scan_idx)
            if scan_idx < SCANS_PER_CYCLE:
                time.sleep(INTER_SCAN_PAUSE_SEC)

        if cycle_idx < CYCLE_COUNT:
            _power_cycle_and_wait_reconnect(outlet, cycle_idx)
        else:
            log.info(f"  ✓ last cycle ({cycle_idx}/{CYCLE_COUNT}) — done")

        _CYCLE_RESULTS.append({
            "cycle":             cycle_idx,
            "scans_planned":     SCANS_PER_CYCLE,
            "scan_duration_min": SCAN_DURATION_MIN,
            "status":            "PASS",
            "finished_at":       datetime.now().isoformat(timespec="seconds"),
        })
