"""
Reduced Mode — end-to-end test.

Covers the full Reduced Mode workflow using the global Settings modal (gear icon).
Sensor dropdowns are a Scan Settings feature and are NOT tested here — Scan Settings
is hidden while Reduced Mode is active.

Two classes:
  TestReducedMode       (01–20) — keyboard-driven interactions
  TestReducedModeMouse  (21–40) — mouse-driven interactions
"""

import time
from datetime import datetime

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
    log,
    require_focus,
    uia_window,
    wait_with_log,
)

# ─────────────────────────────────────────────
# Sidebar coordinates
# ─────────────────────────────────────────────
SIDEBAR_SETTINGS      = (0.019, 0.920)   # gear icon — bottommost sidebar button
SIDEBAR_NOTES_REDUCED = (0.019, 0.210)   # Notes position when Scan Settings is hidden
SIDEBAR_START         = (0.019, 0.115)   # Start / Stop toggle button
SIDEBAR_HISTORY       = (0.020, 0.820)   # History icon

# Relative coordinate of the Reduced Mode Enable toggle within the app window.
# Measured from screenshot — adjust if the toggle position shifts.
REDUCED_MODE_TOGGLE = (0.400, 0.421)

_TABS_TO_REDUCED_MODE = 16

SCAN_WAIT   = 200   # seconds to run the scan (3 minutes 20 seconds)
STOP_BUFFER = 15    # seconds to wait after stopping for data to save
VIZ_WAIT    = 60    # seconds to leave each plot open


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _tab_to_reduced_mode_toggle(tab_into_modal: bool = True):
    """Tab from the current focus to the Reduced Mode Enable toggle in the
    Settings modal, then press Space to toggle it.

    tab_into_modal=True  — press one extra Tab to enter the modal first
                           (use when the modal was just opened and nothing
                           inside has focus yet).
    tab_into_modal=False — skip that first Tab (use when a field inside the
                           modal already has focus).
    """
    require_focus()
    if tab_into_modal:
        pyautogui.press("tab")   # enter modal — lands on first interactive element
        time.sleep(0.3)
    log.info(f"  tabbing {_TABS_TO_REDUCED_MODE} times to Reduced Mode Enable toggle")
    for _ in range(_TABS_TO_REDUCED_MODE):
        pyautogui.press("tab")
        time.sleep(0.1)
    pyautogui.press("space")
    time.sleep(SLEEP)


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


def _scroll_modal_to_bottom():
    """Scroll the Settings modal content down to reveal the Reduced Mode section.

    Scrolls in three passes with a short pause between each to handle
    modals that animate or load content progressively.
    """
    ensure_visible()
    w = get_app_window()
    cx = w.left + w.width // 2
    cy = w.top + w.height // 2
    pyautogui.moveTo(cx, cy, duration=0.2)
    for _ in range(3):
        pyautogui.scroll(-50)   # scroll down
        time.sleep(0.3)
    time.sleep(0.5)
    log.info("  Modal scrolled to bottom")


def _click_coord(rx: float, ry: float, label: str = ""):
    """Move mouse to a relative coordinate within the app window and click."""
    ensure_visible()
    w = get_app_window()
    x = int(w.left + rx * w.width)
    y = int(w.top + ry * w.height)
    log.info(f"  click '{label}'  rel({rx:.3f}, {ry:.3f})  abs({x}, {y})")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click(x, y)
    time.sleep(SLEEP)


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
    """Enable Reduced Mode, run a manual scan, verify History, then restore.

    Uses keyboard interactions. Scan Settings is NOT tested here — it is
    hidden while Reduced Mode is active.
    """

    # ── Settings: enable Reduced Mode ─────────────────────────────────────

    def test_01_open_settings(self, app):
        ensure_visible()
        click_sidebar(*SIDEBAR_SETTINGS, "Settings gear icon")

    def test_02_camera_config_visible(self, app):
        """Default Camera Configuration section is visible at the top."""
        pass  # visual confirmation only

    def test_03_enable_reduced_mode(self, app):
        """Tab into the Settings modal to the Reduced Mode Enable toggle and turn ON."""
        _tab_to_reduced_mode_toggle(tab_into_modal=True)
        log.info("  Reduced Mode enabled")

    def test_04_close_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Notes: type session note ───────────────────────────────────────────

    def test_05_open_notes(self, app):
        """Notes is now at the former Scan Settings position in the reduced sidebar."""
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reduced mode position)")

    def test_06_type_note(self, app):
        require_focus()
        TestReducedMode.session_note = f"ReducedScan_{datetime.now():%Y%m%d_%H%M%S}"
        log.info(f"  Typing note: '{TestReducedMode.session_note}'")
        pyautogui.typewrite(TestReducedMode.session_note, interval=0.04)
        time.sleep(SLEEP)

    def test_07_close_notes(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Scan: start, wait, stop ────────────────────────────────────────────

    def test_08_start_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Start scan")

    def test_09_wait_2_minutes(self, app):
        wait_with_log(SCAN_WAIT, "2-minute manual scan running")

    def test_10_stop_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Stop scan")
        log.info(f"  Waiting {STOP_BUFFER}s for scan data to save...")
        time.sleep(STOP_BUFFER)

    # ── History: verify scan, visualize BFI/BVI only (no Contrast/Mean in Reduced Mode)

    def test_11_open_history(self, app):
        click_sidebar(*SIDEBAR_HISTORY, "History")

    def test_12_latest_scan_selected(self, app):
        scan_text = _selected_scan_text()
        assert len(scan_text) > 0, (
            "History ComboBox is empty — no scans found."
        )
        log.info(f"  Latest scan in ComboBox: '{scan_text}'")

    def test_13_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_14_close_bfi_plot(self, app):
        _close_plot_window()

    def test_15_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Settings: disable Reduced Mode ────────────────────────────────────

    def test_16_reopen_settings(self, app):
        click_sidebar(*SIDEBAR_SETTINGS, "Settings gear icon (reopen)")

    def test_17_disable_reduced_mode(self, app):
        """Tab into the Settings modal to the Reduced Mode Enable toggle and turn OFF."""
        _tab_to_reduced_mode_toggle(tab_into_modal=True)
        log.info("  Reduced Mode disabled")

    def test_18_close_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)


