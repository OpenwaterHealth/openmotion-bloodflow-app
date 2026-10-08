"""
test_startup_watchdog.py — the startup connection watchdog does not raise a
false E-106 while a sensor is still handshaking, and withdraws its warning
as soon as the devices it warned about connect
(openmotion-bloodflow-app#667, fixed by #711, ships in 1.5.4-dev.4).

Before the fix the one-shot watchdog (``connectionTimeoutSec`` = 12 s after
launch) counted a sensor in SDK state CONNECTING as missing, so launching
the app and then powering on the system produced "Sensor not detected"
(E-106) for a sensor that connected a second later, and the toast stayed
up for its full 10 s because nothing ever dismissed it by tag.

With the fix: a device the warning is about that is CONNECTING at the
deadline defers the check once by 10 s ("still connecting at the deadline
— checking again in 10 s"); the toast tagged ``connection-watchdog`` is
withdrawn ("withdrawing its warning") when the devices it warned about
connect.

The system power is switched with the bench Shelly outlet, which powers
the console and both sensors (step 1 proves it from USB enumeration).
Everything is read from the app log: SDK state transitions, the watchdog
lines and the ``Toast #N [warning] tag='connection-watchdog'`` line.

Steps:
  1. Outlet off: the console and both sensors leave USB.
  2. Late power-on (the withdrawal path): launch the app with the system
     off; the deadline finds nothing and warns ("console and sensors
     missing", toast up); then power on. The warning is withdrawn within
     1.5 s of the first sensor reaching CONNECTED. The power-on → sensor
     CONNECTING delay and the launch → deadline offset are measured here.
  3. ROUNDS (10) power-ons timed so a sensor is mid-handshake at the
     deadline (the QA case). Each round is classified from the log: PASS
     ("still connecting … checking again in 10 s", no E-104/E-106, no
     toast), FAIL (a sensor CONNECTING in the SDK at the deadline but the
     app warned), or out of window (the power-on moment is then corrected
     for the next round). All rounds go to a CSV in test_logs; the step
     fails if any in-window round warned or no round was in window.
  4. Teardown: outlet on, the app relaunched with the system on and the
     console CONNECTED, as the bench was found.

Preconditions
- Console + sensors on the Shelly outlet ($SHELLY_IP_ADDRESS); skips
  without it.
- The app's build is taken from the running process (either variant).

Marked ``release``: power-cycles the system ~12 times, ~8 min. No
scan, no laser.
"""

from __future__ import annotations

import csv
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import psutil
import pytest

import shelly
from conftest import log
from console_bench import close_app
from hil_helpers import wait_for_pattern

pytestmark = [pytest.mark.release, pytest.mark.sdk_direct]

CONSOLE_PID, SENSOR_PID = 0xA53E, 0x5A5A
DEADLINE_S = 12          # connectionTimeoutSec
GRACE_S = 10             # _CONNECTION_WATCHDOG_CONNECTING_GRACE_SEC
TARGET_LEAD_S = 0.6      # aim for the first sensor CONNECTING this long before the deadline
ROUNDS = 10

RE_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})")
RE_WARN = re.compile(r"Connection watchdog: (console and sensors missing|console not detected|sensor not detected)")
RE_DEFER = re.compile(r"Connection watchdog: (.+?) still connecting at the deadline")
RE_WITHDRAW = re.compile(r"Connection watchdog: the devices it warned about have connected")
RE_TOAST = re.compile(r"Toast #\d+ \[warning\] tag='connection-watchdog'")
RE_SENSOR = re.compile(r"openmotion\.sdk\.Sensor - (left|right) state (\w+) -> (\w+)")
RE_CONSOLE = re.compile(r"openmotion\.sdk\.Console - console state (\w+) -> (\w+)")
RE_HANDLE_CONSOLE = re.compile(r"Handle console -> CONNECTED")

STATE: dict = {}
TEST_LOGS = Path(__file__).resolve().parent / "test_logs"


# ─────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────
def _usb_present() -> tuple[int, int]:
    """(consoles, sensors) enumerated on USB, via the SDK's vendored libusb."""
    import usb.backend.libusb1 as lb
    import usb.core
    import omotion
    dll = Path(omotion.__file__).resolve().parent / "_vendor" / "libusb" / "windows" / "x64" / "libusb-1.0.dll"
    be = lb.get_backend(find_library=lambda _: str(dll))
    c = len(list(usb.core.find(find_all=True, backend=be, idProduct=CONSOLE_PID)))
    s = len(list(usb.core.find(find_all=True, backend=be, idProduct=SENSOR_PID)))
    return c, s


def _wait_usb(want_present: bool, timeout: float = 30.0) -> tuple[int, int]:
    """Poll USB until the console and a sensor are both present (or both gone)."""
    deadline = time.time() + timeout
    cs = _usb_present()
    while time.time() < deadline:
        cs = _usb_present()
        if (cs[0] >= 1 and cs[1] >= 1) if want_present else cs == (0, 0):
            return cs
        time.sleep(0.5)
    return cs


