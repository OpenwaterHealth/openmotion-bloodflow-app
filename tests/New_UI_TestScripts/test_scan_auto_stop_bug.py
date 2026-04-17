

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
    uia_window,
    wait_with_log,
)

# ─────────────────────────────────────────────
# Configuration — change LOOP to repeat the test
# ─────────────────────────────────────────────
LOOP = 5  # number of times to run the scan test

# ─────────────────────────────────────────────
# Sidebar coordinates
# ─────────────────────────────────────────────
SIDEBAR_SCAN  = (0.019, 0.210)
SIDEBAR_START = (0.019, 0.115)

SENSOR_OPTIONS = [
    "None", "Near", "Middle", "Far", "Outer",
    "Left", "Right", "Third Row", "All",
]

SCAN_DURATION_MIN = 10
# FPGA programming typically completes within ~2 minutes.
# We check at 3 minutes to confirm the scan is still running.
FPGA_SETTLE_WAIT = 180
# Total wait = scan duration + 3-min buffer for post-scan processing.
WAIT_AFTER_SCAN = SCAN_DURATION_MIN * 60 + 180


# ─────────────────────────────────────────────
# Helpers — mouse-click based (mirrors manual interaction)
# ─────────────────────────────────────────────
def _move_window_on_screen():
    """Move the app window onto the primary screen if it is off-screen.

    The app may launch on a disconnected secondary monitor, leaving all UIA
    coordinates negative.  This forces the window to (50, 50) on the primary
    display so pyautogui clicks land correctly.
    """
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


def _click_element_center(elem, label: str = ""):
    """Click the center of a UIA element using the mouse.

    Validates the coordinates are on-screen before clicking.
    """
    rect = elem.rectangle()
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    screen_w, screen_h = pyautogui.size()
    if cx < 0 or cy < 0 or cx > screen_w or cy > screen_h:
        log.warning(
            f"     '{label}' is off-screen at ({cx}, {cy}), "
            f"screen={screen_w}x{screen_h} — moving window on-screen"
        )
        _move_window_on_screen()
        # Re-read coordinates after moving
        rect = elem.rectangle()
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        log.info(f"     after reposition: '{label}' at ({cx}, {cy})")
    log.info(f"     click '{label}' at ({cx}, {cy})")
    pyautogui.click(cx, cy)


def _click_combobox(combobox_index: int):
    """Click a ComboBox by its index in the UIA tree (0=Left, 1=Right)."""
    ensure_visible()
    time.sleep(0.5)
    win = uia_window()
    cbs = win.descendants(control_type="ComboBox")
    assert len(cbs) > combobox_index, (
        f"Expected at least {combobox_index + 1} ComboBox(es), found {len(cbs)}"
    )
    _click_element_center(cbs[combobox_index], f"ComboBox[{combobox_index}]")


def _select_all_by_mouse(combobox_index: int, side: str):
    """Open a sensor dropdown with mouse click and select 'All'.

    Mouse click opens the dropdown (mirrors manual interaction), then
    keyboard navigates to 'All' — QML popups don't reliably expose
    individual list items via UIA, so clicking items directly is fragile.
    """
    ensure_visible()
    require_focus()
    log.info(f"  {side} Sensor: mouse-clicking dropdown, then selecting 'All'")

    # Click the ComboBox to open the dropdown popup
    _click_combobox(combobox_index)
    time.sleep(0.8)

    # Navigate to "All" (last item) — dropdown is open from mouse click above
    idx = SENSOR_OPTIONS.index("All")
    pyautogui.press("home")
    time.sleep(0.2)
    for _ in range(idx):
        pyautogui.press("down")
        time.sleep(0.15)
    pyautogui.press("return")
    time.sleep(0.5)

    # Verify selection
    values = read_combobox_values()
    assert len(values) > combobox_index, (
        f"Expected at least {combobox_index + 1} ComboBox(es), found {len(values)}"
    )
    actual = values[combobox_index]
    assert actual == "All", (
        f"{side} sensor: expected 'All', got '{actual}'"
    )


def _click_minutes_field_and_type(minutes: str):
    """Click directly on the Minutes input field and type the value.

    Finds the duration input fields via UIA (Edit/SpinBox controls) and clicks
    the minutes field (second of three: Hours, Minutes, Seconds).
    Falls back to coordinate-based click if UIA lookup fails.
    """
    require_focus()
    win = uia_window()
    log.info(f"  Clicking minutes field directly and typing '{minutes}'")

    # Try to find editable duration fields (Edit or SpinBox)
    clicked = False
    for control_type in ["Edit", "SpinBox", "Custom"]:
        try:
            fields = win.descendants(control_type=control_type)
            # Filter to small numeric input fields (duration inputs)
            duration_fields = []
            for f in fields:
                try:
                    text = f.window_text().strip()
                    rect = f.rectangle()
                    width = rect.right - rect.left
                    # Duration fields are small numeric inputs (< 150px wide)
                    if width < 150 and (text.isdigit() or text == ""):
                        duration_fields.append(f)
                except Exception:
                    continue

            if len(duration_fields) >= 3:
                # Hours=0, Minutes=1, Seconds=2
                minutes_field = duration_fields[1]
                _click_element_center(minutes_field, "Minutes field")
                time.sleep(0.2)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.typewrite(minutes, interval=0.05)
                clicked = True

                # Also clear hours and seconds to be safe
                _click_element_center(duration_fields[0], "Hours field")
                time.sleep(0.2)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.typewrite("0", interval=0.05)

                _click_element_center(duration_fields[2], "Seconds field")
                time.sleep(0.2)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.typewrite("0", interval=0.05)
                break
        except Exception as e:
            log.warning(f"     Duration field search ({control_type}) failed: {e}")
            continue

    if not clicked:
        # Fallback: use Tab from a known position (last resort)
        log.warning("  Could not find duration fields via UIA — using Tab fallback")
        cbs = win.descendants(control_type="ComboBox")
        if len(cbs) >= 2:
            _click_element_center(cbs[1], "Right ComboBox (anchor)")
            time.sleep(0.2)
        pyautogui.press("tab")       # -> Switch
        time.sleep(0.2)
        pyautogui.press("tab")       # -> Hours
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.typewrite("0", interval=0.05)
        time.sleep(0.2)
        pyautogui.press("tab")       # -> Minutes
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.typewrite(minutes, interval=0.05)
        time.sleep(0.2)
        pyautogui.press("tab")       # -> Seconds
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.typewrite("0", interval=0.05)

    time.sleep(0.3)