# ─────────────────────────────────────────────
# Mouse-based test class — same workflow as TestReducedMode
# but every feature interaction uses mouse movement + click
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestReducedModeMouse:
    """Same end-to-end Reduced Mode workflow as TestReducedMode,
    with every feature interaction driven by mouse movement and clicks.

    Scan Settings is NOT tested here — it is hidden while Reduced Mode is active.
    """

    # ── Settings: enable Reduced Mode ─────────────────────────────────────

    def test_19_open_settings(self, app):
        ensure_visible()
        click_sidebar(*SIDEBAR_SETTINGS, "Settings gear icon")

    def test_20_camera_config_visible(self, app):
        """Default Camera Configuration section is visible at the top."""
        pass  # visual confirmation only

    def test_21_enable_reduced_mode_mouse(self, app):
        """Scroll to Reduced Mode section and click the Enable toggle ON via mouse."""
        _scroll_modal_to_bottom()
        _click_coord(*REDUCED_MODE_TOGGLE, "Reduced Mode Enable toggle ON")
        log.info("  Reduced Mode enabled (mouse)")

    def test_22_close_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Notes: type session note ───────────────────────────────────────────

    def test_23_open_notes(self, app):
        """Notes is now at the former Scan Settings position in the reduced sidebar."""
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reduced mode position)")

    def test_24_type_note(self, app):
        require_focus()
        TestReducedModeMouse.session_note = (
            f"ReducedScanMouse_{datetime.now():%Y%m%d_%H%M%S}"
        )
        log.info(f"  Typing note: '{TestReducedModeMouse.session_note}'")
        pyautogui.typewrite(TestReducedModeMouse.session_note, interval=0.04)
        time.sleep(SLEEP)

    def test_25_close_notes(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Scan: start, wait, stop ────────────────────────────────────────────

    def test_26_start_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Start scan")

    def test_27_wait_scan(self, app):
        wait_with_log(SCAN_WAIT, "manual scan running")

    def test_28_stop_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Stop scan")
        log.info(f"  Waiting {STOP_BUFFER}s for scan data to save...")
        time.sleep(STOP_BUFFER)

    # ── History: verify scan, visualize BFI/BVI only (no Contrast/Mean in Reduced Mode)

    def test_29_open_history(self, app):
        click_sidebar(*SIDEBAR_HISTORY, "History")

    def test_30_latest_scan_selected(self, app):
        scan_text = _selected_scan_text()
        assert len(scan_text) > 0, (
            "History ComboBox is empty — no scans found."
        )
        log.info(f"  Latest scan in ComboBox: '{scan_text}'")

    def test_31_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_32_close_bfi_plot_mouse(self, app):
        """Move mouse to plot window center then close."""
        _close_plot_window_mouse()

    def test_33_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Settings: disable Reduced Mode ────────────────────────────────────

    def test_34_reopen_settings(self, app):
        click_sidebar(*SIDEBAR_SETTINGS, "Settings gear icon (reopen)")

    def test_35_disable_reduced_mode_mouse(self, app):
        """Scroll to Reduced Mode section and click the Enable toggle OFF via mouse."""
        _scroll_modal_to_bottom()
        _click_coord(*REDUCED_MODE_TOGGLE, "Reduced Mode Enable toggle OFF")
        log.info("  Reduced Mode disabled (mouse)")

    def test_36_close_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
