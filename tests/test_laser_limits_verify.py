"""
test_laser_limits_verify.py — the SDK reads back every laser safety-limit
register after each load and refuses to fire the laser on a mismatch
(openmotion-sdk#310, fixed by sdk#311 merge 197d85f; dev app builds from
1.5.4-dev.4 bundle it).

``apply_laser_power()`` writes 12 limit registers on the Safety EE and OPT
FPGAs (PULSE_WIDTH_LL/UL, RATE_LL, DRIVE_CL, PWM_CURRENT, CW_CURRENT on
each), reads each one back and latches the result on the console as
``console.laser_limits_error`` (None = verified). While it is set,
``ScanWorkflow.start_scan`` refuses a laser scan and
``MotionConsole.start_trigger`` raises ``LaserSafetyLimitError``. Only a
later clean load clears it.

The mismatch is simulated the way the PR's hand test does: one read-back
is altered in software, so no register on the console is ever changed.
A real mismatch cannot be forced from the host, because the load itself
rewrites every register before reading it back.

Steps:
  1. A clean load verifies: apply_laser_power() returns True and
     laser_limits_error is None.
  2. With one read-back altered: apply_laser_power() returns False and
     laser_limits_error names the register with its expected and read
     values.
  3. While latched: start_scan (laser on) is refused with that reason in
     last_scan_error, and start_trigger raises LaserSafetyLimitError. No
     laser fires.
  4. Read-back restored, load again (the reconnect): verified, the latch
     clears, and the trigger starts again — a 0.5 s burst at the app's
     trigger config, then stopped; no safety fault latched afterwards.

Preconditions
- Console (+ a sensor for the scan request) on USB, phantom in place.
- The SDK talks to the console directly: the class closes the app at
  setup and relaunches it at teardown (``sdk_direct``).

Marked ``release``: fires the laser for 0.5 s in step 4.
"""

from __future__ import annotations

import re
import time

import pytest

from conftest import log
from console_bench import close_app, relaunch_app, safety_status, sensors_session

pytestmark = [pytest.mark.release, pytest.mark.sdk_direct]

RE_PROBLEM = re.compile(r"\b((?:EE|OPT)_[A-Z_]+): expected (\d+), read (\d+)")
STATE: dict = {}


@pytest.fixture(scope="class")
def console_free(app):
    """Close the app so the SDK can own the console; relaunch afterwards."""
    close_app()
    try:
        yield
    finally:
        relaunch_app()


@pytest.fixture(scope="class")
def bench(console_free):
    """One standalone MotionInterface for the whole class."""
    with sensors_session() as b:
        yield b


class _AlterOneRead:
    """Simulate one register that did not take its value: during the armed
    load, record every register the load writes, and alter the first
    read-back of one of them (value + 1 in the low byte). Only reads on the
    loading thread are touched — the telemetry poller reads the safety
    status registers concurrently and must see real values. Nothing extra
    is written to the console."""

    def __init__(self, console):
        import threading
        self.console = console
        self.orig_read = console.read_i2c_packet
        self.orig_write = console.write_i2c_packet
        self.thread = threading.get_ident()
        self.written: set = set()
        self.armed = False
        self.altered = None

    @staticmethod
    def _loc(kw):
        return (kw.get("mux_index"), kw.get("channel"), kw.get("device_addr"), kw.get("reg_addr"))

    def write(self, *args, **kwargs):
        import threading
        if self.armed and threading.get_ident() == self.thread:
            self.written.add(self._loc(kwargs))
        return self.orig_write(*args, **kwargs)

    def read(self, *args, **kwargs):
        import threading
        data, rest = self.orig_read(*args, **kwargs)
        if (self.armed and self.altered is None and data is not None
                and threading.get_ident() == self.thread and self._loc(kwargs) in self.written):
            b = bytearray(data)
            b[0] = (b[0] + 1) & 0xFF
            self.altered = dict(kwargs, real=bytes(data).hex(), returned=bytes(b).hex())
            return bytes(b), rest
        return data, rest

    def install(self):
        self.console.read_i2c_packet = self.read
        self.console.write_i2c_packet = self.write

    def remove(self):
        self.console.read_i2c_packet = self.orig_read
        self.console.write_i2c_packet = self.orig_write
        self.armed = False


