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
CYCLE_COUNT          = 724       # number of (5-scan + power-cycle) cycles
SCANS_PER_CYCLE      = 5
SCAN_DURATION_MIN    = 10
QUALITY_CHECK_TIMEOUT = 180     # max wait for "Good signal quality" modal
START_RETRY_SEC      = 60       # re-click Start if no dialog after this long
STOP_SETTLE_SEC      = 15       # extra settle after teardown completes
# Under USB stress the app can take ~2 min to register Stop and flush;
# observed 122 s on a stressed scan. Wait comfortably past that so the
# next Start lands after teardown instead of falling through to the retry.
SCAN_TEARDOWN_TIMEOUT = 240     # max wait for "Full scan ended" after Stop
POWER_CYCLE_OFF_SEC  = 5.0
RECONNECT_TIMEOUT    = 60
RECONNECT_SETTLE_SEC = 30       # after CONNECTED: let sensors re-enumerate
INTER_SCAN_PAUSE_SEC = 5

# Per-scan integrity gates catch ACUTE faults — a dead sensor side, a scan
# where a large slice of data was fabricated. A cycle tripping one is
# recorded FAIL and the run continues. Set a threshold to 0 to disable.
#
# Thresholds calibrated against the 2026-06/07 campaign: on a healthy run
# affected scans sat at 8 NaN-filled frames; on the 14-day run the p99 was
# 84 with two outliers in the tens of thousands. 100 therefore flags the
# genuinely broken scan without firing on ordinary repair.
FAIL_ON_CONTACT_QUALITY = True   # any camera reporting no_signal/poor_contact
MAX_NAN_FILLED_FRAMES   = 100    # per scan, summed across the scan summaries
MAX_HISTO_QUEUE_FULL    = 50     # per scan, firmware histo-queue overflows
# Mid-scan contact loss (the SDK live monitor, #364). Contact is allowed to
# drop and come back — the monitor emits RAISED then CLEARED, and a brief
# loss is not a scan failure. What fails a scan is contact that stays lost:
# if any camera is still poor after CQ_RECOVERY_TIMEOUT the scan is stopped.
CQ_RECOVERY_TIMEOUT     = 180    # s to wait for contact to come back
CQ_RECOVERY_POLL_SEC    = 5      # tighter polling while waiting to recover
# Optional churn gate: fail even when every loss recovered, if there were
# more than N of them in one scan. -1 disables (the default — recovery is
# the thing that matters). Set to 0 to fail on any loss at all.
MAX_LIVE_CQ_RAISES      = -1     # per scan; -1 disables the gate

# Per-scan gates alone do NOT catch slow degradation: over the 14-day run the
# share of scans needing repair went 3% -> 100% while the median stayed at 28
# frames, so every individual scan passed. The trend check below compares the
# first and last tenth of the run and is what actually surfaces that.
DEGRADATION_FACTOR     = 3.0     # last-decile / first-decile rate that flags
DEGRADATION_MIN_CYCLES = 20      # below this the comparison is meaningless

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

# Fallback 'Continue' position on the mid-scan Contact Quality Notification
# modal, measured from a 1.5.0-dev.5 clinical window.
_CQ_CONTINUE_BUTTON_REL = (0.538, 0.699)

# SDK logs one transition line per state change.
RE_CONNECTED = re.compile(r"state \S+ -> CONNECTED")

# Connector logs this once scan teardown (flush + post-processing) is done.
RE_SCAN_ENDED = re.compile(r"Full scan ended")

# The connector logs the pre-scan contact-quality verdict as a per-camera
# table, every row at INFO — including failures. A dark camera reads
# `no_signal`, one under the light threshold reads `poor_contact`. Nothing
# escalates, so the only way this run can notice is to parse the table.
RE_CQ_ROW = re.compile(r"\|\s*([LR]\d)\s*\|.*?\|\s*(ok|no_signal|poor_contact)\s*\|")

