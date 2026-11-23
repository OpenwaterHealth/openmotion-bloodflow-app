"""#783: movement shading on the plots.

The SDK stores one IMU average per histogram frame and side (the samples
received since that side's previous frame: accel_dev_g = mean |a - a_mean_2s|,
gyro_dps = mean |omega|, n_samples). The app keeps each frame's trailing
1/8 s rolling average and turns it into yellow / red spans with the GUI
thresholds: ``motion_bands_for_window``. Also covered: the config keys and
the PlotCell / PlotViewer / Settings wiring.

Unit-marked: no app launch, no hardware.
"""

import math
from pathlib import Path

import pytest

from config.app_config import APP_CONFIG, PREFERENCE_KEYS
from data_sources import LiveScanSource, MOTION_AVG_WINDOW_S

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
DT = 0.025                      # 40 Hz frames


def _frames(values, t0=DT, n=1):
    """values: (accel_dev_g, gyro_dps) per consecutive 40 Hz frame."""
    return [(t0 + i * DT, n, a, g) for i, (a, g) in enumerate(values)]


def bands(src, side, t_lo, t_hi, accel, gyro):
    """Red-only query (yellow off): the single-threshold behaviour."""
    return [b[:2] for b in src.motion_bands_for_window(
        side, t_lo, t_hi, 0.0, accel, 0.0, gyro)]


@pytest.fixture
def src():
    s = LiveScanSource(plot_t0=0.0)
    yield s
    s.release()


def test_window_is_one_eighth_second():
    assert MOTION_AVG_WINDOW_S == pytest.approx(0.125)


def test_rolling_average_covers_the_last_eighth_second(src):
    # 5 frames at 40 Hz fill the 1/8 s window: one 0.5 g frame among
    # quiet ones averages to 0.1 g while it is in the window.
    vals = [(0.0, 0.0)] * 5 + [(0.5, 10.0)] + [(0.0, 0.0)] * 6
    src.add_motion_frames("left", _frames(vals))
    t_spike = DT * 6
    assert src.motion_rolling_at("left", t_spike) == pytest.approx((0.1, 2.0))
    # Still in the window 4 frames later, gone at the 5th.
    assert src.motion_rolling_at("left", t_spike + 4 * DT) == pytest.approx((0.1, 2.0))
    assert src.motion_rolling_at("left", t_spike + 5 * DT) == pytest.approx((0.0, 0.0))
    assert src.motion_rolling_at("left", 0.0) is None


def test_average_is_weighted_by_sample_count(src):
    # A 2-sample frame weighs twice a 1-sample frame; an empty frame
    # (None values) weighs nothing.
    src.add_motion_frames("left", [(DT, 1, 0.0, 0.0), (2 * DT, 2, 0.3, 0.0),
                                   (3 * DT, 0, None, None)])
    assert src.motion_rolling_at("left", 3 * DT)[0] == pytest.approx(0.2)


def test_window_with_no_samples_is_nan_and_not_shaded(src):
    src.add_motion_frames("left", [(DT * (i + 1), 0, None, None) for i in range(8)])
    acc, gyr = src.motion_rolling_at("left", 8 * DT)
    assert math.isnan(acc) and math.isnan(gyr)
    assert bands(src, "left", 0.0, 1.0, 0.01, 1.0) == []


def test_quiet_frames_draw_nothing(src):
    src.add_motion_frames("left", _frames([(0.01, 1.0)] * 40))
    assert bands(src, "left", 0.0, 2.0, 0.1, 20.0) == []


def test_sustained_motion_shades_one_merged_span(src):
    vals = [(0.0, 0.0)] * 20 + [(0.3, 0.0)] * 20 + [(0.0, 0.0)] * 20
    src.add_motion_frames("left", _frames(vals))
    out = bands(src, "left", 0.0, 2.0, 0.1, 20.0)
    assert len(out) == 1
    # Frame k covers (t[k-1], t[k]]. The average crosses 0.1 g at the
    # first moving frame (0.3/5 = 0.06 < 0.1, 0.12 >= 0.1 at the 2nd) and
    # drops below it 4 frames after the motion ends.
    first_moving = 21 * DT
    assert out[0][0] == pytest.approx(first_moving)          # end of the 1st frame
    last_moving = 40 * DT
    assert out[0][1] == pytest.approx(last_moving + 3 * DT)