@pytest.mark.incremental
class TestLaserLimitsVerify:
    """Read-back after every load; mismatch latches and blocks the laser."""

    def test_01_clean_load_verifies(self, bench):
        """apply_laser_power() returns True; laser_limits_error is None."""
        from omotion.laser import apply_laser_power
        c = bench.iface.console
        STATE["fw"] = str(c.get_version())
        ok = apply_laser_power(c)
        log.info(f"  console fw {STATE['fw']}; apply_laser_power -> {ok}; laser_limits_error={c.laser_limits_error!r}")
        assert ok is True, "a clean load did not verify"
        assert c.laser_limits_error is None

    def test_02_mismatch_latches_and_names_register(self, bench):
        """One read-back altered: load returns False; the error names the register, expected and read values."""
        from omotion.laser import apply_laser_power
        c = bench.iface.console
        alter = _AlterOneRead(c)
        STATE["alter"] = alter
        alter.install()
        alter.armed = True
        ok = apply_laser_power(c)
        err = c.laser_limits_error
        STATE["err"] = err
        log.info(f"  altered read: {alter.altered}")
        log.info(f"  apply_laser_power -> {ok}; laser_limits_error={err!r}")
        assert ok is False, "a mismatching read-back did not fail the load"
        assert err, "laser_limits_error not set after a mismatch"
        m = RE_PROBLEM.search(err)
        assert m, f"error does not name a register with expected/read values: {err!r}"
        exp, got = int(m.group(2)), int(m.group(3))
        assert got != exp and (got - exp) % 256 == 1, f"read value {got} is not the altered one (expected {exp})"
        STATE["register"] = m.group(1)

    def test_03_laser_refused_while_latched(self, bench):
        """start_scan (laser on) refused with the same reason; start_trigger raises LaserSafetyLimitError."""
        from omotion.laser import LaserSafetyLimitError
        from omotion.ScanWorkflow import ScanRequest
        iface, c = bench.iface, bench.iface.console
        left = 0x66 if "left" in bench.sensors else 0
        right = 0x66 if "right" in bench.sensors else 0
        started = iface.start_scan(ScanRequest(subject_id="LIM310", duration_sec=5,
                                               left_camera_mask=left, right_camera_mask=right,
                                               disable_laser=False))
        reason = iface.scan_workflow.last_scan_error
        log.info(f"  start_scan -> {started}; last_scan_error={reason!r}")
        assert started is False, "a laser scan started while the limits check was failed"
        assert reason == STATE["err"], "last_scan_error is not the latched mismatch"
        with pytest.raises(LaserSafetyLimitError) as exc:
            c.start_trigger()
        log.info(f"  start_trigger raised LaserSafetyLimitError: {exc.value}")
        se, so = safety_status(c)
        assert not (se or so), f"safety fault present (se=0x{se:02x} so=0x{so:02x}) — the laser must not have fired"

    def test_04_clean_reload_clears_and_trigger_runs(self, bench):
        """Read-back restored; a clean load clears the latch; a 0.5 s trigger burst runs; no safety fault."""
        from omotion.laser import apply_laser_power
        iface, c = bench.iface, bench.iface.console
        STATE["alter"].remove()
        ok = apply_laser_power(c)
        log.info(f"  reload -> {ok}; laser_limits_error={c.laser_limits_error!r}")
        assert ok is True and c.laser_limits_error is None, "the clean reload did not clear the latch"
        c.set_trigger_json(data=iface.resolve_trigger_config(None))
        try:
            assert c.start_trigger(), "start_trigger was refused after the clean reload"
            time.sleep(0.5)
        finally:
            c.stop_trigger()
        time.sleep(1.0)
        se, so = safety_status(c)
        log.info(f"  trigger ran 0.5 s and stopped; safety_se=0x{se:02x} safety_so=0x{so:02x}")
        assert not (se or so), "a safety fault latched during the burst"