# Contact loss *during* a scan is reported by the SDK's live monitor
# (bloodflow-app#364) in a completely different format from the pre-scan
# table above — parsing only the table misses every mid-scan event, which is
# the case that matters most clinically.
RE_CQ_LIVE_RAISE = re.compile(r"live CQ ([LR]\d): poor_contact RAISED \(([-\d.]+) DN\)")
# Both edges, in order, so the run can tell "lost and came back" from
# "still lost". The monitor CLEARs at ~2 s of good frames (#364).
RE_CQ_LIVE_EVENT = re.compile(
    r"live CQ ([LR]\d): poor_contact (RAISED|CLEARED) \(([-\d.]+) DN\)"
)

# Pipeline integrity. Frames the firmware never delivered get NaN-filled by
# the timestamp-repair stage; the firmware reports a full histogram queue
# when the host-side drain stalls. Both climb with uptime.
RE_NAN_FILL   = re.compile(r"Scan summary:.*?(\d+) frames NaN-filled")
RE_HISTO_FULL = re.compile(r"HISTO enqueue fail: queue full")

# Sidebar Start/Stop position (relative to the app window).
SIDEBAR_START = (0.019, 0.115)


# ─── Module state (populated by fixtures, consumed by the report) ───
_REPORT_SESSION_START: datetime | None = None
_REPORT_APP_VERSION:   str | None = None
_CYCLE_RESULTS: list[dict] = []
# Set when the bench is no longer usable (app gone). Remaining cycles are
# skipped rather than reported as failures they never got to attempt.
_RUN_ABORTED: str | None = None


class ScanFailure(Exception):
    """A cycle failed but the bench is still usable — record FAIL, keep going.

    Everything that used to call ``pytest.fail`` mid-cycle raises this
    instead. ``pytest.fail`` aborted the entire run, which is how a single
    smart-plug timeout at cycle 42 discarded the remaining 380 cycles.

    ``metrics`` carries the failing scan's observed counters so the report
    can show *why* alongside *how much* — without it a cycle reads
    "FAIL: mid-scan contact loss x4" while its own count column shows 0.
    """

    def __init__(self, message: str, metrics: dict | None = None):
        super().__init__(message)
        self.metrics = metrics or {}


class FatalFailure(Exception):
    """The bench is unusable (app window gone) — record FAIL and stop."""

    def __init__(self, message: str, metrics: dict | None = None):
        super().__init__(message)
        self.metrics = metrics or {}


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
    """The app's writable data root (config overrides, logs, scan data).

    Mirrors utils/app_paths.writable_root for an installed build. The app
    reads no env vars (the old OPENWATER_DATA_ROOT override is gone), so
    this must not honour one either.
    """
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
    """Locate the log written by the app instance under test.

    An installed build writes ``<writable-root>/logs/open-motion-*.log``
    (%PROGRAMDATA%\\Openwater). A **portable** build
    writes next to its own exe instead — e.g.
    ``Documents/OpenMotion/Open-Motion-1.5.0-dev.5/Open-Motion/logs/`` — which
    is several levels below any root below, so the old one-level
    ``logs/open-motion-*.log`` glob missed it entirely and this fell back to
    a stale log from an unrelated launch. The running exe's own directory is
    therefore checked first and wins outright when it has a log.
    """
    exe = _running_app_exe_path()
    if exe:
        exe_logs = sorted(
            Path(exe).parent.glob("logs/open-motion-*.log"),
            key=lambda p: p.stat().st_mtime,
        )
        if exe_logs:
            return exe_logs[-1]

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
        candidates.extend(root.glob("**/logs/open-motion-*.log"))
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


def read_log_slice(log_path: Path | None, start_offset: int) -> str:
    """Everything written to ``log_path`` since ``start_offset``."""
    if not log_path:
        return ""
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(start_offset)
            return f.read()
    except OSError:
        return ""


def contact_quality_failures(slice_text: str) -> list[str]:
    """Cameras the pre-scan contact-quality check flagged, e.g. ``["R1=no_signal"]``.

    Deliberately reports every flagged camera rather than a count: a whole
    side reading ``no_signal`` is a different fault from one marginal camera.
    """
    return [
        f"{cam}={reason}"
        for cam, reason in RE_CQ_ROW.findall(slice_text)
        if reason != "ok"
    ]


