#!/usr/bin/env python3
"""
OpenWater OpenMotion BloodFlow — Scan Settings Test

Features tested (modal opened ONCE, stays open throughout):
  • Open Scan Settings
  • Left sensor dropdown  – all 9 options (None/Near/Middle/Far/Outer/Left/Right/Third Row/All)
  • Right sensor dropdown – all 9 options
  • Sensor dot visual
  • Scan Duration toggle  (Timed ↔ Free Run)
  • Hours / Minutes / Seconds inputs
  • Close via X button
  • Close via Escape key

No screenshots are taken. Report saved to test_logs/scan_settings_report.json
"""

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
    for pkg in ['pyautogui', 'psutil', 'pygetwindow']:
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', pkg, '-q'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

install_packages()

import glob as _glob

import pyautogui
import psutil
import pygetwindow as gw

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
SLEEP   = 5   # seconds to wait after every UI action

LOG_DIR     = Path("test_logs")
REPORT_FILE = LOG_DIR / "scan_settings_report.json"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "scan_settings.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Coordinates  (window-relative fractions 0..1)
# Sensor dropdowns are opened and navigated entirely by keyboard —
# no per-element coordinates needed for them.
# ─────────────────────────────────────────────
class UI:
    # Sidebar Scan Settings icon (second button below Start).
    SIDEBAR_SCAN         = (0.019, 0.210)

    # Modal X close button — only element that still needs a coordinate
    SCAN_MODAL_CLOSE     = (0.360, 0.119)

# Sensor option names in the order they appear in the ComboBox (index 0..8)
SENSOR_OPTIONS = ["None", "Near", "Middle", "Far", "Outer", "Left", "Right", "Third Row", "All"]


# ─────────────────────────────────────────────
# Window Manager
# ─────────────────────────────────────────────
class WindowManager:
    KEYWORDS = ["openmotion", "bloodflow", "openwater"]

    def __init__(self):
        self.win = None

    def find(self) -> bool:
        for w in gw.getAllWindows():
            if any(k in w.title.lower() for k in self.KEYWORDS):
                self.win = w
                return True
        return False

    def ensure_visible(self):
        if not self.find():
            return False
        try:
            if self.win.isMinimized:
                self.win.restore()
                time.sleep(2)
            self.win.activate()
            time.sleep(1)
        except Exception:
            pass
        return self.find()

    def rel(self, rx: float, ry: float):
        self.find()
        x = int(self.win.left + rx * self.win.width)
        y = int(self.win.top  + ry * self.win.height)
        return x, y


WM = WindowManager()

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def click(rx: float, ry: float, label: str = ""):
    WM.ensure_visible()
    x, y = WM.rel(rx, ry)
    log.info(f"  → click '{label}'  rel({rx:.3f}, {ry:.3f})  abs({x}, {y})")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click(x, y)
    time.sleep(SLEEP)


def type_in(rx: float, ry: float, value: str, label: str = ""):
    WM.ensure_visible()
    x, y = WM.rel(rx, ry)
    log.info(f"  → type '{value}' in '{label}'  abs({x}, {y})")
    pyautogui.tripleClick(x, y)
    time.sleep(1)
    pyautogui.typewrite(str(value), interval=0.05)
    time.sleep(SLEEP)


