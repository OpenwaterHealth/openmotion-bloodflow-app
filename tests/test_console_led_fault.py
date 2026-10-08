"""
test_console_led_fault.py — the console status LED must blink blue while a
laser-safety fault is latched (openmotion-bloodflow-app#691, fixed in
console-fw#66 / console-fw#65, ships in console firmware 1.8.2-dev.5).

Before the fix the firmware re-asserted idle green ~40 times a second
while a fault was latched (every telemetry poll tripped the trigger,
whose stop path set green), so a fault latched at connect showed a solid
green LED and even overwrote the app's blue error blink. With the fix the
fault indication owns the LED: blue 500 ms on / 500 ms off whatever the
trigger state, host LED writes ignored, and idle green only once the
latch clears (console power-cycle).

Steps (LED state is read over the protocol with OW_CTRL_GET_IND, the
firmware's commanded indicator state, sampled every ~60 ms):
  1. No fault: host LED writes blue / red / green each apply.
  2. Stage the fault exactly as the app's Force laser fail switch does
     (``apply_laser_power(force_fault=True)``: EE_PULSE_WIDTH_UL = 0),
     then run the trigger at 10 Hz for under a second so the safety FPGA
     measures a pulse against the zeroed limit and latches. The firmware
     stops the trigger itself on the trip; we stop it again regardless.
  3. Latched: 6 s of samples show only BLUE/OFF with a ~500 ms half-period
     and never GREEN.
  4. Host writes green / off / blue while latched are ignored.
  5. Normal laser params restored, console power-cycled (Shelly outlet).
  6. After the power-cycle: no fault, idle GREEN, host writes apply again.

Preconditions
- Console on a Shelly outlet (the latch only clears on a power-cycle;
  the test skips at step 5 without one and the console stays latched
  until someone cycles it by hand).
- Step 2 fires laser pulses for under a second, same as pressing Start
  with Force laser fail on. Phantom on the bench as for any scan.
- The SDK talks to the console directly: the class closes the app at
  setup and relaunches it at teardown.

Marked ``release``: fires the laser and power-cycles the console.
"""

from __future__ import annotations

import time

import pytest

import shelly
from conftest import log
from console_bench import (
    LED_BLUE,
    LED_GREEN,
    LED_OFF,
    LED_RED,
    close_app,
    console_session,
    led_name,
    led_summary,
    relaunch_app,
    safety_status,
    sample_led,
)

pytestmark = [pytest.mark.release, pytest.mark.sdk_direct]

FIXED_IN = "1.8.2-dev.5"
LATCH_TIMEOUT_S = 15.0
SAMPLE_S = 6.0
HALF_PERIOD_RANGE = (0.35, 0.65)   # 500 ms nominal, sampled at ~60 ms

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
def outlet():
    """Shelly outlet powering the console. Skip if unreachable."""
    try:
        out = shelly.default_outlet()
        out.is_on()
    except Exception as e:
        pytest.skip(f"Shelly outlet not reachable: {e}")
    yield out
    try:
        out.on()
    except Exception:
        pass


def _write_and_read(c, want: int) -> int:
    c.set_rgb_led(want)
    time.sleep(0.15)
    return c.get_rgb_led()


