"""
test_sensor_fw_release_strings.py — sensor-fw#139: the production (Release)
sensor image carries no debug strings and still scans.

What it checks
--------------
  1. The Release image on disk (the GitHub release asset, SHA-256 pinned)
     holds no printf format strings and no log text; only the IMU JSON
     format, the version string and the USB descriptors remain. The
     previous release image (1.8.2) is measured the same way when it is
     on disk, for the before/after numbers.
  2. Both sensors are up on the Release firmware and answer with its
     version string.
  3. A normal 60 s laser scan through the SDK (ScanWorkflow, console
     trigger, all 16 cameras) with DEBUG_FLAG_USB_PRINTF set on both
     sensors: the scan completes, every camera delivers its frames, and
     the host receives zero MCU printf lines (on the previous firmware the
     same flag floods the log with "[LEFT-COMM PRINTF] …").
  4. The app is relaunched and runs a 2-minute scan on the same sensors:
     "Full scan started" … "Full scan ended", no CRITICAL E-xxx, streaming
     counts on both sides, and again no printf line in the app log.

Preconditions
-------------
- Console + both sensors on USB, phantom in place (the laser fires).
- Sensors flashed with a Release build (an rc or final tag). A -dev tag
  is a Debug build and keeps the strings, so step 1 would fail on it.
- ``DFU_IMAGE`` points at the release asset that is on the sensors.

Marked ``release``: two laser scans, ~6 min end to end.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import defaultdict
from pathlib import Path

import pytest

from conftest import log
from console_bench import close_app, relaunch_app, sensors_session
from hil_helpers import click_panel, find_app_log, log_size, uia_window, wait_for_pattern, click_element_center
from conftest import SLEEP

pytestmark = [pytest.mark.release, pytest.mark.sdk_direct]

SIDES = ("left", "right")
MASK = 0xFF
SCAN_S = 60
APP_SCAN_S = 120
CONFIGURE_TIMEOUT_S = 90.0
APP_SENSOR_FLAGS = 0xC0          # what the app pushes: HISTO_CMP | SEND_DEFER
FRAME_RATE_HZ = 40.0
MIN_FRAME_FRACTION = 0.90        # every camera must deliver ≥ 90 % of 40 Hz × SCAN_S

RELEASE_IMAGE = Path(r"C:\Users\vpenn\Downloads\motion-sensor-fw-baremetal.bin")
RELEASE_IMAGE_SHA256 = "4a27014a2840bbe4030a06f4c3cc8893d4d7e2ce8ed830b184c16b3f97820c99"
RELEASE_VERSION = "1.8.3-rc.1"
PREVIOUS_IMAGE = Path(r"C:\Users\vpenn\Downloads\motion-sensor-fw-signed.bin")   # 1.8.2, for the before/after

# Strings a Release image is allowed to keep (sensor-fw#139 PR #140).
ALLOWED_FORMATS = {'{"F":%lu,"G":[%d,%d,%d],"M":[%d,%d,%d],"A":[%d,%d,%d],"T":%d.%02d}'}
ALLOWED_WORDS = {"Openwater", "COMMS_HISTO_IMU(HS)", "COMMS_HISTO_IMU Config", "COMMS_HISTO_IMU Interface"}
RE_FORMAT = re.compile(r"%[-0-9.l]*[dusxXcpf]")
RE_LOG_WORD = re.compile(r"error|fail|histo|camera|fsin|init|reset|debug|crosslink|i2c|uart|flush|warn|timeout|overrun|boot", re.I)
RE_PRINTF = re.compile(r"\[(LEFT|RIGHT)-COMM PRINTF\]")
RE_SCAN_STARTED = re.compile(r"Full scan started")
RE_SCAN_ENDED = re.compile(r"Full scan ended: (\w+) — duration (\d\d:\d\d:\d\d)")
RE_CRITICAL = re.compile(r"CRITICAL E-\d+")
RE_STREAMING = re.compile(r"(LEFT|RIGHT)-HISTO: Streaming stopped — (\d+) USB read chunk")

STATE: dict = {}


# --------------------------------------------------------------------------
# 1. the image on disk
# --------------------------------------------------------------------------

def _strings(path: Path, min_len: int = 6) -> list[str]:
    data = path.read_bytes()
    return [m.group().decode() for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data)]


def _classify(strs: list[str]) -> dict:
    fmt = [s for s in strs if RE_FORMAT.search(s)]
    words = [s for s in strs if RE_LOG_WORD.search(s)]
    version = sorted({m.group() for s in strs for m in re.finditer(r"\d+\.\d+\.\d+(?:-(?:dev|rc)\.\d+)?", s)})
    return {"runs": len(strs), "formats": fmt, "log_words": words, "versions": version}


@pytest.mark.incremental
class TestReleaseImageStrings:

    def test_01_release_image_has_no_debug_strings(self):
        assert RELEASE_IMAGE.is_file(), f"release image missing: {RELEASE_IMAGE}"
        sha = hashlib.sha256(RELEASE_IMAGE.read_bytes()).hexdigest()
        assert sha == RELEASE_IMAGE_SHA256, f"{RELEASE_IMAGE.name} sha256 {sha} is not the pinned release asset"
        rel = _classify(_strings(RELEASE_IMAGE))
        log.info(f"  {RELEASE_IMAGE.name}: {RELEASE_IMAGE.stat().st_size} bytes, {rel['runs']} printable runs, "
                 f"{len(rel['formats'])} format string(s), {len(rel['log_words'])} log-word string(s), "
                 f"versions {rel['versions']}")
        for s in rel["formats"]:
            log.info(f"    format: {s!r}")
        for s in rel["log_words"]:
            log.info(f"    text  : {s!r}")
        STATE["release_strings"] = rel
        if PREVIOUS_IMAGE.is_file():
            prev = _classify(_strings(PREVIOUS_IMAGE))
            log.info(f"  {PREVIOUS_IMAGE.name} (previous release {prev['versions']}): {prev['runs']} runs, "
                     f"{len(prev['formats'])} format string(s), {len(prev['log_words'])} log-word string(s); "
                     f"e.g. {prev['formats'][:3]}")
            STATE["previous_strings"] = prev
        assert RELEASE_VERSION in rel["versions"], f"version string {RELEASE_VERSION} not in the image: {rel['versions']}"
        unexpected_fmt = [s for s in rel["formats"] if s not in ALLOWED_FORMATS]
        assert not unexpected_fmt, f"printf format strings still in the Release image: {unexpected_fmt}"
        unexpected_txt = [s for s in rel["log_words"] if s not in ALLOWED_WORDS]
        assert not unexpected_txt, f"log text still in the Release image: {unexpected_txt}"


# --------------------------------------------------------------------------
# 2–3. the sensors: version, then a laser scan with USB printf on
# --------------------------------------------------------------------------

class _FrameSink:
    """Counts frames per (side, camera) from the raw tee."""

    channels = {"raw"}

    def __init__(self) -> None:
        self.frames: dict[tuple[int, int], int] = defaultdict(int)
        self.scan_id = ""

    def on_scan_start(self, meta) -> None:
        self.scan_id = getattr(meta, "scan_id", "")

    def consume(self, channel: str, payload) -> None:
        batch = payload
        n = int(batch.frame_ids.shape[0])
        for i in range(n):
            side = int(batch.side_ids[i]) if batch.side_ids is not None else 0
            self.frames[(side, int(batch.cam_ids[i]))] += 1

    def on_complete(self) -> None:
        pass


class _PrintfCatcher(logging.Handler):
    """Keeps every SDK log record that carries MCU printf output."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []
        self.total = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.total += 1
        try:
            msg = record.getMessage()
        except Exception:
            return
        if "PRINTF]" in msg:
            self.lines.append(msg)