def test_either_channel_triggers_and_zero_turns_a_channel_off(src):
    vals = ([(0.3, 0.0)] * 10 + [(0.0, 0.0)] * 20 + [(0.0, 50.0)] * 10
            + [(0.0, 0.0)] * 10)
    src.add_motion_frames("left", _frames(vals))
    assert len(bands(src, "left", 0.0, 2.0, 0.1, 20.0)) == 2
    accel_only = bands(src, "left", 0.0, 2.0, 0.1, 0.0)
    gyro_only = bands(src, "left", 0.0, 2.0, 0.0, 20.0)
    assert len(accel_only) == 1 and len(gyro_only) == 1
    assert accel_only[0][1] < gyro_only[0][0]
    assert bands(src, "left", 0.0, 2.0, 0.0, 0.0) == []


def test_spans_are_clipped_to_the_window_and_sides_are_separate(src):
    src.add_motion_frames("left", _frames([(1.0, 0.0)] * 80))   # 0.025 .. 2 s
    assert bands(src, "left", 0.5, 1.0, 0.1, 20.0) == [
        pytest.approx([0.5, 1.0])]
    assert bands(src, "right", 0.0, 2.0, 0.1, 20.0) == []


def test_a_gap_in_the_frames_is_not_shaded(src):
    src.add_motion_frames("left", _frames([(1.0, 0.0)] * 4, t0=DT))
    src.add_motion_frames("left", _frames([(1.0, 0.0)] * 4, t0=2.0))
    out = bands(src, "left", 0.0, 3.0, 0.1, 20.0)
    assert len(out) == 2
    assert out[1][0] == pytest.approx(2.0 - 0.1)    # MOTION_FRAME_SPAN_MAX_S


def test_late_out_of_order_frames_still_land_in_order(src):
    src.add_motion_frames("left", _frames([(1.0, 0.0)] * 4, t0=1.0))
    src.add_motion_frames("left", _frames([(1.0, 0.0)] * 4, t0=0.0))
    out = bands(src, "left", -1.0, 2.0, 0.1, 20.0)
    assert len(out) == 2
    assert out[0][1] == pytest.approx(3 * DT)
    assert out[1][1] == pytest.approx(1.0 + 3 * DT)
    # The rebuild recomputed the averages in time order.
    assert src.motion_rolling_at("left", 1.0) == pytest.approx((1.0, 0.0))


def test_malformed_frames_are_ignored(src):
    src.add_motion_frames("left", [None, (1.0,), (float("nan"), 1, 1, 1),
                                   ("x", 1, 1, 1), (DT, 1, 1.0, 0.0)])
    assert bands(src, "left", 0.0, 1.0, 0.1, 20.0) == [
        pytest.approx([0.0, DT])]


def test_yellow_and_red_levels(src):
    """Small motion (talking) shades yellow, large motion red; either
    channel raises the level and red wins."""
    vals = ([(0.03, 0.0)] * 10 + [(0.08, 0.0)] * 10 + [(0.0, 0.0)] * 10
            + [(0.0, 12.0)] * 10 + [(0.0, 30.0)] * 10 + [(0.0, 0.0)] * 10)
    src.add_motion_frames("left", _frames(vals))
    out = src.motion_bands_for_window("left", 0.0, 2.0, 0.02, 0.05, 10.0, 20.0)
    assert [b[2] for b in out] == [1, 2, 1, 1, 2, 1]
    # Yellow off: only the red stretches remain.
    red = src.motion_bands_for_window("left", 0.0, 2.0, 0.0, 0.05, 0.0, 20.0)
    assert [b[2] for b in red] == [2, 2]


def test_config_keys_are_preferences_with_sane_defaults():
    assert APP_CONFIG["motionShadingEnabled"] is False
    assert APP_CONFIG["motionAccelYellowG"] == pytest.approx(0.015)
    assert APP_CONFIG["motionAccelRedG"] == pytest.approx(0.05)
    assert APP_CONFIG["motionGyroYellowDps"] == pytest.approx(8.0)
    assert APP_CONFIG["motionGyroRedDps"] == pytest.approx(25.0)
    for k in ("motionShadingEnabled", "motionAccelYellowG", "motionAccelRedG",
              "motionGyroYellowDps", "motionGyroRedDps"):
        assert k in PREFERENCE_KEYS


