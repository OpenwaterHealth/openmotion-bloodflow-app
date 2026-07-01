"""
test_force_laser_fail.py — verify forceLaserFail tripping and recovery.

End-to-end check that:

  - ``forceLaserFail: true`` in ``config/app_config.json`` causes the
    app to load ``config/laser_params_fault.json``, which trips the
    hardware safety interlock and fires the persistent
    "Laser safety warning detected" toast on first laser pulse.
  - Closing and reopening the app while the hardware is still in the
    safety-failure state surfaces the failure again on the second
    launch — without the connector having to persist anything to
    ``app_config.json`` (issue #107).
  - Restoring ``forceLaserFail = false`` and power-cycling the
    console clears the latched safety state and lets a subsequent
    Check fire cleanly.

Steps (one method, full cleanup in ``finally``):

  1. Close the app if it is open.
  2. On disk: turn clinical mode OFF and forceLaserFail ON.
  3. Open the app, calibrate panel buttons, wait for CONNECT.
  4. Click Check. Expect the 'Laser safety failure' line in the log.
  5. Close the app.
  6. Open the app again (no power-cycle, no Check click). Expect the
     'Laser safety failure' line to reappear in the freshly-rotated
     log purely from telemetry observing the still-latched hardware.
  7. Close the app, turn forceLaserFail OFF on disk.
  8. Power-cycle the console (clears the latched safety state).
  9. Open the app, click Check. Expect no safety-failure line.
 10. Restore forceLaserFail and clinicalMode to whatever was on disk
     at test entry; relaunch the app one more time so the bench is
     clean for whatever follows.

Marked ``release`` because it (a) tampers with persistent app config,
(b) requires a Shelly outlet to power-cycle the console, and (c) takes
~3 minutes end-to-end. Don't run in the dev-tier HIL set.
"""

from __future__ import annotations

import glob
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
from hil_helpers import (
    RE_CONNECTED,
    click_panel,
    find_app_log,
    read_app_config_value,
    recalibrate_panel_buttons,
    wait_for_pattern,
    write_app_config_value,
)

pytestmark = pytest.mark.release

# When the interlock trips, motion_connector logs a single ERROR line
# at the safety-status check site before firing the toast. We detect
# the trip by tailing the log for this prefix rather than walking UIA
# for the toast text — the toast is rendered in NotificationCenter as
# a plain QML Text, which doesn't surface in the Windows UIA tree, so
# a UIA-based check would silently time out.
RE_SAFETY_TRIP = re.compile(r"Laser safety failure:")

# Positive proof that the laser actually pulsed during a Check run.
# Emitted by the SDK's ScanWorkflow right before it calls the
# console's start_trigger; Check / contact-quality preflight reaches
# this code path through ``interface.start_scan``. Used by Step 9 to
# distinguish "Check ran cleanly" from "Check never got far enough
# to fire the laser, so we don't know if safety would have tripped or
# not".
#
# An earlier version of this regex looked for
# "Trigger started successfully" — the line emitted by
# ``motion_connector.startTrigger``. That path is *not* taken during
# a Check run (the SDK uses its own start_trigger), so the regex
# never matched and Step 9 falsely failed even when the laser fired
# cleanly.
RE_TRIGGER_STARTED = re.compile(r"Starting trigger")

# Wall-clock budget for seeing the safety-failure log line after Check
# fires. Fault-laser params trip the interlock almost immediately on
# the first laser pulse; 130 s covers slow camera-config plus the
# contact-quality preflight that Check kicks off.
SAFETY_TRIP_TIMEOUT = 130

# Wall-clock budget for the SDK to reach CONNECTED after a relaunch.
# Console enumeration + ping/version handshake is ~5 s normally; 60 s
# is conservative.
APP_CONNECT_TIMEOUT = 60

# Grace window after seeing 'Trigger started successfully' before we
# scan the log for a (definitely-not-wanted) safety failure. The
# fault-laser params trip the safety chip on the first laser pulse,
# so a real trip would log within the first second of trigger; 10 s
# is generous.
POST_TRIGGER_SAFETY_GRACE_SEC = 10


# ─────────────────────────────────────────────
# App lifecycle helpers
# ─────────────────────────────────────────────
# Inline rather than reusing conftest's session-scoped ``app`` fixture
# because that fixture isn't designed to be re-run mid-session — this
# test relaunches the app several times within a single method.
def _from_source_mode() -> bool:
    return os.environ.get("OPENWATER_FROM_SOURCE", "").lower() in (
        "1", "true", "yes",
    )