def _running_exe() -> str:
    for p in psutil.process_iter(["name", "exe"]):
        if (p.info.get("name") or "").lower() == "open-motion.exe" and p.info.get("exe"):
            return p.info["exe"]
    return ""


def _launch(exe: str, timeout: float = 30.0) -> tuple[Path, datetime]:
    """Start ``exe``; return the new log file and the wall time of its first line."""
    logs_dir = Path(exe).parent / "logs"
    before = set(logs_dir.glob("open-motion-*.log")) if logs_dir.exists() else set()
    subprocess.Popen([exe], cwd=str(Path(exe).parent))
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = set(logs_dir.glob("open-motion-*.log")) - before if logs_dir.exists() else set()
        if new:
            path = max(new, key=lambda p: p.stat().st_mtime)
            lines = _lines(path)
            if lines:
                return path, lines[0][0]
        time.sleep(0.2)
    pytest.fail(f"no new app log appeared in {logs_dir} within {timeout:.0f} s of launching {exe}")


def _lines(path: Path) -> list[tuple[datetime, str]]:
    out = []
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        m = RE_TS.match(raw)
        if m:
            out.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f"), raw))
    return out


def _first(lines, rx, after: datetime | None = None):
    for t, l in lines:
        if (after is None or t >= after) and rx.search(l):
            return t, l
    return None


def _sensor_events(lines, to_state: str):
    return [(t, RE_SENSOR.search(l).group(1)) for t, l in lines
            if RE_SENSOR.search(l) and RE_SENSOR.search(l).group(3) == to_state]


