"""LSL output streams (lsl_output.LslStreamSink).

The sink is driven with hand-built pipeline payloads and a fake outlet
factory, so nothing touches the network. One test round-trips through a
real pylsl outlet/inlet on localhost when pylsl is installed.
"""

import math
from types import SimpleNamespace

import numpy as np
import pytest

from omotion.pipeline.batch import SideAverageSample
from omotion.pipeline.sinks import _NORMAL_HEADERS, _REDUCED_HEADERS
from omotion.pipeline.stages.dark import (
    EnrichedCorrectedFrame,
    EnrichedCorrectedInterval,
)

import lsl_output
from lsl_output import (
    CORRECTED_STREAM_NAME,
    LIVE_STREAM_NAME,
    LslStreamSink,
    channel_metadata,
    stream_columns,
)

pytestmark = pytest.mark.unit


class _FakeOutlet:
    def __init__(self, name, columns, source_id, desc):
        self.name, self.columns = name, columns
        self.source_id, self.desc = source_id, desc
        self.rows, self.stamps = [], []

    def push_chunk(self, rows, stamps):
        assert len(rows) == len(stamps)
        assert all(len(r) == len(self.columns) for r in rows)
        self.rows.extend(list(r) for r in rows)
        self.stamps.extend(stamps)

    def row(self, i):
        return dict(zip(self.columns, self.rows[i]))


def _meta(left=0b00000011, right=0, reduced=False):
    return SimpleNamespace(
        scan_id="20261106_120000", subject_id="owSECRET", operator="",
        started_at_iso="", duration_sec=60, left_camera_mask=left,
        right_camera_mask=right, reduced_mode=reduced)


def _sink(meta):
    outlets = {}

    def factory(name, columns, source_id, desc):
        outlets[name] = _FakeOutlet(name, columns, source_id, desc)
        return outlets[name]

    sink = LslStreamSink(outlet_factory=factory, clock=lambda: 1000.0)
    sink.on_scan_start(meta)
    return sink, outlets


def _live_batch(rows):
    """rows: (abs_frame_id, t, side, cam, frame_type, bfi)."""
    n = len(rows)
    shape = (n, 2, 8)
    bfi = np.full(shape, np.nan, np.float32)
    bvi = np.full(shape, np.nan, np.float32)
    mean = np.full(shape, np.nan, np.float32)
    contrast = np.full(shape, np.nan, np.float32)
    temp = np.full(shape, np.nan, np.float32)
    for i, (_, _, s, c, _, v) in enumerate(rows):
        bfi[i, s, c] = v
        bvi[i, s, c] = v + 0.5
        mean[i, s, c] = 100 + v
        contrast[i, s, c] = 0.25
        temp[i, s, c] = 31.0
    return SimpleNamespace(
        bfi_live=bfi, bvi_live=bvi, mean_dc_rt=mean, contrast_sn_rt=contrast,
        temperature_c=temp,
        abs_frame_ids=np.array([r[0] for r in rows]),
        timestamp_s=np.array([r[1] for r in rows], dtype=np.float64),
        side_ids=np.array([r[2] for r in rows]),
        cam_ids=np.array([r[3] for r in rows]),
        frame_type=np.array([r[4] for r in rows]),
    )


def _frame(fid, t, side, cam, v, temp=30.0):
    return EnrichedCorrectedFrame(
        abs_frame_id=fid, t=t, side=side, cam_id=cam, mean=100 + v, std=1.0,
        contrast=0.2, bfi=v, bvi=v + 1, temp_c=temp)


# ── Schema ───────────────────────────────────────────────────────────────

def test_columns_are_the_corrected_csv_header():
    assert stream_columns(False) == list(_NORMAL_HEADERS)
    assert stream_columns(True) == list(_REDUCED_HEADERS)
    assert len(stream_columns(False)) == 82
    assert stream_columns(True) == [
        "frame_id", "timestamp_s", "bfi_left", "bfi_right", "bvi_left", "bvi_right"]


