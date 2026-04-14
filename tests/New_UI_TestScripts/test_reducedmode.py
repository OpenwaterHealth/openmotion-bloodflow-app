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


def _tab_to_reduced_mode_toggle(tab_into_modal: bool = False): # Tab from current focus to the Reduced Mode Enable toggle in Settings modal, then press Space to toggle it.

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


def _close_plot_window() -> bool: # close the plot window opened by the app, using keyboard shortcuts

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


def _close_plot_window_mouse() -> bool: # same as above but moves mouse to window center before closing

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


def _select_sensor_option_mouse(option_name: str, side: str, combobox_index: int):
    """Open a sensor ComboBox by mouse click, select the option by clicking
    the matching ListItem via UIA (falls back to arrow keys if not exposed),
    then verify via UIA readback."""
    require_focus()
    idx = SENSOR_OPTIONS.index(option_name)
    log.info(f"  {side} CB mouse: clicking '{option_name}' (index {idx})")

    # Move mouse to the ComboBox and click to open it
    win = uia_window()
    cbs = win.descendants(control_type="ComboBox")
    assert len(cbs) > combobox_index, (
        f"ComboBox[{combobox_index}] not found in UIA tree"
    )
    rect = cbs[combobox_index].rectangle()
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    pyautogui.moveTo(cx, cy, duration=0.3)
    pyautogui.click(cx, cy)
    time.sleep(0.5)

    # Try to find and click the matching ListItem via UIA
    clicked = False
    try:
        items = win.descendants(control_type="ListItem")
        for item in items:
            if item.window_text().strip() == option_name:
                ir = item.rectangle()
                ix = (ir.left + ir.right) // 2
                iy = (ir.top + ir.bottom) // 2
                pyautogui.moveTo(ix, iy, duration=0.2)
                pyautogui.click(ix, iy)
                clicked = True
                log.info(f"     UIA ListItem click: PASSED — '{option_name}' at ({ix},{iy})")
                break
        if not clicked:
            log.warning(
                f"     UIA ListItem lookup: FAILED — '{option_name}' not found in UIA tree"
            )
    except Exception as e:
        log.warning(f"     UIA ListItem lookup: FAILED — {e}")

    # Fallback: keyboard arrow navigation when UIA list is not exposed
    if not clicked:
        pyautogui.press("home")
        time.sleep(0.2)
        for _ in range(idx):
            pyautogui.press("down")
            time.sleep(0.15)
        pyautogui.press("return")
        log.info(f"     Arrow key fallback: PASSED — navigated to '{option_name}'")

    time.sleep(SLEEP)

    values = read_combobox_values()
    assert len(values) > combobox_index, (
        f"Expected at least {combobox_index + 1} ComboBox(es), found {len(values)}"
    )
    actual = values[combobox_index]
    assert actual == option_name, (
        f"{side} sensor (mouse): expected '{option_name}', got '{actual}'"
    )


def _scroll_modal_to_bottom():
    """Scroll the Settings modal content down to reveal the Reduced Mode section.

    Scrolls in three passes with a short pause between each to handle
    modals that animate or load content progressively.
    """
    from conftest import get_app_window
    ensure_visible()
    w = get_app_window()
    cx = w.left + w.width // 2
    cy = w.top + w.height // 2
    pyautogui.moveTo(cx, cy, duration=0.2)
    for _ in range(3):
        pyautogui.scroll(-15)   # scroll down
        time.sleep(0.3)
    time.sleep(0.5)
    log.info("  Modal scrolled to bottom")


def _click_coord(rx: float, ry: float, label: str = ""):
    """Move mouse to a relative coordinate within the app window and click."""
    from conftest import get_app_window
    ensure_visible()
    w = get_app_window()
    x = int(w.left + rx * w.width)
    y = int(w.top + ry * w.height)
    log.info(f"  click '{label}'  rel({rx:.3f}, {ry:.3f})  abs({x}, {y})")
    pyautogui.moveTo(x, y, duration=0.3)
    pyautogui.click(x, y)
    time.sleep(SLEEP)


# Relative coordinate of the Reduced Mode Enable toggle within the app window.
# Measured from screenshot — adjust if the toggle position shifts.
REDUCED_MODE_TOGGLE = (0.400, 0.491)


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


