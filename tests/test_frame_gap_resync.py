"""
test_frame_gap_resync.py — after a module-wide frame gap longer than 8
frames the frame classifier re-anchors from the device clock instead of
quarantining a whole 8-bit wrap (openmotion-sdk#286, fixed by sdk#294,
on SDK ``next`` since 2026-09-24; dev-tag app builds from 1.5.4-dev.2
install the SDK from ``next``).

Before the fix a whole-module skip of more than 8 frames (both field
cases: 17 and 9 frames at t≈46 s, both modules at the same instant) had
no per-camera outage evidence, so re-anchoring was never allowed: every
later frame was measured against the stale counter, quarantined as
``gap_too_large`` then ``non_monotonic`` until the wire id wrapped back
round — a fixed 256-frame (6.4 s) hole — and the side's absolute ids
stayed 256 low for the rest of the scan, so every scheduled dark after
that landed on a light frame.

How the gap is made on real hardware: the sensor firmware's histogram-
stall repro (sensor-fw#75, ``DEBUG_FLAG_HISTO_STALL``) streams normally
for 1800 frames of a scan and then silently withholds histogram packets
while the cameras, FSIN and the frame counter keep running. Clearing the
stall bit over the command link lets packets flow again, so "armed →
stall → clear after GAP_S" produces one module-wide gap of ≈ GAP_S × 40
frames with no packets at all during it — the exact field signature.

The scan itself is the SDK's real scan path (``MotionInterface.
start_scan``: the app's camera configure, console trigger, laser on,
compressed packets, the full pipeline), with every attached sensor in
the scan. A custom sink on the ``raw`` channel records the wire rows
(camera, frame id, timestamp, packet) before the classifier, and one on
``diagnostics`` records the real pipeline's events (quarantines, dark-
integrity warnings), so step 4 reads the shipping classifier's verdict
and step 5 replays the same wire rows through the pre-fix classifier.

Steps:
  1. The SDK connects to the console and every attached sensor
     standalone; firmware read; the stall flag is accepted on each.
  2. 60 s scan with the stall armed on every side (app flags + stall
     bit). A side has stalled once its USB packet counter has been still
     for 0.25 s after ≥ 40 s of scan; GAP_S after the last side went
     quiet every stall bit is cleared (app flags restored) and the scan
     runs to completion. Wire rows are saved per side as CSV.
  3. Each side holds one gap on every streaming camera, > 8 and < 128
     frames, at one packet boundary (module-wide, counter and clock
     agree on its length); with two sides, both stalled within 0.5 s.
  4. The real pipeline re-anchored on every side: after the gap at most
     2 FrameQuarantined per camera, none ``non_monotonic`` or
     ``counter_clock_mismatch``, zero DarkIntegrityWarning; replaying the
     rows through the installed classifier agrees and every accepted abs
     id after the gap matches the device clock (no 256-low offset).
  5. Contrast (informational, skipped when the SDK git history is not at
     hand): the same rows through the pre-#294 classifier lock out ≈ 256
     frames per camera and end 256 low — the bug, on this capture.

Preconditions
- Console and one or two sensors on USB, phantom in place (the laser
  runs as in any scan).
- The SDK talks to the hardware directly: the class closes the app at
  setup and relaunches it at teardown (``sdk_direct``).

Marked ``release``: a 60 s laser scan, ~2.5 min end to end.
"""

from __future__ import annotations

import csv
import importlib.util
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from conftest import log
from console_bench import close_app, relaunch_app, sensors_session

pytestmark = [pytest.mark.release, pytest.mark.sdk_direct]

SIDES = ("left", "right")
SIDE_INDEX = {"left": 0, "right": 1}
MASK = 0xFF
SCAN_S = 60
GAP_S = 0.40            # ≈ 16 frames at 40 Hz, like the field cases (17 and 9)
STALL_MIN_S = 40.0      # the firmware trips at frame 1800 ≈ 45 s into the scan
STALL_QUIET_S = 0.25    # 10 missed frames = the stall, not a hiccup
STALL_TIMEOUT_S = 75.0
CONFIGURE_TIMEOUT_S = 90.0
PERIOD_S = 0.025
APP_SENSOR_FLAGS = 0xC0     # what the app pushes: HISTO_CMP | SEND_DEFER
PREFIX_COMMIT = "4d4159c"   # SDK next right before the #294 merge (fb3dc13)
TEST_LOGS = Path(__file__).resolve().parent / "test_logs"