def scan_metrics(
    scan_idx: int, slice_text: str, completed: bool
) -> dict:
    """Observed counters for one scan, from its slice of the app log.

    Built the same way whether the scan passed or failed, so a failing cycle
    reports real numbers instead of zeros.
    """
    nan_filled, histo_full = integrity_counts(slice_text)
    return {
        "scan":             scan_idx,
        "completed":        completed,
        "nan_filled":       nan_filled,
        "histo_queue_full": histo_full,
        "cq_flagged":       contact_quality_failures(slice_text),
        "cq_live_raises":   live_cq_raises(slice_text),
        "cq_unresolved":    cameras_currently_poor(slice_text),
    }


def live_cq_raises(slice_text: str) -> list[str]:
    """Mid-scan contact-quality RAISE events, e.g. ``["R7@1.99DN"]``.

    Separate from ``contact_quality_failures`` because the two come from
    different subsystems: the pre-scan check gates scan start, the live
    monitor watches the scan in flight. A scan can pass one and fail the
    other, and the run needs to notice either.
    """
    return [f"{cam}@{dn}DN" for cam, dn in RE_CQ_LIVE_RAISE.findall(slice_text)]


def cameras_currently_poor(slice_text: str) -> list[str]:
    """Cameras whose most recent live event was RAISED, i.e. still bad.

    A camera that raised and later cleared is not listed — that is a
    recovered transient, not a failure.
    """
    state: dict[str, str] = {}
    for cam, edge, _dn in RE_CQ_LIVE_EVENT.findall(slice_text):
        state[cam] = edge
    return sorted(cam for cam, edge in state.items() if edge == "RAISED")


def integrity_counts(slice_text: str) -> tuple[int, int]:
    """``(frames NaN-filled, histo queue-full events)`` for one scan."""
    nan_filled = sum(int(n) for n in RE_NAN_FILL.findall(slice_text))
    histo_full = len(RE_HISTO_FULL.findall(slice_text))
    return nan_filled, histo_full