def test_columns_match_the_documented_csv_layout():
    """docs/csv-formats.md: frame_id, timestamp_s, then bfi, bvi, mean,
    contrast, temp, each l1..l8 then r1..r8."""
    expected = ["frame_id", "timestamp_s"] + [
        f"{m}_{s}{c}" for m in ("bfi", "bvi", "mean", "contrast", "temp")
        for s in "lr" for c in range(1, 9)]
    assert stream_columns(False) == expected


@pytest.mark.parametrize("label,unit,side,camera", [
    ("frame_id", "count", None, None),
    ("timestamp_s", "s", None, None),
    ("bfi_l3", "index", "left", "3"),
    ("mean_r8", "DN", "right", "8"),
    ("contrast_l1", "unitless", "left", "1"),
    ("temp_r2", "celsius", "right", "2"),
    ("bvi_right", "index", "right", None),
])
def test_channel_metadata(label, unit, side, camera):
    meta = channel_metadata(label)
    assert meta["label"] == label
    assert meta["unit"] == unit
    assert meta.get("side") == side
    assert meta.get("camera") == camera


def test_streams_are_named_and_carry_no_subject_id():
    _, outlets = _sink(_meta())
    assert set(outlets) == {LIVE_STREAM_NAME, CORRECTED_STREAM_NAME}
    live = outlets[LIVE_STREAM_NAME]
    assert live.source_id == "openmotion-bloodflow-live-research"
    assert outlets[CORRECTED_STREAM_NAME].source_id == (
        "openmotion-bloodflow-corrected-research")
    assert live.desc["values"] == "live"
    assert outlets[CORRECTED_STREAM_NAME].desc["values"] == "corrected"
    # The streams are visible to anyone on the network.
    for o in outlets.values():
        assert "owSECRET" not in repr(o.desc)


# ── Live stream ──────────────────────────────────────────────────────────

def test_live_row_is_sent_once_every_scanned_camera_delivered():
    sink, outlets = _sink(_meta(left=0b11))
    live = outlets[LIVE_STREAM_NAME]
    sink.consume("live", _live_batch([(10, 0.225, 0, 0, "light", 4.0)]))
    assert live.rows == []  # camera 2 still missing
    sink.consume("live", _live_batch([(10, 0.226, 0, 1, "light", 5.0)]))
    assert len(live.rows) == 1
    row = live.row(0)
    assert row["frame_id"] == 10
    assert row["timestamp_s"] == pytest.approx(0.225)
    assert row["bfi_l1"] == 4.0 and row["bfi_l2"] == 5.0
    assert row["bvi_l1"] == 4.5
    assert row["mean_l2"] == 105.0
    assert row["contrast_l1"] == pytest.approx(0.25)
    assert row["temp_l1"] == 31.0
    assert math.isnan(row["bfi_r1"])  # not scanned → empty cell → NaN


def test_live_skips_warmup_and_stale_frames():
    sink, outlets = _sink(_meta(left=0b1))
    sink.consume("live", _live_batch([
        (3, 0.05, 0, 0, "warmup", 1.0),
        (4, 0.075, 0, 0, "stale", 1.0),
        (10, 0.225, 0, 0, "dark", 2.0),
        (11, 0.25, 0, 0, "light", 3.0),
    ]))
    assert [r[0] for r in outlets[LIVE_STREAM_NAME].rows] == [10, 11]


def test_live_does_not_stall_behind_a_dropped_camera_frame():
    sink, outlets = _sink(_meta(left=0b11))
    live = outlets[LIVE_STREAM_NAME]
    # Camera 2 never delivers frame 10; camera 1 keeps going.
    rows = [(10, 0.225, 0, 0, "light", 1.0)]
    rows += [(f, 0.025 * f, s, c, "light", 1.0)
             for f in range(11, 11 + lsl_output.LIVE_MAX_LAG_FRAMES)
             for s, c in ((0, 0), (0, 1))]
    sink.consume("live", _live_batch(rows))
    ids = [r[0] for r in live.rows]
    assert ids[0] == 10 and ids == sorted(ids)
    assert math.isnan(live.row(0)["bfi_l2"])
    assert len(ids) == lsl_output.LIVE_MAX_LAG_FRAMES + 1


