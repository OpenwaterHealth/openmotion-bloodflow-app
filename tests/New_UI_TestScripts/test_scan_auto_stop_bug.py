

import time

import pyautogui
import pygetwindow as gw
import pytest

from conftest import (
    APP_KEYWORDS,
    SLEEP,
    click_sidebar,
    ensure_visible,
    get_app_window,
    log,
    read_combobox_values,
    require_focus,
    uia_window,
)

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
LOOP = 5  # number of times to run the scan test

SIDEBAR_SCAN  = (0.019, 0.210)
SIDEBAR_START = (0.019, 0.115)

SENSOR_OPTIONS = [
    "None", "Near", "Middle", "Far", "Outer",
    "Left", "Right", "Third Row", "All",
]

SCAN_DURATION_MIN = 10
SCAN_DURATION_SEC = SCAN_DURATION_MIN * 60


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _is_app_alive() -> bool:
    """Check if the app window still exists."""
    for w in gw.getAllWindows():
        if any(k in w.title.lower() for k in APP_KEYWORDS):
            return True
    return False


def _check_scan_finished():
    """Check if the scan has finished by looking for the Session Notes modal.

    When the scan completes (or stops), the app opens a 'Session Notes'
    dialog with 'Scan completed — duration: HH:MM:SS' text.

    Returns:
      None            — scan not finished yet
      duration_secs   — scan finished, returns duration in seconds
    """
    import re
    try:
        win = uia_window()
        found_notes = False
        for elem in win.descendants():
            try:
                text = elem.window_text().strip()
                text_lower = text.lower()
                if "session notes" in text_lower:
                    found_notes = True
                # Parse duration from "Scan completed — duration: 00:10:03"
                match = re.search(r"duration:\s*(\d{2}):(\d{2}):(\d{2})", text)
                if match:
                    h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3))
                    duration_secs = h * 3600 + m * 60 + s
                    log.info(f"  Scan finished — duration: {text.strip()} ({duration_secs}s)")
                    return duration_secs
            except Exception:
                continue
        if found_notes:
            # Notes modal is open but couldn't parse duration
            log.info("  Scan finished — Session Notes visible (duration not parsed)")
            return 0
    except Exception as e:
        log.warning(f"  _check_scan_finished failed: {e}")
    return None


def _move_window_on_screen():
    """Move the app window onto the primary screen if it is off-screen."""
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
    """Click the center of a UIA element using the mouse."""
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
        rect = elem.rectangle()
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        log.info(f"     after reposition: '{label}' at ({cx}, {cy})")
    log.info(f"     click '{label}' at ({cx}, {cy})")
    pyautogui.click(cx, cy)


def _click_combobox(combobox_index: int):
    """Click a ComboBox by its index in the UIA tree (0=Left, 1=Right)."""
    ensure_visible()
    time.sleep(0.2)
    win = uia_window()
    cbs = win.descendants(control_type="ComboBox")
    assert len(cbs) > combobox_index, (
        f"Expected at least {combobox_index + 1} ComboBox(es), found {len(cbs)}"
    )
    _click_element_center(cbs[combobox_index], f"ComboBox[{combobox_index}]")


def _select_all_by_mouse(combobox_index: int, side: str):
    """Open a sensor dropdown with mouse click and select 'All'."""
    ensure_visible()
    require_focus()
    log.info(f"  {side} Sensor: mouse-clicking dropdown, then selecting 'All'")

    _click_combobox(combobox_index)
    time.sleep(0.2)

    idx = SENSOR_OPTIONS.index("All")
    pyautogui.press("home")
    time.sleep(0.2)
    for _ in range(idx):
        pyautogui.press("down")
        time.sleep(0.2)
    pyautogui.press("return")
    time.sleep(0.2)

    values = read_combobox_values()
    assert len(values) > combobox_index, (
        f"Expected at least {combobox_index + 1} ComboBox(es), found {len(values)}"
    )
    actual = values[combobox_index]
    assert actual == "All", (
        f"{side} sensor: expected 'All', got '{actual}'"
    )


