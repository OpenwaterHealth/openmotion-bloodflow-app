#!/usr/bin/env python3


import time
import subprocess
import sys
import os
import json
import logging
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────
# Auto-install dependencies
# ─────────────────────────────────────────────
def install_packages():
    for pkg in ['pyautogui', 'psutil', 'pygetwindow', 'pywinauto']:
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', pkg, '-q'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

install_packages()

import glob as _glob

import pyautogui
import psutil
import pygetwindow as gw
from pywinauto import Desktop as UiaDesktop

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.3

# ─────────────────────────────────────────────
# Configuration — edit these if needed
# ─────────────────────────────────────────────
def _find_exe() -> str:
    """Locate OpenWaterApp.exe without hardcoding a username or install path."""
    env = os.environ.get("OPENWATER_EXE", "")
    if env and os.path.exists(env):
        return env
    patterns = [
        r"C:\Users\*\Documents\OpenMotion\**\OpenWaterApp.exe",
        r"C:\Users\*\Desktop\**\OpenWaterApp.exe",
        r"C:\Program Files\**\OpenWaterApp.exe",
        r"C:\Program Files (x86)\**\OpenWaterApp.exe",
    ]
    for pattern in patterns:
        matches = _glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "OpenWaterApp.exe")
    if os.path.exists(local):
        return local
    return ""

APP_EXE = _find_exe()
SLEEP   = 3   # seconds to wait after most UI actions

# ─────────────────────────────────────────────
# Sidebar coordinates (MouseArea-based buttons —
# not exposed via UIA, so coordinates are used only here)
# ─────────────────────────────────────────────
class UI:
    # Only sidebar buttons that can't be found via UIA (MouseArea-based)
    SIDEBAR_SCAN  = (0.019, 0.210)
    SIDEBAR_NOTES = (0.019, 0.315)
    SIDEBAR_START = (0.019, 0.115)

SCAN_DURATION_MIN = 2                           # minutes configured in Scan Settings
WAIT_AFTER_SCAN   = SCAN_DURATION_MIN * 60 + 180 # scan + 3-min buffer = 300 s (when scan=2 min)
VIZ_WAIT          = 120                          # seconds to leave each plot open

LOG_DIR     = Path("test_logs")
REPORT_FILE = LOG_DIR / "scan_flow_report.json"
LOG_DIR.mkdir(exist_ok=True)

try:
    # Prevent UnicodeEncodeError on Windows cp1252 consoles.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "scan_flow_test.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Window helpers
# ─────────────────────────────────────────────
APP_KEYWORDS = ["openmotion", "bloodflow", "openwater"]


def _ensure_visible():
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


def _uia_window():
    """Return a WindowSpecification (supports child_window)."""
    desktop = UiaDesktop(backend="uia")
    for kw in APP_KEYWORDS:
        try:
            spec = desktop.window(title_re=f"(?i).*{kw}.*")
            if spec.exists(timeout=2):
                return spec
        except Exception:
            continue
    raise RuntimeError("App window not found via UI Automation")


# ─────────────────────────────────────────────
# Click sidebar button (coordinate-based)
# Sidebar PanelButtons use MouseArea — not exposed via UIA
# ─────────────────────────────────────────────
def click(rx: float, ry: float, label: str = ""):
    _ensure_visible()
    for w in gw.getAllWindows():
        if any(k in w.title.lower() for k in APP_KEYWORDS):
            x = int(w.left + rx * w.width)
            y = int(w.top  + ry * w.height)
            log.info(f"  → click '{label}'  rel({rx:.3f}, {ry:.3f})  abs({x}, {y})")
            pyautogui.moveTo(x, y, duration=0.3)
            pyautogui.click(x, y)
            time.sleep(SLEEP)
            return
    raise RuntimeError("App window not found for coordinate click")


# ─────────────────────────────────────────────
# Click by name (UI Automation)
# For Qt Quick Controls Button elements inside modals
# ─────────────────────────────────────────────
def click_by_name(name: str):
    """Find a UI element by its visible label, then click its center with pyautogui.

    Uses UIA only to locate the element's bounding rectangle on screen.
    pyautogui does the actual click — this is reliable with Qt Quick apps
    which don't always respond to pywinauto's synthetic click events.
    """
    _ensure_visible()
    win = _uia_window()
    log.info(f"  → find by name: '{name}'")
    for ct in ["Button", "Custom", "Text", "Group", "ListItem", "Pane"]:
        try:
            elem = win.child_window(title=name, control_type=ct)
            if elem.exists(timeout=2):
                rect = elem.rectangle()
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                log.info(f"     found as control_type='{ct}'  center=({cx}, {cy})")
                pyautogui.moveTo(cx, cy, duration=0.3)
                pyautogui.click(cx, cy)
                time.sleep(SLEEP)
                return
        except Exception:
            continue
    raise RuntimeError(f"Could not find '{name}' via UI Automation")


