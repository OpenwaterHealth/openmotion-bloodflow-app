"""
Reduced Mode — end-to-end test.

Covers the full Reduced Mode workflow using the global Settings modal (gear icon).
Sensor dropdowns are a Scan Settings feature and are NOT tested here — Scan Settings
is hidden while Reduced Mode is active.

Two classes:
  TestReducedMode       (01–20) — keyboard-driven interactions
  TestReducedModeMouse  (21–40) — mouse-driven interactions
"""

import atexit
import getpass
import json
import os
import platform
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pyautogui
import pygetwindow as gw
import pytest

from conftest import (
    APP_KEYWORDS,
    SLEEP,
    click_by_name,
    click_sidebar,
    ensure_visible,
    get_app_window,
    get_clipboard,
    log,
    require_focus,
    uia_window,
    wait_with_log,
)

# ─────────────────────────────────────────────
# Sidebar coordinates
# ─────────────────────────────────────────────
SIDEBAR_NOTES_REDUCED = (0.019, 0.210)   # Notes position when Scan Settings is hidden
SIDEBAR_START         = (0.019, 0.115)   # Start / Stop toggle button
SIDEBAR_HISTORY       = (0.020, 0.820)   # History icon

SCAN_WAIT   = 200   # seconds to run the scan (3 minutes 20 seconds)
STOP_BUFFER = 15    # seconds to wait after stopping for data to save
VIZ_WAIT    = 60    # seconds to leave each plot open


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _close_plot_window() -> bool:
    """Close the plot window opened by the app using keyboard (alt+f4)."""
    for w in gw.getAllWindows():
        if not w.title.strip():
            continue
        if any(k in w.title.lower() for k in APP_KEYWORDS):
            continue
        try:
            if w.isMinimized:
                w.restore()
                time.sleep(1)
            w.activate()
            time.sleep(0.5)
            log.info(f"  Closing plot window: '{w.title}'")
            pyautogui.hotkey("alt", "f4")
            time.sleep(SLEEP)
            return True
        except Exception as e:
            log.warning(f"  Could not close '{w.title}': {e}")
    log.warning("  No plot window found to close")
    return False


def _close_plot_window_mouse() -> bool:
    """Close the plot window by moving the mouse to its center then alt+f4."""
    for w in gw.getAllWindows():
        if not w.title.strip():
            continue
        if any(k in w.title.lower() for k in APP_KEYWORDS):
            continue
        try:
            if w.isMinimized:
                w.restore()
                time.sleep(1)
            w.activate()
            time.sleep(0.5)
            cx = w.left + w.width // 2
            cy = w.top + w.height // 2
            pyautogui.moveTo(cx, cy, duration=0.3)
            log.info(f"  Closing plot window (mouse): '{w.title}'  center=({cx},{cy})")
            pyautogui.hotkey("alt", "f4")
            time.sleep(SLEEP)
            return True
        except Exception as e:
            log.warning(f"  Could not close '{w.title}': {e}")
    log.warning("  No plot window found to close")
    return False


# Approximate "Start Scan" button position from the signal quality modal
# (relative coordinates within the app window).
# From screenshot: button is ~58% across, ~78% down.
SIGNAL_START_SCAN_BUTTON = (0.58, 0.78)


