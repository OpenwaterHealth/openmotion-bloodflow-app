"""
Abbreviated version of test_scan_auto_stop_bug.py.

Same flow and bug-repro logic as test_scan_auto_stop_bug.py — five scan
iterations, console power-cycled between the first three only, scan #5
expected to fail per issue #47 — but each scan is shortened to 2 minutes
in a Far/Far sensor configuration so the whole run takes ~25 minutes
instead of ~70.

Use this for fast iteration on the test infrastructure or for a quick
sanity check that the repro path still works. Use the full
``test_scan_auto_stop_bug.py`` for thorough verification.
"""

import time

import pyautogui
import pytest

import shelly
from conftest import (
    SLEEP,
    ensure_visible,
    log,
    read_combobox_values,
    require_focus,
    uia_window,
)
from utils import (
    RE_CONNECTED,
    RE_DISCONNECTED,
    SENSOR_OPTIONS,
    click_element_center,
    click_panel_button,
    dismiss_signal_quality_modal,
    find_app_log,
    is_app_alive,
    log_size,
    move_window_on_screen,
    wait_for_pattern,
)

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
LOOP = 5  # number of times to run the scan test

# Bug repro: power-cycle the console between the first POWER_CYCLE_LOOPS
# iterations only. Skipping the cycle before iterations 4 and 5 reproduces
# the failure described in bloodflow-app issue #47, where the 5th scan
# cannot start because the console has been left running across 3 scans.
POWER_CYCLE_LOOPS = 3

# Shelly hold-off when power-cycling the console. ≥3s gives USB enumeration
# time to clean up and the host OS time to drop the device (per shelly.py
# guidance for hardware reconnect tests).
SHELLY_OFF_TIME_SEC = 5.0

# How long to wait for the SDK to log a connection-state transition after
# a power event. The outlet only controls power — it cannot tell us whether
# the app reconnected over USB, so we tail the app log.
DISCONNECT_TIMEOUT = 30
CONNECT_TIMEOUT    = 60

SIDEBAR_START = (0.019, 0.115)
SIDEBAR_CHECK = (0.019, 0.420)   # Check button — between Notes and History

# Scan Settings is opened via UIA label lookup (click_panel_button) rather
# than fixed coords. Coordinate-based clicks were landing too high/left of
# the button at this test's typical window size, and the ratio depends on
# the actual app window dimensions which vary by machine. Coordinates are
# kept here as a fallback for click_panel_button.
SIDEBAR_SCAN_FALLBACK = (0.025, 0.225)

CHECK_WAIT_SEC = 120  # 2 minutes for Check to complete

# Abbreviated: 2 min scans in Far/Far instead of 10 min scans in All/All.
SCAN_DURATION_MIN = 2
SCAN_DURATION_SEC = SCAN_DURATION_MIN * 60
SENSOR_OPTION = "Far"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
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


def _click_combobox(combobox_index: int):
    """Click a ComboBox by its index in the UIA tree (0=Left, 1=Right)."""
    ensure_visible()
    time.sleep(0.2)
    win = uia_window()
    cbs = win.descendants(control_type="ComboBox")
    assert len(cbs) > combobox_index, (
        f"Expected at least {combobox_index + 1} ComboBox(es), found {len(cbs)}"
    )
    click_element_center(cbs[combobox_index], f"ComboBox[{combobox_index}]")


