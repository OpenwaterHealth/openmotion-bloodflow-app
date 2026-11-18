"""#783: movement shading on the plots.

The SDK reduces each sensor's IMU stream to 1/8 s motion bins
(accel_dev_g = max | |a| - 1 g |, gyro_dps = max |omega|). The app only
stores them on the scan source and turns them into red spans with the
GUI thresholds: ``motion_bands_for_window``. Also covered: the config keys
and the PlotCell / PlotViewer / Settings wiring.

Unit-marked: no app launch, no hardware.
"""

from pathlib import Path

import pytest

from config.app_config import APP_CONFIG, PREFERENCE_KEYS
from data_sources import LiveScanSource

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
B = 0.125


def _bins(values, t0=0.0):
    """values: list of (accel_dev_g, gyro_dps), one per consecutive 1/8 s bin."""
    return [(t0 + i * B, t0 + (i + 1) * B, a, g) for i, (a, g) in enumerate(values)]


def bands(src, side, t_lo, t_hi, accel, gyro):
    """Red-only query (yellow off): the single-threshold behaviour."""
    return [b[:2] for b in src.motion_bands_for_window(
        side, t_lo, t_hi, 0.0, accel, 0.0, gyro)]


@pytest.fixture
def src():
    s = LiveScanSource(plot_t0=0.0)
    yield s
    s.release()


def test_quiet_bins_draw_nothing(src):
    src.add_motion_bins("left", _bins([(0.01, 1.0)] * 16))
    assert bands(src, "left", 0.0, 2.0, 0.1, 20.0) == []


def test_touching_bins_merge_into_one_span(src):
    vals = [(0.0, 0.0)] * 4 + [(0.3, 0.0)] * 3 + [(0.0, 0.0)] * 4
    src.add_motion_bins("left", _bins(vals))
    assert bands(src, "left", 0.0, 2.0, 0.1, 20.0) == [
        pytest.approx([0.5, 0.875])]


def test_either_channel_triggers_and_zero_turns_a_channel_off(src):
    vals = [(0.3, 0.0), (0.0, 0.0), (0.0, 50.0)]
    src.add_motion_bins("left", _bins(vals))
    both = bands(src, "left", 0.0, 1.0, 0.1, 20.0)
    assert both == [pytest.approx([0.0, 0.125]), pytest.approx([0.25, 0.375])]
    accel_only = bands(src, "left", 0.0, 1.0, 0.1, 0.0)
    assert accel_only == [pytest.approx([0.0, 0.125])]
    gyro_only = bands(src, "left", 0.0, 1.0, 0.0, 20.0)
    assert gyro_only == [pytest.approx([0.25, 0.375])]
    assert bands(src, "left", 0.0, 1.0, 0.0, 0.0) == []


def test_spans_are_clipped_to_the_window_and_sides_are_separate(src):
    src.add_motion_bins("left", _bins([(1.0, 0.0)] * 16))   # 0 .. 2 s
    assert bands(src, "left", 0.5, 1.0, 0.1, 20.0) == [
        pytest.approx([0.5, 1.0])]
    assert bands(src, "right", 0.0, 2.0, 0.1, 20.0) == []


def test_late_out_of_order_bins_still_land_in_order(src):
    # The SDK holds samples taken before the first camera frame, so those
    # bins (negative times) can arrive after later ones.
    src.add_motion_bins("left", _bins([(1.0, 0.0)] * 2, t0=1.0))
    src.add_motion_bins("left", _bins([(1.0, 0.0)] * 2, t0=-0.25))
    assert bands(src, "left", -1.0, 2.0, 0.1, 20.0) == [
        pytest.approx([-0.25, 0.0]), pytest.approx([1.0, 1.25])]


def test_malformed_bins_are_ignored(src):
    src.add_motion_bins("left", [None, (1.0,), (0.5, 0.25, 1, 1),
                                 (float("nan"), 1.0, 1, 1), (0.0, 0.125, 1.0, 0.0)])
    assert bands(src, "left", 0.0, 1.0, 0.1, 20.0) == [
        pytest.approx([0.0, 0.125])]


def test_yellow_and_red_levels():
    """Small motion (talking) shades yellow, large motion red; either
    channel raises the level and red wins."""
    s = LiveScanSource(plot_t0=0.0)
    try:
        vals = [(0.03, 0.0), (0.03, 0.0), (0.08, 0.0), (0.0, 12.0),
                (0.0, 30.0), (0.0, 0.0)]
        s.add_motion_bins("left", _bins(vals))
        out = s.motion_bands_for_window("left", 0.0, 1.0, 0.02, 0.05, 10.0, 20.0)
        assert [b[2] for b in out] == [1, 2, 1, 2]
        assert out[0][:2] == pytest.approx([0.0, 0.25])     # two yellow bins merge
        assert out[1][:2] == pytest.approx([0.25, 0.375])
        # Yellow off: only the red bins remain.
        red = s.motion_bands_for_window("left", 0.0, 1.0, 0.0, 0.05, 0.0, 20.0)
        assert [b[2] for b in red] == [2, 2]
    finally:
        s.release()


def test_config_keys_are_preferences_with_sane_defaults():
    assert APP_CONFIG["motionShadingEnabled"] is False
    assert APP_CONFIG["motionAccelYellowG"] == pytest.approx(0.02)
    assert APP_CONFIG["motionAccelRedG"] == pytest.approx(0.05)
    assert APP_CONFIG["motionGyroYellowDps"] == pytest.approx(10.0)
    assert APP_CONFIG["motionGyroRedDps"] == pytest.approx(20.0)
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
def test_every_scan_records_motion_bins(tmp_path, clinical, engineering, shading):
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
    class Bin:
        side, t_start_s, t_end_s, accel_dev_g, gyro_dps = "right", 1.0, 1.125, 0.5, 0.0

    sink = motion_connector._ImuMotionSink(src)
    sink.on_scan_start(None)
    sink.on_complete()          # the runner calls it on every sink
    sink.consume("diagnostics", Bin())          # other channels ignored
    sink.consume("imu_motion", Bin())
    sink.consume("imu_motion", object())        # malformed: dropped quietly
    assert bands(src, "right", 0.0, 2.0, 0.1, 20.0) == [
        pytest.approx([1.0, 1.125])]


def test_replay_loader_maps_db_rows_to_sides():
    class DB:
        def get_imu_motion(self, session_id):
            assert session_id == 7
            return [
                {"side": 0, "t_start_s": 0.0, "t_end_s": 0.125,
                 "accel_dev_g": 0.2, "gyro_dps": 3.0},
                {"side": 1, "t_start_s": 0.125, "t_end_s": 0.25,
                 "accel_dev_g": 0.0, "gyro_dps": 40.0},
                {"side": 1},                            # malformed: skipped
            ]

    assert motion_connector._load_motion_bins(DB(), 7) == {
        "left": [(0.0, 0.125, 0.2, 3.0)],
        "right": [(0.125, 0.25, 0.0, 40.0)],
    }


def test_replay_loader_tolerates_old_sdks_and_errors():
    assert motion_connector._load_motion_bins(object(), 1) == {}

    class Broken:
        def get_imu_motion(self, session_id):
            raise RuntimeError("no table")

    assert motion_connector._load_motion_bins(Broken(), 1) == {}