def _wait_for_signal_quality_and_start_scan(timeout: int = 180) -> bool:
    """In Reduced Mode, after clicking Start the app auto-runs signal quality check.

    Wait up to `timeout` seconds for the 'Start Scan' button (in the signal
    quality modal) to be present, then click it.

    Detection methods (in order):
      1. UIA: find a button/element with text 'Start Scan' that is NOT the
         sidebar (different from the original Start position)
      2. UIA: find any 'Good signal quality' / 'signal quality' / 'All cameras'
         text in the descendant tree
      3. Coordinate fallback: blindly click the 'Start Scan' relative position

    Returns True if 'Start Scan' was clicked, False otherwise.
    """
    log.info(f"  Waiting up to {timeout}s for signal quality dialog...")
    elapsed = 0
    poll_interval = 5

    while elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval

        # Method 1: Look for 'Start Scan' UIA element directly
        try:
            win = uia_window()
            descendants = list(win.descendants())
            # Log all visible texts every 30s to debug what UIA sees
            if elapsed % 30 == 0:
                texts = []
                for elem in descendants:
                    try:
                        t = elem.window_text().strip()
                        if t and len(t) < 60:
                            texts.append(t)
                    except Exception:
                        continue
                log.info(f"  UIA texts visible at {elapsed}s: {texts[:30]}")

            for elem in descendants:
                try:
                    text = elem.window_text().strip()
                    if text == "Start Scan":
                        rect = elem.rectangle()
                        cx = (rect.left + rect.right) // 2
                        cy = (rect.top + rect.bottom) // 2
                        log.info(f"  Found 'Start Scan' button at ({cx}, {cy}) — clicking")
                        pyautogui.click(cx, cy)
                        time.sleep(SLEEP)
                        return True
                except Exception:
                    continue

            # Method 2: Look for any signal-quality-related text
            for elem in descendants:
                try:
                    text = elem.window_text().strip().lower()
                    if ("good signal quality" in text
                            or "all cameras are reporting" in text
                            or "ambient light" in text):
                        log.info(
                            f"  Signal quality modal text found at {elapsed}s — "
                            f"using coordinate click for 'Start Scan'"
                        )
                        ensure_visible()
                        w = get_app_window()
                        cx = int(w.left + SIGNAL_START_SCAN_BUTTON[0] * w.width)
                        cy = int(w.top + SIGNAL_START_SCAN_BUTTON[1] * w.height)
                        log.info(f"  Coordinate click 'Start Scan' at ({cx}, {cy})")
                        pyautogui.click(cx, cy)
                        time.sleep(SLEEP)
                        return True
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"  signal quality check failed: {e}")

        if elapsed % 30 == 0:
            log.info(f"  Still waiting for signal quality dialog... {elapsed}/{timeout}s")

    # Method 3: Final fallback — blindly click the Start Scan coordinate
    log.warning(
        f"  Signal quality dialog not detected via UIA after {timeout}s — "
        f"trying coordinate fallback for 'Start Scan'"
    )
    try:
        ensure_visible()
        w = get_app_window()
        cx = int(w.left + SIGNAL_START_SCAN_BUTTON[0] * w.width)
        cy = int(w.top + SIGNAL_START_SCAN_BUTTON[1] * w.height)
        log.info(f"  Coordinate fallback click 'Start Scan' at ({cx}, {cy})")
        pyautogui.click(cx, cy)
        time.sleep(SLEEP)
        return True
    except Exception as e:
        log.warning(f"  coordinate fallback failed: {e}")
    return False


def _move_window_on_screen():
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
        log.warning(f"  _move_window_on_screen failed: {e}")


