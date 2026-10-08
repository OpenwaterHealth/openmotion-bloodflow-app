"""
console_bench.py — plain helpers for bench tests that talk to the console
directly through the SDK (no app in the loop).

The SDK can only open the console while the Open-Motion app is closed, so
these tests close the app at class setup and relaunch it at teardown via
``console_free``. Everything here is a plain function or context manager
(same role as ``shelly.py``) — no fixtures, no pytest state.
"""

from __future__ import annotations

import contextlib
import subprocess
import time
from typing import Iterator

import psutil

from conftest import (
    _APP_PROCESS_NAMES,
    _find_exe,
    _from_source_mode,
    PROJECT_ROOT,
    ensure_visible,
    log,
)
from hil_helpers import RE_CONNECTED, find_app_log, log_size, wait_for_pattern

# Console status LED states as OW_CTRL_GET_IND reports them
# (console-fw led_driver.h: LED_NONE=0, LED_RED=1, LED_GREEN=2, LED_BLUE=3).
LED_OFF, LED_RED, LED_GREEN, LED_BLUE = 0, 1, 2, 3
LED_NAME = {LED_OFF: "OFF", LED_RED: "RED", LED_GREEN: "GREEN", LED_BLUE: "BLUE"}

APP_CONNECT_TIMEOUT = 90


def led_name(state) -> str:
    return LED_NAME.get(state, f"?{state}")


def close_app(timeout: float = 15.0) -> int:
    """Terminate every running Open-Motion process. Returns the count."""
    killed = 0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if name in ("python.exe", "pythonw.exe"):
                if "main.py" not in cmdline or "openmotion-bloodflow-app" not in cmdline:
                    continue
            elif name not in _APP_PROCESS_NAMES:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except psutil.TimeoutExpired:
                proc.kill()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if killed:
        log.info(f"  closed {killed} Open-Motion process(es)")
        time.sleep(3)   # let the USB handles release before the SDK opens them
    return killed


def relaunch_app(connect_timeout: int = APP_CONNECT_TIMEOUT) -> None:
    """Start the app the way the ``app`` fixture would, then wait for its
    window and for the console to log CONNECTED so the next class finds
    a ready bench."""
    log_path = find_app_log()
    start_off = log_size(log_path) if log_path else 0
    if _from_source_mode():
        import sys
        subprocess.Popen([sys.executable, str(PROJECT_ROOT / "main.py")], cwd=str(PROJECT_ROOT))
    else:
        exe = _find_exe()
        if not exe:
            raise RuntimeError("Open-Motion.exe not found and OPENWATER_EXE unset")
        log.info(f"  relaunching {exe}")
        subprocess.Popen([exe])
    deadline = time.time() + 30
    while time.time() < deadline and not ensure_visible():
        time.sleep(1)
    # The launch rotates the log; tail whichever file is newest now.
    deadline = time.time() + connect_timeout
    while time.time() < deadline:
        lp = find_app_log()
        if lp and lp != log_path:
            start_off = 0
            log_path = lp
        if log_path and wait_for_pattern(RE_CONNECTED, log_path, start_off, 5):
            log.info("  app relaunched and console CONNECTED")
            return
    log.warning("  app relaunched but no CONNECTED line seen — bench may need attention")


@contextlib.contextmanager
def console_session(connect_timeout: float = 25.0) -> Iterator[object]:
    """Yield a CONNECTED ``MotionConsole`` handle from a standalone
    ``MotionInterface``; always stops the interface afterwards."""
    from omotion.MotionInterface import MotionInterface
    from omotion.connection_state import ConnectionState

    mi = MotionInterface()
    mi.start(wait=True, wait_timeout=15.0)
    try:
        c = mi.console
        if not c.wait_for(ConnectionState.CONNECTED, timeout=connect_timeout):
            raise RuntimeError(
                "console did not reach CONNECTED — is the Open-Motion app still holding the port?"
            )
        time.sleep(1.0)   # first telemetry snapshot
        yield c
    finally:
        try:
            mi.stop()
        except Exception as e:   # pragma: no cover
            log.warning(f"  MotionInterface.stop raised: {e}")


@contextlib.contextmanager
def sensor_session(side: str = "left", connect_timeout: float = 25.0) -> Iterator[object]:
    """Yield a CONNECTED ``MotionSensor`` handle (``"left"`` or ``"right"``)
    from a standalone ``MotionInterface``; always stops the interface
    afterwards. Same contract as ``console_session``: the app must be
    closed (``console_free``) or the USB interfaces are busy."""
    from omotion.MotionInterface import MotionInterface
    from omotion.connection_state import ConnectionState

    mi = MotionInterface()
    mi.start(wait=True, wait_timeout=15.0)
    try:
        s = getattr(mi, side)
        if not s.wait_for(ConnectionState.CONNECTED, timeout=connect_timeout):
            raise RuntimeError(
                f"{side} sensor did not reach CONNECTED — is the Open-Motion app still holding the USB interfaces?"
            )
        yield s
    finally:
        try:
            mi.stop()
        except Exception as e:   # pragma: no cover
            log.warning(f"  MotionInterface.stop raised: {e}")


@contextlib.contextmanager
def sensors_session(sides=("left", "right"), connect_timeout: float = 25.0) -> Iterator[object]:
    """Yield a namespace with ``iface`` (the standalone ``MotionInterface``)
    and ``sensors`` (``{side: MotionSensor}`` for every requested side that
    reached CONNECTED; at least one, else RuntimeError). The console is
    waited for too so scans can run. Always stops the interface afterwards."""
    from types import SimpleNamespace
    from omotion.MotionInterface import MotionInterface
    from omotion.connection_state import ConnectionState

    mi = MotionInterface()
    mi.start(wait=True, wait_timeout=15.0)
    try:
        found = {}
        deadline = time.time() + connect_timeout
        mi.console.wait_for(ConnectionState.CONNECTED, timeout=connect_timeout)
        for side in sides:
            s = getattr(mi, side)
            if s.wait_for(ConnectionState.CONNECTED, timeout=max(1.0, deadline - time.time())):
                found[side] = s
        if not found:
            raise RuntimeError(
                "no sensor reached CONNECTED — is the Open-Motion app still holding the USB interfaces?"
            )
        yield SimpleNamespace(iface=mi, sensors=found)
    finally:
        try:
            mi.stop()
        except Exception as e:   # pragma: no cover
            log.warning(f"  MotionInterface.stop raised: {e}")

def safety_status(console) -> tuple[int, int]:
    """(safety_se, safety_so) raw fault bytes from the latest telemetry."""
    snap = console.telemetry.get_snapshot()
    return (snap.safety_se, snap.safety_so) if snap else (0, 0)


def sample_led(console, seconds: float, gap: float = 0.06) -> list[tuple[float, int]]:
    """[(t, state)] of OW_CTRL_GET_IND reads over ``seconds``."""
    out = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        out.append((time.monotonic() - t0, console.get_rgb_led()))
        time.sleep(gap)
    return out


def led_summary(samples: list[tuple[float, int]]) -> tuple[set[int], list[float]]:
    """(states seen, half-periods between transitions in seconds)."""
    states = {s for _, s in samples}
    edges = [t for (t, s), (_, p) in zip(samples[1:], samples[:-1]) if s != p]
    halves = [b - a for a, b in zip(edges[:-1], edges[1:])]
    return states, halves
