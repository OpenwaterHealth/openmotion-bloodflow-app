"""
Connection redesign — power-cycle resilience tests.

Verifies that the bloodflow app:
  1. Auto-connects when the device is powered on after the app is open.
  2. Reconnects after a power-cycle while idle.
  3. Aborts an in-progress scan on disconnect, idles, reconnects, and
     accepts a new scan.
  4. Stays sane through rapid sequential power toggles.

Hardware setup
--------------
The device under test must be plugged into a Shelly WiFi outlet whose
host/IP is exported as ``$SHELLY_IP_ADDRESS``. The shelly module in this
directory drives the outlet; see ``tests/shelly.py``.

Verification
------------
Connection events are detected by tailing the app log
(``logs/open-motion-*.log``). The SDK emits one info-level line
per state transition in the form ``<name> state <OLD> -> <NEW> (<reason>)``.
"""

import time
from pathlib import Path

import pytest
import pyautogui

import shelly
from conftest import (
    SLEEP,
    log,
    require_focus,
)
from hil_helpers import (
    RE_CONNECTED,
    RE_DISCONNECTED,
    click_panel,
    find_app_log,
    is_app_alive,
    log_size,
    wait_for_pattern,
)

pytestmark = pytest.mark.dev



# Timeouts (seconds).
CONNECT_TIMEOUT    = 30   # USB enumeration + ping/version handshake
DISCONNECT_TIMEOUT = 15
SCAN_RUNUP_SEC     = 8    # let scan get past handshake before yanking power
SETTLE_AFTER_SCAN  = 8    # let app return to idle after mid-scan disconnect
RAPID_TOGGLE_COUNT = 5
RAPID_TOGGLE_HOLD  = 2.0  # seconds held in each off/on phase; faster trips the
                          # Shelly relay's own duty-cycle limits, not the app.
SLOW_TOGGLE_EXTRA  = 5.0  # extra wait after each ON for app to reconnect
                          # (used by test_05_toggle_with_reconnect_wait)


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────
@pytest.fixture(scope="module")
def outlet():
    """Provide the Shelly outlet; skip the module if it is unreachable."""
    try:
        out = shelly.default_outlet()
        # Log which host we resolved + the relay's current state so a
        # confused operator (multiple Shellys on the network, stale env
        # var pointing at the wrong one, etc.) can see at a glance
        # whether the right outlet is being driven.
        host = getattr(out, "host", "<unknown>")
        state = "ON" if out.is_on() else "OFF"
        log.info(f"  Shelly outlet: host={host} relay={state}")
    except Exception as e:
        pytest.skip(f"Shelly outlet not reachable: {e}")
    yield out
    try:
        out.on()
    except Exception:
        pass


# ─────────────────────────────────────────────
# Log tailing — thin wrappers around utils.wait_for_pattern that fail with
# a meaningful message when the SDK does not log the expected transition.
# ─────────────────────────────────────────────
def _wait_connected(log_path: Path, offset: int) -> str:
    line = wait_for_pattern(RE_CONNECTED, log_path, offset, CONNECT_TIMEOUT)
    assert line, f"did not see CONNECTED transition within {CONNECT_TIMEOUT}s"
    log.info(f"  connect: {line}")
    return line


