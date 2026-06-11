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
                           mean=None, contrast=None):
        self.appended.append({
            "side": side, "cam_id": cam_id, "frame_id": frame_id, "t": t,
            "bfi": bfi, "bvi": bvi, "mean": mean, "contrast": contrast,
        })

    def mark_dropped(self, *, side, cam_id, t):
        self.dropped.append({"side": side, "cam_id": cam_id, "t": t})


def _connector():
    return SimpleNamespace(
        _camera_temp_alert_threshold_c=100.0,
        _camera_dropped=set(),
        _camera_dropped_recovery_logged=set(),
        _camera_last_seen={},
        _camera_last_temp={},
        captureLog=_Signal(),
    )


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
    dropout-watchdog heartbeat — reduced mode shows only the average, but
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
