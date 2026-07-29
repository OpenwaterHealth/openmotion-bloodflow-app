import math
from types import SimpleNamespace

import numpy as np
import pytest

from motion_connector import _LivePlotSink

pytestmark = pytest.mark.unit


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _RecorderLiveSource:
    def __init__(self):
        self.appended = []
        self.dropped = []

    def append_uncorrected(self, *, side, cam_id, frame_id, t, bfi, bvi,
                           mean=None, contrast=None, temp=None):
        self.appended.append({
            "side": side, "cam_id": cam_id, "frame_id": frame_id, "t": t,
            "bfi": bfi, "bvi": bvi, "mean": mean, "contrast": contrast,
            "temp": temp,
        })

    def mark_dropped(self, *, side, cam_id, t):
        self.dropped.append({"side": side, "cam_id": cam_id, "t": t})


def _connector():
    conn = SimpleNamespace(
        _camera_temp_alert_threshold_c=100.0,
        _camera_dropped=set(),
        _camera_last_seen={},
        _camera_last_temp={},
        captureLog=_Signal(),
        recovered=[],
    )
    # Called by the sink when a dropped camera's frames resume.
    conn._on_camera_dropout_recovered = (
        lambda side, cam_id: conn.recovered.append((side, cam_id)))
    return conn


def _make_sink(conn, live_source=None):
    """Helper: build a _LivePlotSink with a stub live_source if not provided."""
    if live_source is None:
        live_source = _RecorderLiveSource()
    return _LivePlotSink(connector=conn, plot_t0=0.0, live_source=live_source), live_source