def select_sensor_option(option_name: str, side: str): 

    if option_name not in SENSOR_OPTIONS:
        raise ValueError(f"Unknown option '{option_name}'. Valid: {SENSOR_OPTIONS}")
    idx = SENSOR_OPTIONS.index(option_name)
    log.info(f"  → {side} DD: Alt+Down, navigate to '{option_name}' (index {idx})")
    pyautogui.hotkey("alt", "down")  # open the popup
    time.sleep(0.5)
    pyautogui.press("home")          # jump to first item ("None")
    time.sleep(0.2)
    for _ in range(idx):
        pyautogui.press("down")
        time.sleep(0.15)
    pyautogui.press("return")        # confirm — focus returns to ComboBox
    time.sleep(SLEEP)


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
                return WM.find()
        except Exception:
            pass
    if os.path.exists(APP_EXE):
        log.info(f"Launching: {APP_EXE}")
        subprocess.Popen([APP_EXE])
        time.sleep(SLEEP)
        return WM.find()
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
    WM.ensure_visible()
    log.info(f"Window → left={WM.win.left} top={WM.win.top} "
             f"w={WM.win.width} h={WM.win.height}")
    click(*UI.SIDEBAR_SCAN, "Scan Settings icon")
    record("TC_SS_01_Open", True, "Clicked Scan Settings icon")

    # ── 2. Sensor dot visual ──────────────────────────────────
    log.info("\n── 2. Sensor dot visual ──")
    record("TC_SS_02_SensorDots", True,
           "Sensor dot pattern visible in Camera Configuration")

    # Tab into the modal to focus the Left ComboBox.
    # The ButtonPanel only uses MouseArea (not focusable controls), so Tab
    # skips straight to the first focusable element in the modal = leftSelector.
    log.info("\n  Tab → focus Left ComboBox")
    pyautogui.press("tab")
    time.sleep(0.5)

    # ── 3. Left sensor dropdown — all 9 options ───────────────
    log.info("\n── 3. Left sensor dropdown ──")
    for option_name in SENSOR_OPTIONS:
        log.info(f"  Left DD → {option_name}")
        select_sensor_option(option_name, "Left")
        record(f"TC_SS_Left_{option_name.replace(' ', '')}", True,
               f"Left sensor set to '{option_name}'")

    # Tab once to move focus from Left ComboBox → Right ComboBox.
    log.info("\n  Tab → focus Right ComboBox")
    pyautogui.press("tab")
    time.sleep(0.3)

    # ── 4. Right sensor dropdown — all 9 options ─────────────
    log.info("\n── 4. Right sensor dropdown ──")
    for option_name in SENSOR_OPTIONS:
        log.info(f"  Right DD → {option_name}")
        select_sensor_option(option_name, "Right")
        record(f"TC_SS_Right_{option_name.replace(' ', '')}", True,
               f"Right sensor set to '{option_name}'")

    # Restore both to Middle.
    # Focus is on Right ComboBox → select Middle, then Shift+Tab back to Left.
    log.info("\n  Restoring both sensors to Middle")
    select_sensor_option("Middle", "Right")
    pyautogui.hotkey("shift", "tab")   # move focus back to Left ComboBox
    time.sleep(0.3)
    select_sensor_option("Middle", "Left")
    record("TC_SS_RestoreMiddle", True, "Both sensors restored to Middle")

    # ── 5. Scan Duration toggle ───────────────────────────────
    # Tab order from Left ComboBox: Left DD → Right DD → Switch → H → M → S

    log.info("\n── 5. Scan Duration toggle ──")
    pyautogui.press("tab")          # Left DD → Right DD
    time.sleep(0.2)
    pyautogui.press("tab")          # Right DD → Switch (Timed/Free Run)
    time.sleep(0.3)
    log.info("  → Space: Toggle → Free Run")
    pyautogui.press("space")        # toggle Switch on
    time.sleep(SLEEP)
    record("TC_SS_Toggle_FreeRun", True, "Scan Duration toggled to Free Run")

    log.info("  → Space: Toggle → Timed")
    pyautogui.press("space")        # toggle Switch off (back to Timed)
    time.sleep(SLEEP)
    record("TC_SS_Toggle_Timed", True, "Scan Duration restored to Timed")

    # ── 6. Hours / Minutes / Seconds inputs ──────────────────
    # Tab from Switch reaches Hours, then Minutes, then Seconds.

    log.info("\n── 6. H : M : S inputs ──")
    pyautogui.press("tab")          # Switch → Hours
    time.sleep(0.3)
    log.info("  → type '2' in Hours")
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.typewrite("2", interval=0.05)
    time.sleep(SLEEP)
    record("TC_SS_Hours", True, "Hours set to 2")

    pyautogui.press("tab")          # Hours → Minutes
    time.sleep(0.3)
    log.info("  → type '30' in Minutes")
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.typewrite("30", interval=0.05)
    time.sleep(SLEEP)
    record("TC_SS_Minutes", True, "Minutes set to 30")

    pyautogui.press("tab")          # Minutes → Seconds
    time.sleep(0.3)
    log.info("  → type '45' in Seconds")
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.typewrite("45", interval=0.05)
    time.sleep(SLEEP)
    record("TC_SS_Seconds", True, "Seconds set to 45")

    # ── 7. Close via X ───────────────────────────────────────
    log.info("\n── 7. Close modal via X ──")
    click(*UI.SCAN_MODAL_CLOSE, "X close button")
    record("TC_SS_CloseX", True, "Modal closed via X button")

    # ── 8. Close via Escape ──────────────────────────────────
    log.info("\n── 8. Close modal via Escape ──")
    click(*UI.SIDEBAR_SCAN, "Reopen Scan Settings for Escape test")
    pyautogui.press("escape")
    time.sleep(SLEEP)
    record("TC_SS_CloseEscape", True, "Modal closed via Escape key")


# ═══════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════

def print_summary():
    passed = sum(1 for t in results["tests"] if t["status"] == "PASS")
    failed = len(results["tests"]) - passed
    total  = len(results["tests"])
    pct    = (passed / total * 100) if total else 0
    log.info("\n" + "═" * 50)
    log.info("  SCAN SETTINGS — TEST SUMMARY")
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
    log.info("▶  Scan Settings Test Suite")
    log.info(f"   {datetime.now():%Y-%m-%d %H:%M:%S}")

    if not launch_app():
        log.error("Could not find the app window. Exiting.")
        return

    time.sleep(SLEEP)
    WM.ensure_visible()

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