def _select_sensor_by_mouse(combobox_index: int, side: str, option: str):
    """Open a sensor dropdown with mouse click and select ``option``."""
    ensure_visible()
    require_focus()
    log.info(f"  {side} Sensor: mouse-clicking dropdown, then selecting '{option}'")

    _click_combobox(combobox_index)
    time.sleep(0.2)

    idx = SENSOR_OPTIONS.index(option)
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
    assert actual == option, (
        f"{side} sensor: expected '{option}', got '{actual}'"
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
                click_element_center(duration_fields[1], "Minutes field")
                time.sleep(0.1)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.typewrite(minutes, interval=0.05)
                clicked = True

                click_element_center(duration_fields[0], "Hours field")
                time.sleep(0.1)
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.typewrite("0", interval=0.05)

                click_element_center(duration_fields[2], "Seconds field")
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
            click_element_center(cbs[1], "Right ComboBox (anchor)")
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
# Fixtures
# ─────────────────────────────────────────────
@pytest.fixture(scope="class")
def outlet():
    """Provide the Shelly outlet that powers the console.

    The whole point of this test is to drive the device through controlled
    power cycles, so a missing/unreachable outlet is a hard skip rather than
    silently degrading to a manual procedure.
    """
    try:
        out = shelly.default_outlet()
        out.is_on()  # one round-trip to confirm reachability
    except Exception as e:
        pytest.skip(f"Shelly outlet not reachable: {e}")
    yield out
    # Always leave the outlet on after the test so the device is usable.
    try:
        out.on()
    except Exception:
        pass


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestScanAutoStopBugAbbreviated:  # 2-min Far/Far scans, same repro logic as TestScanAutoStopBug

    @pytest.mark.parametrize("iteration", range(1, LOOP + 1),
                             ids=[f"loop-{i}" for i in range(1, LOOP + 1)])
    def test_scan_auto_stop(self, app, outlet, iteration):
        """Full scan cycle — configure, start, monitor for early stop."""
        log.info(f"{'='*60}")
        log.info(f"  ITERATION {iteration}/{LOOP}  ({SCAN_DURATION_MIN} min, {SENSOR_OPTION}/{SENSOR_OPTION})")
        log.info(f"{'='*60}")

        # 1. Open scan settings — use UIA label lookup with a coord fallback,
        #    so the click lands on the actual button at any window size.
        move_window_on_screen()
        click_panel_button("Scan\nSettings", fallback=SIDEBAR_SCAN_FALLBACK)

        # 2. Set both sensors to SENSOR_OPTION (Far)
        _select_sensor_by_mouse(combobox_index=0, side="Left",  option=SENSOR_OPTION)
        _select_sensor_by_mouse(combobox_index=1, side="Right", option=SENSOR_OPTION)

        # 3. Set duration to 2 min
        _click_minutes_field_and_type(str(SCAN_DURATION_MIN))

        # 4. Close settings
        require_focus()
        pyautogui.press("escape")
        time.sleep(0.5)

        # 5. Run Check and wait for it to complete (2 min)
        log.info(f"  [{iteration}/{LOOP}] Clicking Check and waiting {CHECK_WAIT_SEC}s...")
        click_panel_button("Check", fallback=SIDEBAR_CHECK)
        check_elapsed = 0
        while check_elapsed < CHECK_WAIT_SEC:
            time.sleep(10)
            check_elapsed += 10
            if not is_app_alive():
                pytest.fail(
                    f"[{iteration}/{LOOP}] APPLICATION CLOSED during Check "
                    f"after {check_elapsed}s."
                )
            # If 'Good signal quality' modal appears early, dismiss and continue
            if dismiss_signal_quality_modal():
                log.info(f"  [{iteration}/{LOOP}] Signal quality modal dismissed at {check_elapsed}s.")
                break
            if check_elapsed % 30 == 0:
                log.info(f"  [{iteration}/{LOOP}] Check running... {check_elapsed}/{CHECK_WAIT_SEC}s")
        log.info(f"  [{iteration}/{LOOP}] Check completed.")

        # Final dismiss check in case the modal appeared exactly when the loop exited
        dismiss_signal_quality_modal()

        # 6. Start scan
        click_panel_button("Start", fallback=SIDEBAR_START)
        log.info(f"  [{iteration}/{LOOP}] Scan started — monitoring for completion...")

        # 5. Monitor scan — poll every 10s for Session Notes modal
        #    When it appears, read the duration from the notes text.
        #    If duration < SCAN_DURATION_SEC → BUG REPRODUCED
        #    If app closes → FAIL
        poll_interval = 10
        max_wait = SCAN_DURATION_SEC + 180  # scan time + 3 min buffer
        elapsed = 0
        scan_duration = None
        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            if not is_app_alive():
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

        # 8. Between loops: power-cycle the console for the first
        #    POWER_CYCLE_LOOPS iterations, then leave it running. The bug
        #    reproduces when the console is not restarted before iterations
        #    4 and 5 (issue #47).
        if iteration < LOOP:
            if iteration < POWER_CYCLE_LOOPS:
                log_path = find_app_log()
                assert log_path, "could not locate bloodflow app-log"
                offset = log_size(log_path)

                log.info(
                    f"  [{iteration}/{LOOP}] Power-cycling console via Shelly "
                    f"(off {SHELLY_OFF_TIME_SEC:.1f}s)..."
                )
                outlet.power_cycle(off_time=SHELLY_OFF_TIME_SEC)

                # Pair the power action with an app-side observation: the
                # outlet only controls power, it cannot tell us whether the
                # app reconnected over USB.
                disc = wait_for_pattern(
                    RE_DISCONNECTED, log_path, offset, DISCONNECT_TIMEOUT
                )
                assert disc, (
                    f"[{iteration}/{LOOP}] no DISCONNECTED line within "
                    f"{DISCONNECT_TIMEOUT}s of power-cycle"
                )
                log.info(f"  [{iteration}/{LOOP}] disconnect: {disc}")

                offset_after_disc = log_size(log_path)
                conn = wait_for_pattern(
                    RE_CONNECTED, log_path, offset_after_disc, CONNECT_TIMEOUT
                )
                assert conn, (
                    f"[{iteration}/{LOOP}] app did not reconnect within "
                    f"{CONNECT_TIMEOUT}s after power-cycle"
                )
                log.info(f"  [{iteration}/{LOOP}] reconnect: {conn}")
            else:
                log.info(
                    f"  [{iteration}/{LOOP}] Skipping power-cycle "
                    f"(repro: leaving console running across scans)"
                )
                time.sleep(SLEEP)