def test_live_plot_sink_appends_only_source_camera_for_each_row():
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((2, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((2, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((2, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.full((2, 2, 8), -999.0, dtype=np.float32),
        temperature_c=np.full((2, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["light", "light"], dtype="<U8"),
        timestamp_s=np.array([0.1, 0.2], dtype=np.float64),
        abs_frame_ids=np.array([10, 11], dtype=np.int64),
        side_ids=np.array([0, 1], dtype=np.int8),
        cam_ids=np.array([0, 2], dtype=np.int8),
    )
    batch.bfi_live[0, 0, 0] = 7.0
    batch.bfi_live[1, 1, 2] = 8.0
    batch.bvi_live[0, 0, 0] = 4.0
    batch.bvi_live[1, 1, 2] = 5.0
    batch.mean_dc_rt[0, 0, 0] = 120.0
    batch.mean_dc_rt[1, 1, 2] = 130.0
    batch.contrast_sn_rt[0, 0, 0] = 0.22
    batch.contrast_sn_rt[1, 1, 2] = 0.24

    sink.consume("live", batch)

    assert [(r["side"], r["cam_id"], r["frame_id"]) for r in src.appended] == [
        ("left", 0, 10),
        ("right", 2, 11),
    ]
    assert [r["contrast"] for r in src.appended] == [
        pytest.approx(0.22), pytest.approx(0.24),
    ]
    assert [r["bfi"] for r in src.appended] == [
        pytest.approx(7.0), pytest.approx(8.0),
    ]


def test_live_plot_sink_uses_per_frame_sdk_timestamps():
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((2, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((2, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((2, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((2, 2, 8), dtype=np.float32),
        temperature_c=np.full((2, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["light", "light"], dtype="<U8"),
        timestamp_s=np.array([1.25, 1.275], dtype=np.float64),
        abs_frame_ids=np.array([10, 11], dtype=np.int64),
        side_ids=np.array([0, 0], dtype=np.int8),
        cam_ids=np.array([1, 1], dtype=np.int8),
    )

    sink.consume("live", batch)

    assert [r["t"] for r in src.appended] == [1.25, 1.275]


def test_live_plot_sink_subscribes_to_live_side_channel():
    sink, _ = _make_sink(_connector())
    assert "live" in sink.channels
    assert "live_side" in sink.channels
    # The app no longer subscribes to the "final" channel anywhere — the
    # corrected record is persisted SDK-side (ScanDBSink) and read back
    # on replay; the live display is realtime-only.
    assert "final" not in sink.channels


def test_live_plot_sink_live_side_appends_under_cam_id_minus_1():
    """A SideAverageSample on the 'live_side' channel (one per capture per side
    from the SDK's LiveSideAverageStage) is appended under cam_id=-1 for its
    side. No dedup/skip here — the stage already produced one per capture."""
    conn = _connector()
    sink, src = _make_sink(conn)
    sink.consume("live_side", SimpleNamespace(t=0.5, frame_id=100, side=0, bfi=0.42, bvi=5.0))
    sink.consume("live_side", SimpleNamespace(t=0.5, frame_id=100, side=1, bfi=0.31, bvi=4.9))

    recs = [r for r in src.appended if r["cam_id"] == -1]
    assert len(recs) == 2
    left = next(r for r in recs if r["side"] == "left")
    assert left["bfi"] == pytest.approx(0.42) and left["bvi"] == pytest.approx(5.0)
    assert left["frame_id"] == 100 and left["t"] == pytest.approx(0.5)
    assert left["mean"] is None and left["contrast"] is None
    right = next(r for r in recs if r["side"] == "right")
    assert right["bfi"] == pytest.approx(0.31)


def test_live_plot_sink_live_channel_appends_no_side_average():
    """The 'live' channel feeds only per-camera buffers; the cam_id=-1 side
    average comes solely from the 'live_side' channel now."""
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((1, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((1, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((1, 2, 8), dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["light"], dtype="<U8"),
        timestamp_s=np.array([0.5], dtype=np.float64),
        abs_frame_ids=np.array([1], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([0], dtype=np.int8),
    )
    batch.bfi_live[0, 0, 0] = 0.3
    batch.bvi_live[0, 0, 0] = 5.0

    sink.consume("live", batch)

    assert [r for r in src.appended if r["cam_id"] == -1] == []
    assert [r for r in src.appended if r["cam_id"] == 0]  # per-cam still appended


def test_live_plot_sink_live_channel_updates_dropout_heartbeat():
    """Per-camera BFI arrival on the 'live' channel still updates the
    dropout-watchdog heartbeat — clinical mode shows only the average, but
    liveness detection must keep seeing each camera."""
    conn = _connector()
    sink, _ = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((1, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((1, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((1, 2, 8), dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["light"], dtype="<U8"),
        timestamp_s=np.array([0.5], dtype=np.float64),
        abs_frame_ids=np.array([1], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([3], dtype=np.int8),
    )
    batch.bfi_live[0, 0, 3] = 0.3
    batch.bvi_live[0, 0, 3] = 5.0

    sink.consume("live", batch)
    assert ("left", 3) in conn._camera_last_seen


def test_live_plot_sink_appends_to_live_source():
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((1, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((1, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.full((1, 2, 8), 0.25, dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["light"], dtype="<U8"),
        timestamp_s=np.array([1.5], dtype=np.float64),
        abs_frame_ids=np.array([77], dtype=np.int64),
        side_ids=np.array([1], dtype=np.int8),
        cam_ids=np.array([4], dtype=np.int8),
    )
    batch.bfi_live[0, 1, 4] = 4.4
    batch.bvi_live[0, 1, 4] = 3.3
    batch.mean_dc_rt[0, 1, 4] = 125.0

    sink.consume("live", batch)

    assert len(src.appended) == 1
    rec = src.appended[0]
    assert rec["side"] == "right"
    assert rec["cam_id"] == 4
    assert rec["frame_id"] == 77
    assert rec["t"] == 1.5
    assert rec["bfi"] == pytest.approx(4.4)
    assert rec["bvi"] == pytest.approx(3.3)
    assert rec["mean"] == pytest.approx(125.0)
    assert rec["contrast"] == pytest.approx(0.25)


def test_live_plot_sink_passes_temp_for_light_frames():
    """Light frames carry the camera temperature into the plot source as
    the "temp" metric (issue #165 — per-cell dev-mode readout)."""
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((1, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((1, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((1, 2, 8), dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 54.3, dtype=np.float32),
        frame_type=np.array(["light"], dtype="<U8"),
        timestamp_s=np.array([0.5], dtype=np.float64),
        abs_frame_ids=np.array([1], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([0], dtype=np.int8),
    )
    batch.bfi_live[0, 0, 0] = 0.3
    batch.bvi_live[0, 0, 0] = 5.0

    sink.consume("live", batch)

    assert len(src.appended) == 1
    assert src.appended[0]["temp"] == pytest.approx(54.3, abs=1e-4)


def test_live_plot_sink_no_temp_for_dark_frames():
    """Dark frames have no meaningful camera-temp reading — temp must be
    None so the plot's temp stream only carries light-frame readings."""
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((1, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((1, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((1, 2, 8), dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 54.3, dtype=np.float32),
        frame_type=np.array(["dark"], dtype="<U8"),
        timestamp_s=np.array([0.5], dtype=np.float64),
        abs_frame_ids=np.array([1], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([0], dtype=np.int8),
    )
    batch.bfi_live[0, 0, 0] = 0.3
    batch.bvi_live[0, 0, 0] = 5.0

    sink.consume("live", batch)

    assert len(src.appended) == 1
    assert src.appended[0]["temp"] is None


def test_live_plot_sink_no_temp_when_non_finite():
    """A NaN temperature (e.g. sensor metadata gap) is not appended —
    the temp buffer carries only real readings."""
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.zeros((1, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((1, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((1, 2, 8), dtype=np.float32),
        temperature_c=np.full((1, 2, 8), np.nan, dtype=np.float32),
        frame_type=np.array(["light"], dtype="<U8"),
        timestamp_s=np.array([0.5], dtype=np.float64),
        abs_frame_ids=np.array([1], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([0], dtype=np.int8),
    )
    batch.bfi_live[0, 0, 0] = 0.3
    batch.bvi_live[0, 0, 0] = 5.0

    sink.consume("live", batch)

    assert len(src.appended) == 1
    assert src.appended[0]["temp"] is None


def test_live_plot_sink_records_every_arrival_into_gap_tracker():
    """The gap tracker is fed on frame ARRIVAL — an unlit frame whose
    BFI is NaN (covered sensor) is still delivery, not a data gap. Gaps
    now mean "frames stopped arriving"."""
    from nan_gap_tracker import NanGapTracker

    conn = _connector()
    tracker = NanGapTracker()
    src = _RecorderLiveSource()
    sink = _LivePlotSink(connector=conn, plot_t0=0.0, live_source=src,
                         nan_gap_tracker=tracker)
    batch = SimpleNamespace(
        bfi_live=np.zeros((3, 2, 8), dtype=np.float32),
        bvi_live=np.zeros((3, 2, 8), dtype=np.float32),
        mean_dc_rt=np.zeros((3, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((3, 2, 8), dtype=np.float32),
        temperature_c=np.full((3, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["light", "light", "light"], dtype="<U8"),
        timestamp_s=np.array([0.1, 0.125, 3.0], dtype=np.float64),
        abs_frame_ids=np.array([10, 11, 12], dtype=np.int64),
        side_ids=np.array([0, 0, 0], dtype=np.int8),
        cam_ids=np.array([1, 1, 1], dtype=np.int8),
    )
    # Middle sample is NaN → recorded in the tracker (the frame arrived)
    # AND appended to the plot as a gap sample (issue #418). The only
    # delivery gap is the genuine silence between t=0.125 and t=3.0.
    batch.bfi_live[:] = 7.0
    batch.bvi_live[:] = 4.0
    batch.bfi_live[1, 0, 1] = np.nan

    sink.consume("live", batch)

    assert tracker.merged_gaps() == [
        (pytest.approx(0.125), pytest.approx(3.0))
    ]
    # The NaN light row is appended too (blank-gap sample) — the plot's
    # buffers advance on every arriving light frame.
    assert [r["t"] for r in src.appended] == [
        pytest.approx(0.1), pytest.approx(0.125), pytest.approx(3.0)
    ]
    assert math.isnan(src.appended[1]["bfi"])


def test_live_plot_sink_covered_camera_appends_nan_gap_samples():
    """A covered / off-target camera streams light-typed frames whose
    BFI/BVI are NaN. It keeps feeding the heartbeat and the gap tracker,
    and (issue #418) the NaN sample IS appended — buffers and liveEdge
    keep advancing so the time axis scrolls through a contact loss; the
    renderer draws NaN runs as a blank gap. mean/contrast stay None
    (non-finite); chip temperature is real and keeps flowing."""
    from nan_gap_tracker import NanGapTracker

    conn = _connector()
    tracker = NanGapTracker()
    src = _RecorderLiveSource()
    sink = _LivePlotSink(connector=conn, plot_t0=0.0, live_source=src,
                         nan_gap_tracker=tracker)
    batch = SimpleNamespace(
        bfi_live=np.full((2, 2, 8), np.nan, dtype=np.float32),
        bvi_live=np.full((2, 2, 8), np.nan, dtype=np.float32),
        mean_dc_rt=np.full((2, 2, 8), np.nan, dtype=np.float32),
        contrast_sn_rt=np.full((2, 2, 8), np.nan, dtype=np.float32),
        temperature_c=np.full((2, 2, 8), 55.0, dtype=np.float32),
        frame_type=np.array(["light", "light"], dtype="<U8"),
        timestamp_s=np.array([0.1, 0.125], dtype=np.float64),
        abs_frame_ids=np.array([10, 11], dtype=np.int64),
        side_ids=np.array([0, 0], dtype=np.int8),
        cam_ids=np.array([2, 2], dtype=np.int8),
    )

    sink.consume("live", batch)

    assert ("left", 2) in conn._camera_last_seen   # alive
    assert tracker.t0 == pytest.approx(0.1)        # delivery recorded
    assert [(r["side"], r["cam_id"], r["frame_id"]) for r in src.appended] == [
        ("left", 2, 10),
        ("left", 2, 11),
    ]
    assert all(math.isnan(r["bfi"]) and math.isnan(r["bvi"])
               for r in src.appended)
    assert all(r["mean"] is None and r["contrast"] is None
               for r in src.appended)
    # Chip temperature is real even when the camera sees no light — the
    # temp trace keeps updating through the gap.
    assert [r["temp"] for r in src.appended] == [
        pytest.approx(55.0, abs=1e-4), pytest.approx(55.0, abs=1e-4)]
    assert conn._camera_last_temp[("left", 2)] == pytest.approx(55.0, abs=1e-4)


def test_live_plot_sink_dark_frame_nan_still_skipped():
    """Scheduled dark frames with NaN BFI/BVI stay unappended — appending
    them would notch every trace with a one-frame blank at each ~15 s
    dark now that the renderer breaks the line at NaN samples (issue #418)."""
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.full((1, 2, 8), np.nan, dtype=np.float32),
        bvi_live=np.full((1, 2, 8), np.nan, dtype=np.float32),
        mean_dc_rt=np.full((1, 2, 8), np.nan, dtype=np.float32),
        contrast_sn_rt=np.full((1, 2, 8), np.nan, dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["dark"], dtype="<U8"),
        timestamp_s=np.array([0.5], dtype=np.float64),
        abs_frame_ids=np.array([20], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([1], dtype=np.int8),
    )

    sink.consume("live", batch)

    assert src.appended == []


def test_live_plot_sink_warmup_and_stale_nan_still_skipped():
    """Warmup and stale frames are skipped before the NaN gate — the
    relaxed append (issue #418) must not resurrect them."""
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.full((2, 2, 8), np.nan, dtype=np.float32),
        bvi_live=np.full((2, 2, 8), np.nan, dtype=np.float32),
        mean_dc_rt=np.full((2, 2, 8), np.nan, dtype=np.float32),
        contrast_sn_rt=np.full((2, 2, 8), np.nan, dtype=np.float32),
        temperature_c=np.full((2, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["warmup", "stale"], dtype="<U8"),
        timestamp_s=np.array([0.1, 0.125], dtype=np.float64),
        abs_frame_ids=np.array([10, 11], dtype=np.int64),
        side_ids=np.array([0, 0], dtype=np.int8),
        cam_ids=np.array([2, 2], dtype=np.int8),
    )

    sink.consume("live", batch)

    assert src.appended == []


def test_live_plot_sink_legacy_batch_nan_still_skipped():
    """Batches without side_ids/cam_ids fan every row out to all 16
    cameras; a finite BFI is the only evidence a camera produced the
    row. NaN rows must stay unappended there or every camera's buffer
    would collect 16-way NaN garbage (issue #418)."""
    conn = _connector()
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.full((1, 2, 8), np.nan, dtype=np.float32),
        bvi_live=np.full((1, 2, 8), np.nan, dtype=np.float32),
        mean_dc_rt=np.full((1, 2, 8), np.nan, dtype=np.float32),
        contrast_sn_rt=np.full((1, 2, 8), np.nan, dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["light"], dtype="<U8"),
        timestamp_s=np.array([0.5], dtype=np.float64),
        abs_frame_ids=np.array([1], dtype=np.int64),
    )

    sink.consume("live", batch)

    assert src.appended == []


def test_live_plot_sink_rearms_dropped_camera_on_frame_arrival():
    """A camera in the dropped set that sends a fresh frame is re-armed:
    removed from the set, recovery surfaced, and the sample plotted —
    dropout marking is no longer permanent for the scan."""
    conn = _connector()
    conn._camera_dropped = {("left", 1)}
    sink, src = _make_sink(conn)
    batch = SimpleNamespace(
        bfi_live=np.full((1, 2, 8), 7.0, dtype=np.float32),
        bvi_live=np.full((1, 2, 8), 4.0, dtype=np.float32),
        mean_dc_rt=np.zeros((1, 2, 8), dtype=np.float32),
        contrast_sn_rt=np.zeros((1, 2, 8), dtype=np.float32),
        temperature_c=np.full((1, 2, 8), 35.0, dtype=np.float32),
        frame_type=np.array(["light"], dtype="<U8"),
        timestamp_s=np.array([0.1], dtype=np.float64),
        abs_frame_ids=np.array([10], dtype=np.int64),
        side_ids=np.array([0], dtype=np.int8),
        cam_ids=np.array([1], dtype=np.int8),
    )

    sink.consume("live", batch)

    assert conn._camera_dropped == set()           # re-armed
    assert conn.recovered == [("left", 1)]         # recovery surfaced
    assert len(src.appended) == 1                  # display resumed


def test_live_plot_sink_low_light_transition_logged_after_debounce():
    """batch.low_light_rt drives a debounced per-camera state: after
    _LOW_LIGHT_DEBOUNCE_FRAMES consecutive unlit frames, one capture-log
    line explains the empty plot. No spam before the debounce."""
    conn = _connector()
    sink, _ = _make_sink(conn)
    n = sink._LOW_LIGHT_DEBOUNCE_FRAMES

    def _covered_batch(n_frames, t0):
        b = SimpleNamespace(
            bfi_live=np.full((n_frames, 2, 8), np.nan, dtype=np.float32),
            bvi_live=np.full((n_frames, 2, 8), np.nan, dtype=np.float32),
            mean_dc_rt=np.full((n_frames, 2, 8), np.nan, dtype=np.float32),
            contrast_sn_rt=np.full((n_frames, 2, 8), np.nan, dtype=np.float32),
            temperature_c=np.full((n_frames, 2, 8), 40.0, dtype=np.float32),
            frame_type=np.array(["light"] * n_frames, dtype="<U8"),
            timestamp_s=t0 + np.arange(n_frames, dtype=np.float64) * 0.025,
            abs_frame_ids=np.arange(10, 10 + n_frames, dtype=np.int64),
            side_ids=np.zeros(n_frames, dtype=np.int8),
            cam_ids=np.full(n_frames, 3, dtype=np.int8),
            low_light_rt=np.ones((n_frames, 2, 8), dtype=bool),
        )
        return b

    # One frame short of the debounce: no message yet.
    sink.consume("live", _covered_batch(n - 1, 0.0))
    low_msgs = [c[0] for c in conn.captureLog.calls if "low light" in c[0]]
    assert low_msgs == []

    # One more unlit frame crosses the debounce: exactly one message.
    sink.consume("live", _covered_batch(1, (n - 1) * 0.025))
    low_msgs = [c[0] for c in conn.captureLog.calls if "low light" in c[0]]
    assert len(low_msgs) == 1
    assert "LEFT 4" in low_msgs[0]

    # Staying covered produces no further messages.
    sink.consume("live", _covered_batch(n, n * 0.025))
    low_msgs = [c[0] for c in conn.captureLog.calls if "low light" in c[0]]
    assert len(low_msgs) == 1


def test_live_plot_sink_side_average_records_arrivals_into_tracker():
    from nan_gap_tracker import NanGapTracker

    conn = _connector()
    tracker = NanGapTracker()
    src = _RecorderLiveSource()
    sink = _LivePlotSink(connector=conn, plot_t0=0.0, live_source=src,
                         nan_gap_tracker=tracker)

    sink.consume("live_side", SimpleNamespace(
        t=0.5, frame_id=100, side=0, bfi=0.42, bvi=5.0))
    sink.consume("live_side", SimpleNamespace(
        t=0.6, frame_id=101, side=0, bfi=float("nan"), bvi=5.0))
    sink.consume("live_side", SimpleNamespace(
        t=2.0, frame_id=102, side=0, bfi=0.40, bvi=4.8))

    # Side-average appends still store NaN (existing behavior)...
    assert len(src.appended) == 3
    # ...and the tracker records every arriving sample — the NaN average
    # at t=0.6 (e.g. all cameras covered) is delivery, so the only gap is
    # the genuine 0.6→2.0 silence.
    assert tracker.merged_gaps() == [
        (pytest.approx(0.6), pytest.approx(2.0))
    ]


def test_live_plot_sink_tracker_is_optional():
    """Default nan_gap_tracker=None keeps the sink working unchanged —
    existing callers and tests construct it without a tracker."""
    conn = _connector()
    sink, src = _make_sink(conn)
    sink.consume("live_side", SimpleNamespace(
        t=0.5, frame_id=100, side=0, bfi=0.42, bvi=5.0))
    assert len(src.appended) == 1