def degradation_check(results: list[dict]) -> dict:
    """Compare the first and last tenth of the run for a worsening trend.

    Slow degradation is invisible to per-scan gates — the campaign that
    motivated this ran 380 cycles while frame repair climbed from 3% to 100%
    of scans and every individual scan stayed under threshold. Comparing the
    ends of the run is what makes that visible.
    """
    n = len(results)
    if n < DEGRADATION_MIN_CYCLES:
        return {"checked": False, "reason": f"only {n} cycles (need {DEGRADATION_MIN_CYCLES})"}

    span = max(1, n // 10)
    first, last = results[:span], results[-span:]

    def rate(rows: list[dict], key: str) -> float:
        return sum(r.get(key, 0) for r in rows) / len(rows)

    out: dict = {"checked": True, "cycles_compared": span, "degraded": False, "metrics": {}}
    for key in ("nan_filled", "histo_queue_full"):
        f, l = rate(first, key), rate(last, key)
        # A near-zero baseline is normal and healthy; treat it as 1 so the
        # ratio reflects real growth instead of dividing by ~0.
        ratio = l / max(f, 1.0)
        degraded = ratio >= DEGRADATION_FACTOR and l > 1.0
        out["metrics"][key] = {
            "first_decile_per_cycle": round(f, 2),
            "last_decile_per_cycle":  round(l, 2),
            "ratio":                  round(ratio, 1),
            "degraded":               degraded,
        }
        out["degraded"] = out["degraded"] or degraded
    return out


def _degradation_md(deg: dict) -> str:
    """Render the degradation check as a markdown block for the report."""
    if not deg.get("checked"):
        return f"_Not evaluated — {deg.get('reason', 'insufficient data')}._"

    rows = "\n".join(
        f"| {key} | {m['first_decile_per_cycle']} | {m['last_decile_per_cycle']} | "
        f"{m['ratio']}x | {'**YES**' if m['degraded'] else 'no'} |"
        for key, m in deg["metrics"].items()
    )
    verdict = (
        "**DEGRADATION DETECTED** — the run got measurably worse over time. "
        "Per-cycle results may all be PASS; that does not mean the system was "
        "stable across the run."
        if deg["degraded"] else
        "No significant trend between the start and end of the run."
    )
    return f"""Comparing the first and last {deg['cycles_compared']} cycle(s):

| Metric | First decile / cycle | Last decile / cycle | Ratio | Degraded |
|--------|---------------------|---------------------|-------|----------|
{rows}

{verdict}"""


def evaluate_scan(slice_text: str, scan_ended: bool) -> list[str]:
    """Reasons this scan should be considered failed. Empty list = pass."""
    reasons: list[str] = []

    if not scan_ended:
        reasons.append(
            f"no 'Full scan ended' within {SCAN_TEARDOWN_TIMEOUT}s of Stop"
        )

    if FAIL_ON_CONTACT_QUALITY:
        flagged = contact_quality_failures(slice_text)
        if flagged:
            reasons.append(f"pre-scan contact quality: {', '.join(flagged)}")

    # Contact still down when the scan ended — never recovered.
    unresolved = cameras_currently_poor(slice_text)
    if unresolved:
        reasons.append(f"contact still poor at scan end: {', '.join(unresolved)}")

    # Optional churn gate: every loss recovered, but there were too many.
    if MAX_LIVE_CQ_RAISES >= 0:
        raises = live_cq_raises(slice_text)
        if len(raises) > MAX_LIVE_CQ_RAISES:
            reasons.append(
                f"mid-scan contact loss x{len(raises)} (limit "
                f"{MAX_LIVE_CQ_RAISES}): {', '.join(raises[:6])}"
                + (" …" if len(raises) > 6 else "")
            )

    nan_filled, histo_full = integrity_counts(slice_text)
    if MAX_NAN_FILLED_FRAMES and nan_filled > MAX_NAN_FILLED_FRAMES:
        reasons.append(
            f"{nan_filled} frames NaN-filled (limit {MAX_NAN_FILLED_FRAMES})"
        )
    if MAX_HISTO_QUEUE_FULL and histo_full > MAX_HISTO_QUEUE_FULL:
        reasons.append(
            f"{histo_full} histo queue-full events (limit {MAX_HISTO_QUEUE_FULL})"
        )
    return reasons


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


def _cq_notification_visible() -> bool:
    """True if the mid-scan Contact Quality Notification modal is up."""
    try:
        win = uia_window()
        if win.descendants(title="Contact Quality Notification"):
            return True
        # The pre-scan modal has 'Start Scan'; only the mid-scan one pairs
        # 'Continue' with 'Stop scan'.
        return bool(
            win.descendants(title="Continue", control_type="Button")
            and win.descendants(title="Stop scan", control_type="Button")
        )
    except Exception:
        return False


def _dismiss_cq_notification(label: str = "") -> bool:
    """Click 'Continue' on the mid-scan contact-quality modal.

    The modal does not close itself when contact comes back — it switches to
    "All contact quality issues are currently inactive. You may dismiss." and
    waits. Left up it covers the plots and sits in front of the sidebar, so
    the next Stop click lands on the modal instead of the sidebar button.

    Only ever clicks 'Continue'. 'Stop scan' is right next to it and would
    end the scan, so the UIA lookup is title-exact and the coordinate
    fallback is only used when the modal is confirmed present.
    """
    if not _cq_notification_visible():
        return False
    try:
        for elem in uia_window().descendants(title="Continue", control_type="Button"):
            r = elem.rectangle()
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            if r.right > r.left and r.bottom > r.top and cx > 0 and cy > 0:
                log.info(f"  {label}: clicking 'Continue' on CQ modal at ({cx}, {cy})")
                pyautogui.moveTo(cx, cy, duration=0.15)
                time.sleep(0.15)
                pyautogui.click(cx, cy)
                time.sleep(SLEEP)
                return True
    except Exception as e:
        log.warning(f"  {label}: UIA Continue lookup failed: {e}")

    try:
        w = get_app_window()
        cx = int(w.left + _CQ_CONTINUE_BUTTON_REL[0] * w.width)
        cy = int(w.top + _CQ_CONTINUE_BUTTON_REL[1] * w.height)
        log.info(f"  {label}: coord-fallback 'Continue' at ({cx}, {cy})")
        pyautogui.moveTo(cx, cy, duration=0.15)
        time.sleep(0.15)
        pyautogui.click(cx, cy)
        time.sleep(SLEEP)
        return True
    except Exception as e:
        log.warning(f"  {label}: could not dismiss CQ modal ({e}) — continuing")
    return False


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
    label: str = "", timeout: int = QUALITY_CHECK_TIMEOUT,
    log_path: Path | None = None, scan_offset: int = 0,
) -> None:
    """Poll until the 'Good signal quality' dialog appears, then click Start Scan.

    If the dialog hasn't shown after START_RETRY_SEC, re-click the sidebar
    Start button: the app silently swallows a Start click that lands while
    the previous scan is still tearing down (or right after a power cycle),
    and without a retry that swallowed click strands the whole run. The
    dialog appears ~8 s after an accepted Start, so a 60 s retry interval
    can't interrupt an in-flight quality check.

    The contact-quality verdict is already in the app log by the time the
    dialog is up, so it is checked *here* — before Start Scan is clicked.
    Checking it after the scan instead would mean sitting through a full
    10-minute scan already known to be running on a bad sensor.
    """
    log.info(f"  waiting up to {timeout}s for signal quality dialog…")
    deadline = time.time() + timeout
    last_start_click = time.time()
    while time.time() < deadline:
        time.sleep(5)
        if not is_app_alive():
            raise FatalFailure("APP CLOSED while waiting for signal-quality dialog.")
        if _quality_dialog_visible():
            time.sleep(0.5)             # let modal finish animating in

            if FAIL_ON_CONTACT_QUALITY:
                flagged = contact_quality_failures(
                    read_log_slice(log_path, scan_offset)
                )
                if flagged:
                    # Dismiss the modal so the app is left idle, not mid-scan.
                    pyautogui.press("escape")
                    time.sleep(SLEEP)
                    raise ScanFailure(
                        f"{label}: contact quality: {', '.join(flagged)}"
                    )

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
    raise ScanFailure(f"Signal-quality dialog did not appear within {timeout}s.")


