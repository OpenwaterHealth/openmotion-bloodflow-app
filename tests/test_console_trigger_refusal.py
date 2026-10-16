"""
test_console_trigger_refusal.py — the console answers OW_ERROR when a trigger
start or a trigger configuration is refused (openmotion-console-fw#71, CFW-05/06,
commit 198abc3, ships in console firmware 1.8.2-rc.2).

Before the fix OW_CTRL_START_TRIG replied OW_RESP even when the host-loss,
laser-safety or TEC interlock refused the start, and SET_TRIG replied OK for any
JSON that parsed. With the fix: START_TRIG answers OW_ERROR with one status byte
(TRIG_INTERLOCK_HOST_LOSS 0x01, _LASER_SAFETY 0x02, _TEC 0x04,
TRIG_START_TIMER_FAILED 0x80); SET_TRIG validates (1..40 Hz, pulse width
1..period-1 us, laser gate 20..2200 us, delay + gate + skip < period) and answers
OW_ERROR for a rejected configuration, keeping the previous one; a rejected
configuration no longer stops a running trigger.

The SDK side (describe_trigger_refusal / last_start_refusal_reason) is on
openmotion-sdk `next`; run with PYTHONPATH pointing at a checkout of it so the
status byte is decoded. With SDK 1.13.0 the same checks hold, minus the text.

Steps (sdk_direct; the app is closed; TA trigger and sync-out disabled in every
configuration so the laser never fires):
  1. Console on the fixed firmware, no safety fault latched; a valid 40 Hz
     configuration is accepted; start and stop succeed.
  2. Six invalid configurations are each refused (set_trigger_json -> None) and
     the configuration read back is unchanged.
  3. While the trigger runs, a rejected configuration leaves it running; a valid
     one stops it (as before).
  4. Laser-safety interlock: stage a faulted limit (apply_laser_power
     force_fault=True, EE_PULSE_WIDTH_UL = 0), run the trigger until safety_se
     latches, stop; START_TRIG is now refused with status 0x02 and the SDK
     reports the reason.
  5. Restore the laser parameters, power-cycle the console (the latch clears
     only on power-cycle), start accepted again.
"""
from __future__ import annotations

import json
import time

import pytest

import shelly
from conftest import log
from console_bench import close_app, console_session, relaunch_app, safety_status

pytestmark = [pytest.mark.release, pytest.mark.sdk_direct]

FIXED_IN = "1.8.2-rc.2"
LATCH_TIMEOUT_S = 15.0
VALID = {"TriggerFrequencyHz": 40.0, "TriggerPulseWidthUsec": 500, "LaserPulseDelayUsec": 100,
         "LaserPulseWidthUsec": 500, "LaserPulseSkipInterval": 600, "LaserPulseSkipDelayUsec": 1800,
         "EnableSyncOut": False, "EnableTaTrigger": False, "TriggerStatus": 0}
INVALID = [
    ("frequency 50 Hz (max 40)", {**VALID, "TriggerFrequencyHz": 50.0}),
    ("frequency 0 Hz", {**VALID, "TriggerFrequencyHz": 0.0}),
    ("trigger pulse width = period (25000 us at 40 Hz)", {**VALID, "TriggerPulseWidthUsec": 25000}),
    ("laser gate 10 us (min 20)", {**VALID, "LaserPulseWidthUsec": 10}),
    ("laser gate 2300 us (max 2200)", {**VALID, "LaserPulseWidthUsec": 2300}),
    ("delay + gate + skip delay >= period (skip delay 24500)", {**VALID, "LaserPulseSkipDelayUsec": 24500}),
]
STATE: dict = {}


def _cfg(c) -> dict:
    j = c.get_trigger_json()
    return j if isinstance(j, dict) else json.loads(j)


def _running(c, window_s: float = 0.5) -> bool:
    """FSYNC timer state: the pulse counter advances only while the trigger runs."""
    a = c.get_fsync_pulsecount()
    time.sleep(window_s)
    return c.get_fsync_pulsecount() > a


def _raw_set_trigger(c, cfg: dict) -> tuple[str, str]:
    """Send SET_TRIG straight to the console, skipping the SDK's own range check,
    so the firmware's verdict is observed. Returns (verdict, reply)."""
    from omotion.config import OW_CONTROLLER, OW_CTRL_SET_TRIG, OW_ERROR
    payload = json.dumps(cfg, separators=(",", ":")).encode("utf-8")
    r = c.uart.send_packet(id=None, packetType=OW_CONTROLLER, command=OW_CTRL_SET_TRIG, data=payload)
    c.uart.clear_buffer()
    body = bytes(r.data[:r.data_len]).decode("utf-8", "replace") if r.data_len else ""
    return ("refused (OW_ERROR)" if r.packetType == OW_ERROR else "ACCEPTED"), body[:80]


@pytest.fixture(scope="class")
def console_free(app):
    close_app()
    try:
        yield
    finally:
        relaunch_app()