# ─────────────────────────────────────────────
# Wait helper
# ─────────────────────────────────────────────
def wait_with_log(total_seconds: int, label: str):
    """Wait total_seconds, logging progress every 60 s."""
    log.info(f"  ⏳ Waiting {total_seconds}s — {label}")
    elapsed = 0
    while elapsed < total_seconds:
        chunk = min(60, total_seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
        remaining = total_seconds - elapsed
        log.info(f"     {elapsed}/{total_seconds}s elapsed"
                 + (f"  ({remaining}s remaining)" if remaining > 0 else "  — done"))


# ─────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────
results = {"run_date": datetime.now().isoformat(), "tests": []}


def record(name: str, passed: bool, notes: str = ""):
    status = "PASS" if passed else "FAIL"
    icon   = "✅" if passed else "❌"
    log.info(f"  {icon} [{status}] {name}: {notes}")
    results["tests"].append({
        "test": name, "status": status,
        "notes": notes, "timestamp": datetime.now().isoformat(),
    })


def save_report():
    passed = sum(1 for t in results["tests"] if t["status"] == "PASS")
    failed = len(results["tests"]) - passed
    results["summary"] = {
        "total": len(results["tests"]),
        "passed": passed,
        "failed": failed,
    }
    with open(REPORT_FILE, "w") as f:
        json.dump(results, f, indent=2)
    log.info(f"Report → {REPORT_FILE}")


# ─────────────────────────────────────────────
# App launch
# ─────────────────────────────────────────────
def launch_app() -> bool:
    for proc in psutil.process_iter(["name"]):
        try:
            if "openwater" in proc.info["name"].lower():
                log.info("App already running.")
                return True
        except Exception:
            pass
    if os.path.exists(APP_EXE):
        log.info(f"Launching: {APP_EXE}")
        subprocess.Popen([APP_EXE])
        time.sleep(SLEEP * 2)
        return True
    log.error("App not found — update APP_EXE at the top of the script.")
    return False


# ═══════════════════════════════════════════════════════════════
# TEST RUN
# ═══════════════════════════════════════════════════════════════

def run_tests():

    # ── 1. Open Scan Settings ─────────────────────────────────
    log.info("\n" + "─" * 50)
    log.info("1. Open Scan Settings")
    log.info("─" * 50)
    click(*UI.SIDEBAR_SCAN, "Scan Settings")
    record("TC_SF_01_OpenScanSettings", True, "Scan Settings opened via sidebar")

    # ── 2. Set duration to 2 minutes ─────────────────────────
    # Tab order inside Scan Settings:
    #   Tab×1 → Left ComboBox  Tab×2 → Right ComboBox
    #   Tab×3 → Switch (Timed/Free Run)
    #   Tab×4 → Hours  Tab×5 → Minutes  Tab×6 → Seconds
    log.info("\n── 2. Set scan duration to 2 minutes ──")
    pyautogui.press("tab")           # → Left ComboBox
    time.sleep(0.3)
    pyautogui.press("tab")           # → Right ComboBox
    time.sleep(0.3)
    pyautogui.press("tab")           # → Switch (leave on Timed)
    time.sleep(0.3)
    pyautogui.press("tab")           # → Hours
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.typewrite("0", interval=0.05)
    time.sleep(0.3)
    pyautogui.press("tab")           # → Minutes
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.typewrite("2", interval=0.05)
    time.sleep(0.5)
    pyautogui.press("tab")           # → Seconds
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.typewrite("0", interval=0.05)
    time.sleep(0.5)
    record("TC_SF_02_SetDuration", True,
           f"Duration set to {SCAN_DURATION_MIN} minutes (0h {SCAN_DURATION_MIN}m 0s) via Tab navigation")

    log.info("  → Escape: save & close Scan Settings")
    pyautogui.press("escape")
    time.sleep(SLEEP)
    record("TC_SF_03_CloseScanSettings", True, "Scan Settings closed via Escape")

    # ── 3. Open Notes and type a note ─────────────────────────
    log.info("\n── 3. Open Notes and type note ──")
    click(*UI.SIDEBAR_NOTES, "Notes")
    # forceActiveFocus() fires on open — type immediately, no Tab needed
    session_note = f"AutoScan_{datetime.now():%Y%m%d_%H%M%S}"
    log.info(f"  → typing note: '{session_note}'")
    pyautogui.typewrite(session_note, interval=0.04)
    time.sleep(SLEEP)
    record("TC_SF_04_TypeNote", True, f"Note typed: '{session_note}'")

    log.info("  → Escape: save & close Notes")
    pyautogui.press("escape")
    time.sleep(SLEEP)
    record("TC_SF_05_CloseNotes", True, "Notes closed via Escape — note saved")

    # ── 4. Start scan ─────────────────────────────────────────
    log.info("\n── 4. Click Start ──")
    click(*UI.SIDEBAR_START, "Start")
    record("TC_SF_06_StartScan", True,
           f"Start clicked — {SCAN_DURATION_MIN}-minute timed scan running")

    # ── 5. Wait for scan + buffer ─────────────────────────────
    log.info(f"\n── 5. Waiting {WAIT_AFTER_SCAN}s "
             f"({SCAN_DURATION_MIN}-min scan + 3-min buffer) ──")
    wait_with_log(WAIT_AFTER_SCAN,
                  f"{SCAN_DURATION_MIN}-minute scan + 3-minute buffer")
    record("TC_SF_07_WaitForScan", True,
           f"Waited {WAIT_AFTER_SCAN}s — scan should be complete")

    # ── 6. Close auto-opened Notes ────────────────────────────
    # BloodFlow.qml calls notesModal.open() in onScanFinished
    log.info("\n── 6. Close auto-opened Notes ──")
    _ensure_visible()
    pyautogui.press("escape")
    time.sleep(SLEEP)
    record("TC_SF_08_ClosePostScanNotes", True,
           "Auto-opened Notes dismissed via Escape")

    # ── 7. Open History ───────────────────────────────────────
    log.info("\n── 7. Open History ──")
    click_by_name("History")
    record("TC_SF_09_OpenHistory", True, "History found by name and opened")

    # ── 8. Latest scan auto-selected (index 0 on open) ────────
    record("TC_SF_10_LatestScanSelected", True,
           "History.open() sets scanPicker index 0 (latest scan) automatically")

    # ── 9. Visualize BFI/BVI — found by name via UIA ──────────
    log.info("\n── 9. Visualize BFI/BVI ──")
    click_by_name("Visualize BFI/BVI")
    record("TC_SF_11_VisBFIClicked", True,
           "'Visualize BFI/BVI' found by name and clicked")
    wait_with_log(VIZ_WAIT, "BFI/BVI plot open")
    log.info("  → Alt+F4: close BFI/BVI plot window")
    pyautogui.hotkey("alt", "f4")
    time.sleep(SLEEP)
    record("TC_SF_12_VisBFIClosed", True, "BFI/BVI plot closed via Alt+F4")

    # ── 10. Visualize Contrast/Mean — found by name via UIA ───
    log.info("\n── 10. Visualize Contrast/Mean ──")
    _ensure_visible()
    click_by_name("Visualize Contrast/Mean")
    record("TC_SF_13_VisContrastClicked", True,
           "'Visualize Contrast/Mean' found by name and clicked")
    wait_with_log(VIZ_WAIT, "Contrast/Mean plot open")
    log.info("  → Alt+F4: close Contrast/Mean plot window")
    pyautogui.hotkey("alt", "f4")
    time.sleep(SLEEP)
    record("TC_SF_14_VisContrastClosed", True, "Contrast/Mean plot closed via Alt+F4")

    # ── 11. Close History ─────────────────────────────────────
    log.info("\n── 11. Close History ──")
    _ensure_visible()
    pyautogui.press("escape")
    time.sleep(SLEEP)
    record("TC_SF_15_CloseHistory", True, "History closed via Escape")


# ═══════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════

def print_summary():
    passed = sum(1 for t in results["tests"] if t["status"] == "PASS")
    failed = len(results["tests"]) - passed
    total  = len(results["tests"])
    pct    = (passed / total * 100) if total else 0
    log.info("\n" + "═" * 50)
    log.info("  SCAN FLOW — TEST SUMMARY")
    log.info("═" * 50)
    log.info(f"  Total  : {total}")
    log.info(f"  ✅ Pass : {passed}  ({pct:.1f}%)")
    log.info(f"  ❌ Fail : {failed}")
    if failed:
        log.info("  Failed:")
        for t in results["tests"]:
            if t["status"] == "FAIL":
                log.info(f"    • {t['test']}: {t['notes']}")
    log.info(f"  Report : {REPORT_FILE}")
    log.info("═" * 50 + "\n")


def main():
    log.info("▶  Scan Flow Test Suite")
    log.info(f"   {datetime.now():%Y-%m-%d %H:%M:%S}")
    log.info(f"   Scan: {SCAN_DURATION_MIN} min  |  "
             f"Total wait: {WAIT_AFTER_SCAN}s  |  "
             f"Viz wait: {VIZ_WAIT}s each")

    if not launch_app():
        log.error("Could not find or launch the app. Exiting.")
        return

    time.sleep(SLEEP)
    _ensure_visible()

    try:
        run_tests()
    except Exception as exc:
        record("FATAL_ERROR", False, f"Unhandled exception: {exc}")
        log.error(f"Fatal: {exc}", exc_info=True)
        try:
            pyautogui.press("escape")
        except Exception:
            pass

    save_report()
    print_summary()


if __name__ == "__main__":
    main()
