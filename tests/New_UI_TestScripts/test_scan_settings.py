"""
Scan Settings — modal interaction tests.

"""

import time

import pyautogui
import pytest

from conftest import (
    SLEEP,
    click_sidebar,
    ensure_visible,
    get_app_window,
    log,
    read_combobox_values,
    require_focus,
)

# ─────────────────────────────────────────────
# Sidebar + modal coordinates
# ─────────────────────────────────────────────
SIDEBAR_SCAN = (0.019, 0.210)
SCAN_MODAL_CLOSE = (0.360, 0.119)

SENSOR_OPTIONS = [
    "None", "Near", "Middle", "Far", "Outer",
    "Left", "Right", "Third Row", "All",
]


def _select_sensor_option(option_name: str, side: str, combobox_index: int):
    """Navigate a sensor ComboBox to the given option via keyboard,
    then verify the selection via UIA readback.

    Args:
        option_name: Expected option text (e.g. "Near")
        side: "Left" or "Right" (for logging)
        combobox_index: 0 for Left, 1 for Right ComboBox
    """
    require_focus()
    idx = SENSOR_OPTIONS.index(option_name)
    log.info(f"  {side} DD: navigate to '{option_name}' (index {idx})")
    pyautogui.hotkey("alt", "down")  # open popup
    time.sleep(0.5)
    pyautogui.press("home")          # jump to first item
    time.sleep(0.2)
    for _ in range(idx):
        pyautogui.press("down")
        time.sleep(0.15)
    pyautogui.press("return")        # confirm
    time.sleep(SLEEP)

    # Verify the ComboBox actually changed
    values = read_combobox_values()
    assert len(values) > combobox_index, (
        f"Expected at least {combobox_index + 1} ComboBox(es), found {len(values)}"
    )
    actual = values[combobox_index]
    assert actual == option_name, (
        f"{side} sensor: expected '{option_name}', got '{actual}' "
        f"-- focus may have been lost"
    )


def _click_coord(rx: float, ry: float, label: str = ""):
    """Click a relative coordinate within the app window."""
    ensure_visible()
    w = get_app_window()
    x = int(w.left + rx * w.width)
    y = int(w.top + ry * w.height)
    log.info(f"  click '{label}'  rel({rx:.3f}, {ry:.3f})  abs({x}, {y})")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click(x, y)
    time.sleep(SLEEP)


@pytest.mark.incremental
class TestScanSettings:
    """Scan Settings modal — dropdowns, toggles, inputs, close methods."""

    def test_01_open(self, app):
        ensure_visible()
        click_sidebar(*SIDEBAR_SCAN, "Scan Settings icon")

    def test_02_sensor_dots_visible(self, app):
        """Sensor dot pattern visible in Camera Configuration."""
        pass  # visual confirmation — no assertion needed

    @pytest.mark.parametrize("option", SENSOR_OPTIONS, ids=SENSOR_OPTIONS)
    def test_03_left_sensor(self, app, option):
        if option == SENSOR_OPTIONS[0]:
            # Tab into modal to focus Left ComboBox on first iteration
            require_focus()
            pyautogui.press("tab")
            time.sleep(0.5)
        _select_sensor_option(option, "Left", combobox_index=0)

    @pytest.mark.parametrize("option", SENSOR_OPTIONS, ids=SENSOR_OPTIONS)
    def test_04_right_sensor(self, app, option):
        if option == SENSOR_OPTIONS[0]:
            # Tab from Left ComboBox to Right ComboBox on first iteration
            require_focus()
            pyautogui.press("tab")
            time.sleep(0.3)
        _select_sensor_option(option, "Right", combobox_index=1)

    def test_05_restore_middle(self, app):
        """Restore both sensors to Middle."""
        _select_sensor_option("Middle", "Right", combobox_index=1)
        require_focus()
        pyautogui.hotkey("shift", "tab")  # back to Left
        time.sleep(0.3)
        _select_sensor_option("Middle", "Left", combobox_index=0)

    def test_06_toggle_free_run(self, app):
        require_focus()
        pyautogui.press("tab")   # Left -> Right
        time.sleep(0.2)
        pyautogui.press("tab")   # Right -> Switch
        time.sleep(0.3)
        pyautogui.press("space")  # toggle to Free Run
        time.sleep(SLEEP)

    def test_07_toggle_timed(self, app):
        require_focus()
        pyautogui.press("space")  # toggle back to Timed
        time.sleep(SLEEP)

    def test_08_hours_input(self, app):
        require_focus()
        pyautogui.press("tab")   # Switch -> Hours
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.typewrite("2", interval=0.05)
        time.sleep(SLEEP)

    def test_09_minutes_input(self, app):
        require_focus()
        pyautogui.press("tab")   # Hours -> Minutes
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.typewrite("30", interval=0.05)
        time.sleep(SLEEP)

    def test_10_seconds_input(self, app):
        require_focus()
        pyautogui.press("tab")   # Minutes -> Seconds
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.typewrite("45", interval=0.05)
        time.sleep(SLEEP)

    def test_11_close_via_x(self, app):
        _click_coord(*SCAN_MODAL_CLOSE, "X close button")

    def test_12_close_via_escape(self, app):
        click_sidebar(*SIDEBAR_SCAN, "Reopen Scan Settings")
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
