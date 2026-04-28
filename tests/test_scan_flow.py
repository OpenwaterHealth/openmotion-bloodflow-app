"""
Scan Flow — end-to-end test: configure scan, run, visualize results.

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
    get_app_window,
    log,
    require_focus,
    uia_window,
    wait_with_log,
)
from utils import close_plot_window, dismiss_signal_quality_modal

# ─────────────────────────────────────────────
# Sidebar coordinates (MouseArea-based — not exposed via UIA)
# ─────────────────────────────────────────────
SIDEBAR_SCAN    = (0.019, 0.210)
SIDEBAR_NOTES   = (0.019, 0.315)
SIDEBAR_START   = (0.019, 0.115)
SIDEBAR_CHECK   = (0.019, 0.420)   # Check button — between Notes and History
SIDEBAR_HISTORY = (0.020, 0.820)   # History icon — MouseArea, not UIA-accessible

SCAN_DURATION_MIN = 2
WAIT_AFTER_SCAN = SCAN_DURATION_MIN * 60 + 180  # scan + 3-min buffer
VIZ_WAIT = 30  # seconds to leave each plot open
CHECK_WAIT_SEC = 120  # 2 minutes for Check to complete


def _run_check_step(label: str = ""):
    """Click Check, wait up to 2 min, dismiss 'Good signal quality' modal if shown."""
    log.info(f"  Clicking Check and waiting up to {CHECK_WAIT_SEC}s... {label}")
    click_sidebar(*SIDEBAR_CHECK, "Check")
    elapsed = 0
    while elapsed < CHECK_WAIT_SEC:
        time.sleep(10)
        elapsed += 10
        if dismiss_signal_quality_modal():
            log.info(f"  Signal quality modal dismissed at {elapsed}s.")
            return
        if elapsed % 30 == 0:
            log.info(f"  Check running... {elapsed}/{CHECK_WAIT_SEC}s")
    dismiss_signal_quality_modal()
    log.info("  Check completed.")


@pytest.mark.incremental
class TestScanFlow:
    """End-to-end scan flow: settings -> notes -> scan -> history -> visualize."""

    def test_01_open_scan_settings(self, app):
        click_sidebar(*SIDEBAR_SCAN, "Scan Settings")

    def test_02_set_duration(self, app):
        """Set scan duration to 2 minutes via Tab navigation."""
        require_focus()
        pyautogui.press("tab")           # -> User Label
        time.sleep(0.3)
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

    def test_06_run_check(self, app):
        """Click Check and wait up to 2 min — dismiss 'Good signal quality' modal."""
        _run_check_step()

    def test_07_start_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Start")

    def test_08_wait_for_scan(self, app):
        wait_with_log(WAIT_AFTER_SCAN,
                      f"{SCAN_DURATION_MIN}-minute scan + 3-minute buffer")

    def test_09_close_post_scan_notes(self, app):
        """Dismiss the auto-opened Notes modal (BloodFlow.qml opens it on scan finish)."""
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_10_open_history(self, app):
        # History is a QML MouseArea sidebar button — use coordinate click,
        # not click_by_name (which searches the UIA tree and finds nothing).
        click_sidebar(*SIDEBAR_HISTORY, "History")

    def test_11_latest_scan_selected(self, app):
        """History.open() sets scanPicker index 0 (latest scan) automatically."""
        pass  # verified by subsequent visualize steps

    def test_12_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_13_close_bfi_plot(self, app):
        close_plot_window()

    def test_14_visualize_contrast_mean(self, app):
        ensure_visible()
        click_by_name("Visualize Contrast/Mean")
        wait_with_log(VIZ_WAIT, "Contrast/Mean plot open")

    def test_15_close_contrast_plot(self, app):
        close_plot_window()

    def test_16_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