def _is_scan_running() -> bool:
    """Check whether the scan appears to still be running.

    Returns True only if a 'Stop' button is found (scan active).
    Returns False if:
      - A 'Start' button is found (scan idle)
      - 'Session Notes' or 'Scan completed' is visible (post-scan modal)
      - Neither 'Stop' nor 'Start' is found (modal covering UI)
    """
    try:
        win = uia_window()
        found_stop = False
        for elem in win.descendants():
            try:
                text = elem.window_text().strip().lower()
                # Post-scan modal means scan is done
                if "session notes" in text or "scan completed" in text:
                    log.info(f"  _is_scan_running: found '{text}' — scan finished")
                    return False
                if "stop" in text:
                    found_stop = True
                if text in ("start scan", "start"):
                    log.info("  _is_scan_running: found 'Start' — scan is idle")
                    return False
            except Exception:
                continue
        if found_stop:
            log.info("  _is_scan_running: found 'Stop' — scan is running")
            return True
        # Neither stop nor start found — likely post-scan state
        log.info("  _is_scan_running: no Stop/Start found — assuming scan finished")
        return False
    except Exception as e:
        log.warning(f"  _is_scan_running check failed: {e}")
    return False


class TestScanAutoStopBug:
    """Reproduce: scan stops after FPGA programming when both cameras set to ALL.

    Prerequisites (manual):
      - Turn on the console (OpenWaterApp_console.exe) before running this test.
      - Launch the application and wait for the system to become ready.

    Change LOOP at the top of this file to control how many times to repeat.
    """

    @pytest.mark.parametrize("iteration", range(1, LOOP + 1),
                             ids=[f"loop-{i}" for i in range(1, LOOP + 1)])
    def test_scan_auto_stop(self, app, iteration):
        """Full scan cycle — repeated LOOP times."""
        log.info(f"{'='*60}")
        log.info(f"  ITERATION {iteration}/{LOOP}")
        log.info(f"{'='*60}")

        # Step 1: Open scan settings
        log.info(f"  [{iteration}/{LOOP}] Opening Scan Settings...")
        _move_window_on_screen()
        click_sidebar(*SIDEBAR_SCAN, "Scan Settings")

        # Step 2: Set Left Sensor to ALL
        log.info(f"  [{iteration}/{LOOP}] Setting Left Sensor to ALL...")
        _select_all_by_mouse(combobox_index=0, side="Left")

        # Step 3: Set Right Sensor to ALL
        log.info(f"  [{iteration}/{LOOP}] Setting Right Sensor to ALL...")
        _select_all_by_mouse(combobox_index=1, side="Right")

        # Step 4: Set duration to 10 min
        log.info(f"  [{iteration}/{LOOP}] Setting duration to 10 minutes...")
        _click_minutes_field_and_type("10")

        # Step 5: Close settings and start scan immediately
        log.info(f"  [{iteration}/{LOOP}] Closing settings and starting scan...")
        require_focus()
        pyautogui.press("escape")
        time.sleep(0.5)
        click_sidebar(*SIDEBAR_START, "Start")

        # Step 6: Wait for FPGA programming, then verify scan is still running
        log.info(f"  [{iteration}/{LOOP}] Waiting for FPGA programming (~3 min)...")
        wait_with_log(FPGA_SETTLE_WAIT,
                      f"[{iteration}/{LOOP}] FPGA programming settle")

        running = _is_scan_running()
        assert running, (
            f"BUG REPRODUCED on iteration {iteration}/{LOOP}: "
            f"Scan stopped automatically after FPGA programming. "
            f"Expected the scan to continue for the full 10-minute duration."
        )
        log.info(f"  [{iteration}/{LOOP}] Scan still running after FPGA — good.")

        # Step 7: Wait for remaining scan time
        remaining = WAIT_AFTER_SCAN - FPGA_SETTLE_WAIT
        log.info(f"  [{iteration}/{LOOP}] Waiting for scan to complete ({remaining}s)...")
        wait_with_log(remaining,
                      f"[{iteration}/{LOOP}] Remaining scan + buffer")

        # Step 8: Verify scan completed
        running = _is_scan_running()
        assert not running, (
            f"Iteration {iteration}/{LOOP}: Scan still running after expected "
            f"duration + buffer. May be stuck or duration not applied."
        )
        log.info(f"  [{iteration}/{LOOP}] Scan completed within expected time.")

        # Step 9: Dismiss post-scan modal
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

        log.info(f"  [{iteration}/{LOOP}] PASSED")