@pytest.mark.incremental
@pytest.mark.usefixtures("console_free")
class TestConsoleTriggerRefusal:

    def test_01_valid_config_accepted_start_stop(self):
        with console_session() as c:
            fw = str(c.get_version())
            se, so = safety_status(c)
            log.info(f"  firmware {fw}, boot mode {c.get_boot_mode()}, safety_se=0x{se:02x} safety_so=0x{so:02x}")
            assert FIXED_IN in fw, f"console is on {fw}, the fix ships in {FIXED_IN}"
            assert not (se or so), "a safety fault is already latched — power-cycle the console first"
            r = c.set_trigger_json(VALID)
            log.info(f"  set_trigger_json(valid 40 Hz, TA off) -> {'accepted' if r else r}")
            assert r, "valid configuration refused"
            assert c.start_trigger(), f"start_trigger refused on a clean console: {getattr(c, 'last_start_refusal_reason', None)}"
            time.sleep(1.0)
            running = _running(c)
            assert c.stop_trigger(), "stop_trigger failed"
            log.info(f"  start accepted (TriggerStatus while running = {int(running)}), stop ok")
            STATE["fw"] = fw

    def test_02_invalid_configs_refused_previous_kept(self):
        with console_session() as c:
            assert c.set_trigger_json(VALID), "valid configuration refused"
            before = _cfg(c)
            rows = []
            for label, cfg in INVALID:
                # SDK verdict (its own validate_trigger_config) and the firmware's verdict on the same JSON
                try:
                    c.set_trigger_json(cfg)
                    sdk = "sent"
                except ValueError as e:
                    sdk = f"refused by the SDK ({str(e)[:60]})"
                fw, reply = _raw_set_trigger(c, cfg)
                after = _cfg(c)
                kept = all(after.get(k) == before.get(k) for k in VALID if k != "TriggerStatus")
                rows.append((label, fw.startswith("refused"), kept, sdk))
                log.info(f"  {label}: console {fw}; SDK {sdk}; previous config kept: {kept}")
            STATE["invalid"] = rows
            bad = [l for l, refused, kept, _ in rows if not refused or not kept]
            assert not bad, f"not refused or config changed: {bad}"

    def test_03_rejected_config_does_not_stop_a_running_trigger(self):
        with console_session() as c:
            assert c.set_trigger_json(VALID)
            assert c.start_trigger(), "start refused"
            time.sleep(0.5)
            assert _running(c), "trigger not reported running after start"
            fw, _ = _raw_set_trigger(c, {**VALID, "TriggerFrequencyHz": 50.0})
            time.sleep(0.5)
            still = _running(c)
            log.info(f"  rejected config while running -> console {fw}; trigger still running: {still}")
            r2 = c.set_trigger_json(VALID)
            time.sleep(0.5)
            stopped = not _running(c)
            log.info(f"  valid config while running -> {'accepted' if r2 else r2}; trigger stopped: {stopped}")
            c.stop_trigger()
            assert fw.startswith("refused") and still, "a rejected configuration must be refused and leave the trigger running"
            assert r2 and stopped, "a valid configuration applied while running should stop the trigger (previous behaviour)"

    def test_04_start_refused_with_laser_safety_interlock(self):
        from omotion.laser import apply_laser_power
        with console_session() as c:
            r = apply_laser_power(c, force_fault=True)
            log.info(f"  apply_laser_power(force_fault=True) -> {r}; laser_limits_error={getattr(c, 'laser_limits_error', None)}")
            # The EE safety FPGA watches the laser gate, so the TA trigger must be on for the
            # faulted limit (EE_PULSE_WIDTH_UL = 0) to trip (latched 0.30 s into the burst on
            # 2026-10-16; <1 s of laser, sensors on the phantom). The console keeps the previous
            # configuration's flags, so set them explicitly rather than via the "rate" shorthand.
            assert c.set_trigger_json({**VALID, "EnableSyncOut": True, "EnableTaTrigger": True})
            assert c.start_trigger(), "start refused before the fault latched"
            se = so = 0
            t0 = time.time()
            try:
                while time.time() - t0 < LATCH_TIMEOUT_S:
                    se, so = safety_status(c)
                    if se or so:
                        break
                    time.sleep(0.2)
            finally:
                c.stop_trigger()
            log.info(f"  latched after {time.time() - t0:.1f} s: safety_se=0x{se:02x} safety_so=0x{so:02x}")
            assert se or so, "no laser-safety fault latched"
            ok = c.start_trigger()
            status = getattr(c, "last_start_refusal", "n/a")
            reason = getattr(c, "last_start_refusal_reason", "n/a")
            log.info(f"  start_trigger with the interlock latched -> {ok}; status byte {status if status is None or isinstance(status, str) else f'0x{status:02x}'}; reason: {reason}")
            STATE["interlock"] = dict(ok=ok, status=status, reason=reason, se=se, so=so)
            if ok:
                c.stop_trigger()
            assert not ok, "start_trigger returned True with the laser-safety interlock latched"
            if status != "n/a":
                assert status == 0x02, f"expected status 0x02 (TRIG_INTERLOCK_LASER_SAFETY), got {status!r}"

    def test_05_restore_and_power_cycle(self):
        from omotion.laser import apply_laser_power
        with console_session() as c:
            apply_laser_power(c, force_fault=False)
        outlet = shelly.default_outlet()
        outlet.power_cycle(off_time=5.0)
        time.sleep(3)
        with console_session(connect_timeout=60) as c:
            se, so = safety_status(c)
            log.info(f"  after power-cycle: safety_se=0x{se:02x} safety_so=0x{so:02x}")
            assert not (se or so), "fault still latched after the power-cycle"
            assert c.set_trigger_json(VALID)
            ok = c.start_trigger()
            c.stop_trigger()
            log.info(f"  start_trigger after the power-cycle -> {ok}")
            assert ok, f"start refused after the power-cycle: {getattr(c, 'last_start_refusal_reason', None)}"