STATE: dict = {}


@pytest.fixture(scope="class")
def console_free(app):
    """Close the app so the SDK can own the hardware; relaunch afterwards."""
    close_app()
    try:
        yield
    finally:
        relaunch_app()


# --------------------------------------------------------------------------
# capture: a real scan with the stall injected
# --------------------------------------------------------------------------

class _WireSink:
    """Records wire rows from the ``raw`` tee (before the classifier) and
    the real pipeline's events from ``diagnostics``."""

    channels = {"raw", "diagnostics"}

    def __init__(self) -> None:
        self.rows: dict[int, list[tuple[int, int, float, int]]] = defaultdict(list)
        self.events: list = []
        self.scan_id = ""
        self._next_packet: dict[int, int] = defaultdict(int)
        self._last_key: dict[int, tuple] = {}

    def on_scan_start(self, meta) -> None:
        self.scan_id = getattr(meta, "scan_id", "")

    def consume(self, channel: str, payload) -> None:
        if channel == "diagnostics":
            self.events.append(payload)
            return
        batch = payload
        n = int(batch.frame_ids.shape[0])
        for i in range(n):
            side = int(batch.side_ids[i]) if batch.side_ids is not None else 0
            if batch.packet_ids is not None:
                pid = int(batch.packet_ids[i])
            else:
                key = (float(batch.timestamp_s[i]),)
                if self._last_key.get(side) != key:
                    self._last_key[side] = key
                    self._next_packet[side] += 1
                pid = self._next_packet[side]
            self.rows[side].append(
                (int(batch.cam_ids[i]), int(batch.frame_ids[i]), float(batch.timestamp_s[i]), pid)
            )

    def on_complete(self) -> None:
        pass


def _configure_cameras(iface, sides: tuple) -> None:
    """The app's camera bring-up: power on, then the SDK configure request."""
    from omotion.ScanWorkflow import ConfigureRequest

    for side in sides:
        assert getattr(iface, side).enable_camera_power(MASK) is not False, f"{side}: enable_camera_power failed"
    done = threading.Event()
    result = {}

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