def test_live_frame_no_camera_delivered_is_skipped_not_a_stall():
    sink, outlets = _sink(_meta(left=0b1))
    live = outlets[LIVE_STREAM_NAME]
    sink.consume("live", _live_batch([(10, 0.225, 0, 0, "light", 1.0)]))
    # frame 11 missing entirely; rows after it are complete
    sink.consume("live", _live_batch(
        [(f, 0.025 * f, 0, 0, "light", 1.0) for f in range(12, 15)]))
    assert [r[0] for r in live.rows] == [10, 12, 13, 14]


def test_live_reduced_layout_from_side_averages():
    sink, outlets = _sink(_meta(left=0b11000011, right=0b11000011, reduced=True))
    live = outlets[LIVE_STREAM_NAME]
    assert live.columns == list(_REDUCED_HEADERS)
    sink.consume("live", _live_batch([(10, 0.2, 0, 0, "light", 9.0)]))  # ignored
    sink.consume("live_side", SideAverageSample(t=0.25, frame_id=11, side=0,
                                                bfi=4.0, bvi=6.0))
    assert live.rows == []
    sink.consume("live_side", SideAverageSample(t=0.251, frame_id=11, side=1,
                                                bfi=5.0, bvi=7.0))
    assert live.rows == [[11.0, 0.25, 4.0, 5.0, 6.0, 7.0]]


# ── Corrected stream ─────────────────────────────────────────────────────

def test_corrected_rows_match_what_the_csv_would_hold():
    sink, outlets = _sink(_meta(left=0b1, right=0b1))
    corrected = outlets[CORRECTED_STREAM_NAME]
    left = EnrichedCorrectedInterval(left_abs=10, right_abs=13, frames=[
        _frame(f, 0.025 * f, "left", 0, float(f)) for f in range(10, 13)])
    right = EnrichedCorrectedInterval(left_abs=10, right_abs=13, frames=[
        _frame(f, 0.025 * f + 0.002, "right", 0, float(f) + 0.5,
               temp=None) for f in range(10, 13)])
    sink.consume("final", left)
    assert corrected.rows == []  # waiting for the right module
    sink.consume("final", right)
    assert [r[0] for r in corrected.rows] == [10, 11, 12]
    row = corrected.row(1)
    assert row["timestamp_s"] == pytest.approx(0.275)  # earliest camera
    assert row["bfi_l1"] == 11.0 and row["bfi_r1"] == 11.5
    assert row["bvi_l1"] == 12.0
    assert row["mean_r1"] == 111.5
    assert row["contrast_l1"] == pytest.approx(0.2)
    assert row["temp_l1"] == 30.0
    assert math.isnan(row["temp_r1"])  # None → empty cell
    assert outlets[LIVE_STREAM_NAME].rows == []


def test_corrected_reduced_layout_reads_only_side_averages():
    sink, outlets = _sink(_meta(left=0b1, right=0b1, reduced=True))
    corrected = outlets[CORRECTED_STREAM_NAME]
    frames = [_frame(10, 0.225, "left", 0, 99.0),  # per-camera: ignored
              _frame(10, 0.225, "left", -1, 4.0),
              _frame(10, 0.226, "right", -1, 5.0)]
    sink.consume("final", EnrichedCorrectedInterval(10, 11, frames))
    assert corrected.rows == [[10.0, 0.225, 4.0, 5.0, 5.0, 6.0]]


def test_on_complete_flushes_partial_rows():
    sink, outlets = _sink(_meta(left=0b1, right=0b1))
    sink.consume("final", EnrichedCorrectedInterval(10, 12, [
        _frame(10, 0.225, "left", 0, 1.0), _frame(11, 0.25, "left", 0, 2.0)]))
    assert outlets[CORRECTED_STREAM_NAME].rows == []
    sink.on_complete()
    rows = outlets[CORRECTED_STREAM_NAME].rows
    assert [r[0] for r in rows] == [10, 11]
    assert math.isnan(rows[0][_NORMAL_HEADERS.index("bfi_r1")])


# ── Timestamps / failure handling ────────────────────────────────────────