def _configure_cameras(iface, sides: tuple) -> None:
    from omotion.ScanWorkflow import ConfigureRequest

    for side in sides:
        assert getattr(iface, side).enable_camera_power(MASK) is not False, f"{side}: enable_camera_power failed"
    done = threading.Event()
    result: dict = {}

    def _on_complete(r):
        result["r"] = r
        done.set()

    req = ConfigureRequest(
        left_camera_mask=MASK if "left" in sides else 0,
        right_camera_mask=MASK if "right" in sides else 0,
        power_off_unused_cameras=False,
    )
    assert iface.start_configure_camera_sensors(req, on_complete_fn=_on_complete), "configure request refused"
    assert done.wait(CONFIGURE_TIMEOUT_S), f"camera configure did not finish within {CONFIGURE_TIMEOUT_S:.0f} s"
    r = result.get("r")
    ok = getattr(r, "ok", True) if r is not None else True
    assert ok, f"camera configure failed: {getattr(r, 'error', r)}"


@pytest.fixture(scope="class")
def console_free(app):
    """Close the app so the SDK can own the hardware; relaunch afterwards."""
    close_app()
    try:
        yield
    finally:
        relaunch_app()


@pytest.mark.incremental
@pytest.mark.usefixtures("console_free")
class TestReleaseFirmwareScans:

    def test_02_sensors_report_the_release_version(self):
        with sensors_session(SIDES) as bench:
            assert set(bench.sensors) == set(SIDES), f"not every sensor connected: {list(bench.sensors)}"
            for side, s in bench.sensors.items():
                v = s.get_version()
                flags = s.get_debug_flags()
                log.info(f"  {side} ({s.read_serial_number()}): firmware {v}, debug flags 0x{flags:X}")
                assert v == RELEASE_VERSION, f"{side} is on {v}, not the Release image {RELEASE_VERSION}"

    def test_03_laser_scan_with_usb_printf_on_prints_nothing(self):
        from omotion.ScanWorkflow import ScanRequest
        from omotion.config import DEBUG_FLAG_USB_PRINTF

        catcher = _PrintfCatcher()
        logging.getLogger("openmotion").addHandler(catcher)
        logging.getLogger("omotion").addHandler(catcher)
        try:
            with sensors_session(SIDES) as bench:
                iface, sensors = bench.iface, bench.sensors
                _configure_cameras(iface, SIDES)
                iface.apply_laser_power()
                for side, s in sensors.items():
                    assert s.set_debug_flags(APP_SENSOR_FLAGS | DEBUG_FLAG_USB_PRINTF), f"{side}: set_debug_flags failed"
                    rb = s.get_debug_flags()
                    log.info(f"  {side}: debug flags now 0x{rb:X} (USB printf bit {'set' if rb & DEBUG_FLAG_USB_PRINTF else 'CLEAR'})")
                    assert rb & DEBUG_FLAG_USB_PRINTF, f"{side}: USB printf bit did not stick (0x{rb:X})"
                time.sleep(2.0)     # on old firmware the flag change alone prints a line
                sink = _FrameSink()
                req = ScanRequest(
                    subject_id="FW139",
                    duration_sec=SCAN_S,
                    left_camera_mask=MASK,
                    right_camera_mask=MASK,
                    disable_laser=False,
                    write_corrected_csv=False,
                    write_telemetry_csv=False,
                    sinks=[sink],
                    skip_default_storage=True,
                    raw_save_max_duration_s=None,
                )
                sw = iface.scan_workflow
                assert iface.start_scan(req), f"start_scan refused: {sw.last_scan_error}"
                t0 = time.monotonic()
                try:
                    while sw.running and time.monotonic() - t0 < SCAN_S + 60:
                        sw.await_complete(timeout_sec=1.0)
                    elapsed = time.monotonic() - t0
                    facts = dict(
                        running=bool(sw.running), error=sw.last_scan_error,
                        canceled=bool(getattr(sw, "last_scan_canceled", False)), elapsed_s=round(elapsed, 1),
                        packets={side: int(getattr(s.uart.histo, "packets_received", 0)) for side, s in sensors.items()},
                    )
                finally:
                    if sw.running:
                        iface.cancel_scan()
                    for s in sensors.values():
                        try:
                            s.set_debug_flags(APP_SENSOR_FLAGS)
                        except Exception:
                            pass
                time.sleep(1.0)
        finally:
            logging.getLogger("openmotion").removeHandler(catcher)
            logging.getLogger("omotion").removeHandler(catcher)

        log.info(f"  scan: {facts}")
        expected = FRAME_RATE_HZ * SCAN_S
        per_side: dict[int, list[str]] = defaultdict(list)
        short = []
        for side_idx, side in enumerate(SIDES):
            for cam in range(8):
                n = sink.frames.get((side_idx, cam), 0)
                per_side[side_idx].append(f"cam{cam + 1}={n}")
                if n < MIN_FRAME_FRACTION * expected:
                    short.append(f"{side} cam{cam + 1}: {n} frames (< {MIN_FRAME_FRACTION:.0%} of {expected:.0f})")
            log.info(f"  {side} frames: {' '.join(per_side[side_idx])}")
        log.info(f"  SDK log records during the scan: {catcher.total}; MCU printf lines: {len(catcher.lines)}")
        for line in catcher.lines[:10]:
            log.info(f"    printf: {line}")
        STATE["sdk_scan"] = dict(facts=facts, frames={f"{SIDES[k[0]]}-cam{k[1] + 1}": v for k, v in sorted(sink.frames.items())},
                                 printf_lines=len(catcher.lines), log_records=catcher.total)
        assert not facts["running"], "scan still running after its duration"
        assert not facts["canceled"], "scan was canceled"
        assert not facts["error"], f"scan error: {facts['error']}"
        assert not short, "cameras short of frames: " + "; ".join(short)
        assert not catcher.lines, f"{len(catcher.lines)} MCU printf line(s) reached the host, e.g. {catcher.lines[:3]}"