def _scan_with_gap(iface, sensors: dict) -> tuple[_WireSink, dict]:
    """Run one SCAN_S scan with the stall armed on every side; clear the
    stall bits GAP_S after the last side went quiet; return the sink and
    the per-side timing facts."""
    from omotion.ScanWorkflow import ScanRequest
    from omotion.config import DEBUG_FLAG_HISTO_STALL

    sides = tuple(sensors)
    _configure_cameras(iface, sides)
    iface.apply_laser_power()
    for side, s in sensors.items():
        assert s.set_debug_flags(APP_SENSOR_FLAGS | DEBUG_FLAG_HISTO_STALL), f"{side}: arming the stall flag failed"

    sink = _WireSink()
    facts: dict = {side: {} for side in sides}
    req = ScanRequest(
        subject_id="GAP286",
        duration_sec=SCAN_S,
        left_camera_mask=MASK if "left" in sides else 0,
        right_camera_mask=MASK if "right" in sides else 0,
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
    counts = {side: (0, t0) for side in sides}        # (packets_received, last change)

    def _poll():
        now = time.monotonic()
        for side, s in sensors.items():
            n = int(getattr(s.uart.histo, "packets_received", 0))
            if n != counts[side][0]:
                counts[side] = (n, now)

    def _stalled(side) -> bool:
        n, last = counts[side]
        now = time.monotonic()
        return n > 0 and now - t0 >= STALL_MIN_S and now - last >= STALL_QUIET_S

    try:
        deadline = t0 + STALL_TIMEOUT_S
        while time.monotonic() < deadline and sw.running and not all(_stalled(s) for s in sides):
            _poll()
            time.sleep(0.01)
        for side in sides:
            facts[side]["stalled"] = _stalled(side)
            facts[side]["packets_at_stall"] = counts[side][0]
            facts[side]["stall_at_s"] = counts[side][1] - t0
        if all(f["stalled"] for f in facts.values()):
            latest_quiet = max(counts[s][1] for s in sides)
            while time.monotonic() - latest_quiet < GAP_S:
                time.sleep(0.002)
            for side, s in sensors.items():
                t_clear = time.monotonic()
                facts[side]["clear_ok"] = bool(s.set_debug_flags(APP_SENSOR_FLAGS))
                facts[side]["clear_latency_s"] = time.monotonic() - t_clear
                facts[side]["gap_commanded_s"] = time.monotonic() - counts[side][1]
        while sw.running and time.monotonic() - t0 < SCAN_S + 30:
            sw.await_complete(timeout_sec=1.0)
        for side, s in sensors.items():
            facts[side]["packets_total"] = int(getattr(s.uart.histo, "packets_received", 0))
        facts["scan"] = dict(
            running=bool(sw.running), error=sw.last_scan_error,
            canceled=bool(getattr(sw, "last_scan_canceled", False)),
            label=getattr(sw, "current_scan_label", ""), elapsed_s=time.monotonic() - t0,
        )
    finally:
        if sw.running:
            try:
                iface.cancel_scan()
            except Exception as e:
                log.warning(f"  cancel_scan raised: {e}")
        for s in sensors.values():
            try:
                s.set_debug_flags(APP_SENSOR_FLAGS)
            except Exception:
                pass
    return sink, facts


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def _per_camera(rows):
    by_cam: dict[int, list] = defaultdict(list)
    for cam, fid, ts, pid in rows:
        by_cam[cam].append((fid, ts, pid))
    return by_cam


def _largest_gap(seq):
    """(packet_id_before, step_frames, dt_s, ts_before) of the largest forward wire step."""
    best = (None, 0, 0.0, 0.0)
    for (f0, t0, p0), (f1, t1, _) in zip(seq, seq[1:]):
        step = (f1 - f0) & 0xFF
        if step > best[1]:
            best = (p0, step, t1 - t0, t0)
    return best


def _run_stages(rows, side_idx: int, classify_cls, repair_cls):
    """Feed rows through classify + repair in capture-aligned batches
    (a packet's rows never straddle a batch, like LiveUsbSource) and
    return (per-row results, events). Each result is
    (cam, raw, ts, packet, abs_id, frame_type) — the classifier's verdict,
    snapshotted before the repair stage touches the batch."""
    from omotion.pipeline.batch import FrameBatch

    classify = classify_cls()
    repair = repair_cls()
    events, results = [], []
    batches, current, current_packet, captures = [], [], None, 0
    for row in rows:
        if row[3] != current_packet:
            if captures >= 10 and current:
                batches.append(current)
                current, captures = [], 0
            current_packet = row[3]
            captures += 1
        current.append(row)
    if current:
        batches.append(current)
    batch = None
    for chunk in batches:
        n = len(chunk)
        batch = FrameBatch(
            cam_ids=np.array([r[0] for r in chunk], dtype=np.int8),
            frame_ids=np.array([r[1] for r in chunk], dtype=np.uint8),
            packet_ids=np.array([r[3] for r in chunk], dtype=np.int64),
            side_ids=np.full(n, side_idx, dtype=np.int8),
            raw_histograms=np.zeros((n, 2, 8, 1024), dtype=np.uint32),
            temperature_c=np.zeros((n, 2, 8), dtype=np.float32),
            timestamp_s=np.array([r[2] for r in chunk], dtype=np.float64),
            pdc=None, tcm=None, tcl=None,
        )
        batch = classify.process(batch)
        abs_ids = [int(v) for v in batch.abs_frame_ids]
        types = [str(v) for v in batch.frame_type]
        batch = repair.process(batch)
        events.extend(batch.events)
        for i, r in enumerate(chunk):
            results.append((r[0], r[1], r[2], r[3], abs_ids[i], types[i]))
    if batch is not None:
        before = len(batch.events)
        repair.on_scan_stop(batch)
        events.extend(batch.events[before:])
    return results, events


def _summarise(results, events, gap_packet: int) -> dict:
    """Per camera, after the gap: quarantine count/reasons and the worst
    clock-vs-counter disagreement of accepted rows (frames)."""
    from omotion.pipeline.batch import FrameQuarantined

    out = {}
    reasons = defaultdict(Counter)
    for e in events:
        if isinstance(e, FrameQuarantined) and e.packet_id is not None and e.packet_id > gap_packet:
            reasons[e.cam_id][e.reason] += 1
    by_cam = defaultdict(list)
    for r in results:
        by_cam[r[0]].append(r)
    for cam, rs in by_cam.items():
        before = [r for r in rs if r[3] <= gap_packet and r[5] != "stale"]
        after = [r for r in rs if r[3] > gap_packet]
        accepted_after = [r for r in after if r[5] != "stale"]
        quarantined_after = [r for r in after if r[5] == "stale"]
        if not before or not accepted_after:
            out[cam] = dict(quarantined=len(quarantined_after), reasons=dict(reasons[cam]),
                            worst_offset=None, final_offset=None)
            continue
        _, _, ts0, _, abs0, _ = before[-1]
        offsets = [(r[4] - abs0) - round((r[2] - ts0) / PERIOD_S) for r in accepted_after]
        out[cam] = dict(
            quarantined=len(quarantined_after),
            reasons=dict(reasons[cam]),
            worst_offset=max(offsets, key=abs),
            final_offset=offsets[-1],
            first_accepted_after_gap_abs=accepted_after[0][4],
            last_abs=accepted_after[-1][4],
        )
    return out


def _pipeline_quarantines(events, side_idx: int, gap_packet: int) -> dict[int, Counter]:
    """Real-pipeline FrameQuarantined after the gap, per camera, by reason."""
    from omotion.pipeline.batch import FrameQuarantined

    out: dict[int, Counter] = defaultdict(Counter)
    for e in events:
        if (isinstance(e, FrameQuarantined) and int(e.side) == side_idx
                and e.packet_id is not None and e.packet_id > gap_packet):
            out[int(e.cam_id)][e.reason] += 1
    return out


def _load_prefix_classifier():
    """Pre-#294 FrameClassificationStage from the SDK checkout's git
    history, loaded inside the live omotion package so its relative
    imports resolve. None when unavailable."""
    import omotion
    sdk_root = Path(omotion.__file__).resolve().parent.parent
    try:
        src = subprocess.run(
            ["git", "-C", str(sdk_root), "show", f"{PREFIX_COMMIT}:omotion/pipeline/stages/classify.py"],
            capture_output=True, check=True, timeout=20,
        ).stdout.decode("utf-8")
    except Exception as e:
        log.warning(f"  pre-fix classifier not available from {sdk_root}: {e}")
        return None
    TEST_LOGS.mkdir(exist_ok=True)
    path = TEST_LOGS / f"_classify_prefix_{PREFIX_COMMIT}.py"
    path.write_text(src, encoding="utf-8")
    name = "omotion.pipeline.stages._classify_prefix"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "omotion.pipeline.stages"
    sys.modules[name] = mod          # dataclass() resolves cls.__module__ through sys.modules
    spec.loader.exec_module(mod)
    return mod.FrameClassificationStage


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

@pytest.mark.incremental
@pytest.mark.usefixtures("console_free")
class TestFrameGapResync:
    """A > 8-frame module-wide gap costs one frame, not a 256-frame wrap."""

    def test_01_sensors_reachable(self):
        """The SDK connects to the console and every attached sensor; firmware read; the stall flag is accepted."""
        from omotion.config import DEBUG_FLAG_HISTO_STALL
        import omotion
        STATE["sdk"] = getattr(omotion, "__version__", "?")
        STATE["fw"] = {}
        with sensors_session(SIDES) as bench:
            STATE["sides"] = tuple(bench.sensors)
            STATE["console_fw"] = str(bench.iface.console.get_version())
            for side, s in bench.sensors.items():
                fw = str(s.get_version())
                STATE["fw"][side] = fw
                assert s.set_debug_flags(DEBUG_FLAG_HISTO_STALL), f"{side}: sensor refused DEBUG_FLAG_HISTO_STALL"
                flags = s.get_debug_flags()
                assert s.set_debug_flags(APP_SENSOR_FLAGS), f"{side}: sensor refused restoring the app flags"
                log.info(f"  {side} sensor firmware {fw}; flags read back 0x{flags:X}")
                assert flags & DEBUG_FLAG_HISTO_STALL, f"{side}: stall flag not set (read 0x{flags:X})"
        log.info(f"  SDK {STATE['sdk']}; console firmware {STATE['console_fw']}; sides under test: {STATE['sides']}")
        if len(STATE["sides"]) < 2:
            log.warning("  only one sensor attached — the two-module simultaneous case is not covered by this run")

    def test_02_scan_with_injected_gap(self):
        """60 s scan, stall armed on every side; cleared GAP_S after the last side went quiet; scan completes."""
        with sensors_session(STATE["sides"]) as bench:
            sink, facts = _scan_with_gap(bench.iface, bench.sensors)
        STATE["sink"], STATE["facts"] = sink, facts
        TEST_LOGS.mkdir(exist_ok=True)
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
        for side in STATE["sides"]:
            rows = sink.rows[SIDE_INDEX[side]]
            path = TEST_LOGS / f"frame_gap_resync_{side}_{stamp}.csv"
            with path.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["cam_id", "raw_frame_id", "timestamp_s", "packet_id"])
                w.writerows(rows)
            log.info(f"  {side}: {len(rows)} wire rows from {len({r[0] for r in rows})} camera(s); "
                     f"facts {facts[side]}; csv -> {path}")
        log.info(f"  scan {sink.scan_id}: {facts['scan']}; pipeline events {Counter(type(e).__name__ for e in sink.events)}")
        assert not facts["scan"]["error"], f"scan error: {facts['scan']['error']}"
        assert not facts["scan"]["canceled"], "the scan was canceled"
        for side in STATE["sides"]:
            f_ = facts[side]
            assert f_.get("stalled"), (
                f"{side}: the stall never tripped within {STALL_TIMEOUT_S:.0f} s "
                f"({f_.get('packets_at_stall')} packets) — is DEBUG_FLAG_HISTO_STALL in this sensor firmware?"
            )
            assert f_.get("clear_ok"), f"{side}: clearing the stall bit mid-scan failed"
            rows = sink.rows[SIDE_INDEX[side]]
            packets = len({r[3] for r in rows})
            assert packets >= 1800 + 40, f"{side}: only {packets} packets reached the pipeline — the stream did not resume"
            assert len(rows) >= 7 * packets, f"{side}: {len(rows)} rows for {packets} packets — samples missing"

    def test_03_gap_is_module_wide(self):
        """Each side: one gap of 9..127 frames on every camera at one packet boundary; both sides stalled within 0.5 s."""
        STATE["gap"] = {}
        for side in STATE["sides"]:
            by_cam = _per_camera(STATE["sink"].rows[SIDE_INDEX[side]])
            gaps = {cam: _largest_gap(seq) for cam, seq in by_cam.items()}
            for cam, (pid, step, dt, _) in sorted(gaps.items()):
                log.info(f"  {side} cam {cam}: gap after packet {pid}: +{step} frames, clock +{dt*1000:.0f} ms")
            assert len(by_cam) >= 4, f"{side}: only {len(by_cam)} camera(s) streamed"
            steps = {g[1] for g in gaps.values()}
            pids = {g[0] for g in gaps.values()}
            assert len(pids) == 1, f"{side}: gap is not at one packet boundary across cameras: {pids}"
            assert len(steps) == 1, f"{side}: cameras disagree on the gap length: {steps}"
            step = steps.pop()
            assert 8 < step < 128, f"{side}: gap of {step} frames is outside the 9..127 window this test needs"
            for cam, (_, s_, dt, _) in gaps.items():
                assert abs(dt / PERIOD_S - s_) <= 1.0, f"{side} cam {cam}: clock {dt:.3f}s disagrees with counter +{s_}"
            STATE["gap"][side] = dict(packet=pids.pop(), frames=step)
        if len(STATE["sides"]) == 2:
            skew = abs(STATE["facts"]["left"]["stall_at_s"] - STATE["facts"]["right"]["stall_at_s"])
            log.info(f"  both modules stalled within {skew*1000:.0f} ms of each other")
            assert skew < 0.5, f"the two modules' gaps are {skew:.2f} s apart — not the simultaneous field case"

    def test_04_fixed_classifier_reanchors(self):
        """Real pipeline + installed classifier, every side: ≤ 2 quarantines per camera after the gap, no non_monotonic, no dark-integrity warning, abs ids follow the clock."""
        from omotion.pipeline.batch import DarkIntegrityWarning
        from omotion.pipeline.stages.classify import FrameClassificationStage
        from omotion.pipeline.stages.timestamp_repair import TimestampRepairStage
        sink = STATE["sink"]
        dark_warnings = [e for e in sink.events if isinstance(e, DarkIntegrityWarning)]
        log.info(f"  real pipeline: {len(dark_warnings)} DarkIntegrityWarning(s) in the whole scan")
        STATE["fixed"], STATE["pipeline_q"] = {}, {}
        for side in STATE["sides"]:
            idx, gap = SIDE_INDEX[side], STATE["gap"][side]
            live_q = _pipeline_quarantines(sink.events, idx, gap["packet"])
            STATE["pipeline_q"][side] = live_q
            log.info(f"  {side}: real pipeline quarantines after the gap, per camera: "
                     f"{ {cam: dict(c) for cam, c in sorted(live_q.items())} }")
            results, events = _run_stages(sink.rows[idx], idx, FrameClassificationStage, TimestampRepairStage)
            summary = _summarise(results, events, gap["packet"])
            STATE["fixed"][side] = summary
            for cam, s_ in sorted(summary.items()):
                log.info(f"  {side} cam {cam}: replay quarantined after gap {s_['quarantined']} {s_['reasons']}; "
                         f"worst clock offset {s_['worst_offset']} frames, final {s_['final_offset']}")
            for cam, c in live_q.items():
                assert sum(c.values()) <= 2, (
                    f"{side} cam {cam}: the real pipeline quarantined {sum(c.values())} frames after the gap {dict(c)} — "
                    f"lockout (sdk#286 not fixed in SDK {STATE.get('sdk')})"
                )
                assert not ({"non_monotonic", "counter_clock_mismatch"} & set(c)), f"{side} cam {cam}: {dict(c)}"
            for cam, s_ in summary.items():
                assert s_["quarantined"] <= 2, f"{side} cam {cam}: replay quarantined {s_['quarantined']} {s_['reasons']}"
                assert s_["worst_offset"] is not None and abs(s_["worst_offset"]) <= 1, (
                    f"{side} cam {cam}: accepted abs ids drift {s_['worst_offset']} frames from the device clock "
                    f"after the gap (256 low = the wrap was never counted)"
                )
        assert not dark_warnings, (
            f"{len(dark_warnings)} DarkIntegrityWarning(s): scheduled darks landed on light frames after the gap"
        )

    def test_05_prefix_classifier_locks_out(self):
        """Contrast: the pre-#294 classifier quarantines ≈ 256 frames per camera on the same rows and ends 256 low."""
        from omotion.pipeline.stages.timestamp_repair import TimestampRepairStage
        prefix = _load_prefix_classifier()
        if prefix is None:
            pytest.skip("pre-fix classifier not available (SDK git history)")
        STATE["prefix"] = {}
        for side in STATE["sides"]:
            idx = SIDE_INDEX[side]
            results, events = _run_stages(STATE["sink"].rows[idx], idx, prefix, TimestampRepairStage)
            summary = _summarise(results, events, STATE["gap"][side]["packet"])
            STATE["prefix"][side] = summary
            for cam, s_ in sorted(summary.items()):
                log.info(f"  [pre-fix] {side} cam {cam}: quarantined after gap {s_['quarantined']} {s_['reasons']}; "
                         f"final clock offset {s_['final_offset']} frames")
            locked = [cam for cam, s_ in summary.items() if s_["quarantined"] >= 100]
            low = [cam for cam, s_ in summary.items()
                   if s_["final_offset"] is not None and s_["final_offset"] <= -200]
            log.info(f"  [pre-fix] {side}: {len(locked)}/{len(summary)} cameras locked out, {len(low)} end >=200 frames low")
            assert locked, f"{side}: the pre-fix classifier did not lock out on this capture — the gap did not reproduce sdk#286"
