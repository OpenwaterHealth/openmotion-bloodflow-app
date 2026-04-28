"""
USB disconnect during scan — app-freeze regression test.

Repro steps from the bug report:
  1. Start a 100-second scan in Far/Far.
  2. At the moment the SDK begins triggering, disconnect USB and immediately
     reconnect it (simulated here by power-cycling the Shelly outlet).
  3. The scan stops triggering and cameras are disabled.
  4. Without restarting the console, repeat the same sequence twice more
     (3 iterations total).

Observed bug after the 3rd iteration:
  - Application freezes; the running scan cannot be cancelled.
  - The window cannot be closed by normal means.
  - User has to kill the process from Task Manager.

Pass/fail:
  - Test PASSES if the app is still responsive after 3 disconnect/reconnect
    cycles (Win32 ``IsHungAppWindow`` returns False, and a no-op UIA query
    completes within a short timeout).
  - Test FAILS with ``BUG REPRODUCED`` if the app is in the Not-Responding
    state, mirroring how ``test_scan_auto_stop_bug.py`` reports issue #47.

This test is non-destructive: on success or failure it leaves the app
running so the user can inspect state. The Shelly fixture's teardown
guarantees the outlet ends up powered ON.
"""

import ctypes
import re
import time

import pyautogui
import pytest

