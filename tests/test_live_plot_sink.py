from types import SimpleNamespace

import numpy as np
import pytest

from motion_connector import _FinalBatchSink, _LivePlotSink

pytestmark = pytest.mark.unit


class _Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


def _connector():
    return SimpleNamespace(
        _camera_temp_alert_threshold_c=100.0,
        _camera_dropped=set(),
        _camera_dropped_recovery_logged=set(),
        _camera_last_seen={},
        _camera_last_temp={},
        captureLog=_Signal(),
        scanMeanSampled=_Signal(),
        scanContrastSampled=_Signal(),
        scanBfiSampled=_Signal(),
        scanBviSampled=_Signal(),
        scanCameraTemperature=_Signal(),
        scanCorrectedBatch=_Signal(),
    )


def test_live_plot_sink_emits_only_source_camera_for_each_row():
    conn = _connector()
    sink = _LivePlotSink(connector=conn, plot_t0=0.0)
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

    # Signal shape: (side, cam_id, frame_id, timestamp_s, value)
    assert [(c[0], c[1], c[2], c[4]) for c in conn.scanContrastSampled.calls] == [
        ("left", 0, 10, np.float32(0.22)),
        ("right", 2, 11, np.float32(0.24)),
    ]
    assert [(c[0], c[1]) for c in conn.scanBfiSampled.calls] == [
        ("left", 0),
        ("right", 2),
    ]


def test_live_plot_sink_uses_per_frame_sdk_timestamps():
    conn = _connector()
    sink = _LivePlotSink(connector=conn, plot_t0=0.0)
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

    # Signal shape: (side, cam_id, frame_id, timestamp_s, value); ts is at [3]
    assert [call[3] for call in conn.scanMeanSampled.calls] == [1.25, 1.275]
    assert [call[3] for call in conn.scanContrastSampled.calls] == [1.25, 1.275]
    assert [call[3] for call in conn.scanBfiSampled.calls] == [1.25, 1.275]
    assert [call[3] for call in conn.scanBviSampled.calls] == [1.25, 1.275]


def test_final_batch_sink_accepts_enriched_interval_frames():
    conn = _connector()
    sink = _FinalBatchSink(connector=conn, plot_t0=0.0)
    interval = SimpleNamespace(
        frames=[
            SimpleNamespace(
                side="left",
                cam_id=1,
                abs_frame_id=42,
                bfi=2.5,
                bvi=6.5,
                mean=125.0,
                contrast=0.31,
            ),
            SimpleNamespace(
                side="right",
                cam_id=6,
                abs_frame_id=43,
                bfi=3.5,
                bvi=7.5,
                mean=140.0,
                contrast=0.29,
            ),
        ]
    )

    sink.consume("final", interval)

    assert len(conn.scanCorrectedBatch.calls) == 1
    payload = conn.scanCorrectedBatch.calls[0][0]
    assert [(p["side"], p["camId"], p["frameId"], p["bfi"], p["bvi"], p["mean"], p["contrast"]) for p in payload] == [
        ("left", 1, 42, 2.5, 6.5, 125.0, 0.31),
        ("right", 6, 43, 3.5, 7.5, 140.0, 0.29),
    ]