# --------------------------------------------------------------------------
# 4. the app scans on the same sensors
# --------------------------------------------------------------------------

def _click_button(name: str, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            buttons = list(uia_window().descendants(control_type="Button"))
        except Exception:
            buttons = []
        for b in buttons:
            if (b.window_text() or "").strip() == name:
                click_element_center(b, name)
                time.sleep(SLEEP)
                return name
        time.sleep(0.5)
    return ""


@pytest.mark.incremental
class TestReleaseFirmwareAppScan:

    def test_04_app_connects_to_both_sensors(self, app):
        log_path = find_app_log()
        assert log_path, "no app log found"
        STATE["log"] = log_path
        text = log_path.read_text(encoding="utf-8", errors="replace")
        fw = sorted(set(re.findall(r"firmware = (\S+)", text)))
        log.info(f"  app log {log_path.name}: sensor firmware {fw}")
        assert fw == [RELEASE_VERSION], f"app sees firmware {fw}, expected only {RELEASE_VERSION}"
        assert "Handle left -> CONNECTED" in text and "Handle right -> CONNECTED" in text, "both sensors must be CONNECTED"

    def test_05_app_scan_runs_two_minutes(self, app):
        log_path = STATE["log"]
        off = log_size(log_path)
        click_panel("Start")
        deadline, gate = time.time() + 240, False
        line = None
        while time.time() < deadline and not line:
            line = wait_for_pattern(RE_SCAN_STARTED, log_path, off, 2)
            if not line and not gate and _click_button("Start Scan", timeout=0.5):
                log.info("  pre-scan gate: clicked 'Start Scan'")
                gate = True
        assert line, "no 'Full scan started' line within 240 s of clicking Start"
        log.info(f"  {line.strip()[:160]}")
        t0 = time.time()
        while time.time() - t0 < APP_SCAN_S:
            time.sleep(5)
            text = log_path.read_text(encoding="utf-8", errors="replace")[off:]
            if RE_SCAN_ENDED.search(text):
                break
        text = log_path.read_text(encoding="utf-8", errors="replace")[off:]
        if not RE_SCAN_ENDED.search(text):
            click_panel("Start")        # Start toggles to Stop while scanning
            ended = wait_for_pattern(RE_SCAN_ENDED, log_path, off, 60)
            assert ended, "no 'Full scan ended' line within 60 s of clicking Stop"
            time.sleep(3)
            pyautogui_escape()
        text = log_path.read_text(encoding="utf-8", errors="replace")[off:]
        m = RE_SCAN_ENDED.search(text)
        crit = RE_CRITICAL.findall(text)
        streams = RE_STREAMING.findall(text)
        printf = RE_PRINTF.findall(text)
        log.info(f"  Full scan ended: {m.group(1)} — duration {m.group(2)}; critical={crit}; "
                 f"streaming={streams}; printf lines={len(printf)}")
        STATE["app_scan"] = dict(how=m.group(1), duration=m.group(2), critical=crit, streaming=streams, printf=len(printf))
        h, mm, ss = (int(x) for x in m.group(2).split(":"))
        ran_s = h * 3600 + mm * 60 + ss
        assert m.group(1) == "completed" or ran_s >= APP_SCAN_S - 5,             f"app scan {m.group(1)} after {ran_s} s — it must run the full {APP_SCAN_S} s"
        assert not crit, f"critical errors during the app scan: {crit}"
        assert {s[0] for s in streams} == {"LEFT", "RIGHT"}, f"streaming stopped lines: {streams}"
        assert all(int(s[1]) > 0 for s in streams), f"a side received no USB data: {streams}"
        assert not printf, f"{len(printf)} MCU printf line(s) in the app log"


def pyautogui_escape() -> None:
    import pyautogui
    from conftest import require_focus
    require_focus()
    pyautogui.press("escape")
    time.sleep(SLEEP)
