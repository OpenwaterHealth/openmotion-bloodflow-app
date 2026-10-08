"""
test_console_hwid_stability.py — the console hardware ID must not change
between reads (openmotion-console-fw#53, fixed by console-fw#58, ships in
console firmware 1.8.2-dev.4).

Before the fix the OW_CMD_HWID reply was 16 bytes read from a 12-byte
buffer, so bytes 12-15 carried whatever sat next in memory: the last fan
RPM reading. Every host that derives a device identity from the reply
(SDK ``get_hardware_id``, the apps' Device ID, audit records) saw one
console as several devices, and the ID changed whenever the fan was read.

Procedure (boringethan's bench check on the ticket): sweep the fan duty
0 -> 40 -> 80 -> 100 -> 50 % and, at each step, read ``get_fan_rpm(1..3)``
then ``get_hardware_id()`` right after every RPM read (15 ID reads).
Pass = one unique ID ending in ``00000000``.

Preconditions
- Console connected over USB (sensors optional).
- The SDK talks to the console directly, so the class closes the running
  app at setup and relaunches it at teardown.

Marked ``dev``: ~1 minute, no scan, no laser.
"""

from __future__ import annotations

import time

import pytest

from conftest import log
from console_bench import close_app, console_session, relaunch_app

pytestmark = [pytest.mark.dev, pytest.mark.sdk_direct]

DUTIES = (0, 40, 80, 100, 50)
SETTLE_S = 3.0
FIXED_IN = "1.8.2-dev.4"

STATE: dict = {"reads": []}


@pytest.fixture(scope="class")
def console_free(app):
    """Close the app so the SDK can own the console; relaunch afterwards
    so the bench (and the session-scoped ``app`` fixture's assumptions)
    are restored even if an assertion failed."""
    close_app()
    try:
        yield
    finally:
        relaunch_app()


@pytest.mark.incremental
@pytest.mark.usefixtures("console_free")
class TestConsoleHwidStability:
    """OW_CMD_HWID is stable across reads regardless of fan RPM."""

    def test_01_console_reachable(self):
        """The SDK connects to the console standalone and reports its firmware."""
        with console_session() as c:
            fw = str(c.get_version())
            STATE["fw"] = fw
            STATE["initial"] = c.get_hardware_id()
        log.info(f"  console firmware {fw}; initial hardware id {STATE['initial']}")
        if FIXED_IN not in fw:
            log.warning(f"  firmware is not {FIXED_IN}; the fix ships there — result describes this build")
        assert STATE["initial"], "get_hardware_id() returned nothing"

    def test_02_fan_sweep_reads(self):
        """15 hardware-ID reads, each taken right after a fan RPM read, across five fan duties."""
        reads = []
        with console_session() as c:
            for duty in DUTIES:
                r = c.set_fan_speed(duty)
                assert r == duty, f"set_fan_speed({duty}) returned {r}"
                time.sleep(SETTLE_S)
                for fan in (1, 2, 3):
                    rpm = c.get_fan_rpm(fan)
                    hid = c.get_hardware_id()
                    reads.append((duty, fan, rpm, hid))
                    log.info(f"  duty {duty:>3}%  fan {fan}  rpm {str(rpm):>6}  {hid}")
            c.set_fan_speed(50)
        STATE["reads"] = reads
        assert len(reads) == 15

    def test_03_single_id_zero_padded(self):
        """All 15 reads are one ID, 32 hex chars, and bytes 12-15 are 00000000."""
        ids = {hid for _, _, _, hid in STATE["reads"]}
        log.info(f"  unique ids: {sorted(ids)}")
        assert len(ids) == 1, (
            f"hardware id varied across the fan sweep: {sorted(ids)} — the fan RPM is "
            f"leaking into bytes 12-15 (console-fw#53 not fixed in firmware {STATE.get('fw')})"
        )
        hid = ids.pop()
        assert hid and len(hid) == 32, f"unexpected id length: {hid!r}"
        assert hid.endswith("00000000"), f"padding is not zero: {hid}"
        assert hid == STATE["initial"], "id differs from the pre-sweep read"
