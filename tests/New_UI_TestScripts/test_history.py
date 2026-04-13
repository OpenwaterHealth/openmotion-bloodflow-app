"""
History — modal and visualization tests.

Test sequence:
  1. Open History modal via sidebar
  2. Confirm latest scan is listed in ComboBox
  3. Visualize BFI/BVI plot, wait, close
  4. Visualize Contrast/Mean plot, wait, close
  5. Close History modal
"""

import time

import pyautogui
import pytest

from conftest import (
    SLEEP,
    click_by_name,
    click_sidebar,
    ensure_visible,
    log,
    require_focus,
    uia_window,
    wait_with_log,
)

SIDEBAR_HISTORY = (0.020, 0.830)
VIZ_WAIT = 120  # seconds to leave each plot open


def _selected_scan_text() -> str:
    """Read the current text of the scan-picker ComboBox."""
    try:
        win = uia_window()
        cb = win.child_window(control_type="ComboBox")
        if cb.exists(timeout=2):
            return cb.window_text().strip()
    except Exception:
        pass
    return ""


@pytest.mark.incremental
class TestHistory:
    """History modal — scan listing and visualization."""

    def test_01_open(self, app):
        click_sidebar(*SIDEBAR_HISTORY, "History sidebar button")

    def test_02_latest_scan_listed(self, app):
        scan_text = _selected_scan_text()
        assert len(scan_text) > 0, (
            "ComboBox is empty -- no scans found. Run a scan first."
        )
        log.info(f"  Scan ComboBox text: '{scan_text}'")

    def test_03_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_04_close_bfi_plot(self, app):
        ensure_visible()
        pyautogui.hotkey("alt", "f4")
        time.sleep(SLEEP)

    def test_05_visualize_contrast_mean(self, app):
        ensure_visible()
        click_by_name("Visualize Contrast/Mean")
        wait_with_log(VIZ_WAIT, "Contrast/Mean plot open")

    def test_06_close_contrast_plot(self, app):
        ensure_visible()
        pyautogui.hotkey("alt", "f4")
        time.sleep(SLEEP)

    def test_07_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
