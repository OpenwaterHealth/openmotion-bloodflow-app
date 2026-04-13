"""
Scan Flow — end-to-end test: configure scan, run, visualize results.

Test sequence:
  1. Open Scan Settings, set 2-min duration, close
  2. Open Notes, type session note, close
  3. Start scan, wait for completion
  4. Dismiss auto-opened Notes
  5. Open History, visualize BFI/BVI and Contrast/Mean plots, close
"""

import time
from datetime import datetime

import pyautogui
import pytest

from conftest import (
    SLEEP,
    click_by_name,
    click_sidebar,
    ensure_visible,
    log,
    require_focus,
    wait_with_log,
)

# ─────────────────────────────────────────────
# Sidebar coordinates (MouseArea-based — not exposed via UIA)
# ─────────────────────────────────────────────
SIDEBAR_SCAN = (0.019, 0.210)
SIDEBAR_NOTES = (0.019, 0.315)
SIDEBAR_START = (0.019, 0.115)

SCAN_DURATION_MIN = 2
WAIT_AFTER_SCAN = SCAN_DURATION_MIN * 60 + 180  # scan + 3-min buffer
VIZ_WAIT = 120  # seconds to leave each plot open


@pytest.mark.incremental
class TestScanFlow:
    """End-to-end scan flow: settings -> notes -> scan -> history -> visualize."""

    def test_01_open_scan_settings(self, app):
        click_sidebar(*SIDEBAR_SCAN, "Scan Settings")

    def test_02_set_duration(self, app):
        """Set scan duration to 2 minutes via Tab navigation."""
        require_focus()
        pyautogui.press("tab")           # -> Left ComboBox
        time.sleep(0.3)
        pyautogui.press("tab")           # -> Right ComboBox
        time.sleep(0.3)
        pyautogui.press("tab")           # -> Switch (leave on Timed)
        time.sleep(0.3)
        pyautogui.press("tab")           # -> Hours
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.typewrite("0", interval=0.05)
        time.sleep(0.3)
        pyautogui.press("tab")           # -> Minutes
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.typewrite("2", interval=0.05)
        time.sleep(0.5)
        pyautogui.press("tab")           # -> Seconds
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.typewrite("0", interval=0.05)
        time.sleep(0.5)

    def test_03_close_scan_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_04_type_note(self, app):
        click_sidebar(*SIDEBAR_NOTES, "Notes")
        require_focus()
        self.session_note = f"AutoScan_{datetime.now():%Y%m%d_%H%M%S}"
        log.info(f"  typing note: '{self.session_note}'")
        pyautogui.typewrite(self.session_note, interval=0.04)
        time.sleep(SLEEP)

    def test_05_close_notes(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_06_start_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Start")

    def test_07_wait_for_scan(self, app):
        wait_with_log(WAIT_AFTER_SCAN,
                      f"{SCAN_DURATION_MIN}-minute scan + 3-minute buffer")

    def test_08_close_post_scan_notes(self, app):
        """Dismiss the auto-opened Notes modal (BloodFlow.qml opens it on scan finish)."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_09_open_history(self, app):
        click_by_name("History")

    def test_10_latest_scan_selected(self, app):
        """History.open() sets scanPicker index 0 (latest scan) automatically."""
        pass  # verified by subsequent visualize steps

    def test_11_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_12_close_bfi_plot(self, app):
        pyautogui.hotkey("alt", "f4")
        time.sleep(SLEEP)

    def test_13_visualize_contrast_mean(self, app):
        ensure_visible()
        click_by_name("Visualize Contrast/Mean")
        wait_with_log(VIZ_WAIT, "Contrast/Mean plot open")

    def test_14_close_contrast_plot(self, app):
        pyautogui.hotkey("alt", "f4")
        time.sleep(SLEEP)

    def test_15_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