@pytest.mark.incremental
@pytest.mark.usefixtures("console_free")
class TestConsoleLedFault:
    """Status LED blinks blue while the laser-safety latch is set."""

    def test_01_no_fault_host_writes_apply(self):
        """With no fault latched, host LED writes of blue, red and green each take effect."""
        with console_session() as c:
            fw = str(c.get_version())
            STATE["fw"] = fw
            se, so = safety_status(c)
            log.info(f"  firmware {fw}; safety_se=0x{se:02x} safety_so=0x{so:02x}; LED={led_name(c.get_rgb_led())}")
            assert not (se or so), "a fault is already latched — power-cycle the console first"
            for want in (LED_BLUE, LED_RED, LED_GREEN):
                got = _write_and_read(c, want)
                log.info(f"  host write {led_name(want)} -> {led_name(got)}")
                assert got == want, f"host write {led_name(want)} not applied (read {led_name(got)})"
        if FIXED_IN not in fw:
            log.warning(f"  firmware is not {FIXED_IN}; the fix ships there — result describes this build")

    def test_02_fault_latches_under_trigger(self):
        """Faulted limit staged like the Force laser fail switch; a <1 s trigger burst latches safety_se."""
        from omotion.laser import apply_laser_power
        with console_session() as c:
            r = apply_laser_power(c, force_fault=True)
            log.info(f"  apply_laser_power(force_fault=True) -> {r}")
            c.set_trigger_json({"rate": 10})
            assert c.start_trigger(), "start_trigger refused before the fault was staged"
            se = so = 0
            deadline = time.time() + LATCH_TIMEOUT_S
            try:
                while time.time() < deadline:
                    se, so = safety_status(c)
                    if se or so:
                        break
                    time.sleep(0.2)
            finally:
                c.stop_trigger()
            STATE["latched"] = bool(se or so)
            log.info(f"  latch: safety_se=0x{se:02x} safety_so=0x{so:02x}")
        assert STATE["latched"], f"no laser-safety fault latched within {LATCH_TIMEOUT_S:.0f} s of the trigger burst"

    def test_03_led_blinks_blue_while_latched(self):
        """6 s of LED samples while latched: only BLUE/OFF, ~500 ms half-period, never GREEN."""
        with console_session() as c:
            samples = sample_led(c, SAMPLE_S)
        states, halves = led_summary(samples)
        log.info(f"  {len(samples)} samples; states {[led_name(s) for s in sorted(states)]}; "
                 f"transitions {max(len(halves) - 1, 0) + (1 if halves else 0)}; "
                 + (f"half-period {min(halves)*1000:.0f}-{max(halves)*1000:.0f} ms" if halves else "no transitions"))
        assert states <= {LED_BLUE, LED_OFF} and LED_BLUE in states and LED_OFF in states, (
            f"expected a BLUE/OFF blink, saw {[led_name(s) for s in sorted(states)]}"
        )
        lo, hi = HALF_PERIOD_RANGE
        assert halves and all(lo <= h <= hi for h in halves), (
            f"blink cadence off: half-periods {[round(h*1000) for h in halves]} ms, expected ~500 ms"
        )

    def test_04_host_writes_ignored_while_latched(self):
        """Host writes of green, off and blue while latched do not interrupt the blink; GREEN never appears."""
        with console_session() as c:
            for want in (LED_GREEN, LED_OFF, LED_BLUE):
                c.set_rgb_led(want)
                time.sleep(0.05)
            samples = sample_led(c, 3.0)
        states, _ = led_summary(samples)
        log.info(f"  after host writes: states {[led_name(s) for s in sorted(states)]}")
        assert LED_GREEN not in states and states <= {LED_BLUE, LED_OFF}, (
            f"a host write was applied while latched: {[led_name(s) for s in sorted(states)]}"
        )

    def test_05_restore_params_and_power_cycle(self, outlet):
        """Normal laser params restored; console power-cycled to clear the latch."""
        from omotion.laser import apply_laser_power
        with console_session() as c:
            apply_laser_power(c, force_fault=False)
        log.info("  power-cycling the console")
        outlet.power_cycle(off_time=5.0)
        time.sleep(10)

    def test_06_after_power_cycle_idle_green(self):
        """After the power-cycle: no fault, LED idle GREEN, host writes apply again."""
        with console_session(connect_timeout=60.0) as c:
            se, so = safety_status(c)
            led = c.get_rgb_led()
            log.info(f"  safety_se=0x{se:02x} safety_so=0x{so:02x}; LED={led_name(led)}")
            assert not (se or so), "fault still latched after the power-cycle"
            assert led == LED_GREEN, f"LED is {led_name(led)}, expected GREEN"
            for want in (LED_BLUE, LED_GREEN):
                got = _write_and_read(c, want)
                assert got == want, f"host write {led_name(want)} not applied after clear"
