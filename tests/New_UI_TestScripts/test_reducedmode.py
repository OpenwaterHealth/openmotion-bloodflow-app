"""
Reduced Mode — end-to-end test.

This test covers the full Reduced Mode workflow
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
    log,
    read_combobox_values,
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

SENSOR_OPTIONS = [
    "None", "Near", "Middle", "Far", "Outer",
    "Left", "Right", "Third Row", "All",
]


_TABS_TO_REDUCED_MODE = 17

SCAN_WAIT   = 200   # seconds to run the scan (3 minutes 20 seconds)
STOP_BUFFER = 15    # seconds to wait after stopping for data to save
VIZ_WAIT    = 60    # seconds to leave each plot open


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _select_sensor_option(option_name: str, side: str, combobox_index: int):
    """Navigate a sensor ComboBox to the given option via keyboard,
    then verify the selection via UIA readback."""
    require_focus()
    idx = SENSOR_OPTIONS.index(option_name)
    log.info(f"  {side} DD: navigate to '{option_name}' (index {idx})")
    pyautogui.hotkey("alt", "down")
    time.sleep(0.5)
    pyautogui.press("home")
    time.sleep(0.2)
    for _ in range(idx):
        pyautogui.press("down")
        time.sleep(0.15)
    pyautogui.press("return")
    time.sleep(SLEEP)

    values = read_combobox_values()
    assert len(values) > combobox_index, (
        f"Expected at least {combobox_index + 1} ComboBox(es), found {len(values)}"
    )
    actual = values[combobox_index]
    assert actual == option_name, (
        f"{side} sensor: expected '{option_name}', got '{actual}' "
        f"-- focus may have been lost"
    )


def _tab_to_reduced_mode_toggle(tab_into_modal: bool = False):
    """Tab to the Reduced Mode Enable toggle and press Space to toggle it.

    Args:
        tab_into_modal: True when the modal was just (re)opened and focus
                        is not yet on the Left ComboBox — adds one Tab first.
    """
    require_focus()
    if tab_into_modal:
        pyautogui.press("tab")   # enter modal — lands on Left CB
        time.sleep(0.3)
    log.info(f"  tabbing {_TABS_TO_REDUCED_MODE} times to Reduced Mode Enable toggle")
    for _ in range(_TABS_TO_REDUCED_MODE):
        pyautogui.press("tab")
        time.sleep(0.1)
    pyautogui.press("space")
    time.sleep(SLEEP)


def _close_plot_window() -> bool:
    """Close the plot window opened by the app.

    Iterates all top-level windows and closes the first one whose title
    does not match the main app keywords, avoiding closing the main app.
    """
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
# Test class
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestReducedMode:
    """Enable Reduced Mode, run a manual scan, verify History, then restore."""

    # ── Settings: sensor dropdowns + enable Reduced Mode ──────────────────

    def test_01_open_settings(self, app):
        ensure_visible()
        click_sidebar(*SIDEBAR_SETTINGS, "Settings gear icon")

    def test_02_camera_config_visible(self, app):
        """Default Camera Configuration section is visible at the top."""
        pass  # visual confirmation only

    @pytest.mark.parametrize("option", SENSOR_OPTIONS, ids=SENSOR_OPTIONS)
    def test_03_left_sensor(self, app, option):
        if option == SENSOR_OPTIONS[0]:
            require_focus()
            pyautogui.press("tab")   # enter modal, focus Left CB
            time.sleep(0.5)
        _select_sensor_option(option, "Left", combobox_index=0)

    @pytest.mark.parametrize("option", SENSOR_OPTIONS, ids=SENSOR_OPTIONS)
    def test_04_right_sensor(self, app, option):
        if option == SENSOR_OPTIONS[0]:
            require_focus()
            pyautogui.press("tab")   # Left CB -> Right CB
            time.sleep(0.3)
        _select_sensor_option(option, "Right", combobox_index=1)

    def test_05_restore_middle(self, app):
        """Restore both sensors to Middle."""
        _select_sensor_option("Middle", "Right", combobox_index=1)
        require_focus()
        pyautogui.hotkey("shift", "tab")   # back to Left CB
        time.sleep(0.3)
        _select_sensor_option("Middle", "Left", combobox_index=0)

    def test_06_enable_reduced_mode(self, app):
        """Tab from Left CB to Reduced Mode Enable toggle and turn ON."""
        _tab_to_reduced_mode_toggle(tab_into_modal=False)
        log.info("  Reduced Mode enabled")

    def test_07_close_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Notes: type session note ───────────────────────────────────────────

    def test_08_open_notes(self, app):
        """Notes is now at the former Scan Settings position in reduced sidebar."""
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reduced mode position)")

    def test_09_type_note(self, app):
        require_focus()
        TestReducedMode.session_note = f"ReducedScan_{datetime.now():%Y%m%d_%H%M%S}"
        log.info(f"  Typing note: '{TestReducedMode.session_note}'")
        pyautogui.typewrite(TestReducedMode.session_note, interval=0.04)
        time.sleep(SLEEP)

    def test_10_close_notes(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Scan: start, wait 2 minutes, stop ─────────────────────────────────

    def test_11_start_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Start scan")

    def test_12_wait_2_minutes(self, app):
        wait_with_log(SCAN_WAIT, "2-minute manual scan running")

    def test_13_stop_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Stop scan")
        log.info(f"  Waiting {STOP_BUFFER}s for scan data to save...")
        time.sleep(STOP_BUFFER)

    # ── History: verify scan, visualize plots ──────────────────────────────

    def test_14_open_history(self, app):
        click_sidebar(*SIDEBAR_HISTORY, "History")

    def test_15_latest_scan_selected(self, app):
        scan_text = _selected_scan_text()
        assert len(scan_text) > 0, (
            "History ComboBox is empty — no scans found."
        )
        log.info(f"  Latest scan in ComboBox: '{scan_text}'")

    def test_16_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_17_close_bfi_plot(self, app):
        _close_plot_window()

    def test_18_visualize_contrast_mean(self, app):
        ensure_visible()
        click_by_name("Visualize Contrast/Mean")
        wait_with_log(VIZ_WAIT, "Contrast/Mean plot open")

    def test_19_close_contrast_plot(self, app):
        _close_plot_window()

    def test_20_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Settings: disable Reduced Mode ────────────────────────────────────

    def test_21_reopen_settings(self, app):
        click_sidebar(*SIDEBAR_SETTINGS, "Settings gear icon (reopen)")

    def test_22_disable_reduced_mode(self, app):
        """Tab to Reduced Mode Enable toggle and turn OFF to restore default layout."""
        _tab_to_reduced_mode_toggle(tab_into_modal=True)
        log.info("  Reduced Mode disabled")

    def test_23_close_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
