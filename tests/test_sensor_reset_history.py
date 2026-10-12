"""
test_sensor_reset_history.py — a sensor reports why it last went down, and
the SDK/app read it at every connect (openmotion-sensor-fw#137 with
sensor-fw#138 in 1.8.3-dev.3+, SDK openmotion-sdk#314 bundled from app
1.5.4-dev.4).

Before: when a module dropped off USB the host could not tell a power loss
from a watchdog reset loop; the reset counters lived in the module's RAM
and were only printed on a debug UART at boot. Now OW_CMD_RESET_HISTORY
(0x10) returns a persistent record: the reason the previous session ended
(power_off / host_reset / host_dfu / unexpected / ecc / fault /
error_handler), boots since power-on with a count per reason, the
previous session's length, the current uptime, USB recoveries, ECC and
DMA error counts. The SDK reads it in ``get_reset_history()`` and
``log_sensor_info()`` prints it at every sensor connect.

Steps (SDK direct, app closed; no scan, no laser):
  1. Baseline: every attached sensor answers the command; boot_count ≥ 1,
     uptime > 0, reason is a known one.
  2. Host reset of one sensor (OW_CMD_RESET): it drops off USB and comes
     back; its record now says host_reset, boot_count +1, host_reset
     count +1, and the previous session length matches the uptime read
     just before the reset. The other sensor is untouched (same
     boot_count, larger uptime).
  3. The app is relaunched: its connect-time block for that sensor logs
     "shutdown = host reset (OW_CMD_RESET)" and "boots = N since power-on
     (… host_reset 1)". The app is closed again.
  4. Power-cycle through the Shelly outlet (skipped without one): every
     sensor reports power_off with boot_count 1 and a small uptime — the
     counters are cleared by a power-on.
  5. host_dfu: OW_CMD_DFU, then the identical rc.1 image is re-flashed
     through the SDK's bundled dfu-util (libusb 1.0.30). On a bare-metal
     unit the ROM DFU does not come back after ':leave -R' (sdk#315), the
     step power-cycles to recover and skips, because the record is lost
     with the power. On a bootloader unit it would re-enumerate and
     report host_dfu, boot +1.
  6. Brown-out tries: 0.3 s and 0.6 s outlet cuts. A sensor whose RAM
     survives reports 'unexpected' with the BOR flag; one that loses RAM
     reports power_off, boot 1 (what this outlet produces).

Not covered here: watchdog / CPU-fault / Error_Handler reasons — no
production command hangs or crashes the firmware; they need the author's
hook build.

Preconditions
- Console + at least one sensor on 1.8.3-dev.3 or newer; Shelly outlet for
  step 4.

Marked ``dev``: ~1.5 min (≈3 min with the power-cycle).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

import shelly
from conftest import log
from console_bench import close_app, relaunch_app, sensors_session
from hil_helpers import find_app_log

pytestmark = [pytest.mark.dev, pytest.mark.sdk_direct]

KNOWN_REASONS = {"power_off", "unexpected", "host_reset", "host_dfu", "ecc", "fault", "error_handler"}
RESET_SIDE = "left"
REENUMERATE_S = 40.0
# The image already on the sensors (1.8.3-rc.1 motion-sensor-fw-baremetal.bin); the sha256 is the
# release asset's digest, checked before anything is flashed.
DFU_IMAGE = r"C:\Users\vpenn\Downloads\motion-sensor-fw-baremetal.bin"
DFU_IMAGE_SHA256 = "4a27014a2840bbe4030a06f4c3cc8893d4d7e2ce8ed830b184c16b3f97820c99"
BROWNOUT_OFF_S = (0.3, 0.6)
STATE: dict = {}


def _file_version(path) -> str:
    try:
        import win32api
        info = win32api.GetFileVersionInfo(str(path), "\\")
        return f"{info['FileVersionMS'] >> 16}.{info['FileVersionMS'] & 0xFFFF}.{info['FileVersionLS'] >> 16}"
    except Exception:
        return "?"


@pytest.fixture(scope="class")
def console_free(app):
    close_app()
    try:
        yield
    finally:
        relaunch_app()


def _history(sensor, side: str) -> dict:
    h = sensor.get_reset_history()
    assert h, f"{side}: get_reset_history() returned nothing (firmware {sensor.get_version()})"
    log.info(f"  {side} ({sensor.read_serial_number()}, fw {sensor.get_version()}): "
             f"last_shutdown={h['last_shutdown']} boot_count={h['boot_count']} "
             f"prev_alive={h['prev_alive_ms']/1000:.1f}s uptime={h['uptime_ms']/1000:.1f}s "
             f"counts={ {k: v for k, v in h['shutdown_counts'].items() if v} } "
             f"usb_recover={h['usb_recover_count']} ecc={h['ecc_sbe_count']}/{h['ecc_dbe_count']} "
             f"dma={h['dma_err_count']} rcc={h['last_rcc_flags'] or 'none'}")
    return h


@pytest.mark.incremental
@pytest.mark.usefixtures("console_free")
class TestSensorResetHistory:
    """The reset record answers "why did it drop?" and survives a host reset."""

    def test_01_every_sensor_answers(self):
        """Each attached sensor returns a parsed record with a known reason, boot_count ≥ 1 and uptime > 0."""
        with sensors_session() as bench:
            STATE["sides"] = tuple(bench.sensors)
            STATE["base"] = {}
            for side, s in bench.sensors.items():
                h = _history(s, side)
                assert h["last_shutdown"] in KNOWN_REASONS, f"{side}: unknown reason {h['last_shutdown']!r}"
                assert h["boot_count"] >= 1 and h["uptime_ms"] > 0
                assert sum(h["shutdown_counts"].values()) == h["boot_count"], (
                    f"{side}: per-reason counts {h['shutdown_counts']} do not sum to boot_count {h['boot_count']}"
                )
                STATE["base"][side] = h
        assert RESET_SIDE in STATE["sides"], f"no {RESET_SIDE} sensor attached"

    def test_02_host_reset_is_recorded(self):
        """OW_CMD_RESET on one sensor: it re-enumerates and reports host_reset, boot_count +1, previous session ≈ uptime before the reset."""
        from omotion.connection_state import ConnectionState
        with sensors_session() as bench:
            s = bench.sensors[RESET_SIDE]
            before = _history(s, RESET_SIDE)
            t0 = time.monotonic()
            assert s.soft_reset(), "soft_reset was refused"
            assert s.wait_for(ConnectionState.DISCONNECTED, timeout=15), "sensor did not drop off USB after the reset"
            assert s.wait_for(ConnectionState.CONNECTED, timeout=REENUMERATE_S), "sensor did not come back within 40 s"
            log.info(f"  {RESET_SIDE} back on USB {time.monotonic() - t0:.1f} s after OW_CMD_RESET")
            time.sleep(1.0)
            after = _history(s, RESET_SIDE)
            others = {side: _history(x, side) for side, x in bench.sensors.items() if side != RESET_SIDE}
        STATE["after_reset"] = after
        assert after["last_shutdown"] == "host_reset", f"reason after a host reset is {after['last_shutdown']!r}"
        assert after["boot_count"] == before["boot_count"] + 1
        assert after["shutdown_counts"].get("host_reset", 0) == before["shutdown_counts"].get("host_reset", 0) + 1
        assert abs(after["prev_alive_ms"] - before["uptime_ms"]) <= 2500, (
            f"previous-session length {after['prev_alive_ms']} ms vs uptime before reset {before['uptime_ms']} ms"
        )
        assert after["uptime_ms"] < before["uptime_ms"], "uptime did not restart"
        for side, h in others.items():
            b = STATE["base"][side]
            assert h["boot_count"] == b["boot_count"] and h["uptime_ms"] > b["uptime_ms"], f"{side} was disturbed by the reset"

    def test_03_app_logs_it_at_connect(self):
        """Relaunched app: its connect block for the reset sensor says host reset with the boot count; app closed again."""
        relaunch_app()
        try:
            lp = find_app_log()
            assert lp, "app log not found"
            deadline = time.time() + 30
            text = ""
            while time.time() < deadline:
                text = lp.read_bytes().decode("utf-8", errors="replace")
                if text.count("\n  shutdown = ") >= len(STATE["sides"]):
                    time.sleep(1)          # the block's last lines land a moment after the first
                    text = lp.read_bytes().decode("utf-8", errors="replace")
                    break
                time.sleep(1)
            text = text.replace("\r\n", "\n")     # the app log is CRLF
            blocks = re.findall(
                r"  firmware = (\S+)\n(?:  hw_id\s+= \S+\n)?  serial   = (\S+)\n  shutdown = ([^\n]+)\n  boots    = ([^\n]+)",
                text,
            )
            for fw, serial, shutdown, boots in blocks:
                log.info(f"  app connect block: {serial} fw {fw}: shutdown = {shutdown}; boots = {boots}")
            n = STATE["after_reset"]["boot_count"]
            hit = [b for b in blocks if b[2].startswith("host reset (OW_CMD_RESET)") and b[3].startswith(f"{n} since power-on")]
            assert hit, f"no connect block with 'host reset (OW_CMD_RESET)' and 'boots = {n}' (blocks: {blocks})"
            assert "host_reset 1" in hit[0][3] or re.search(r"host_reset \d+", hit[0][3]), hit[0][3]
        finally:
            close_app()

    def test_04_power_cycle_clears_the_counters(self):
        """Shelly power-cycle: every sensor reports power_off, boot_count 1, small uptime."""
        try:
            outlet = shelly.default_outlet()
            outlet.is_on()
        except Exception as e:
            pytest.skip(f"Shelly outlet not reachable: {e}")
        outlet.power_cycle(off_time=5.0)
        time.sleep(12)
        with sensors_session(connect_timeout=60.0) as bench:
            time.sleep(1.0)
            for side, s in bench.sensors.items():
                h = _history(s, side)
                assert h["last_shutdown"] == "power_off", f"{side}: {h['last_shutdown']!r} after a power-cycle"
                assert h["boot_count"] == 1 and h["shutdown_counts"].get("power_off") == 1, f"{side}: counts not cleared {h['shutdown_counts']}"
                assert h["uptime_ms"] < 120_000, f"{side}: uptime {h['uptime_ms']} ms is not a fresh boot"

    def test_05_host_dfu_is_recorded(self):
        """OW_CMD_DFU + re-flash of the identical image through the SDK's dfu-util: the sensor comes back reporting host_dfu, boot +1."""
        import hashlib
        from omotion.connection_state import ConnectionState
        from omotion.firmware_update import FirmwareUpdater
        image = Path(DFU_IMAGE)
        assert image.is_file(), f"{image} not found"
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        assert digest == DFU_IMAGE_SHA256, f"{image} is not the 1.8.3-rc.1 bare-metal image (sha256 {digest[:16]}…)"
        updater = FirmwareUpdater()
        dfu = updater._dfu
        log.info(f"  dfu-util: {dfu.dfu_util_path} (libusb {_file_version(dfu.dfu_dir / 'libusb-1.0.dll')})")
        try:
            outlet = shelly.default_outlet()
            outlet.is_on()
        except Exception:
            outlet = None
        with sensors_session() as bench:
            s = bench.sensors[RESET_SIDE]
            before = _history(s, RESET_SIDE)
            t0 = time.monotonic()
            result = updater.update(s, image)
            log.info(f"  boot mode in DFU: {updater.last_boot_mode}; dfu-util rc={result.returncode} success={result.success}; "
                     f"{(result.stdout or '').strip().splitlines()[-1][:120] if result.stdout else ''}")
            assert result.success, f"dfu-util failed (rc {result.returncode}):\n{(result.stdout or '')[-1500:]}"
            came_back = s.wait_for(ConnectionState.CONNECTED, timeout=REENUMERATE_S)
            log.info(f"  {RESET_SIDE} {'back on USB' if came_back else 'NOT back'} {time.monotonic() - t0:.1f} s after the DFU request")
            if not came_back:
                if outlet is not None:
                    log.warning("  sensor stayed in DFU after ':leave' — power-cycling to recover (host_dfu record will be lost)")
                    outlet.power_cycle(off_time=5.0)
                    time.sleep(12)
                pytest.skip("host_dfu is not demonstrable on a bare-metal unit: after the download dfu-util's "
                            "':leave -R' leaves the ROM DFU wedged (sdk#315) and only a power-cycle recovers it, "
                            "which clears the record; the sensor was power-cycled and is back on its firmware")
            time.sleep(1.0)
            after = _history(s, RESET_SIDE)
        STATE["after_dfu"] = after
        assert after["last_shutdown"] == "host_dfu", f"reason after a DFU session is {after['last_shutdown']!r}"
        assert after["boot_count"] == before["boot_count"] + 1
        assert after["shutdown_counts"].get("host_dfu", 0) == before["shutdown_counts"].get("host_dfu", 0) + 1
        assert abs(after["prev_alive_ms"] - before["uptime_ms"]) <= 2500

    def test_06_brownout_attempts(self):
        """Short outlet cuts: a sensor whose RAM survives reports 'unexpected' with the BOR flag; one that loses RAM reports power_off, boot 1. Either way the record is consistent."""
        try:
            outlet = shelly.default_outlet()
            outlet.is_on()
        except Exception as e:
            pytest.skip(f"Shelly outlet not reachable: {e}")
        seen = []
        for off_s in BROWNOUT_OFF_S:
            with sensors_session(connect_timeout=60.0) as bench:
                prev = {side: _history(x, side)["boot_count"] for side, x in bench.sensors.items()}
            outlet.off(); time.sleep(off_s); outlet.on()
            time.sleep(12)
            with sensors_session(connect_timeout=60.0) as bench:
                time.sleep(1.0)
                for side, x in bench.sensors.items():
                    h = _history(x, side)
                    seen.append((off_s, side, h["last_shutdown"], h["boot_count"], h["last_rcc_flags"]))
                    assert h["last_shutdown"] in ("unexpected", "power_off"), f"{side}: {h['last_shutdown']!r} after a {off_s} s cut"
                    if h["last_shutdown"] == "power_off":
                        assert h["boot_count"] == 1, f"{side}: power_off but boot_count {h['boot_count']}"
                    else:
                        assert h["boot_count"] == prev[side] + 1 and "BOR" in h["last_rcc_flags"], f"{side}: {h}"
        for off_s, side, reason, boots, flags in seen:
            log.info(f"  {off_s:.1f} s cut: {side} -> {reason}, boot {boots}, rcc {flags}")
        STATE["brownout"] = seen
