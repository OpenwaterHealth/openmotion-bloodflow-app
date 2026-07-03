"""
Reliability Test Script — overnight life test in clinical (FDA) mode.

The script runs the app exactly as configured on the host. Set the desired
mode before launching, e.g. ``"clinicalMode": true`` in the writable
overrides file ``%PROGRAMDATA%\\Openwater\\app_config.local.json`` (the
packaged app's bundled config in Program Files is read-only). A module
fixture logs the effective mode and warns when clinicalMode is off.
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
CYCLE_COUNT          = 60       # number of (5-scan + power-cycle) cycles
SCANS_PER_CYCLE      = 5
SCAN_DURATION_MIN    = 10
QUALITY_CHECK_TIMEOUT = 180     # max wait for "Good signal quality" modal
START_RETRY_SEC      = 60       # re-click Start if no dialog after this long
STOP_SETTLE_SEC      = 15       # extra settle after teardown completes
SCAN_TEARDOWN_TIMEOUT = 90      # max wait for "Full scan ended" after Stop
POWER_CYCLE_OFF_SEC  = 5.0
RECONNECT_TIMEOUT    = 60
RECONNECT_SETTLE_SEC = 30       # after CONNECTED: let sensors re-enumerate
INTER_SCAN_PAUSE_SEC = 5

SCAN_DURATION_SEC = SCAN_DURATION_MIN * 60
SLEEP             = 2

# App window identification + paths. The packaged 1.4.x window is titled
# "Open-Motion" (clinical) or "Open-Motion Research".
APP_KEYWORDS = ("open-motion", "openmotion", "bloodflow", "openwater")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_CONFIG_PATH = PROJECT_ROOT / "config" / "app_config.json"

# Fallback Start-Scan button position (relative to the app window),
# used when UIA can't find the modal's button element.
_START_SCAN_BUTTON_REL = (0.58, 0.78)

# SDK logs one transition line per state change.
RE_CONNECTED = re.compile(r"state \S+ -> CONNECTED")

# Connector logs this once scan teardown (flush + post-processing) is done.
RE_SCAN_ENDED = re.compile(r"Full scan ended")

# Sidebar Start/Stop position (relative to the app window).
SIDEBAR_START = (0.019, 0.115)


# ─── Module state (populated by fixtures, consumed by the report) ───
_REPORT_SESSION_START: datetime | None = None
_REPORT_APP_VERSION:   str | None = None
_CYCLE_RESULTS: list[dict] = []


# ═══════════════════════════════════════════════════════════════════
# Window / UIA helpers (inlined so this script has no conftest dep)
# ═══════════════════════════════════════════════════════════════════
# Title keywords false-positive on a File Explorer window sitting at the
# repo folder or a browser tab with the repo open, so a matching window
# is accepted only when its owning process is the app itself.
_APP_PROCESS_PREFIXES = ("open-motion", "openwaterapp")


def _is_app_window(w) -> bool:
    """True if window ``w`` is the bloodflow app (title + owning process)."""
    title = (w.title or "").strip().lower()
    if not title or not any(k in title for k in APP_KEYWORDS):
        return False
    hwnd = getattr(w, "_hWnd", None)
    if hwnd is None:
        return True
    try:
        import ctypes
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        proc = psutil.Process(pid.value)
        name = proc.name().lower()
    except Exception:
        return True          # PID lookup failed — fall back to title match
    if name.startswith(_APP_PROCESS_PREFIXES):
        return True
    if name in ("python.exe", "pythonw.exe"):   # from-source launch
        try:
            return "main.py" in " ".join(proc.cmdline()).lower()
        except Exception:
            return False
    return False


def get_app_window():
    """Return a pygetwindow Window object for the bloodflow app."""
    for w in gw.getAllWindows():
        if _is_app_window(w):
            return w
    raise RuntimeError("App window not found")


def is_app_alive() -> bool:
    """True if the app window still exists."""
    for w in gw.getAllWindows():
        if _is_app_window(w):
            return True
    return False


def ensure_visible() -> bool:
    """Bring the app window to the foreground. True if found."""
    for w in gw.getAllWindows():
        if _is_app_window(w):
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


# Exact window titles by build variant: clinical, research, legacy.
_UIA_TITLES = ("Open-Motion", "Open-Motion Research", "OpenWater Bloodflow")


def uia_window(retries: int = 3):
    """Return the bloodflow app's UIA window spec."""
    for attempt in range(retries):
        ensure_visible()
        desktop = UiaDesktop(backend="uia")
        for exact in _UIA_TITLES:
            try:
                spec = desktop.window(title=exact)
                if spec.exists(timeout=2):
                    return spec
            except Exception:
                pass
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
# App config helpers (read-only — the operator sets the mode)
# ═══════════════════════════════════════════════════════════════════
# The packaged app layers %PROGRAMDATA%\Openwater\app_config.local.json
# over its read-only bundled config (see utils/config_store.py). This
# script never writes either file: put the desired mode in the overrides
# file before starting the run.
def _writable_root() -> Path:
    """The app's writable data root (config overrides, logs, scan data)."""
    env = os.environ.get("OPENWATER_DATA_ROOT")
    if env:
        return Path(env)
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Openwater"


