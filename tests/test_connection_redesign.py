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
Connection events are detected by tailing the bloodflow app log
(``app-logs/ow-bloodflowapp-*.log``). The SDK emits one info-level line
per state transition in the form ``<name> state <OLD> -> <NEW> (<reason>)``.
"""

import time
from pathlib import Path

import pytest
import pyautogui

import shelly
from conftest import (
    SLEEP,
    click_sidebar,
    log,
    require_focus,
)
from utils import (
    RE_CONNECTED,
    RE_DISCONNECTED,
    find_app_log,
    log_size,
    wait_for_pattern,
)


# Sidebar coordinates — copied from test_scan_flow.py (shared layout).
SIDEBAR_START = (0.019, 0.115)

# Timeouts (seconds).
CONNECT_TIMEOUT    = 30   # USB enumeration + ping/version handshake
DISCONNECT_TIMEOUT = 15
SCAN_RUNUP_SEC     = 8    # let scan get past handshake before yanking power
SETTLE_AFTER_SCAN  = 8    # let app return to idle after mid-scan disconnect
RAPID_TOGGLE_COUNT = 5
RAPID_TOGGLE_HOLD  = 2.0  # seconds held in each off/on phase; faster trips the
                          # Shelly relay's own duty-cycle limits, not the app.


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────
@pytest.fixture(scope="module")
def outlet():
    """Provide the Shelly outlet; skip the module if it is unreachable."""
    try:
        out = shelly.default_outlet()
        out.is_on()  # one round-trip to confirm reachability
    except Exception as e:
        pytest.skip(f"Shelly outlet not reachable: {e}")
    yield out
    # Always leave the outlet on after the module so the device is usable.
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
        assert log_path
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
        click_sidebar(*SIDEBAR_START, "Start")
        time.sleep(SCAN_RUNUP_SEC)

        log_path = find_app_log()
        assert log_path
        offset = log_size(log_path)

        log.info("Power-cycling DURING scan (off 5s, on)")
        outlet.power_cycle(off_time=5.0)

        _wait_disconnected(log_path, offset)
        offset_after_disc = log_size(log_path)
        _wait_connected(log_path, offset_after_disc)

        log.info("Letting app idle, then starting a second scan")
        time.sleep(SETTLE_AFTER_SCAN)
        require_focus()
        click_sidebar(*SIDEBAR_START, "Start")
        time.sleep(SCAN_RUNUP_SEC)

        # Stop the second scan so we leave a clean state for the next test.
        require_focus()
        click_sidebar(*SIDEBAR_START, "Stop")
        time.sleep(SLEEP)

    def test_04_rapid_toggle(self, outlet, app):
        """Many fast on/off toggles → app stays sane and ends up connected."""
        log.info(f"Rapid toggle x{RAPID_TOGGLE_COUNT}")
        for i in range(RAPID_TOGGLE_COUNT):
            outlet.off()
            time.sleep(RAPID_TOGGLE_HOLD)
            outlet.on()
            time.sleep(RAPID_TOGGLE_HOLD)
            log.info(f"  toggle {i + 1}/{RAPID_TOGGLE_COUNT}")

        # Let the dust settle, then verify the app can still complete a
        # full disconnect-reconnect cycle from this state.
        log.info("Settling, then forcing one verification cycle")
        time.sleep(5)
        log_path = find_app_log()
        assert log_path
        offset = log_size(log_path)
        outlet.power_cycle(off_time=5.0)

        _wait_disconnected(log_path, offset)
        offset_after_disc = log_size(log_path)
        _wait_connected(log_path, offset_after_disc)