def _wait_log(path: Path, rx, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hit = _first(_lines(path), rx)
        if hit:
            return hit
        time.sleep(0.25)
    return None


def _power_off_and_close(outlet) -> None:
    close_app()
    outlet.off()
    cs = _wait_usb(False, 30)
    assert cs == (0, 0), f"console/sensors still on USB 30 s after the outlet went off: {cs}"
    time.sleep(2)


# ─────────────────────────────────────────────
# fixtures
# ─────────────────────────────────────────────
@pytest.fixture(scope="class")
def outlet(app):
    try:
        out = shelly.default_outlet()
        out.is_on()
    except Exception as e:
        pytest.skip(f"Shelly outlet not reachable: {e}")
    exe = _running_exe()
    if not exe:
        pytest.skip("no running Open-Motion.exe to take the build under test from")
    STATE["exe"] = exe
    log.info(f"  build under test: {exe}")
    try:
        yield out
    finally:
        # Leave the bench as found: system on, the same build running and connected.
        try:
            out.on()
        except Exception as e:
            log.warning(f"  outlet on failed in teardown: {e}")
        close_app()
        _wait_usb(True, 40)
        path, _ = _launch(exe)
        if not wait_for_pattern(RE_HANDLE_CONSOLE, path, 0, 60):
            log.warning("  app relaunched but the console did not reach CONNECTED within 60 s")
        else:
            log.info("  app relaunched with the system on; console CONNECTED")


# ─────────────────────────────────────────────
# tests
# ─────────────────────────────────────────────
@pytest.mark.incremental
@pytest.mark.usefixtures("outlet")
class TestStartupWatchdog:
    """No false E-106 for a handshaking sensor; the warning comes down on connect."""

    def test_01_outlet_powers_console_and_sensors(self, outlet):
        """Outlet off: the console and both sensors leave USB."""
        before = _usb_present()
        log.info(f"  on USB before: {before[0]} console(s), {before[1]} sensor(s)")
        assert before[0] and before[1], f"console/sensors not on USB to begin with: {before}"
        STATE["n_sensors"] = before[1]
        _power_off_and_close(outlet)
        log.info("  outlet off: console and sensors gone from USB")

    def test_02_late_power_on_warns_then_withdraws(self, outlet):
        """Nothing at the deadline: warning + toast; power on: withdrawn within 1.5 s of the first sensor CONNECTED."""
        path, t0 = _launch(STATE["exe"])
        warn = _wait_log(path, RE_WARN, DEADLINE_S + 15)
        assert warn, f"no watchdog warning within {DEADLINE_S + 15} s of launch with the system off"
        assert _wait_log(path, RE_TOAST, 3), "warning logged but no connection-watchdog toast"
        STATE["deadline_after_first_line"] = (warn[0] - t0).total_seconds()
        log.info(f"  deadline {STATE['deadline_after_first_line']:.2f} s after the log's first line: {warn[1][24:140]}")
        t_on = datetime.now()
        assert outlet.on(), "outlet on failed"
        wd = _wait_log(path, RE_WITHDRAW, 45)
        lines = _lines(path)
        connected = [e for e in _sensor_events(lines, "CONNECTED") if e[0] >= t_on]
        connecting = [e for e in _sensor_events(lines, "CONNECTING") if e[0] >= t_on]
        console_on = _first(lines, RE_CONSOLE, after=t_on)
        log.info(f"  after power-on: console {((console_on[0] - t_on).total_seconds() if console_on else None)} s, "
                 f"sensors CONNECTING {[(s, round((t - t_on).total_seconds(), 2)) for t, s in connecting]}, "
                 f"CONNECTED {[(s, round((t - t_on).total_seconds(), 2)) for t, s in connected]}")
        assert connected, "no sensor reached CONNECTED within 45 s of power-on"
        assert connecting, "no sensor CONNECTING transition logged after power-on"
        STATE["on_to_sensor_connecting"] = (connecting[0][0] - t_on).total_seconds()
        assert wd, "the watchdog warning was never withdrawn after the sensors connected"
        lag = (wd[0] - connected[0][0]).total_seconds()
        log.info(f"  warning withdrawn {lag:+.2f} s after the first sensor CONNECTED")
        assert -0.1 <= lag <= 1.5, f"withdrawal came {lag:.2f} s after the first sensor CONNECTED"
        _power_off_and_close(outlet)

    def test_03_power_on_mid_handshake_no_false_e106(self, outlet):
        """ROUNDS power-ons timed for a sensor mid-handshake at the deadline; every in-window round must defer with no E-104/E-106 and no toast."""
        delay = STATE["deadline_after_first_line"] - STATE["on_to_sensor_connecting"] - TARGET_LEAD_S
        rounds = []
        for rnd in range(1, ROUNDS + 1):
            path, t0 = _launch(STATE["exe"])
            t_power = t0 + timedelta(seconds=delay)
            while datetime.now() < t_power:
                time.sleep(0.01)
            t_on = datetime.now()
            assert outlet.on(), "outlet on failed"
            time.sleep(STATE["deadline_after_first_line"] - delay + GRACE_S + 6)
            lines = _lines(path)
            defer = _first(lines, RE_DEFER)
            warn = _first(lines, RE_WARN)
            toast = _first(lines, RE_TOAST)
            withdraw = _first(lines, RE_WITHDRAW)
            connecting = [e for e in _sensor_events(lines, "CONNECTING") if e[0] >= t_on]
            connected = [e for e in _sensor_events(lines, "CONNECTED") if e[0] >= t_on]
            t_deadline = (defer or warn or (t0 + timedelta(seconds=STATE["deadline_after_first_line"]), ""))[0]
            rel = lambda t: round((t - t_deadline).total_seconds(), 2)
            sensor_deferred = bool(defer and re.search(r"(left|right)", RE_DEFER.search(defer[1]).group(1)))
            mid = [s for t, s in connecting
                   if t < t_deadline and not any(s2 == s and t2 <= t_deadline for t2, s2 in connected)]
            if sensor_deferred:
                outcome = "PASS deferred" if not (warn or toast) else "FAIL deferred, then warned"
            elif mid:
                outcome = "FAIL false E-106"
            elif connected and min(t for t, _ in connected) <= t_deadline:
                outcome = "out of window (connected before deadline)"
            else:
                outcome = "out of window (not connecting at deadline)"
            first_c = min((rel(t) for t, _ in connecting), default=None)
            first_C = min((rel(t) for t, _ in connected), default=None)
            row = dict(round=rnd, log=path.name, delay_s=round(delay, 2), outcome=outcome,
                       connecting_s=first_c, connected_s=first_C,
                       warn=bool(warn), toast=bool(toast),
                       withdrawn_s=rel(withdraw[0]) if withdraw else None)
            rounds.append(row)
            log.info(f"  round {rnd}/{ROUNDS}: {outcome}; power-on {delay:.2f} s after launch; first sensor CONNECTING "
                     f"{first_c} s, CONNECTED {first_C} s (vs deadline); warn={row['warn']} toast={row['toast']} "
                     f"withdrawn={row['withdrawn_s']} s; {path.name}")
            if outcome.startswith("out of window"):
                if connecting:
                    delay += (t_deadline - connecting[0][0]).total_seconds() - TARGET_LEAD_S
                else:
                    delay -= 3.0
            _power_off_and_close(outlet)
        STATE["rounds"] = rounds
        TEST_LOGS.mkdir(exist_ok=True)
        out = TEST_LOGS / f"startup_watchdog_rounds_{datetime.now():%Y%m%d_%H%M%S}.csv"
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rounds[0]))
            w.writeheader()
            w.writerows(rounds)
        in_window = [r for r in rounds if not r["outcome"].startswith("out of window")]
        failed = [r for r in in_window if r["outcome"].startswith("FAIL")]
        log.info(f"  summary: {len(rounds)} rounds, {len(in_window)} with a sensor mid-handshake at the deadline, "
                 f"{len(failed)} false warnings; csv -> {out}")
        assert in_window, f"no round put a sensor mid-handshake at the deadline in {ROUNDS} rounds — timing, not the fix"
        assert not failed, (
            f"{len(failed)}/{len(in_window)} in-window rounds raised a false warning: "
            + "; ".join(f"round {r['round']} ({r['log']}): CONNECTING {r['connecting_s']} s, CONNECTED {r['connected_s']} s"
                        for r in failed)
        )