# ─────────────────────────────────────────────
# Mouse-based test class — same steps as TestReducedMode
# but every feature interaction uses mouse movement + click
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestReducedModeMouse:
    """Same end-to-end Reduced Mode workflow as TestReducedMode,
    with every feature interaction driven by mouse movement and clicks."""

    # ── Settings: sensor dropdowns + enable Reduced Mode ──────────────────

    def test_24_open_settings(self, app):
        ensure_visible()
        click_sidebar(*SIDEBAR_SETTINGS, "Settings gear icon")

    def test_25_camera_config_visible(self, app):
        """Default Camera Configuration section is visible at the top."""
        pass  # visual confirmation only

    @pytest.mark.parametrize("option", SENSOR_OPTIONS, ids=SENSOR_OPTIONS)
    def test_26_left_sensor_mouse(self, app, option):
        _select_sensor_option_mouse(option, "Left", combobox_index=0)

    @pytest.mark.parametrize("option", SENSOR_OPTIONS, ids=SENSOR_OPTIONS)
    def test_27_right_sensor_mouse(self, app, option):
        _select_sensor_option_mouse(option, "Right", combobox_index=1)

    def test_28_restore_middle_mouse(self, app):
        """Restore both sensors to Middle using mouse clicks."""
        _select_sensor_option_mouse("Middle", "Right", combobox_index=1)
        _select_sensor_option_mouse("Middle", "Left", combobox_index=0)

    def test_29_enable_reduced_mode_mouse(self, app):
        """Scroll to Reduced Mode section and click the Enable toggle ON via mouse."""
        _scroll_modal_to_bottom()
        _click_coord(*REDUCED_MODE_TOGGLE, "Reduced Mode Enable toggle ON")
        log.info("  Reduced Mode enabled (mouse)")

    def test_30_close_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Notes: type session note ───────────────────────────────────────────

    def test_31_open_notes(self, app):
        """Notes is now at the former Scan Settings position in reduced sidebar."""
        click_sidebar(*SIDEBAR_NOTES_REDUCED, "Notes (reduced mode position)")

    def test_32_type_note(self, app):
        require_focus()
        TestReducedModeMouse.session_note = (
            f"ReducedScanMouse_{datetime.now():%Y%m%d_%H%M%S}"
        )
        log.info(f"  Typing note: '{TestReducedModeMouse.session_note}'")
        pyautogui.typewrite(TestReducedModeMouse.session_note, interval=0.04)
        time.sleep(SLEEP)

    def test_33_close_notes(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Scan: start, wait, stop ────────────────────────────────────────────

    def test_34_start_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Start scan")

    def test_35_wait_scan(self, app):
        wait_with_log(SCAN_WAIT, "manual scan running")

    def test_36_stop_scan(self, app):
        click_sidebar(*SIDEBAR_START, "Stop scan")
        log.info(f"  Waiting {STOP_BUFFER}s for scan data to save...")
        time.sleep(STOP_BUFFER)

    # ── History: verify scan, visualize plots ──────────────────────────────

    def test_37_open_history(self, app):
        click_sidebar(*SIDEBAR_HISTORY, "History")

    def test_38_latest_scan_selected(self, app):
        scan_text = _selected_scan_text()
        assert len(scan_text) > 0, (
            "History ComboBox is empty — no scans found."
        )
        log.info(f"  Latest scan in ComboBox: '{scan_text}'")

    def test_39_visualize_bfi_bvi(self, app):
        click_by_name("Visualize BFI/BVI")
        wait_with_log(VIZ_WAIT, "BFI/BVI plot open")

    def test_40_close_bfi_plot_mouse(self, app):
        """Move mouse to plot window center then close."""
        _close_plot_window_mouse()

    def test_41_visualize_contrast_mean(self, app):
        ensure_visible()
        click_by_name("Visualize Contrast/Mean")
        wait_with_log(VIZ_WAIT, "Contrast/Mean plot open")

    def test_42_close_contrast_plot_mouse(self, app):
        """Move mouse to plot window center then close."""
        _close_plot_window_mouse()

    def test_43_close_history(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    # ── Settings: disable Reduced Mode ────────────────────────────────────

    def test_44_reopen_settings(self, app):
        click_sidebar(*SIDEBAR_SETTINGS, "Settings gear icon (reopen)")

    def test_45_disable_reduced_mode_mouse(self, app):
        """Scroll to Reduced Mode section and click the Enable toggle OFF via mouse."""
        _scroll_modal_to_bottom()
        _click_coord(*REDUCED_MODE_TOGGLE, "Reduced Mode Enable toggle OFF")
        log.info("  Reduced Mode disabled (mouse)")

    def test_46_close_settings(self, app):
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