def test_live_and_corrected_share_one_lsl_time_base():
    sink, outlets = _sink(_meta(left=0b1))
    sink.consume("live", _live_batch([(10, 0.5, 0, 0, "light", 1.0),
                                      (11, 0.525, 0, 0, "light", 1.0)]))
    sink.consume("final", EnrichedCorrectedInterval(10, 12, [
        _frame(10, 0.5, "left", 0, 1.0), _frame(11, 0.525, "left", 0, 1.0)]))
    live = outlets[LIVE_STREAM_NAME].stamps
    corr = outlets[CORRECTED_STREAM_NAME].stamps
    assert live[0] == pytest.approx(1000.0)
    assert live == pytest.approx(corr)
    assert live[1] - live[0] == pytest.approx(0.025)


def test_a_failing_outlet_never_raises_into_the_scan(caplog):
    def factory(*_a):
        raise RuntimeError("no network")

    sink = LslStreamSink(outlet_factory=factory, clock=lambda: 0.0)
    sink.on_scan_start(_meta())
    sink.consume("live", _live_batch([(10, 0.2, 0, 0, "light", 1.0)]))
    sink.on_complete()
    assert "LSL output could not start" in caplog.text


def test_a_failing_push_disables_the_sink_for_the_scan():
    sink, outlets = _sink(_meta(left=0b1))
    calls = []

    def boom(rows, stamps):
        calls.append(1)
        raise OSError("gone")

    outlets[LIVE_STREAM_NAME].push_chunk = boom
    sink.consume("live", _live_batch([(10, 0.2, 0, 0, "light", 1.0)]))
    sink.consume("live", _live_batch([(11, 0.225, 0, 0, "light", 1.0)]))
    assert calls == [1]


# ── Real liblsl round trip ───────────────────────────────────────────────

def test_round_trip_through_liblsl():
    pylsl = pytest.importorskip("pylsl")
    sink = LslStreamSink(source_prefix="openmotion-unit-test")
    sink.on_scan_start(_meta(left=0b1, right=0, reduced=True))
    try:
        found = pylsl.resolve_byprop(
            "source_id", "openmotion-unit-test-live-clinical", timeout=5)
        assert found, "outlet not discoverable on localhost"
        inlet = pylsl.StreamInlet(found[0])
        info = inlet.info(timeout=5)
        assert info.channel_count() == 6
        assert info.channel_format() == pylsl.cf_double64
        ch = info.desc().child("channels").first_child()
        labels = []
        while not ch.empty():
            labels.append(ch.child_value("label"))
            ch = ch.next_sibling()
        assert labels == list(_REDUCED_HEADERS)
        inlet.open_stream(timeout=5)
        sink.consume("live_side", SideAverageSample(t=0.25, frame_id=11, side=0,
                                                    bfi=4.0, bvi=6.0))
        sample, ts = inlet.pull_sample(timeout=5)
        assert sample[:3] == [11.0, 0.25, 4.0]
        assert math.isnan(sample[3])
        assert ts > 0
    finally:
        sink.on_complete()


# ── Packaging ────────────────────────────────────────────────────────────

def test_every_build_bundles_liblsl():
    """pylsl finds liblsl only at pylsl/lib/ inside the bundle: a build that
    leaves it out ships an LSL switch that always reports 'unavailable'."""
    from pathlib import Path

    import test_pyinstaller_spec as specs

    root = Path(__file__).resolve().parents[1]
    assert 'collect_dynamic_libs("pylsl")' in (root / "openwater.spec").read_text()
    assert 'collect_dynamic_libs("pylsl")' in specs._macos_spec_source()
    nuitka = (root / "scripts" / "build_nuitka.ps1").read_text()
    assert '"--include-package=pylsl"' in nuitka
    assert '"--include-raw-dir=$pylslStage=pylsl\\lib"' in nuitka
    assert any(line.startswith("pylsl==")
               for line in (root / "requirements.txt").read_text().splitlines())


def test_host_cannot_redirect_liblsl_through_the_environment():
    import main
    assert {"PYLSL_LIB", "LSLAPICFG"} <= set(main._QT_ENV_SCRUB)
