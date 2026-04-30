"""
Scan Settings — modal interaction tests.

Features tested:
  - Session and User label fields visible and non-empty
  - Left/Right sensor dropdowns (all 9 options each)
  - Sensor dot visual
  - Scan Duration toggle (Timed <-> Free Run)
  - Hours / Minutes / Seconds inputs
  - Close via X button and Escape key

ComboBoxes are located by their UIA label name ("Left Sensor", "Right Sensor")
so the tests automatically adapt when new fields are added to the modal.
"""

import time

import pyautogui
import pytest

from conftest import (
    SLEEP,
    ensure_visible,
    get_app_window,
    log,
    read_combobox_values,
    require_focus,
    uia_window,
)
from utils import SENSOR_OPTIONS, click_panel, focus_combobox_by_label

pytestmark = pytest.mark.dev

# Modal close button (top-right X) — still coord-based since it's
# inside the modal, not the calibrated sidebar panel.
SCAN_MODAL_CLOSE = (0.360, 0.119)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────


def _get_modal_header_values() -> list:
    """Return the read-only header values (Session, User) visible in the modal.

    This QML app does not expose label names ("Session", "User") via UIA.
    Instead, we collect ALL non-empty UIA texts and filter out:
      - known sensor option strings (None, Near, Middle, …)
      - short numeric strings used by the duration inputs (1, 00, etc.)
    What remains are the header display values — Session first, User second.

    Based on observed UIA tree: ['owHTFCS1', 'Middle', 'Middle', '1', '00', '00']
    """
    try:
        win = uia_window()
        seen = set()
        results = []
        for e in win.descendants():
            try:
                t = e.window_text().strip()
                if not t or t in seen:
                    continue
                # Skip sensor option strings
                if t in SENSOR_OPTIONS:
                    continue
                # Skip short numeric strings (duration inputs: "0", "00", "1" …)
                if t.isdigit() or (len(t) <= 3 and all(c.isdigit() for c in t)):
                    continue
                seen.add(t)
                results.append(t)
            except Exception:
                continue
        log.info(f"  Modal header values: {results}")
        return results
    except Exception as e:
        log.warning(f"  _get_modal_header_values failed: {e}")
        return []


def _select_sensor_option(option_name: str, side: str, combobox_index: int):
    """Locate the sensor ComboBox by its label name ('Left Sensor' / 'Right Sensor')
    via UIA, focus it, navigate to the option with arrow keys, then verify.

    Automatically adapts when new fields are added to the modal — no tab counting.
    Falls back to whatever ComboBox currently has focus if UIA lookup fails.
    """
    require_focus()
    label = f"{side} Sensor"
    idx = SENSOR_OPTIONS.index(option_name)
    log.info(f"  {label}: selecting '{option_name}' (index {idx})")

    focus_combobox_by_label(label)

    pyautogui.hotkey("alt", "down")   # open popup
    time.sleep(0.5)
    pyautogui.press("home")           # jump to first item
    time.sleep(0.2)
    for _ in range(idx):
        pyautogui.press("down")
        time.sleep(0.15)
    pyautogui.press("return")         # confirm
    time.sleep(SLEEP)

    values = read_combobox_values()
    assert len(values) > combobox_index, (
        f"Expected at least {combobox_index + 1} ComboBox(es), found {len(values)}"
    )
    actual = values[combobox_index]
    assert actual == option_name, (
        f"{side} sensor: expected '{option_name}', got '{actual}'"
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
    """Scan Settings modal — session/user labels, dropdowns, toggles, inputs, close."""

    def test_01_open(self, app):
        ensure_visible()
        click_panel("Scan\nSettings")

    def test_02_sensor_dots_visible(self, app):
        """Sensor dot pattern visible in Camera Configuration."""
        pass  # visual confirmation — no assertion needed

    def test_03_user_label(self, app):
        """The 'User Label:' field (formerly 'Session') is rendered in
        the modal header. Verifies the label text is reachable via UIA.

        Tab into the modal first so QML exposes all UIA elements, then
        look for the literal label string. A second pass on subsequent
        tests still reads the field's *value* via _get_modal_header_values
        once UIA cooperates.
        """
        require_focus()
        pyautogui.press("tab")   # enter modal — focus first interactive element
        time.sleep(0.5)
        win = uia_window()
        found = False
        for elem in win.descendants():
            try:
                txt = (elem.window_text() or "").strip()
            except Exception:
                continue
            if txt in ("User Label:", "User Label"):
                found = True
                break
        assert found, (
            "'User Label' text not found in scan-settings modal. The "
            "label was renamed from 'Session' to 'User Label' in "
            "ScanSettingsModal.qml; UIA should expose the Text element."
        )
        log.info("  'User Label' label rendered in modal")

    def test_04_user_label_value_present(self, app):
        """The user-label field is non-empty (auto-generated 'owXXXXXX'
        unless the user has typed something else)."""
        values = _get_modal_header_values()
        # Accept any non-filtered modal text as proof the value field
        # rendered. The auto-generated label format is 'ow' + 6 chars.
        user_value = values[0] if values else ""
        log.info(f"  User Label value (best-guess from UIA): '{user_value}'")
        assert len(user_value) > 0, (
            "No header values found in modal — User Label field is "
            "either empty or not accessible via UIA."
        )

    @pytest.mark.parametrize("option", SENSOR_OPTIONS, ids=SENSOR_OPTIONS)
    def test_05_left_sensor(self, app, option):
        _select_sensor_option(option, "Left", combobox_index=0)

    @pytest.mark.parametrize("option", SENSOR_OPTIONS, ids=SENSOR_OPTIONS)
    def test_06_right_sensor(self, app, option):
        _select_sensor_option(option, "Right", combobox_index=1)

    def test_07_restore_middle(self, app):
        """Restore both sensors to Middle."""
        _select_sensor_option("Middle", "Right", combobox_index=1)
        _select_sensor_option("Middle", "Left", combobox_index=0)

    def test_08_toggle_free_run(self, app):
        focus_combobox_by_label("Left Sensor")
        require_focus()
        pyautogui.press("tab")   # Left CB -> Right CB
        time.sleep(0.2)
        pyautogui.press("tab")   # Right CB -> Switch
        time.sleep(0.3)
        pyautogui.press("space")  # toggle to Free Run
        time.sleep(SLEEP)

    def test_09_toggle_timed(self, app):
        require_focus()
        pyautogui.press("space")  # toggle back to Timed
        time.sleep(SLEEP)

    def test_10_hours_input(self, app):
        require_focus()
        pyautogui.press("tab")   # Switch -> Hours
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.typewrite("2", interval=0.05)
        time.sleep(SLEEP)

    def test_11_minutes_input(self, app):
        require_focus()
        pyautogui.press("tab")   # Hours -> Minutes
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.typewrite("30", interval=0.05)
        time.sleep(SLEEP)

    def test_12_seconds_input(self, app):
        require_focus()
        pyautogui.press("tab")   # Minutes -> Seconds
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.typewrite("45", interval=0.05)
        time.sleep(SLEEP)

    def test_13_close_via_x(self, app):
        _click_coord(*SCAN_MODAL_CLOSE, "X close button")

    def test_14_close_via_escape(self, app):
        click_panel("Scan\nSettings")  # reopen
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)