def _merged_app_config() -> dict:
    """Best-effort view of the config the app reads: the installed exe's
    bundled config (repo config as fallback) + writable overrides."""
    cfg: dict = {}
    exe = _running_app_exe_path() or _find_installed_exe()
    if exe:
        bundled = Path(exe).parent / "_internal" / "config" / "app_config.json"
        try:
            cfg.update(json.loads(bundled.read_text(encoding="utf-8")))
        except Exception:
            pass
    if not cfg:
        try:
            cfg.update(json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    try:
        overrides = _writable_root() / "app_config.local.json"
        cfg.update(json.loads(overrides.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


# ═══════════════════════════════════════════════════════════════════
# App-log tailing (used to verify console reconnect after power cycle)
# ═══════════════════════════════════════════════════════════════════
def find_app_log() -> Path | None:
    """Locate the most recently modified bloodflow app log.

    The installed 1.4.x app writes ``<writable-root>/logs/open-motion-*.log``
    where the writable root is %PROGRAMDATA%\\Openwater (or
    $OPENWATER_DATA_ROOT). Older layouts are kept as fallbacks.
    """
    home = Path.home()
    roots = [
        _writable_root(),
        Path.cwd(),
        PROJECT_ROOT,
        home / "Documents" / "Open-Motion",
        home / "Documents" / "OpenWater Bloodflow",
        home / "Documents" / "OpenMotion",
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        candidates.extend(root.glob("logs/open-motion-*.log"))
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
            if name in (
                "open-motion.exe", "open-motion_console.exe",
                "openwaterapp.exe", "openwaterapp_console.exe",
            ):
                exe = proc.info.get("exe") or ""
                if exe and os.path.exists(exe):
                    return exe
        except Exception:
            continue
    return ""


def _find_installed_exe() -> str:
    """Find an installed Open-Motion.exe / legacy OpenWaterApp.exe
    (newest by mtime)."""
    env = os.environ.get("OPENWATER_EXE", "")
    if env and os.path.exists(env):
        return env
    patterns = (
        r"C:\Program Files (x86)\Openwater\**\Open-Motion.exe",
        r"C:\Program Files\Openwater\**\Open-Motion.exe",
        r"C:\Users\*\Documents\OpenMotion\**\Open-Motion.exe",
        r"C:\Users\*\Desktop\**\Open-Motion.exe",
        r"C:\Users\*\Documents\OpenMotion\**\OpenWaterApp.exe",
        r"C:\Users\*\Desktop\**\OpenWaterApp.exe",
        r"C:\Program Files\**\OpenWaterApp.exe",
        r"C:\Program Files (x86)\**\OpenWaterApp.exe",
    )
    matches: list[str] = []
    for p in patterns:
        matches.extend(glob.glob(p, recursive=True))
    return max(matches, key=os.path.getmtime) if matches else ""


def _registry_display_version() -> str:
    """DisplayVersion of the installed Open-Motion app from the Windows
    uninstall registry ('' when not found / not on Windows)."""
    try:
        import winreg
    except ImportError:
        return ""
    for root_key in (
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    ):
        try:
            hive = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_key)
        except OSError:
            continue
        with hive:
            for i in range(winreg.QueryInfoKey(hive)[0]):
                try:
                    with winreg.OpenKey(hive, winreg.EnumKey(hive, i)) as k:
                        name = str(winreg.QueryValueEx(k, "DisplayName")[0])
                        if "open-motion" not in name.lower():
                            continue
                        ver = str(winreg.QueryValueEx(k, "DisplayVersion")[0]).strip()
                        if ver:
                            return ver
                except OSError:
                    continue
    return ""


def resolve_app_version() -> str:
    """Resolve the version of the running/installed bloodflow app.
    """
    # 0. The installer's registry entry — the packaged exe itself ships
    #    without VersionInfo metadata, so this is the most reliable source.
    reg_version = _registry_display_version()
    if reg_version:
        return reg_version

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


def _wait_quality_then_start(
    label: str = "", timeout: int = QUALITY_CHECK_TIMEOUT
) -> None:
    """Poll until the 'Good signal quality' dialog appears, then click Start Scan.

    If the dialog hasn't shown after START_RETRY_SEC, re-click the sidebar
    Start button: the app silently swallows a Start click that lands while
    the previous scan is still tearing down (or right after a power cycle),
    and without a retry that swallowed click strands the whole run. The
    dialog appears ~8 s after an accepted Start, so a 60 s retry interval
    can't interrupt an in-flight quality check.
    """
    log.info(f"  waiting up to {timeout}s for signal quality dialog…")
    deadline = time.time() + timeout
    last_start_click = time.time()
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
        if time.time() - last_start_click >= START_RETRY_SEC:
            log.warning(
                f"  no signal-quality dialog {START_RETRY_SEC}s after Start "
                f"— the click may have been swallowed; re-clicking Start"
            )
            ensure_visible()
            click_sidebar(*SIDEBAR_START, f"{label}: Start (retry)")
            last_start_click = time.time()
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

    _wait_quality_then_start(label)
    _sleep_with_alive_check(label, SCAN_DURATION_SEC)

    move_window_on_screen()
    ensure_visible()
    require_focus()
    log.info(f"  {label}: clicking Stop")
    # Scan teardown (USB flush, pipeline post-processing, DB save) takes
    # ~30 s after the Stop click; a Start clicked before the app logs
    # "Full scan ended" is silently swallowed. Tail the app log so the
    # next scan waits exactly as long as teardown actually takes.
    log_path = find_app_log()
    stop_offset = log_size(log_path) if log_path else 0
    click_sidebar(*SIDEBAR_START, f"{label}: Stop")
    if log_path:
        ended = wait_for_pattern(
            RE_SCAN_ENDED, log_path, stop_offset, SCAN_TEARDOWN_TIMEOUT
        )
        if ended:
            log.info(f"  {label}: teardown complete: {ended}")
        else:
            log.warning(
                f"  {label}: no 'Full scan ended' in app log within "
                f"{SCAN_TEARDOWN_TIMEOUT}s — continuing anyway"
            )
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
    log.info(f"  settling {RECONNECT_SETTLE_SEC}s for sensors to re-enumerate")
    time.sleep(RECONNECT_SETTLE_SEC)
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

    cfg = _merged_app_config()
    env = {
        "tester":         os.environ.get("TESTER_NAME", getpass.getuser()),
        "hostname":       socket.gethostname(),
        "os":             f"{platform.system()} {platform.release()} ({platform.version()})",
        "python_version": sys.version.split()[0],
        "app_version":    app_version,
        "clinical_mode":  cfg.get("clinicalMode"),
        "engineering_mode": cfg.get("engineeringMode"),
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
- **Clinical mode:** {env['clinical_mode']} (engineeringMode={env['engineering_mode']})

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
# Fixtures
# ═══════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module", autouse=True)
def _log_app_mode():
    """Log the effective app mode; warn when clinicalMode is off.

    The script must not write any config file (the bundled config is
    read-only under Program Files, and import-time writes to the repo
    config abort collection — see conftest). The operator sets the mode
    in app_config.local.json before the run instead.
    """
    cfg = _merged_app_config()
    clinical = cfg.get("clinicalMode")
    engineering = cfg.get("engineeringMode")
    log.info(f"App mode: clinicalMode={clinical} engineeringMode={engineering}")
    if clinical is not True:
        log.warning(
            "clinicalMode is not enabled — this reliability run is meant to "
            "exercise clinical (FDA) mode. Set \"clinicalMode\": true in "
            f"{_writable_root() / 'app_config.local.json'} and restart."
        )
    yield


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