def _selected_scan_text() -> str:
    """Read the current text of the scan-picker ComboBox in History."""
    try:
        win = uia_window()
        cb = win.child_window(control_type="ComboBox")
        if cb.exists(timeout=2):
            return cb.window_text().strip()
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────
# Test class — keyboard
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestReducedMode:
    """Reduced Mode workflow — Notes, Scan, History (keyboard-driven).

    Scan Settings is NOT visible in this layout.
    """

    # ── Notes: full feature test ─────────────────────────────────────────

    def test_01_open_notes(self, app):
        """Notes is at the former Scan Settings position in the reduced sidebar."""
        _move_window_on_screen()
        ensure_visible()
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reduced mode position)")

    def test_02_type_note(self, app):
        """Type a unique note and save it."""
        require_focus()
        TestReducedMode.session_note = f"ReducedScan_{datetime.now():%Y%m%d_%H%M%S}"
        log.info(f"  Typing note: '{TestReducedMode.session_note}'")
        pyautogui.typewrite(TestReducedMode.session_note, interval=0.04)
        time.sleep(SLEEP)

    def test_03_close_notes(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_04_persist_after_reopen(self, app):
        """Verify the note persists after closing and reopening."""
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reopen)")
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.3)
        clip = get_clipboard()
        assert TestReducedMode.session_note in clip, (
            f"Note not persisted: expected '{TestReducedMode.session_note}' "
            f"in clipboard, got: '{clip[:60]}'"
        )
        log.info(f"  Note persisted: '{clip[:60]}'")

    def test_05_append_text(self, app):
        """Append text to existing note."""
        require_focus()
        pyautogui.hotkey("ctrl", "end")
        time.sleep(0.2)
        pyautogui.typewrite(" -- appended", interval=0.04)
        time.sleep(SLEEP)

    def test_06_clear_and_multiline(self, app):
        """Clear textarea and type multi-line note."""
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.press("delete")
        time.sleep(0.3)
        for line in ["Line one", "Line two", "Line three"]:
            pyautogui.typewrite(line, interval=0.04)
            pyautogui.press("enter")
        time.sleep(SLEEP)

    def test_07_multiline_persists(self, app):
        """Close and reopen — verify multi-line note persists."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reopen)")
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.3)
        clip = get_clipboard()
        assert "Line one" in clip and "Line three" in clip, (
            f"Multi-line text not preserved: '{clip[:80]}'"
        )
        log.info("  Multi-line note persisted OK")

    def test_08_cut_paste(self, app):
        """Ctrl+X cuts text, Ctrl+V pastes it back."""
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "x")
        time.sleep(0.3)
        clip = get_clipboard()
        assert len(clip) > 0, "Ctrl+X did not put text in clipboard"
        pyautogui.hotkey("ctrl", "v")
        time.sleep(SLEEP)
        log.info("  Cut/paste OK")

    def test_09_close_notes_for_scan(self, app):
        """Clear and close notes before starting scan."""
        require_focus()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.press("delete")
        time.sleep(0.2)
        # Re-type the session note for the scan
        TestReducedMode.session_note = f"ReducedScan_{datetime.now():%Y%m%d_%H%M%S}"
        pyautogui.typewrite(TestReducedMode.session_note, interval=0.04)
        time.sleep(SLEEP)
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Scan: start, wait, stop ────────────────────────────────────────────

    def test_10_start_scan(self, app):
        """Click Start — the app auto-runs signal quality check, then click 'Start Scan'."""
        click_sidebar(*SIDEBAR_START, "Start scan")
        # The 'Good signal quality' dialog auto-appears
        _wait_for_signal_quality_and_start_scan()

    def test_11_wait_2_minutes(self, app):
        wait_with_log(SCAN_WAIT, "2-minute manual scan running")

    def test_12_stop_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Stop scan")
        log.info(f"  Waiting {STOP_BUFFER}s for scan data to save...")
        time.sleep(STOP_BUFFER)

    # ── History: verify scan, visualize BFI/BVI

    def test_13_open_history(self, app):
        click_sidebar(*SIDEBAR_HISTORY, "History")

    def test_14_latest_scan_selected(self, app):
        scan_text = _selected_scan_text()
        assert len(scan_text) > 0, (
            "History ComboBox is empty — no scans found."
        )
        log.info(f"  Latest scan in ComboBox: '{scan_text}'")

    def test_15_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_16_close_bfi_plot(self, app):
        _close_plot_window()

    def test_17_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)



# ─────────────────────────────────────────────
# Mouse-based test class — continues with Reduced Mode already ON
# from TestReducedMode above
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestReducedModeMouse:
    """Reduced Mode mouse workflow — Reduced Mode is already enabled by TestReducedMode.

    Scan Settings is NOT tested here — it is hidden while Reduced Mode is active.
    """

    # ── Notes: type session note ───────────────────────────────────────────

    def test_22_open_notes(self, app):
        """Notes is now at the former Scan Settings position in the reduced sidebar."""
        _move_window_on_screen()
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reduced mode position)")

    def test_23_type_note(self, app):
        require_focus()
        TestReducedModeMouse.session_note = (
            f"ReducedScanMouse_{datetime.now():%Y%m%d_%H%M%S}"
        )
        log.info(f"  Typing note: '{TestReducedModeMouse.session_note}'")
        pyautogui.typewrite(TestReducedModeMouse.session_note, interval=0.04)
        time.sleep(SLEEP)

    def test_24_close_notes(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Scan: start, wait, stop ────────────────────────────────────────────

    def test_25_start_scan(self, app):
        """Click Start — the app auto-runs signal quality check, then click 'Start Scan'."""
        click_sidebar(*SIDEBAR_START, "Start scan")
        # In Reduced Mode, the 'Good signal quality' dialog auto-appears
        _wait_for_signal_quality_and_start_scan()

    def test_26_wait_scan(self, app):
        wait_with_log(SCAN_WAIT, "manual scan running")

    def test_27_stop_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Stop scan")
        log.info(f"  Waiting {STOP_BUFFER}s for scan data to save...")
        time.sleep(STOP_BUFFER)

    # ── History: verify scan, visualize BFI/BVI only

    def test_28_open_history(self, app):
        click_sidebar(*SIDEBAR_HISTORY, "History")

    def test_29_latest_scan_selected(self, app):
        scan_text = _selected_scan_text()
        assert len(scan_text) > 0, (
            "History ComboBox is empty — no scans found."
        )
        log.info(f"  Latest scan in ComboBox: '{scan_text}'")

    def test_30_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_31_close_bfi_plot_mouse(self, app):
        """Move mouse to plot window center then close."""
        _close_plot_window_mouse()

    def test_32_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)


# ─────────────────────────────────────────────────────────────────────────
# Test Report Generation
# ─────────────────────────────────────────────────────────────────────────
# Collects each pytest test result and writes a structured report at session
# end suitable for verification & validation evidence.
# Output:
#   tests/test_logs/ReducedMode_Report_<timestamp>.json
#   tests/test_logs/ReducedMode_Report_<timestamp>.md

_REPORT_RESULTS = []
_REPORT_SESSION_START = None


def _report_get_app_version() -> str:
    """Try to read the app build version from the OpenWaterApp.exe path."""
    try:
        for proc_name in ["OpenWaterApp.exe", "OpenWaterApp_console.exe"]:
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
    """Collect environment info for the report header."""
    return {
        "tester": os.environ.get("TESTER_NAME", getpass.getuser()),
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "python_version": sys.version.split()[0],
        "app_version": _report_get_app_version(),
        "test_script": os.path.basename(__file__),
    }


@pytest.fixture(scope="session", autouse=True)
def _report_session():
    """Session-scoped autouse fixture.

    Captures start time and registers _write_report() to run via atexit —
    which fires AFTER pytest has written results.xml, ensuring we get the
    current run's results (not stale data from a previous run).
    """
    global _REPORT_SESSION_START
    _REPORT_SESSION_START = datetime.now()
    log.info(f"Test report session started at {_REPORT_SESSION_START}")
    atexit.register(_write_report)
    yield


def _parse_junit_xml(xml_path: Path) -> list:
    """Parse the pytest JUnit XML output and return per-test result dicts.

    Filters to only tests in this module's classes (TestReducedMode*).
    """
    results = []
    if not xml_path.exists():
        log.warning(f"  JUnit XML not found at {xml_path}")
        return results

    try:
        tree = ET.parse(xml_path)
    except Exception as e:
        log.warning(f"  Failed to parse {xml_path}: {e}")
        return results

    for testcase in tree.iter("testcase"):
        classname = testcase.get("classname", "")
        # Only include this file's tests
        if "test_reducedmode" not in classname:
            continue

        test_class = classname.split(".")[-1]
        test_id = testcase.get("name", "")
        duration = float(testcase.get("time", "0"))

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

        results.append({
            "test_id": test_id,
            "test_class": test_class,
            "description": "",  # JUnit XML doesn't include docstrings
            "status": status,
            "duration_sec": round(duration, 2),
            "timestamp": "",  # individual timestamps not in JUnit XML
            "details": details,
        })

    return results


def _write_report():
    """Write test report at end of session — parses results.xml."""
    log_dir_default = Path(__file__).parent / "test_logs"
    junit_xml = log_dir_default / "results.xml"

    # JUnit XML is written by pytest at session-finish, AFTER autouse fixture
    # teardown. So poll briefly for the file/content to be ready.
    for _ in range(10):
        if junit_xml.exists() and junit_xml.stat().st_size > 0:
            break
        time.sleep(0.5)

    global _REPORT_RESULTS
    _REPORT_RESULTS = _parse_junit_xml(junit_xml)
    if not _REPORT_RESULTS:
        log.warning("No test results captured — skipping report generation.")
        return

    log_dir = Path(__file__).parent / "test_logs"
    log_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    env = _report_get_environment()
    duration = (datetime.now() - _REPORT_SESSION_START).total_seconds() if _REPORT_SESSION_START else 0
    summary = {
        "total":   len(_REPORT_RESULTS),
        "passed":  sum(1 for r in _REPORT_RESULTS if r["status"] == "PASS"),
        "failed":  sum(1 for r in _REPORT_RESULTS if r["status"] == "FAIL"),
        "skipped": sum(1 for r in _REPORT_RESULTS if r["status"] == "SKIP"),
    }

    # ── JSON report ───────────────────────────────────────────────────
    report_data = {
        "report_title": "OpenWater BloodFlow — Reduced Mode Test Report",
        "purpose": "Verification & validation evidence for the Reduced Mode workflow.",
        "session_start": _REPORT_SESSION_START.isoformat(timespec="seconds") if _REPORT_SESSION_START else "",
        "session_end":   datetime.now().isoformat(timespec="seconds"),
        "duration_sec":  round(duration, 1),
        "environment":   env,
        "summary":       summary,
        "test_results":  _REPORT_RESULTS,
    }
    json_path = log_dir / f"ReducedMode_Report_{ts}.json"
    json_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")

    # ── Markdown report ───────────────────────────────────────────────
    lines = [
        f"# {report_data['report_title']}",
        "",
        f"**Purpose:** {report_data['purpose']}",
        "",
        "## Session Information",
        "",
        f"- **Session Start:** {report_data['session_start']}",
        f"- **Session End:** {report_data['session_end']}",
        f"- **Duration:** {report_data['duration_sec']}s",
        "",
        "## Test Environment",
        "",
        f"- **Tester:** {env['tester']}",
        f"- **Hostname:** {env['hostname']}",
        f"- **Operating System:** {env['os']}",
        f"- **Python Version:** {env['python_version']}",
        f"- **Application Version:** {env['app_version']}",
        f"- **Test Script:** {env['test_script']}",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Total  | {summary['total']} |",
        f"| Passed | {summary['passed']} |",
        f"| Failed | {summary['failed']} |",
        f"| Skipped | {summary['skipped']} |",
        "",
        "## Test Results",
        "",
        "| # | Test ID | Class | Description | Status | Duration (s) | Timestamp |",
        "|---|---------|-------|-------------|--------|--------------|-----------|",
    ]
    for i, r in enumerate(_REPORT_RESULTS, 1):
        desc = r["description"].replace("|", "\\|") if r["description"] else "—"
        lines.append(
            f"| {i} | `{r['test_id']}` | {r['test_class']} | {desc} | "
            f"**{r['status']}** | {r['duration_sec']} | {r['timestamp']} |"
        )

    failures = [r for r in _REPORT_RESULTS if r["status"] == "FAIL"]
    if failures:
        lines += ["", "## Failure Details", ""]
        for r in failures:
            lines += [
                f"### {r['test_id']}",
                "",
                f"- **Class:** {r['test_class']}",
                f"- **Timestamp:** {r['timestamp']}",
                f"- **Description:** {r['description'] or 'N/A'}",
                f"- **Error:** `{r['details']}`",
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
        f"_Report generated automatically by `{env['test_script']}` on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
    ]
    md_path = log_dir / f"ReducedMode_Report_{ts}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    log.info("Test Report written:")
    log.info(f"  JSON: {json_path}")
    log.info(f"  Markdown: {md_path}")