def _click_minutes_field_and_type(minutes: str):
    """Click directly on the Minutes input field and type the value."""
    require_focus()
    win = uia_window()
    log.info(f"  Clicking minutes field directly and typing '{minutes}'")

    clicked = False
    for control_type in ["Edit", "SpinBox", "Custom"]:
        try:
            fields = win.descendants(control_type=control_type)
            duration_fields = []
            for f in fields:
                try:
                    text = f.window_text().strip()
                    rect = f.rectangle()
                    width = rect.right - rect.left
                    if width < 150 and (text.isdigit() or text == ""):
                        duration_fields.append(f)
                except Exception:
                    continue

            if len(duration_fields) >= 3:
                _click_element_center(duration_fields[1], "Minutes field")
                time.sleep(0.1)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.typewrite(minutes, interval=0.05)
                clicked = True

                _click_element_center(duration_fields[0], "Hours field")
                time.sleep(0.1)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.typewrite("0", interval=0.05)

                _click_element_center(duration_fields[2], "Seconds field")
                time.sleep(0.1)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.typewrite("0", interval=0.05)
                break
        except Exception as e:
            log.warning(f"     Duration field search ({control_type}) failed: {e}")
            continue

    if not clicked:
        log.warning("  Could not find duration fields via UIA — using Tab fallback")
        cbs = win.descendants(control_type="ComboBox")
        if len(cbs) >= 2:
            _click_element_center(cbs[1], "Right ComboBox (anchor)")
            time.sleep(0.1)
        pyautogui.press("tab")       # -> Switch
        time.sleep(0.1)
        pyautogui.press("tab")       # -> Hours
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.typewrite("0", interval=0.05)
        time.sleep(0.1)
        pyautogui.press("tab")       # -> Minutes
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.typewrite(minutes, interval=0.05)
        time.sleep(0.1)
        pyautogui.press("tab")       # -> Seconds
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.typewrite("0", interval=0.05)

    time.sleep(0.1)


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestScanAutoStopBug: # (1) Repro scan auto-stop bug, (2) Verify fix, (3) Regression test multiple iterations


    @pytest.mark.parametrize("iteration", range(1, LOOP + 1),
                             ids=[f"loop-{i}" for i in range(1, LOOP + 1)])
    def test_scan_auto_stop(self, app, iteration):
        """Full scan cycle — configure, start, monitor for early stop."""
        log.info(f"{'='*60}")
        log.info(f"  ITERATION {iteration}/{LOOP}")
        log.info(f"{'='*60}")

        # 1. Open scan settings
        _move_window_on_screen()
        click_sidebar(*SIDEBAR_SCAN, "Scan Settings")

        # 2. Set both sensors to ALL
        _select_all_by_mouse(combobox_index=0, side="Left")
        _select_all_by_mouse(combobox_index=1, side="Right")

        # 3. Set duration to 10 min
        _click_minutes_field_and_type("10")

        # 4. Close settings and start scan immediately
        require_focus()
        pyautogui.press("escape")
        time.sleep(0.5)
        click_sidebar(*SIDEBAR_START, "Start")

        log.info(f"  [{iteration}/{LOOP}] Scan started — monitoring for completion...")

        # 5. Monitor scan — poll every 10s for Session Notes modal
        #    When it appears, read the duration from the notes text.
        #    If duration < 10 min → BUG REPRODUCED
        #    If app closes → FAIL
        poll_interval = 10
        max_wait = SCAN_DURATION_SEC + 180  # scan time + 3 min buffer
        elapsed = 0
        scan_duration = None
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            if not _is_app_alive():
                pytest.fail(
                    f"[{iteration}/{LOOP}] APPLICATION CLOSED after {elapsed}s."
                )

            result = _check_scan_finished()
            if result is not None:
                scan_duration = result
                log.info(
                    f"  [{iteration}/{LOOP}] Session Notes appeared after {elapsed}s. "
                    f"Reported scan duration: {scan_duration}s."
                )
                break

            if elapsed % 60 == 0:
                log.info(
                    f"  [{iteration}/{LOOP}] {elapsed}s elapsed — scan still going..."
                )

        # 6. Verify the scan ran for the full configured duration
        assert scan_duration is not None, (
            f"[{iteration}/{LOOP}] Session Notes never appeared after {max_wait}s. "
            f"Scan may be stuck."
        )
        assert scan_duration >= SCAN_DURATION_SEC, (
            f"BUG REPRODUCED on iteration {iteration}/{LOOP}: "
            f"Scan duration was {scan_duration}s "
            f"({scan_duration // 60}m {scan_duration % 60}s) — "
            f"expected at least {SCAN_DURATION_SEC}s ({SCAN_DURATION_MIN}m). "
            f"Scan stopped automatically after FPGA programming."
        )
        log.info(
            f"  [{iteration}/{LOOP}] Scan duration OK: {scan_duration}s "
            f"(>= {SCAN_DURATION_SEC}s)."
        )

        # 7. Dismiss post-scan modal
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

        log.info(f"  [{iteration}/{LOOP}] PASSED — scan ran full duration.")

        # 8. Wait 60s between loops for console power cycle
        if iteration < LOOP:
            log.info(f"  [{iteration}/{LOOP}] Waiting 60s for console power cycle...")
            time.sleep(60)