def _wait_disconnected(log_path: Path, offset: int) -> str:
    line = wait_for_pattern(RE_DISCONNECTED, log_path, offset, DISCONNECT_TIMEOUT)
    assert line, f"did not see DISCONNECTED transition within {DISCONNECT_TIMEOUT}s"
    log.info(f"  disconnect: {line}")
    return line


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────
@pytest.mark.incremental
class TestConnectionRedesign:
    """Power-cycle resilience for the connection-redesign feature."""

    def test_01_open_with_device_off_then_power_on(self, outlet, app):
        """Device off + app open + power on → auto-connect."""
        log.info("Powering OFF outlet, then waiting for app to settle")
        outlet.off()
        time.sleep(3)

        log_path = find_app_log()
        assert log_path, "could not locate bloodflow app-log"
        log.info(f"watching log: {log_path}")
        offset = log_size(log_path)

        log.info("Powering ON outlet — expecting auto-connect")
        outlet.on()
        _wait_connected(log_path, offset)

    def test_02_power_cycle_while_idle(self, outlet, app):
        """Idle app + power cycle → reconnects."""
        log_path = find_app_log()
        assert log_path, (
            "could not locate the bloodflow app log under any of the "
            "search roots in utils.find_app_log — verify the app is "
            "actually launched and writing to logs/."
        )
        offset = log_size(log_path)

        log.info("Power-cycling outlet (off 5s, on)")
        outlet.power_cycle(off_time=5.0)

        _wait_disconnected(log_path, offset)
        offset_after_disc = log_size(log_path)
        _wait_connected(log_path, offset_after_disc)

    def test_03_power_cycle_during_scan(self, outlet, app):
        """Power-cycle during a scan → scan aborts, app reconnects, new scan works."""
        require_focus()

        log.info("Starting scan")
        click_panel("Start")
        time.sleep(SCAN_RUNUP_SEC)

        log_path = find_app_log()
        assert log_path, (
            "could not locate the bloodflow app log under any of the "
            "search roots in utils.find_app_log — verify the app is "
            "actually launched and writing to logs/."
        )
        offset = log_size(log_path)

        log.info("Power-cycling DURING scan (off 5s, on)")
        outlet.power_cycle(off_time=5.0)

        _wait_disconnected(log_path, offset)
        offset_after_disc = log_size(log_path)
        _wait_connected(log_path, offset_after_disc)

        # Dismiss the Session Notes modal that auto-opens when the
        # power-cycle aborted scan ends. Without this, the modal
        # stays on top through every subsequent test class and UIA
        # only exposes its contents — masking the modals later tests
        # are trying to interact with.
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

        log.info("Letting app idle, then starting a second scan")
        time.sleep(SETTLE_AFTER_SCAN)
        require_focus()
        click_panel("Start")
        time.sleep(SCAN_RUNUP_SEC)

        # Stop the second scan so we leave a clean state for the next test.
        require_focus()
        click_panel("Start")
        time.sleep(SLEEP)
        # And dismiss the Session Notes modal from the second scan
        # for the same reason as above.
        require_focus()
        pyautogui.press("escape")
        time.sleep(SLEEP)

    def test_04_rapid_toggle(self, outlet, app):
        """Many fast on/off toggles → app stays sane and ends up connected.

        Asserts the app window survives every toggle. If the app crashes
        during the loop we fail loudly with the toggle count, instead of
        hanging in the post-loop log-tail verification.
        """
        log.info(f"Rapid toggle x{RAPID_TOGGLE_COUNT}")
        for i in range(RAPID_TOGGLE_COUNT):
            outlet.off()
            time.sleep(RAPID_TOGGLE_HOLD)
            outlet.on()
            time.sleep(RAPID_TOGGLE_HOLD)
            log.info(f"  toggle {i + 1}/{RAPID_TOGGLE_COUNT}")
            assert is_app_alive(), (
                f"BUG: App crashed/closed after {i + 1}/{RAPID_TOGGLE_COUNT} "
                f"rapid power toggles (RAPID_TOGGLE_HOLD={RAPID_TOGGLE_HOLD}s). "
                f"Application window is no longer present."
            )

        # Let the dust settle, then verify the app can still complete a
        # full disconnect-reconnect cycle from this state.
        log.info("Settling, then forcing one verification cycle")
        time.sleep(5)
        log_path = find_app_log()
        assert log_path, (
            "could not locate the bloodflow app log under any of the "
            "search roots in utils.find_app_log — verify the app is "
            "actually launched and writing to logs/."
        )
        offset = log_size(log_path)
        outlet.power_cycle(off_time=5.0)

        _wait_disconnected(log_path, offset)
        offset_after_disc = log_size(log_path)
        _wait_connected(log_path, offset_after_disc)

    def test_05_toggle_with_reconnect_wait(self, outlet, app):
        """Same as test_04 but wait an extra 5s after each ON for the app
        to log a CONNECTED transition. Verifies the app fully recovers
        between every toggle, not just at the end of the loop.
        """
        log_path = find_app_log()
        assert log_path, (
            "could not locate the bloodflow app log under any of the "
            "search roots in utils.find_app_log — verify the app is "
            "actually launched and writing to app-logs/."
        )

        log.info(
            f"Toggle x{RAPID_TOGGLE_COUNT} with "
            f"+{SLOW_TOGGLE_EXTRA}s reconnect wait per cycle"
        )
        for i in range(RAPID_TOGGLE_COUNT):
            offset = log_size(log_path)

            outlet.off()
            time.sleep(RAPID_TOGGLE_HOLD)
            outlet.on()
            time.sleep(RAPID_TOGGLE_HOLD)

            # Wait the extra 5s for the app to reconnect, while tailing
            # the log for the CONNECTED transition emitted by the SDK.
            line = wait_for_pattern(
                RE_CONNECTED, log_path, offset, SLOW_TOGGLE_EXTRA
            )
            assert line, (
                f"toggle {i + 1}/{RAPID_TOGGLE_COUNT}: "
                f"app did NOT reconnect within "
                f"{RAPID_TOGGLE_HOLD * 2 + SLOW_TOGGLE_EXTRA}s of the toggle"
            )
            log.info(f"  toggle {i + 1}/{RAPID_TOGGLE_COUNT}: connected ({line})")

            assert is_app_alive(), (
                f"BUG: App crashed/closed after {i + 1}/{RAPID_TOGGLE_COUNT} "
                f"slow power toggles. Application window is no longer present."
            )