def _stop_scan_quietly(label: str) -> None:
    """Click Stop and dismiss any modal, so a failed scan leaves the app idle.

    Best-effort: an abort path must not mask the real failure with its own.
    """
    try:
        move_window_on_screen()
        ensure_visible()
        require_focus()
        # If the contact-quality modal is up it covers the sidebar, and it
        # carries its own 'Stop scan' — use that rather than clicking through.
        if _cq_notification_visible():
            try:
                for elem in uia_window().descendants(
                    title="Stop scan", control_type="Button"
                ):
                    r = elem.rectangle()
                    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                    if r.right > r.left and r.bottom > r.top:
                        log.info(f"  {label}: 'Stop scan' on CQ modal ({cx}, {cy})")
                        pyautogui.moveTo(cx, cy, duration=0.15)
                        pyautogui.click(cx, cy)
                        time.sleep(STOP_SETTLE_SEC)
                        pyautogui.press("escape")
                        time.sleep(SLEEP)
                        return
            except Exception as e:
                log.warning(f"  {label}: modal Stop lookup failed ({e})")
        click_sidebar(*SIDEBAR_START, f"{label}: Stop (abort)")
        time.sleep(STOP_SETTLE_SEC)
        pyautogui.press("escape")
        time.sleep(SLEEP)
    except Exception as exc:                      # noqa: BLE001
        log.warning(f"  {label}: abort-stop failed ({exc}) — continuing")


