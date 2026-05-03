"""
test_force_laser_fail.py — verify the ``forceLaserFail`` config flag.

End-to-end check that the ``forceLaserFail: true`` flag in
``config/app_config.json`` causes the app to load
``config/laser_params_fault.json`` instead of the normal laser
parameters, which in turn trips the hardware safety interlock and
fires the persistent "Laser safety warning detected" toast.

Test sequence (one method, with cleanup in ``finally``):

  1. Snapshot the original ``forceLaserFail`` value.
  2. Kill the running bloodflow app.
  3. Set ``forceLaserFail = true`` in ``app_config.json``.
  4. Launch the app, wait for it to connect.
  5. Click Check (which fires the laser briefly).
  6. Wait up to ``SAFETY_TRIP_TIMEOUT`` seconds for the laser-safety
     toast to appear in the UI.
  7. **Always** in ``finally``: kill the app, restore the original
     flag, power-cycle the console (clears any latched safety state
     in the hardware), launch the app again, recalibrate panel
     buttons. This leaves the bench in a clean state for subsequent
     tests in the same session.

Marked ``release`` because it (a) tampers with persistent app config,
(b) requires a Shelly outlet to power-cycle the console, and (c) takes
~2 minutes end-to-end. Don't run in the dev-tier HIL set.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

import shelly
from conftest import (
    PROJECT_ROOT,
    SLEEP,
    ensure_visible,
    log,
)
from utils import (
    RE_CONNECTED,
    click_panel,
    find_app_log,
    log_size,
    recalibrate_panel_buttons,
    wait_for_pattern,
)

pytestmark = pytest.mark.release

CONFIG_PATH = PROJECT_ROOT / "config" / "app_config.json"

# When the interlock trips, motion_connector logs a single ERROR
# line at the safety-status check site (line ~1984) before firing
# the toast. We detect the trip by tailing the log for this prefix
# rather than walking UIA for the toast text — the toast is rendered
# in NotificationCenter as a plain QML Text, which doesn't surface in
# the Windows UIA tree, so a UIA-based check would silently time out.
RE_SAFETY_TRIP = re.compile(r"Laser safety failure:")

# How long the test will wait after Check fires before giving up on
# seeing the safety failure log line. The fault-laser params trip the
# interlock almost immediately on the first laser pulse, so anything
# more than ~30s is conservative; 130s (2 min 10 s) covers slow
# camera-config + the contact-quality preflight that Check kicks off.
SAFETY_TRIP_TIMEOUT = 130

# How long to wait for the SDK to reach CONNECTED state after a
# relaunch. Console enumeration + ping/version handshake is ~5s
# normally; 60s is conservative.
APP_CONNECT_TIMEOUT = 60

# Time after Phase 4 power-cycle relaunch to settle before checking
# that the toast is *not* present (i.e. clean state).
CLEAN_STATE_SETTLE_SEC = 15


# ─────────────────────────────────────────────
# App lifecycle helpers — duplicate of conftest's app-fixture logic
# kept inline because the fixture isn't designed to be re-run mid
# session.
# ─────────────────────────────────────────────
def _from_source_mode() -> bool:
    return os.environ.get("OPENWATER_FROM_SOURCE", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _find_exe() -> str | None:
    env = os.environ.get("OPENWATER_EXE", "")
    if env and os.path.exists(env):
        return env
    patterns = [
        r"C:\Users\*\Documents\OpenMotion\**\OpenWaterApp.exe",
        r"C:\Users\*\Desktop\**\OpenWaterApp.exe",
        r"C:\Program Files\**\OpenWaterApp.exe",
        r"C:\Program Files (x86)\**\OpenWaterApp.exe",
    ]
    matches: list[str] = []
    for p in patterns:
        matches.extend(glob.glob(p, recursive=True))
    return max(matches, key=os.path.getmtime) if matches else None


def _kill_bloodflow_processes() -> int:
    """Terminate every running bloodflow process. Returns kill count."""
    killed = 0
    from_source = _from_source_mode()
    for proc in psutil.process_iter(["name", "cmdline", "pid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if from_source:
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                match = (
                    "python" in name
                    and "main.py" in cmdline
                    and "openmotion-bloodflow-app" in cmdline
                )
            else:
                match = "openwater" in name
            if not match:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
            killed += 1
            log.info(f"  killed bloodflow pid={proc.info['pid']}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


def _launch_app() -> None:
    if _from_source_mode():
        main_py = PROJECT_ROOT / "main.py"
        if not main_py.exists():
            pytest.fail(f"main.py not found at {main_py}")
        log.info(f"  launching from source: {sys.executable} {main_py}")
        subprocess.Popen([sys.executable, str(main_py)], cwd=str(PROJECT_ROOT))
    else:
        exe = _find_exe()
        if not exe:
            pytest.fail("OpenWaterApp.exe not found and OPENWATER_EXE unset")
        log.info(f"  launching: {exe}")
        subprocess.Popen([exe])


def _wait_for_app_window(timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ensure_visible():
            return True
        time.sleep(1)
    return False


def _wait_for_connect(timeout: int = APP_CONNECT_TIMEOUT) -> str | None:
    """Tail the freshly-rotated bloodflow log for a CONNECTED line."""
    log_path = find_app_log()
    if log_path is None:
        return None
    return wait_for_pattern(RE_CONNECTED, log_path, 0, timeout)


# ─────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────
def _set_force_laser_fail(value: bool) -> None:
    """Toggle ``forceLaserFail`` in ``app_config.json``. Preserves all
    other keys and key order."""
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["forceLaserFail"] = bool(value)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    log.info(f"  config: forceLaserFail = {value}")


def _read_force_laser_fail() -> bool:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return bool(json.load(f).get("forceLaserFail", False))


# ─────────────────────────────────────────────
# Safety trip detection (log-tail based)
# ─────────────────────────────────────────────
def _wait_for_safety_trip(timeout: int = SAFETY_TRIP_TIMEOUT) -> str | None:
    """Tail the most recent bloodflow app log for the 'Laser safety
    failure' line. Returns the matching log line or None on timeout.

    Uses offset=0 because each launch rotates the log file (filename
    contains a timestamp), so 0 means 'scan the entire fresh log'."""
    log_path = find_app_log()
    if log_path is None:
        return None
    return wait_for_pattern(RE_SAFETY_TRIP, log_path, 0, timeout)


def _check_no_recent_safety_trip(window_sec: int) -> str | None:
    """Watch the current log for ``window_sec`` and return the first
    matching 'Laser safety failure' line if one appears, or None if
    none did. Used by Phase 4 to verify the relaunch with
    forceLaserFail=false didn't re-trip the interlock."""
    return _wait_for_safety_trip(window_sec)


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────
@pytest.fixture(scope="class")
def outlet():
    """Shelly outlet powering the console. Skip if unreachable."""
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
# Test
# ─────────────────────────────────────────────
class TestForceLaserFail:
    """End-to-end verification of the forceLaserFail config flag."""

    def test_force_laser_fail_lifecycle(self, outlet, app):
        """Toggle the flag, run a scan, watch the safety trip, then
        restore the flag and power-cycle so the bench is clean for
        whatever runs next.

        ``app`` fixture dependency is intentional: it ensures the app
        has already been launched once at session start, so the
        autouse ``_check_app_alive`` guard doesn't trip before this
        test even gets to run. The body manages every subsequent
        launch/kill itself.
        """
        original_flag = _read_force_laser_fail()
        log.info(
            f"original config state: forceLaserFail = {original_flag}"
        )

        try:
            # ─── Phase 1: enable forceLaserFail and relaunch app ──
            log.info("=" * 60)
            log.info(
                "Phase 1: enabling forceLaserFail and relaunching app"
            )
            log.info("=" * 60)
            _kill_bloodflow_processes()
            time.sleep(2)
            _set_force_laser_fail(True)
            _launch_app()
            assert _wait_for_app_window(timeout=30), (
                "App window did not appear after relaunch with "
                "forceLaserFail=true"
            )

            # Window almost certainly moved on relaunch; refresh
            # the per-machine panel button calibration cache.
            recalibrate_panel_buttons()

            connect_line = _wait_for_connect(timeout=APP_CONNECT_TIMEOUT)
            assert connect_line, (
                f"App did not reach CONNECTED state within "
                f"{APP_CONNECT_TIMEOUT}s after relaunch"
            )
            log.info(f"  connected: {connect_line}")
            time.sleep(SLEEP)  # let camera auto-config settle

            # ─── Phase 2: fire the laser, expect safety trip ──────
            log.info("=" * 60)
            log.info("Phase 2: clicking Check to fire the laser")
            log.info("=" * 60)
            click_panel("Check")
            log.info(
                f"  waiting up to {SAFETY_TRIP_TIMEOUT}s for "
                f"'Laser safety failure' line in the app log..."
            )
            trip_line = _wait_for_safety_trip(SAFETY_TRIP_TIMEOUT)
            assert trip_line, (
                f"'Laser safety failure' did not appear in the app log "
                f"within {SAFETY_TRIP_TIMEOUT}s with forceLaserFail=true. "
                f"The fault laser params should have tripped the "
                f"safety interlock immediately on first laser pulse."
            )
            log.info(f"  PASS: safety trip detected: {trip_line.strip()}")

        finally:
            # ─── Phase 3 (always): restore flag, power-cycle, relaunch ─
            log.info("=" * 60)
            log.info(
                "Phase 3 (cleanup): restoring flag, power-cycling, "
                "relaunching app"
            )
            log.info("=" * 60)
            _kill_bloodflow_processes()
            time.sleep(2)
            _set_force_laser_fail(original_flag)
            log.info("  power-cycling outlet (off 5s, on)")
            outlet.power_cycle(off_time=5.0)
            time.sleep(3)
            _launch_app()
            if not _wait_for_app_window(timeout=30):
                # Don't mask the original failure with a teardown one;
                # log loudly but don't raise.
                log.error(
                    "Cleanup failure: app window did not reappear "
                    "after restore + power-cycle. Subsequent tests "
                    "will likely fail until the app is relaunched "
                    "manually."
                )
                return
            recalibrate_panel_buttons()
            log.info("  cleanup complete; app is running with restored config")

        # ─── Phase 4: verify clean state ──────────────────────────
        log.info("=" * 60)
        log.info(
            f"Phase 4: verifying clean state (no 'Laser safety "
            f"failure' line in app log over a {CLEAN_STATE_SETTLE_SEC}s "
            f"window)"
        )
        log.info("=" * 60)
        late_trip = _check_no_recent_safety_trip(CLEAN_STATE_SETTLE_SEC)
        assert late_trip is None, (
            f"'Laser safety failure' appeared in the app log after "
            f"restoring forceLaserFail=false and power-cycling: "
            f"{late_trip.strip() if late_trip else ''}. Either the "
            f"flag wasn't honoured on relaunch (check load_laser_params "
            f"wiring), or the safety interlock latched in hardware and "
            f"a single power-cycle wasn't enough to clear it."
        )
        log.info("  PASS: clean state confirmed")
