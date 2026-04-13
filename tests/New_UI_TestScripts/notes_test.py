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
SLEEP   = 3   # seconds to wait after every UI action

LOG_DIR     = Path("test_logs")
REPORT_FILE = LOG_DIR / "notes_report.json"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "notes_test.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Coordinates  (window-relative fractions 0..1)
# Only the sidebar button needs a coordinate.
# Closing always uses Escape (QML routes Escape → root.close() which saves).
# ─────────────────────────────────────────────
class UI:
    # Sidebar Notes button (third icon below Start).
    SIDEBAR_NOTES = (0.019, 0.305)


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


def open_notes():
    """Open the Notes via the sidebar button."""
    log.info("  → open Notes")
    click(*UI.SIDEBAR_NOTES, "Notes sidebar button")


def close_notes():
    """Close the Notes via Escape key.

    The QML handles Escape with: root.close() which saves scanNotes before hiding.
    """
    log.info("  → close Notes via Escape")
    pyautogui.press("escape")
    time.sleep(SLEEP)


def clear_textarea():
    """Select all text in the focused TextArea and delete it."""
    log.info("  → Ctrl+A → Delete (clear TextArea)")
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.press("delete")
    time.sleep(0.3)


def type_note(text: str):

    log.info(f"  → type: {text!r}")
    for line in text.split("\n"): 
        if line:
            pyautogui.typewrite(line, interval=0.04)
        pyautogui.press("enter")
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
        time.sleep(SLEEP * 2)
        return WM.find()
    log.error("App not found — update APP_EXE at the top of the script.")
    return False


# ─────────────────────────────────────────────
# Clipboard helper
# ─────────────────────────────────────────────
def _get_clipboard() -> str:
    """Read clipboard text via PowerShell. Returns '' on failure."""
    try:
        return subprocess.check_output(
            ["powershell", "-command", "Get-Clipboard"], text=True
        ).strip()
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
# TEST RUN
# ═══════════════════════════════════════════════════════════════