def _sleep_with_alive_check(
    label: str, seconds: int,
    log_path: Path | None = None, scan_offset: int = 0,
) -> None:
    """Sleep ``seconds``, checking every 30 s that the app is still alive.

    Also watches for mid-scan contact loss as it happens, so an induced or
    genuine fault surfaces within ~30 s instead of at the end of a 10-minute
    scan the tester already knows is bad.
    """
    log.info(f"  {label}: scanning for {seconds}s ({seconds // 60} min)…")
    elapsed = 0
    poor_since: float | None = None      # when contact was first lost
    last_report = 0
    while elapsed < seconds:
        # Poll faster while contact is down so recovery is picked up promptly.
        step = CQ_RECOVERY_POLL_SEC if poor_since is not None else 30
        nap = min(step, seconds - elapsed)
        time.sleep(nap)
        elapsed += nap
        if not is_app_alive():
            raise FatalFailure(f"APP CLOSED during {label} after {elapsed}s.")

        if log_path:
            poor = cameras_currently_poor(read_log_slice(log_path, scan_offset))
            if poor and poor_since is None:
                poor_since = time.time()
                log.warning(
                    f"  {label}: contact lost on {', '.join(poor)} at {elapsed}s "
                    f"— waiting up to {CQ_RECOVERY_TIMEOUT}s for recovery"
                )
            elif poor and poor_since is not None:
                down = time.time() - poor_since
                if down >= CQ_RECOVERY_TIMEOUT:
                    log.error(
                        f"  {label}: contact still lost on {', '.join(poor)} "
                        f"after {down:.0f}s — stopping scan"
                    )
                    _stop_scan_quietly(label)
                    raise ScanFailure(
                        f"{label}: contact not recovered within "
                        f"{CQ_RECOVERY_TIMEOUT}s — still poor on "
                        f"{', '.join(poor)} at {elapsed}s"
                    )
            elif not poor and poor_since is not None:
                log.info(
                    f"  {label}: contact recovered after "
                    f"{time.time() - poor_since:.0f}s — continuing scan"
                )
                poor_since = None
                # Acknowledge the modal so the scan really is left running
                # and the sidebar is clickable again.
                _dismiss_cq_notification(label)

        if elapsed - last_report >= 60:
            last_report = elapsed
            log.info(f"  {label}: {elapsed}/{seconds}s")


