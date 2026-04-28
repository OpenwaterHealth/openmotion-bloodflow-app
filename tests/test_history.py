"""
History — modal and visualization tests.


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
    wait_with_log,
)
from utils import close_plot_window, selected_scan_text

SIDEBAR_HISTORY = (0.020, 0.830)
VIZ_WAIT = 60  # seconds to leave each plot open


@pytest.mark.incremental
class TestHistory:
    """History modal — scan listing and visualization."""

    def test_01_open(self, app):
        click_sidebar(*SIDEBAR_HISTORY, "History sidebar button")

    def test_02_latest_scan_listed(self, app):
        scan_text = selected_scan_text()
        assert len(scan_text) > 0, (
            "ComboBox is empty -- no scans found. Run a scan first."
        )
        log.info(f"  Scan ComboBox text: '{scan_text}'")

    def test_03_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_04_close_bfi_plot(self, app):
        close_plot_window()

    def test_05_visualize_contrast_mean(self, app):
        ensure_visible()
        click_by_name("Visualize Contrast/Mean")
        wait_with_log(VIZ_WAIT, "Contrast/Mean plot open")

    def test_06_close_contrast_plot(self, app):
        close_plot_window()

    def test_07_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