def _find_exe() -> str | None:
    env = os.environ.get("OPENWATER_EXE", "")
    if env and os.path.exists(env):
        return env
    patterns = [
        r"C:\Users\*\Documents\OpenMotion\**\Open-Motion.exe",
        r"C:\Users\*\Desktop\**\Open-Motion.exe",
        r"C:\Program Files\**\Open-Motion.exe",
        r"C:\Program Files (x86)\**\Open-Motion.exe",
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
            pytest.fail("Open-Motion.exe not found and OPENWATER_EXE unset")
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


def _wait_for_safety_trip(timeout: int = SAFETY_TRIP_TIMEOUT) -> str | None:
    """Tail the most recent bloodflow app log for the 'Laser safety
    failure' line. ``offset=0`` because each launch rotates the log
    file (filename contains a timestamp), so 0 means 'scan the entire
    fresh log'."""
    log_path = find_app_log()
    if log_path is None:
        return None
    return wait_for_pattern(RE_SAFETY_TRIP, log_path, 0, timeout)


def _restart_app(label: str) -> None:
    """Kill, relaunch, wait-for-window, recalibrate, wait-for-connect.

    Centralizes the post-relaunch setup so each step in the lifecycle
    test reads as a single call. ``label`` is logged for context.
    """
    log.info(f"  [{label}] relaunching app")
    _kill_bloodflow_processes()
    time.sleep(2)
    _launch_app()
    assert _wait_for_app_window(timeout=30), (
        f"[{label}] app window did not appear within 30 s of launch"
    )
    recalibrate_panel_buttons()
    connect_line = _wait_for_connect(timeout=APP_CONNECT_TIMEOUT)
    assert connect_line, (
        f"[{label}] app did not reach CONNECTED state within "
        f"{APP_CONNECT_TIMEOUT} s of relaunch"
    )
    log.info(f"  [{label}] connected: {connect_line.strip()}")
    time.sleep(SLEEP)  # let camera auto-config settle


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
    """End-to-end verification of forceLaserFail tripping + #107 recovery."""

    def test_force_laser_fail_lifecycle(self, outlet, app):
        """Walk through the 10-step lifecycle from the module docstring.

        ``app`` fixture dependency is intentional: it ensures the app
        has already been launched once at session start, so the
        autouse ``_check_app_alive`` guard doesn't trip before this
        test even gets to run. The body manages every subsequent
        launch/kill itself.
        """
        # Snapshot original config so the finally block can fully
        # restore it. Both flags affect what the test sees, so both
        # have to be remembered.
        original_force_laser_fail = bool(
            read_app_config_value("forceLaserFail", False)
        )
        original_clinical_mode = bool(
            read_app_config_value("clinicalMode", False)
        )
        log.info(
            f"original config state: forceLaserFail="
            f"{original_force_laser_fail}, clinicalMode="
            f"{original_clinical_mode}"
        )

        try:
            # ─── Step 1+2: kill, then write both flags before launch ─
            # ``force_app_config_value`` writes to disk but does not
            # poke the running app, so this only has the right effect
            # if the app has already been killed. The running app caches
            # its config at startup and won't reload.
            log.info("=" * 60)
            log.info(
                "Step 1+2: killing app, then writing clinicalMode=false "
                "+ forceLaserFail=true to disk"
            )
            log.info("=" * 60)
            _kill_bloodflow_processes()
            time.sleep(2)
            write_app_config_value("clinicalMode", False)
            write_app_config_value("forceLaserFail", True)
            log.info("  config staged for fault-trip launch")

            # ─── Step 3: launch + calibrate ──────────────────────────
            log.info("=" * 60)
            log.info("Step 3: launching app, calibrating panel buttons")
            log.info("=" * 60)
            _restart_app("step 3")

            # ─── Step 4: click Check, watch it fail ──────────────────
            log.info("=" * 60)
            log.info(
                "Step 4: clicking Check, expecting 'Laser safety "
                "failure' in the app log"
            )
            log.info("=" * 60)
            click_panel("Check")
            log.info(
                f"  waiting up to {SAFETY_TRIP_TIMEOUT} s for trip line"
            )
            trip_line = _wait_for_safety_trip(SAFETY_TRIP_TIMEOUT)
            assert trip_line, (
                f"'Laser safety failure' did not appear in the app log "
                f"within {SAFETY_TRIP_TIMEOUT} s with forceLaserFail=true. "
                f"The fault laser params should have tripped the "
                f"interlock immediately on first laser pulse."
            )
            log.info(f"  PASS: safety trip detected: {trip_line.strip()}")

            # ─── Step 5+6: close, reopen WITHOUT power-cycle, expect re-trip ─
            # Issue #107: closing and reopening the app while the
            # hardware safety latch is still tripped should make the
            # toast (and the log line) reappear from telemetry alone —
            # no Check click, no config write, no state persisted.
            log.info("=" * 60)
            log.info(
                "Step 5+6: closing the app, reopening WITHOUT power-cycle, "
                "expecting safety failure to re-surface from latched "
                "hardware via telemetry"
            )
            log.info("=" * 60)
            _restart_app("step 6")
            log.info(
                f"  waiting up to {SAFETY_TRIP_TIMEOUT} s for the trip "
                f"line in the fresh log (no Check click — telemetry "
                f"should observe the latched fault on its own)"
            )
            second_trip = _wait_for_safety_trip(SAFETY_TRIP_TIMEOUT)
            assert second_trip, (
                f"Issue #107 reproduction: 'Laser safety failure' "
                f"did not appear in the SECOND-launch app log within "
                f"{SAFETY_TRIP_TIMEOUT} s. The safety chip's fault "
                f"latch should still be tripped after a USB "
                f"disconnect/reconnect, and the telemetry reader "
                f"thread should observe it on the first poll cycle "
                f"and fire the persistent laser-safety toast — even "
                f"without any state written to app_config.json. If "
                f"this fails, either the SDK silently defaulted "
                f"safety_ok=True when the chip didn't respond, or "
                f"the chip cleared its latch on USB reset (in which "
                f"case the detection has to come from a different "
                f"signal)."
            )
            log.info(
                f"  PASS: second-launch safety trip detected: "
                f"{second_trip.strip()}"
            )

            # ─── Step 7: close app, turn off forceLaserFail ──────────
            log.info("=" * 60)
            log.info(
                "Step 7: closing app, writing forceLaserFail=false"
            )
            log.info("=" * 60)
            _kill_bloodflow_processes()
            time.sleep(2)
            write_app_config_value("forceLaserFail", False)

            # ─── Step 8: power-cycle the console ─────────────────────
            # The hardware safety latch only clears on a console power
            # cycle. Without this, Step 9's Check would re-trip the
            # interlock from the same latched state we observed in
            # Step 6 — so the "clean run" assertion would fail not
            # because the fix is broken but because the bench was
            # never reset.
            log.info("=" * 60)
            log.info("Step 8: power-cycling the console (off 5s, on)")
            log.info("=" * 60)
            outlet.power_cycle(off_time=5.0)
            time.sleep(3)  # let USB enumeration settle before relaunch

            # ─── Step 9: launch + click Check, expect clean run ──────
            # "Clean run" means two things, in order:
            #   (a) the laser actually fires (we see
            #       'Trigger started successfully' in the fresh log),
            #   (b) no 'Laser safety failure:' line shows up — neither
            #       before, during, nor in the grace window after.
            #
            # An earlier version of this step only waited 15 s after
            # the click for a safety failure and then declared success.
            # That's wrong: the contact-quality preflight Check kicks
            # off takes ~2 minutes before the laser actually pulses,
            # so 15 s of "no failure line" mostly proved the laser
            # hadn't fired YET — not that it fired cleanly.
            log.info("=" * 60)
            log.info(
                "Step 9: launching app, clicking Check, expecting the "
                "laser to fire AND no 'Laser safety failure' in the "
                "freshly-rotated log"
            )
            log.info("=" * 60)
            _restart_app("step 9")
            click_panel("Check")
            log.info(
                f"  waiting up to {SAFETY_TRIP_TIMEOUT} s for "
                f"'Trigger started successfully' (positive proof the "
                f"laser pulsed)"
            )
            log_path = find_app_log()
            assert log_path is not None, (
                "Step 9: could not locate the bloodflow app log; "
                "cannot verify Check fired the laser"
            )
            trigger_line = wait_for_pattern(
                RE_TRIGGER_STARTED, log_path, 0, SAFETY_TRIP_TIMEOUT
            )
            assert trigger_line, (
                f"Step 9: 'Trigger started successfully' did not "
                f"appear in the fresh app log within "
                f"{SAFETY_TRIP_TIMEOUT} s of clicking Check. The "
                f"laser never fired, so the test can't verify the "
                f"safety interlock cleared. Likely causes: Check "
                f"didn't actually click (panel calibration off), or "
                f"contact-quality preflight stalled before reaching "
                f"the trigger stage."
            )
            log.info(f"  laser fired: {trigger_line.strip()}")
            log.info(
                f"  grace window: waiting {POST_TRIGGER_SAFETY_GRACE_SEC} s "
                f"to catch any late safety trip"
            )
            late_trip = _wait_for_safety_trip(POST_TRIGGER_SAFETY_GRACE_SEC)
            assert late_trip is None, (
                f"Step 9: 'Laser safety failure' appeared after the "
                f"laser fired with forceLaserFail=false and a fresh "
                f"power cycle: {late_trip.strip()}. Either the flag "
                f"wasn't honoured on relaunch (check load_laser_params "
                f"wiring) or the safety interlock didn't clear on the "
                f"power cycle."
            )
            log.info(
                "  PASS: clean state confirmed — laser fired without "
                "tripping safety"
            )

        finally:
            # ─── Step 10: restore both flags to their original values ─
            # Always runs so the bench ends in the same configuration
            # we found it. Log loudly but don't raise on cleanup
            # failure — that would mask whatever real failure already
            # broke the test.
            log.info("=" * 60)
            log.info(
                f"Step 10 (cleanup): restoring forceLaserFail="
                f"{original_force_laser_fail}, clinicalMode="
                f"{original_clinical_mode}"
            )
            log.info("=" * 60)
            _kill_bloodflow_processes()
            time.sleep(2)
            write_app_config_value("forceLaserFail", original_force_laser_fail)
            write_app_config_value("clinicalMode", original_clinical_mode)
            _launch_app()
            if not _wait_for_app_window(timeout=30):
                log.error(
                    "Cleanup failure: app window did not reappear "
                    "after restore. Subsequent tests will likely fail "
                    "until the app is relaunched manually."
                )
                return
            recalibrate_panel_buttons()
            log.info("  cleanup complete; original config restored")