def test_viewer_gates_shading_like_the_raw_csvs():
    qml = (ROOT / "components" / "PlotViewer.qml").read_text(encoding="utf-8")
    gate = qml[qml.index("showMotion:"):qml.index("motionAccelYellowG:")]
    assert "motionShadingEnabled === true" in gate
    assert "clinicalMode !== true" in gate
    assert "engineeringMode === true" in gate


def test_cell_draws_bands_before_the_traces():
    qml = (ROOT / "components" / "PlotCell.qml").read_text(encoding="utf-8")
    paint = qml[qml.index("onPaint:"):]
    assert paint.index("_drawMotionBands(") < paint.index("_drawTrace(")


# ── connector wiring ──────────────────────────────────────────────────────

import motion_connector  # noqa: E402
from tests.test_telemetry_dev_gate import (  # noqa: E402
    _captured_request, _make_connector)

_needs_sdk = pytest.mark.skipif(
    not motion_connector._SCAN_REQUEST_HAS_IMU_MOTION,
    reason="installed SDK predates ScanRequest.imu_motion")


@_needs_sdk
@pytest.mark.parametrize("clinical, engineering, shading", [
    (False, False, False),
    (False, True, True),
    (True, False, False),
    (True, False, True),
])
def test_every_scan_records_motion_frames(tmp_path, clinical, engineering, shading):
    """Ethan, 2026-11-17: record the motion magnitude to the DB always,
    whatever the build, mode or shading switch."""
    c = _make_connector(tmp_path, {
        "clinicalMode": clinical, "engineeringMode": engineering,
        "motionShadingEnabled": shading})
    req = _captured_request(c)
    assert req.imu_motion is True
    assert any(isinstance(s, motion_connector._ImuMotionSink) for s in req.sinks)


def test_old_sdk_without_imu_motion_still_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_connector, "_SCAN_REQUEST_HAS_IMU_MOTION", False)
    c = _make_connector(tmp_path, {})
    req = _captured_request(c)
    assert getattr(req, "imu_motion", False) is False
    assert not any(isinstance(s, motion_connector._ImuMotionSink)
                   for s in req.sinks)


def test_motion_sink_feeds_the_live_source(src):
    class Frame:
        side, frame_id, timestamp_s, n_samples = "right", 41, 1.0, 1
        accel_dev_g, gyro_dps = 0.5, 0.0

    sink = motion_connector._ImuMotionSink(src)
    assert sink.channels == {"imu_frame"}
    sink.on_scan_start(None)
    sink.on_complete()          # the runner calls it on every sink
    sink.consume("diagnostics", Frame())        # other channels ignored
    sink.consume("imu_frame", Frame())
    sink.consume("imu_frame", object())         # malformed: dropped quietly
    assert bands(src, "right", 0.0, 2.0, 0.1, 20.0) == [
        pytest.approx([0.9, 1.0])]


def test_sink_matches_the_sdk_event_and_channel():
    pipeline = pytest.importorskip("omotion.pipeline")
    if not hasattr(pipeline, "ImuFrameMotion"):
        pytest.skip("installed SDK predates ImuFrameMotion")
    assert pipeline.IMU_FRAME_CHANNEL in motion_connector._ImuMotionSink.channels
    fields = set(pipeline.ImuFrameMotion.__dataclass_fields__)
    assert {"side", "timestamp_s", "n_samples", "accel_dev_g",
            "gyro_dps"} <= fields


def test_replay_loader_maps_db_rows_to_sides():
    class DB:
        def get_imu_frames(self, session_id):
            assert session_id == 7
            return [
                {"side": 0, "frame_id": 1, "timestamp_s": 0.025, "n_samples": 1,
                 "accel_dev_g": 0.2, "gyro_dps": 3.0},
                {"side": 1, "frame_id": 2, "timestamp_s": 0.05, "n_samples": 0,
                 "accel_dev_g": None, "gyro_dps": None},
                {"side": 1},                            # malformed: skipped
            ]

    assert motion_connector._load_motion_frames(DB(), 7) == {
        "left": [(0.025, 1, 0.2, 3.0)],
        "right": [(0.05, 0, None, None)],
    }


def test_replay_loader_tolerates_old_sdks_and_errors():
    assert motion_connector._load_motion_frames(object(), 1) == {}

    class Broken:
        def get_imu_frames(self, session_id):
            raise RuntimeError("no table")

    assert motion_connector._load_motion_frames(Broken(), 1) == {}