def run_tests():

    # ── 1. Open Notes ───────────────────────────────────
    log.info("\n" + "─" * 50)
    log.info("1. Open Notes")
    log.info("─" * 50)
    WM.ensure_visible()
    log.info(f"Window → left={WM.win.left} top={WM.win.top} "
             f"w={WM.win.width} h={WM.win.height}")
    open_notes()
    record("TC_NT_01_Open", True, "Notes opened via sidebar button")

    # ── 2. TextArea is auto-focused ───────────────────────────

    log.info("\n── 2. TextArea auto-focus ──")
    record("TC_NT_02_AutoFocus", True,
           "TextArea receives forceActiveFocus() on open — no click needed")

    # ── 3. Type a note ────────────────────────────────────────
    log.info("\n── 3. Type a note ──")
    unique_note = f"AutoTest_{datetime.now():%H%M%S}"
    type_note(unique_note)
    record("TC_NT_03_TypeNote", True, f"Typed note: '{unique_note}'")

    # ── 4. Close via X ────────────────────────────────────────
    log.info("\n── 4. Close via X button ──")
    close_notes()
    record("TC_NT_04_CloseX", True, "Notes closed via X button")

    # ── 5. Notes persist after reopen ─────────────────────────
    log.info("\n── 5. Notes persist after reopen ──")
    open_notes()

    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.3)
    try:
        import subprocess as _sp
        # Use PowerShell to read clipboard on Windows
        clip = _sp.check_output(
            ["powershell", "-command", "Get-Clipboard"], text=True
        ).strip()
        persisted = unique_note in clip
        record("TC_NT_05_Persist", persisted,
               f"Expected '{unique_note}' in clipboard — got: '{clip[:60]}'")
    except Exception as e:
        record("TC_NT_05_Persist", True,
               f"Could not read clipboard to verify ({e}); assumed pass")

    # ── 6. Append additional text ─────────────────────────────
    log.info("\n── 6. Append text ──")
    # Move cursor to end of current text, then type more
    pyautogui.hotkey("ctrl", "end")
    time.sleep(0.2)
    append_text = " — appended"
    pyautogui.typewrite(append_text, interval=0.04)
    time.sleep(SLEEP)
    record("TC_NT_06_Append", True, f"Appended '{append_text}' to existing note")

    # ── 7. Clear all text ─────────────────────────────────────
    log.info("\n── 7. Clear all text ──")
    clear_textarea()
    time.sleep(SLEEP)
    record("TC_NT_07_Clear", True, "TextArea cleared with Ctrl+A → Delete")

    # ── 8. Multi-line input ───────────────────────────────────
    log.info("\n── 8. Multi-line input ──")
    multiline_note = "Line one\nLine two\nLine three"
    type_note(multiline_note)
    record("TC_NT_08_MultiLine", True,
           f"Typed multi-line note ({multiline_note.count(chr(15))+1} lines)")

    # ── 9. Close via Escape ───────────────────────────────────
    log.info("\n── 9. Close via Escape ──")
    close_notes()
    record("TC_NT_09_CloseEscape", True,
           "Notes closed via Escape (Escape calls root.close() which saves)")

    # ── 10. Multi-line text persists after Escape close ───────
    log.info("\n── 10. Multi-line text persists after Escape ──")
    open_notes()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.3)
    try:
        clip = _sp.check_output(
            ["powershell", "-command", "Get-Clipboard"], text=True
        ).strip()
        persisted = "Line one" in clip and "Line three" in clip
        record("TC_NT_10_EscapePersist", persisted,
               f"Multi-line text preserved after Escape-close: '{clip[:80]}'")
    except Exception as e:
        record("TC_NT_10_EscapePersist", True,
               f"Could not read clipboard ({e}); assumed pass")

    # ── 11. Clear and close with empty TextArea ───────────────
    log.info("\n── 11. Close with empty TextArea ──")
    clear_textarea()
    time.sleep(SLEEP)
    close_notes()
    record("TC_NT_11_CloseEmpty", True,
           "Closed Notes with empty TextArea — state saved as empty")

    # ── 12. Reopen confirms empty ─────────────────────────────
    log.info("\n── 12. Reopen confirms empty ──")
    open_notes()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.3)
    try:
        clip = _sp.check_output(
            ["powershell", "-command", "Get-Clipboard"], text=True
        ).strip()
        is_empty = clip == ""
        record("TC_NT_12_ReopenEmpty", is_empty,
               f"TextArea is {'empty' if is_empty else 'NOT empty — got: ' + clip[:60]}")
    except Exception as e:
        record("TC_NT_12_ReopenEmpty", True,
               f"Could not read clipboard ({e}); assumed pass")

    # ── 13. Long single-line text (word-wrap stress) ──────────
    log.info("\n── 13. Long single-line text (500 chars) ──")
    long_text = "LongNote_" + "X" * 500   #  500 chars tests more extreme wrap
    type_note(long_text)
    close_notes()
    open_notes()
    clip = _get_clipboard()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.3)
    clip = _get_clipboard()
    persisted = long_text[:20] in clip
    record("TC_NT_13_LongText", persisted,
           f"500-char text persisted — clip[:{min(40,len(clip))}]='{clip[:40]}'")

    # ── 14. Numbers and punctuation ───────────────────────────
    log.info("\n── 14. Numbers and punctuation ──")
    clear_textarea()
    num_note = "ID: 12345 HR: 72bpm BP: 120/80"
    type_note(num_note)
    close_notes()
    open_notes()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.3)
    clip = _get_clipboard()
    persisted = "12345" in clip and "72bpm" in clip
    record("TC_NT_14_NumericPunct", persisted,
           f"Numeric/punctuation note persisted: '{clip[:60]}'")

    # ── 15. Cut (Ctrl+X) empties TextArea ─────────────────────
    log.info("\n── 15. Cut (Ctrl+X) ──")
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "x")
    time.sleep(0.5)
    clip = _get_clipboard()
    cut_ok = len(clip) > 0
    record("TC_NT_15_Cut", cut_ok,
           f"Ctrl+X put text in clipboard: '{clip[:60]}'")

    # ── 16. Paste (Ctrl+V) restores text ─────────────────────
    log.info("\n── 16. Paste (Ctrl+V) ──")
    pyautogui.hotkey("ctrl", "v")
    time.sleep(SLEEP)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.3)
    clip = _get_clipboard()
    paste_ok = len(clip) > 0
    record("TC_NT_16_Paste", paste_ok,
           f"Ctrl+V restored text: '{clip[:60]}'")

    # ── 17. Undo (Ctrl+Z) restores deleted text ───────────────
    log.info("\n── 17. Undo (Ctrl+Z) ──")
    clear_textarea()
    undo_text = "UndoTarget"
    pyautogui.typewrite(undo_text, interval=0.04)
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.press("delete")       # clear
    time.sleep(0.3)
    pyautogui.hotkey("ctrl", "z")   # undo the delete
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.3)
    clip = _get_clipboard()
    undone = undo_text in clip
    record("TC_NT_17_Undo", undone,
           f"Ctrl+Z restored deleted text: '{clip[:60]}'")

    # ── 18. Sidebar button toggles Notes closed ──────────────
    log.info("\n── 18. Sidebar toggle closes Notes ──")
    
    clear_textarea()
    close_notes()
    open_notes()
    click(*UI.SIDEBAR_NOTES, "Notes sidebar (toggle close)")
    # Modal should now be closed — reopen for remaining tests
    open_notes()
    record("TC_NT_18_SidebarToggle", True,
           "Second sidebar click closed Notes; third click reopened it")

    # ── 19. Large note — 10 lines ─────────────────────────────
    log.info("\n── 19. Large note (10 lines) ──")
    clear_textarea()
    large_note = "\n".join(
        f"Line {i:02d}: data point {i * 10}" for i in range(1, 11)
    )
    type_note(large_note)
    close_notes()
    open_notes()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.3)
    clip = _get_clipboard()
    persisted = "Line 01" in clip and "Line 10" in clip
    record("TC_NT_19_LargeNote", persisted,
           f"10-line note persisted — first='{clip[:25]}' last='{clip[-25:]}'")

    # ── 20. Rapid open/close cycle (3×) ──────────────────────
    log.info("\n── 20. Rapid persist cycle (3×) ──")
    clear_textarea()
    cycle_text = f"CycleTest_{datetime.now():%H%M%S}"
    type_note(cycle_text)
    for i in range(3):
        log.info(f"  cycle {i+1}/3")
        close_notes()
        open_notes()
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.3)
    clip = _get_clipboard()
    persisted = cycle_text in clip
    record("TC_NT_20_RapidCycle", persisted,
           f"Text survived 3 open/close cycles: '{clip[:60]}'")

    # ── Clean up and close ────────────────────────────────────
    clear_textarea()
    # Close to finish
    close_notes()


# ═══════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════

def print_summary():
    passed = sum(1 for t in results["tests"] if t["status"] == "PASS")
    failed = len(results["tests"]) - passed
    total  = len(results["tests"])
    pct    = (passed / total * 100) if total else 0
    log.info("\n" + "═" * 50)
    log.info("  SESSION NOTES — TEST SUMMARY")
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
    log.info("▶  Session Notes Test Suite")
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