def _run_single_scan(cycle_idx: int, scan_idx: int) -> dict:
    """One scan: Start → quality check → Start Scan → 10 min → Stop.

    Returns the scan's observed metrics. Raises ``ScanFailure`` if the scan
    tripped an integrity gate, ``FatalFailure`` if the app went away.
    """
    label = f"cycle {cycle_idx}/{CYCLE_COUNT} scan {scan_idx}/{SCANS_PER_CYCLE}"
    log.info(f"─── ▶ {label} ───")

    # Take the offset before clicking Start so the slice covers the whole
    # scan, including the contact-quality table the check writes on entry.
    log_path = find_app_log()
    scan_offset = log_size(log_path) if log_path else 0
    if log_path:
        log.info(f"  {label}: watching app log {log_path}")
    else:
        log.warning(f"  {label}: NO APP LOG FOUND — integrity will NOT be checked")

    move_window_on_screen()
    ensure_visible()
    require_focus()
    click_sidebar(*SIDEBAR_START, f"{label}: Start")

    try:
        _wait_quality_then_start(
            label, log_path=log_path, scan_offset=scan_offset
        )
        _sleep_with_alive_check(
            label, SCAN_DURATION_SEC, log_path=log_path, scan_offset=scan_offset
        )
    except (ScanFailure, FatalFailure) as exc:
        # Attach what the log shows so far — a failing cycle must report the
        # counts behind its own failure message, not zeros.
        exc.metrics = scan_metrics(
            scan_idx, read_log_slice(log_path, scan_offset), completed=False
        )
        raise

    move_window_on_screen()
    ensure_visible()
    require_focus()
    # A contact-quality modal left over from a recovered loss sits in front
    # of the sidebar; clear it before aiming at Stop.
    _dismiss_cq_notification(label)
    log.info(f"  {label}: clicking Stop")
    # Scan teardown (USB flush, pipeline post-processing, DB save) takes
    # ~30 s after the Stop click; a Start clicked before the app logs
    # "Full scan ended" is silently swallowed. Tail the app log so the
    # next scan waits exactly as long as teardown actually takes.
    stop_offset = log_size(log_path) if log_path else 0
    click_sidebar(*SIDEBAR_START, f"{label}: Stop")
    ended = None
    if log_path:
        ended = wait_for_pattern(
            RE_SCAN_ENDED, log_path, stop_offset, SCAN_TEARDOWN_TIMEOUT
        )
        if ended:
            log.info(f"  {label}: teardown complete: {ended}")
        else:
            log.warning(
                f"  {label}: no 'Full scan ended' in app log within "
                f"{SCAN_TEARDOWN_TIMEOUT}s"
            )
    time.sleep(STOP_SETTLE_SEC)
    pyautogui.press("escape")              # dismiss any post-scan modal
    time.sleep(SLEEP)

    # Everything above only proves the UI was driven. Read back what the app
    # actually recorded before calling this scan good.
    slice_text = read_log_slice(log_path, scan_offset)
    metrics = scan_metrics(scan_idx, slice_text, completed=bool(ended))
    if not log_path:
        log.warning(f"  {label}: no app log found — integrity NOT verified")

    reasons = evaluate_scan(slice_text, scan_ended=bool(ended))
    if reasons:
        raise ScanFailure(f"{label}: " + "; ".join(reasons), metrics=metrics)

    log.info(
        f"  ✓ {label} done (NaN-filled={metrics['nan_filled']}, "
        f"histo-full={metrics['histo_queue_full']})"
    )
    return metrics


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
    # Counted from observed "Full scan ended" lines, not inferred from the
    # cycle count. The old `passed * SCANS_PER_CYCLE` reported scans that
    # were never confirmed to have run.
    scans_completed = sum(r.get("scans_completed", 0) for r in _CYCLE_RESULTS)
    summary = {
        "cycles_planned":  CYCLE_COUNT,
        "cycles_run":      len(_CYCLE_RESULTS),
        "cycles_passed":   passed,
        "cycles_failed":   failed,
        "scans_per_cycle": SCANS_PER_CYCLE,
        "scans_planned":   len(_CYCLE_RESULTS) * SCANS_PER_CYCLE,
        "scans_completed": scans_completed,
        "total_nan_filled":       sum(r.get("nan_filled", 0) for r in _CYCLE_RESULTS),
        "total_histo_queue_full": sum(r.get("histo_queue_full", 0) for r in _CYCLE_RESULTS),
        "cycles_with_cq_flags":   sum(1 for r in _CYCLE_RESULTS if r.get("cq_flagged")),
        "total_cq_live_raises":   sum(r.get("cq_live_raises", 0) for r in _CYCLE_RESULTS),
        "run_aborted":            _RUN_ABORTED,
        "degradation":            degradation_check(_CYCLE_RESULTS),
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
        f"| {r['cycle']} | {r.get('scans_completed', 0)}/{r['scans_planned']} | "
        f"{r['scan_duration_min']} | **{r['status']}** | "
        f"{r.get('nan_filled', 0)} | {r.get('histo_queue_full', 0)} | "
        f"{', '.join(r.get('cq_flagged') or []) or '—'} | "
        f"{r.get('cq_live_raises', 0)} | "
        f"{r['finished_at']} | {r.get('failure') or '—'} |"
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
| Scans planned   | {summary['scans_planned']} |
| Scans completed (observed) | {summary['scans_completed']} |
| Cycles with pre-scan contact-quality flags | {summary['cycles_with_cq_flags']} |
| Mid-scan contact-loss events (total) | {summary['total_cq_live_raises']} |
| Frames NaN-filled (total) | {summary['total_nan_filled']} |
| Histo queue-full events (total) | {summary['total_histo_queue_full']} |
| Run aborted | {summary['run_aborted'] or 'no'} |

**Scans completed** counts `Full scan ended` lines observed in the app log,
not `cycles x scans_per_cycle`. Contact-quality flags, NaN-filled frames and
histo queue-full events are read back from the app log per scan; a cycle
tripping a gate is recorded FAIL and the run continues.

## Degradation Trend

{_degradation_md(summary['degradation'])}

## Per-Cycle Results

| Cycle | Scans done | Scan duration (min) | Status | NaN-filled | Histo full | CQ pre-scan | CQ mid-scan | Finished At | Failure |
|-------|-----------|---------------------|--------|-----------|-----------|------------|------------|-------------|---------|
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
class TestReliabilityTestScript:
    """Overnight reliability test — CYCLE_COUNT cycles of
    (SCANS_PER_CYCLE × 10-min scans + power cycle).

    Cycles are independent: a failed cycle is recorded FAIL and the run moves
    on. Only a FatalFailure (app gone) stops the run, and the remaining
    cycles are then skipped rather than recorded as failures they never
    attempted. The old ``@pytest.mark.incremental`` is deliberately absent —
    with it, one failure skipped every remaining cycle.
    """

    @pytest.mark.parametrize(
        "cycle_idx",
        range(1, CYCLE_COUNT + 1),
        ids=[f"cycle-{i:02d}" for i in range(1, CYCLE_COUNT + 1)],
    )
    def test_cycle(self, outlet, app, cycle_idx):
        global _RUN_ABORTED
        if _RUN_ABORTED:
            pytest.skip(f"run aborted earlier: {_RUN_ABORTED}")

        scans: list[dict] = []
        status = "PASS"
        failure: str | None = None

        try:
            for scan_idx in range(1, SCANS_PER_CYCLE + 1):
                scans.append(_run_single_scan(cycle_idx, scan_idx))
                if scan_idx < SCANS_PER_CYCLE:
                    time.sleep(INTER_SCAN_PAUSE_SEC)

            if cycle_idx < CYCLE_COUNT:
                _power_cycle_and_wait_reconnect(outlet, cycle_idx)
            else:
                log.info(f"  ✓ last cycle ({cycle_idx}/{CYCLE_COUNT}) — done")

        except FatalFailure as exc:
            status, failure = "FAIL", str(exc)
            _RUN_ABORTED = failure
            if exc.metrics:
                scans.append(exc.metrics)
            log.error(f"  ✗ cycle {cycle_idx}: FATAL — {failure}")
        except ScanFailure as exc:
            status, failure = "FAIL", str(exc)
            if exc.metrics:
                scans.append(exc.metrics)
            log.error(f"  ✗ cycle {cycle_idx}: FAIL — {failure}")
        except Exception as exc:
            # Bench infrastructure (e.g. a smart-plug HTTP timeout) is
            # recoverable: record it and let the next cycle try.
            status = "FAIL"
            failure = f"{type(exc).__name__}: {exc}"
            log.error(f"  ✗ cycle {cycle_idx}: FAIL — {failure}")

        _CYCLE_RESULTS.append({
            "cycle":             cycle_idx,
            "scans_planned":     SCANS_PER_CYCLE,
            "scans_completed":   sum(1 for s in scans if s["completed"]),
            "scan_duration_min": SCAN_DURATION_MIN,
            "status":            status,
            "failure":           failure,
            "nan_filled":        sum(s["nan_filled"] for s in scans),
            "histo_queue_full":  sum(s["histo_queue_full"] for s in scans),
            "cq_flagged":        sorted({c for s in scans for c in s["cq_flagged"]}),
            "cq_live_raises":    sum(len(s["cq_live_raises"]) for s in scans),
            "finished_at":       datetime.now().isoformat(timespec="seconds"),
        })

        if status == "FAIL":
            pytest.fail(failure or "cycle failed")