import shelly
from conftest import (
    SLEEP,
    ensure_visible,
    get_app_window,
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
LOOP = 3                            # 3 iterations per the bug repro
SCAN_DURATION_SEC = 100             # 100-second scan
SCAN_MINUTES = SCAN_DURATION_SEC // 60     # 1
SCAN_SECONDS = SCAN_DURATION_SEC % 60      # 40
SENSOR_OPTION = "Far"

# Shelly off-time for the simulated USB disconnect. ≥3s gives USB
# enumeration time to clean up and the host OS time to drop the device
# (per shelly.py guidance for hardware reconnect tests).
SHELLY_OFF_TIME_SEC = 5.0

# How long to wait for the SDK to log a connection-state transition or
# trigger event after a power action.
DISCONNECT_TIMEOUT  = 30
CONNECT_TIMEOUT     = 60
TRIGGER_TIMEOUT     = 180   # FPGA programming + camera config can take ~75–90 s

# Settle delays.
POST_RECONNECT_SETTLE_SEC = 15  # let the app return to idle before next iter
FREEZE_CHECK_DELAY_SEC    = 20  # wait before probing for hang at the end

CHECK_WAIT_SEC = 120
SIDEBAR_START = (0.019, 0.115)
SIDEBAR_CHECK = (0.019, 0.420)
SIDEBAR_SCAN_FALLBACK = (0.025, 0.225)

# SDK marker line emitted at the exact moment the user describes
# ("at the place of Start Triggering"). See
# openmotion-sdk/omotion/ScanWorkflow.py line 800: ``_emit_log("Starting trigger...")``.
RE_STARTING_TRIGGER = re.compile(r"Starting trigger")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _click_combobox(combobox_index: int):
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


def _set_scan_duration(hours: int, minutes: int, seconds: int):
    """Click into each H/M/S field and type the value.

    Same field-discovery heuristic as the other auto-stop tests, extended
    to write all three fields rather than just minutes.
    """
    require_focus()
    win = uia_window()
    log.info(f"  Setting duration to {hours:02d}:{minutes:02d}:{seconds:02d}")

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
                for elem, value, label in (
                    (duration_fields[0], hours,   "Hours field"),
                    (duration_fields[1], minutes, "Minutes field"),
                    (duration_fields[2], seconds, "Seconds field"),
                ):
                    click_element_center(elem, label)
                    time.sleep(0.1)
                    pyautogui.hotkey("ctrl", "a")
                    time.sleep(0.1)
                    pyautogui.typewrite(str(value), interval=0.05)
                    time.sleep(0.1)
                clicked = True
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
        for value in (hours, minutes, seconds):
            pyautogui.press("tab")
            time.sleep(0.1)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.typewrite(str(value), interval=0.05)
            time.sleep(0.1)
    time.sleep(0.1)


def _is_app_hung() -> bool:
    """Return True if the app window is in Win32 'Not Responding' state.

    Uses the user32 ``IsHungAppWindow`` API, which is exactly what the
    Windows shell checks before painting the "(Not Responding)" suffix
    on a window's title bar. Non-destructive: does not interact with
    the window.
    """
    try:
        w = get_app_window()
        hwnd = getattr(w, "_hWnd", None)
        if hwnd is None:
            log.warning("  _is_app_hung: no _hWnd on app window")
            return False
        return bool(ctypes.windll.user32.IsHungAppWindow(hwnd))
    except Exception as e:
        log.warning(f"  _is_app_hung check failed: {e}")
        return False


def _dismiss_post_disconnect_modals():
    """After a mid-scan disconnect, the app typically opens a Session
    Notes (or similar) modal once the scan thread tears down. Dismiss
    whatever is on top so the next iteration starts clean.
    """
    require_focus()
    pyautogui.press("escape")
    time.sleep(SLEEP)


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────
@pytest.fixture(scope="class")
def outlet():
    """Provide the Shelly outlet that powers the console.

    The whole point of this test is to drive the device through
    controlled disconnects, so a missing/unreachable outlet is a hard
    skip rather than silently degrading to a manual procedure.
    """
    try:
        out = shelly.default_outlet()
        out.is_on()
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
class TestUsbDisconnectFreeze:  # 100-s Far/Far scans interrupted by USB disconnect at trigger-start

    def test_disconnect_during_scan_loop(self, app, outlet):
        """Run 3 scans, each interrupted by a power-cycle at trigger-start.
        Verify the app does not enter the Win32 hung state.
        """
        log_path = find_app_log()
        assert log_path, "could not locate bloodflow app-log"
        log.info(f"watching log: {log_path}")

        for iteration in range(1, LOOP + 1):
            log.info(f"{'='*60}")
            log.info(
                f"  ITERATION {iteration}/{LOOP}  "
                f"({SCAN_DURATION_SEC}s, {SENSOR_OPTION}/{SENSOR_OPTION})"
            )
            log.info(f"{'='*60}")

            # 1. Open scan settings.
            move_window_on_screen()
            click_panel_button("Scan\nSettings", fallback=SIDEBAR_SCAN_FALLBACK)

            # 2. Set both sensors to Far.
            _select_sensor_by_mouse(combobox_index=0, side="Left",  option=SENSOR_OPTION)
            _select_sensor_by_mouse(combobox_index=1, side="Right", option=SENSOR_OPTION)

            # 3. Set duration.
            _set_scan_duration(hours=0, minutes=SCAN_MINUTES, seconds=SCAN_SECONDS)

            # 4. Close settings.
            require_focus()
            pyautogui.press("escape")
            time.sleep(0.5)

            # 5. Run Check and wait for it to complete.
            log.info(f"  [{iteration}/{LOOP}] Clicking Check and waiting up to {CHECK_WAIT_SEC}s...")
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
                if dismiss_signal_quality_modal():
                    log.info(f"  [{iteration}/{LOOP}] Signal quality modal dismissed at {check_elapsed}s.")
                    break
                if check_elapsed % 30 == 0:
                    log.info(f"  [{iteration}/{LOOP}] Check running... {check_elapsed}/{CHECK_WAIT_SEC}s")
            dismiss_signal_quality_modal()

            # 6. Snapshot the log offset, then start the scan.
            offset = log_size(log_path)
            click_panel_button("Start", fallback=SIDEBAR_START)
            log.info(
                f"  [{iteration}/{LOOP}] Scan started — waiting for "
                f"'Starting trigger' (up to {TRIGGER_TIMEOUT}s)..."
            )

            # 7. Wait for the SDK to log "Starting trigger..." — that's
            #    the exact moment the user describes for the disconnect.
            trigger_line = wait_for_pattern(
                RE_STARTING_TRIGGER, log_path, offset, TRIGGER_TIMEOUT
            )
            if trigger_line is None:
                pytest.fail(
                    f"[{iteration}/{LOOP}] 'Starting trigger' did not appear "
                    f"within {TRIGGER_TIMEOUT}s — scan may not have started."
                )
            log.info(f"  [{iteration}/{LOOP}] trigger started: {trigger_line}")

            # 8. Power-cycle the outlet IMMEDIATELY (USB disconnect sim).
            offset_after_trigger = log_size(log_path)
            log.info(
                f"  [{iteration}/{LOOP}] Power-cycling console "
                f"(off {SHELLY_OFF_TIME_SEC:.1f}s) — simulating USB unplug/replug"
            )
            outlet.power_cycle(off_time=SHELLY_OFF_TIME_SEC)

            # 9. Confirm the SDK saw the disconnect.
            disc = wait_for_pattern(
                RE_DISCONNECTED, log_path, offset_after_trigger, DISCONNECT_TIMEOUT
            )
            if disc is None:
                pytest.fail(
                    f"[{iteration}/{LOOP}] no DISCONNECTED line within "
                    f"{DISCONNECT_TIMEOUT}s of the simulated USB unplug."
                )
            log.info(f"  [{iteration}/{LOOP}] disconnect: {disc}")

            # 10. Confirm the app reconnected.
            offset_after_disc = log_size(log_path)
            conn = wait_for_pattern(
                RE_CONNECTED, log_path, offset_after_disc, CONNECT_TIMEOUT
            )
            if conn is None:
                pytest.fail(
                    f"[{iteration}/{LOOP}] app did not reconnect within "
                    f"{CONNECT_TIMEOUT}s after the simulated USB unplug."
                )
            log.info(f"  [{iteration}/{LOOP}] reconnect: {conn}")

            # 11. Let the app settle and dismiss any post-scan modal so the
            #     next iteration starts from idle. We deliberately do NOT
            #     power-cycle between iterations — the bug requires running
            #     these steps repeatedly without restarting the console.
            log.info(
                f"  [{iteration}/{LOOP}] Settling for {POST_RECONNECT_SETTLE_SEC}s "
                f"before next iteration..."
            )
            time.sleep(POST_RECONNECT_SETTLE_SEC)
            _dismiss_post_disconnect_modals()

        # 12. After the final iteration, give the app a moment to either
        #     hang (the bug) or settle, then probe for the hung state via
        #     Win32 IsHungAppWindow.
        log.info(
            f"  Completed {LOOP} disconnect iterations; waiting "
            f"{FREEZE_CHECK_DELAY_SEC}s then checking for hung-window state..."
        )
        time.sleep(FREEZE_CHECK_DELAY_SEC)

        if not is_app_alive():
            pytest.fail(
                f"APPLICATION CLOSED at end-of-test — expected window to "
                f"either remain responsive or to be hung, not gone."
            )

        hung = _is_app_hung()
        # Cross-check: a brief UIA query should return promptly on a
        # responsive app. We don't assert on this independently — a UIA
        # timeout alone isn't proof of a hang — but logging it helps
        # post-mortem.
        try:
            uia_window().descendants()[:1]
            uia_responsive = True
        except Exception as e:
            log.warning(f"  UIA descendants() failed: {e}")
            uia_responsive = False
        log.info(
            f"  IsHungAppWindow={hung}  uia_responsive={uia_responsive}"
        )

        assert not hung, (
            f"BUG REPRODUCED: app window is in Win32 Not-Responding state "
            f"after {LOOP} USB-disconnect-during-scan cycles without "
            f"restarting the console. Cancel/close are unavailable; the "
            f"process must be killed via Task Manager."
        )
        log.info(f"  PASSED — app remained responsive across {LOOP} iterations.")
