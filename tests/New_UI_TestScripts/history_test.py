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
# Configuration
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

APP_EXE  = _find_exe()
SLEEP    = 3    # seconds after each UI action
VIZ_WAIT = 120  # seconds to leave each plot open

LOG_DIR     = Path("test_logs")
REPORT_FILE = LOG_DIR / "history_report.json"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "history_test.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

APP_KEYWORDS = ["openmotion", "bloodflow", "openwater"]

# ─────────────────────────────────────────────
# Sidebar coordinates
# ─────────────────────────────────────────────
class UI:
    SIDEBAR_HISTORY = (0.020, 0.830)  # relative (x, y) for History button in sidebar

# ─────────────────────────────────────────────
# Window helpers
# ─────────────────────────────────────────────
def _ensure_visible():
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
    """Return a WindowSpecification for the app."""
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
# Coordinate click (for sidebar MouseArea buttons)
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
# Find by name → click center with pyautogui
# ─────────────────────────────────────────────
def click_by_name(name: str):
    _ensure_visible()
    win = _uia_window()
    log.info(f"  → find by name: '{name}'")

    # 1. Search entire tree (finds disabled buttons too)
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

    # 2. Fallback: child_window per control type
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


# ─────────────────────────────────────────────
# Get text of the currently selected ComboBox item
# ─────────────────────────────────────────────
def _selected_scan_text() -> str:
    """Read the current text of the scan-picker ComboBox."""
    try:
        win = _uia_window()
        cb = win.child_window(control_type="ComboBox")
        if cb.exists(timeout=2):
            return cb.window_text().strip()
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────
# Wait helper
# ─────────────────────────────────────────────
def wait_with_log(total_seconds: int, label: str):
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

    # ── 1. Open History ───────────────────────────────────────
    log.info("\n" + "─" * 50)
    log.info("1. Open History")
    log.info("─" * 50)
    click(*UI.SIDEBAR_HISTORY, "History sidebar button")
    record("TC_HT_01_Open", True, "History opened via sidebar coordinate click")

    # ── 2. Confirm latest scan is listed ──────────────────────
    log.info("\n── 2. Confirm latest scan listed ──")
    scan_text = _selected_scan_text()
    has_scan = len(scan_text) > 0
    record("TC_HT_02_LatestScan", has_scan,
           f"Scan ComboBox text: '{scan_text}'" if has_scan
           else "ComboBox is empty — no scans found in data directory")
    if not has_scan:
        log.warning("  ⚠ No scan data found — Visualize buttons will be disabled.")
        log.warning("  ⚠ Run a scan first, then re-run this script.")

    # ── 3. Visualize BFI/BVI ──────────────────────────────────
    log.info("\n── 3. Visualize BFI/BVI ──")
    try:
        click_by_name("Visualize BFI/BVI")
        record("TC_HT_03_VisBFIClicked", True,
               "'Visualize BFI/BVI' found by name and clicked")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")
        log.info("  → Alt+F4: close BFI/BVI plot window")
        _ensure_visible()
        pyautogui.hotkey("alt", "f4")
        time.sleep(SLEEP)
        record("TC_HT_04_VisBFIClosed", True, "BFI/BVI plot closed via Alt+F4")
    except RuntimeError as e:
        record("TC_HT_03_VisBFIClicked", False, str(e))
        record("TC_HT_04_VisBFIClosed", False, "Skipped — button not found")

    # ── 4. Visualize Contrast/Mean ────────────────────────────
    log.info("\n── 4. Visualize Contrast/Mean ──")
    _ensure_visible()
    try:
        click_by_name("Visualize Contrast/Mean")
        record("TC_HT_05_VisContrastClicked", True,
               "'Visualize Contrast/Mean' found by name and clicked")
        wait_with_log(VIZ_WAIT, "Contrast/Mean plot open")
        log.info("  → Alt+F4: close Contrast/Mean plot window")
        _ensure_visible()
        pyautogui.hotkey("alt", "f4")
        time.sleep(SLEEP)
        record("TC_HT_06_VisContrastClosed", True, "Contrast/Mean plot closed via Alt+F4")
    except RuntimeError as e:
        record("TC_HT_05_VisContrastClicked", False, str(e))
        record("TC_HT_06_VisContrastClosed", False, "Skipped — button not found")

    # ── 5. Close History ──────────────────────────────────────
    log.info("\n── 5. Close History ──")
    _ensure_visible()
    pyautogui.press("escape")
    time.sleep(SLEEP)
    record("TC_HT_07_Close", True, "History closed via Escape")


# ═══════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════

def print_summary():
    passed = sum(1 for t in results["tests"] if t["status"] == "PASS")
    failed = len(results["tests"]) - passed
    total  = len(results["tests"])
    pct    = (passed / total * 100) if total else 0
    log.info("\n" + "═" * 50)
    log.info("  HISTORY — TEST SUMMARY")
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
    log.info("▶  History Test Suite")
    log.info(f"   {datetime.now():%Y-%m-%d %H:%M:%S}")

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
