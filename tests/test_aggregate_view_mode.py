"""
Issue #621 — research "Aggregate" plot view.

A third choice in the plot's ⋯ menu between Individual and Average: each
module's eight cameras fold into four plots, one per mirrored camera pair
(1+8, 2+7, 3+6, 4+5), i.e. one per row of the U-shaped Individual grid.
The pair streams are derived on the display side alongside the Average
view's side average (#606), under cam_ids 8..11 so nothing that walks the
physical cameras sees them.

Covered here:
  - the bulk derivation (past scans, sample CSV, DB tail) and the live
    accumulator: NaN-aware per-capture pair means, single-camera pairs,
    the BVI low-pass on each pair stream, masks/mode unaffected;
  - autoscale: the Individual and Average fits ignore the pair streams,
    the Aggregate fit uses only them, per-plot fits each pair cell;
  - PlotViewer.qml: the grid, labels, badge, clinical gate, fallbacks.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import math
import time

# test_average_view_mode creates the offscreen QGuiApplication at import
# (it must exist before conftest's bare QCoreApplication); reuse its stubs.
from test_average_view_mode import (  # noqa: E402
    ALL_MASK,
    PER_CAM,
    _FakeScanDB,
    _StubLiveSource,
    _StubMotionInterface,
    _arr,
    _basic_controls_style,
    _buf,
    _cell_model,
    _cells,
    _row,
    _walk,
)

import pytest  # noqa: E402
from PyQt6.QtCore import QCoreApplication, QUrl, pyqtSlot  # noqa: E402
from PyQt6.QtQml import (  # noqa: E402
    QQmlComponent,
    QQmlEngine,
    qmlRegisterSingletonInstance,
)

from data_sources import (  # noqa: E402
    AGGREGATE_CAM_BASE,
    AGGREGATE_PAIRS,
    LiveScanSource,
    PastScanSource,
    bvi_lpf_alpha,
    derive_aggregate_buffers,
    is_aggregate_cam_id,
    load_csv_scan_buffers,
    load_past_scan_buffers,
)
from test_average_view_mode import PLOT_VIEWER_QML, SAMPLE_SCAN_CSV  # noqa: E402

pytestmark = pytest.mark.unit

NAN = float("nan")
PAIR_18, PAIR_27, PAIR_36, PAIR_45 = (AGGREGATE_CAM_BASE + p for p in range(4))


# ── Numbering ───────────────────────────────────────────────────────────


def test_pairs_are_the_mirrored_cameras_1_8_2_7_3_6_4_5():
    one_based = [(a + 1, b + 1) for a, b in AGGREGATE_PAIRS]
    assert one_based == [(1, 8), (2, 7), (3, 6), (4, 5)]
    assert [c for c in range(-1, 16) if is_aggregate_cam_id(c)] \
        == [PAIR_18, PAIR_27, PAIR_36, PAIR_45]
    # Clear of the physical cameras and of the side average.
    assert AGGREGATE_CAM_BASE >= 8


# ── Bulk derivation ─────────────────────────────────────────────────────


def test_derive_averages_each_pair_per_capture():
    buffers = {
        ("left", 0, "bfi"): _buf([(0.000, 1.0, 10), (0.025, 2.0, 11)]),
        ("left", 7, "bfi"): _buf([(0.001, 3.0, 10), (0.026, 6.0, 11)]),
        ("left", 3, "bfi"): _buf([(0.002, 5.0, 10)]),
        ("left", 4, "bfi"): _buf([(0.003, 9.0, 10)]),
        ("right", 1, "bfi"): _buf([(0.000, 7.0, 10)]),
    }
    out = derive_aggregate_buffers(buffers)
    assert set(out) == {("left", PAIR_18, "bfi"), ("left", PAIR_45, "bfi"),
                        ("right", PAIR_27, "bfi")}
    t, v, fid = _arr(out[("left", PAIR_18, "bfi")])
    assert fid == [10, 11]
    assert v == pytest.approx([2.0, 4.0])
    assert t == pytest.approx([0.000, 0.025])     # earliest camera stamp
    assert _arr(out[("left", PAIR_45, "bfi")])[1] == pytest.approx([7.0])
    # A pair with one recorded camera is that camera.
    assert _arr(out[("right", PAIR_27, "bfi")])[1] == pytest.approx([7.0])
    assert all(b.derived for b in out.values())


def test_derive_pair_is_nan_aware():
    buffers = {
        ("left", 2, "bvi"): _buf([(0.0, NAN, 1), (0.025, NAN, 2)]),
        ("left", 5, "bvi"): _buf([(0.0, 6.0, 1), (0.025, NAN, 2)]),
    }
    _t, v, _f = _arr(derive_aggregate_buffers(buffers)[("left", PAIR_36, "bvi")])
    assert v[0] == pytest.approx(6.0)
    assert math.isnan(v[1])


def test_past_research_scan_gets_pairs_without_changing_masks_or_mode():
    db = _FakeScanDB([
        _row(0, 0, 1, 0.0, 1.0), _row(0, 7, 1, 0.0, 3.0),
        _row(0, 1, 1, 0.0, 5.0),
        _row(0, 0, 2, 0.025, 2.0), _row(0, 7, 2, 0.025, 4.0),
        _row(0, 1, 2, 0.025, 6.0),
    ])
    buffers, _ = load_past_scan_buffers(db, 1, derive_side_average=True)
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers=buffers)
    try:
        assert src.clinicalMode == 0
        assert src.leftMask == 0x83           # pair streams don't widen it
        assert src.value_at("left", PAIR_18, "bfi", 0.025) == pytest.approx(3.0)
        assert src.value_at("left", PAIR_27, "bfi", 0.025) == pytest.approx(6.0)
        # The side average is still over all three cameras, not the pairs.
        assert src.value_at("left", -1, "bfi", 0.025) == pytest.approx(4.0)
        for m in ("bfi", "bvi", "mean", "contrast"):
            assert ("left", PAIR_18, m) in src.buffers
        assert not any(k[1] in (PAIR_36, PAIR_45) for k in src.buffers)
    finally:
        src.release()


def test_clinical_recording_gets_no_pairs():
    db = _FakeScanDB([_row(0, -1, 1, 0.0, 7.0)])
    buffers, _ = load_past_scan_buffers(db, 1, derive_side_average=True)
    assert not any(is_aggregate_cam_id(k[1]) for k in buffers)


def test_loader_flag_off_derives_nothing():
    db = _FakeScanDB([_row(0, 0, 1, 0.0, 1.0)])
    buffers, _ = load_past_scan_buffers(db, 1)
    assert not any(is_aggregate_cam_id(k[1]) for k in buffers)


@pytest.mark.skipif(not SAMPLE_SCAN_CSV.exists(), reason="no sample scan")
def test_sample_scan_offers_the_aggregate_view():
    buffers = load_csv_scan_buffers(str(SAMPLE_SCAN_CSV),
                                    derive_side_average=True)
    pairs = [k for k in buffers if is_aggregate_cam_id(k[1]) and k[2] == "bfi"]
    assert pairs
    assert all(buffers[k].n > 0 and buffers[k].derived for k in pairs)


def test_db_tail_window_derives_pairs():
    """The live source's DB-tail window (history past the in-memory ring)
    carries the pair streams too, so the Aggregate view pans back like
    the others."""
    src = LiveScanSource(plot_t0=0.0, derive_side_average=True)
    src._db = _FakeScanDB([_row(1, 3, 1, 0.0, 2.0), _row(1, 4, 1, 0.0, 4.0)])
    src._db_session_id = 1
    try:
        src._db_window_load(0.0, 1.0)
        dbuf = src._db_window_buffers[("right", PAIR_45, "bfi")]
        assert _arr(dbuf)[1] == pytest.approx([3.0])
    finally:
        src._db = None
        src.release()


# ── Autoscale (data side) ───────────────────────────────────────────────


def test_each_fit_sees_only_its_own_streams():
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers={
        ("left", 0, "bfi"): _buf([(i * 0.025, float(i), i) for i in range(100)]),
        ("left", -1, "bfi"): _buf([(i * 0.025, 500.0 + i, i)
                                   for i in range(100)], derived=True),
        ("left", PAIR_18, "bfi"): _buf([(i * 0.025, 1000.0 + i, i)
                                        for i in range(100)], derived=True),
    })
    try:
        assert src.compute_bounds_for_metric("bfi")["yMax"] < 200.0
        avg = src.compute_bounds_for_side_average("bfi")
        assert 400.0 < avg["yMin"] and avg["yMax"] < 700.0
        agg = src.compute_bounds_for_aggregate("bfi")
        assert agg["yMin"] > 900.0
        # Per-plot fits a pair cell from its own stream.
        cell = src.compute_bounds_for_cell("left", PAIR_18, "bfi")
        assert cell["yMin"] > 900.0
    finally:
        src.release()


# ── Live accumulator ────────────────────────────────────────────────────


def test_live_pairs_emit_with_the_side_average():
    src = LiveScanSource(plot_t0=0.0, derive_side_average=True)
    try:
        src.append_uncorrected("left", 0, 1, 0.000, bfi=1.0, bvi=4.0,
                               mean=100.0, contrast=0.2)
        src.append_uncorrected("left", 7, 1, 0.001, bfi=3.0, bvi=6.0,
                               mean=200.0, contrast=0.4)
        src.append_uncorrected("left", 1, 1, 0.002, bfi=8.0, bvi=2.0)
        assert not any(is_aggregate_cam_id(k[1]) for k in src.buffers)
        src.append_uncorrected("left", 0, 2, 0.025, bfi=5.0, bvi=8.0)
        assert _arr(src.buffers[("left", PAIR_18, "bfi")]) == ([0.0], [2.0], [1])
        assert src.value_at("left", PAIR_18, "bvi", 0.0) == pytest.approx(5.0)
        assert src.value_at("left", PAIR_18, "mean", 0.0) == pytest.approx(150.0)
        # 2+7 had only camera 2, and no mean/contrast from it.
        assert src.value_at("left", PAIR_27, "bfi", 0.0) == pytest.approx(8.0)
        assert ("left", PAIR_27, "mean") not in src.buffers
        # Pairs with no camera in the capture get no stream at all.
        assert ("left", PAIR_36, "bfi") not in src.buffers
        # The side average is unchanged by the pairs: all three cameras.
        assert src.value_at("left", -1, "bfi", 0.0) == pytest.approx(4.0)
        assert src.buffers[("left", PAIR_18, "bfi")].derived

        src.flush_side_average()
        assert _arr(src.buffers[("left", PAIR_18, "bfi")])[1] \
            == pytest.approx([2.0, 5.0])
        assert src.buffers[("left", PAIR_27, "bfi")].n == 1
    finally:
        src.release()


def test_live_pair_is_nan_aware_and_keeps_all_unlit_as_nan():
    src = LiveScanSource(plot_t0=0.0, derive_side_average=True)
    try:
        src.append_uncorrected("right", 3, 1, 0.0, bfi=NAN, bvi=NAN)
        src.append_uncorrected("right", 4, 1, 0.0, bfi=4.0, bvi=2.0)
        src.append_uncorrected("right", 2, 1, 0.0, bfi=NAN, bvi=NAN)
        src.flush_side_average()
        assert src.value_at("right", PAIR_45, "bfi", 0.0) == pytest.approx(4.0)
        assert math.isnan(src.value_at("right", PAIR_36, "bfi", 0.0))
        assert src.buffers[("right", PAIR_36, "bfi")].n == 1
    finally:
        src.release()


def test_live_pair_bvi_is_averaged_raw_then_low_passed_per_pair():
    src = LiveScanSource(plot_t0=0.0, derive_side_average=True,
                         bvi_lpf_cutoff_hz=20.0)
    alpha = bvi_lpf_alpha(20.0)
    try:
        for fid, (a, b) in enumerate([(2.0, 4.0), (6.0, 8.0)]):
            src.append_uncorrected("left", 0, fid, fid * 0.025, bfi=1.0, bvi=a)
            src.append_uncorrected("left", 7, fid, fid * 0.025, bfi=1.0, bvi=b)
        src.flush_side_average()
        v = _arr(src.buffers[("left", PAIR_18, "bvi")])[1]
        assert v[0] == pytest.approx(3.0)
        assert v[1] == pytest.approx(3.0 + alpha * (7.0 - 3.0))
    finally:
        src.release()


def test_clinical_live_source_derives_no_pairs():
    src = LiveScanSource(plot_t0=0.0, derive_side_average=False)
    try:
        src.append_uncorrected("left", 0, 1, 0.0, bfi=1.0, bvi=1.0)
        src.append_uncorrected("left", 7, 2, 0.025, bfi=1.0, bvi=1.0)
        src.flush_side_average()
        assert not any(is_aggregate_cam_id(k[1]) for k in src.buffers)
    finally:
        src.release()


# ── PlotViewer.qml ──────────────────────────────────────────────────────

AGGREGATE = {"bfi": (20.0, 30.0), "bvi": (70.0, 80.0)}
PAIR_CELL = {"bfi": (1.0, 2.0), "bvi": (3.0, 4.0)}


class _StubAggregateSource(_StubLiveSource):
    """_StubLiveSource plus the Aggregate slot and a real per-cell fit."""

    @pyqtSlot(str, result="QVariantMap")
    def compute_bounds_for_aggregate(self, metric):
        y_min, y_max = AGGREGATE.get(metric, (0.0, 1.0))
        return {"yMin": y_min, "yMax": y_max}

    @pyqtSlot(str, int, str, result="QVariantMap")
    @pyqtSlot(str, int, str, float, float, result="QVariantMap")
    def compute_bounds_for_cell(self, side, cam_id, metric, t_lo=None, t_hi=None):
        y_min, y_max = PAIR_CELL.get(metric, (0.0, 1.0))
        return {"yMin": y_min, "yMax": y_max}


@pytest.fixture(scope="module")
def viewer_factory():
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(PLOT_VIEWER_QML)))
    if component.isError():
        raise RuntimeError(
            "PlotViewer.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors()))
    created = []

    def make():
        obj = component.create()
        if obj is None:
            raise RuntimeError(
                "PlotViewer.qml failed to instantiate:\n"
                + "\n".join(e.toString() for e in component.errors()))
        created.append(obj)
        return obj

    make.stub = stub
    yield make
    for obj in created:
        obj.deleteLater()
    del engine
    del stub


@pytest.fixture
def aggregate_viewer(viewer_factory):
    stub = viewer_factory.stub
    stub.setConfig("plotViewMode", "individual")
    stub.setConfig("autoScalePerPlot", False)
    viewer = viewer_factory()
    viewer.setProperty("clinicalMode", False)
    viewer.setProperty("leftMask", ALL_MASK)
    viewer.setProperty("rightMask", ALL_MASK)
    viewer.setProperty("autoScale", True)
    src = _StubAggregateSource()
    stub.setScanSource(src)
    try:
        yield viewer, src, stub
    finally:
        stub.setScanSource(None)
        stub.setConfig("plotViewMode", "individual")
        stub.setConfig("autoScalePerPlot", False)


def _layout(viewer):
    val = viewer.property("_activeCellModel")
    val = val.toVariant() if hasattr(val, "toVariant") else val
    return sorted((c["side"], int(c["camId"]), int(c["row"]), int(c["col"]))
                  for c in val)


def _pump(ms=150):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def test_aggregate_view_folds_each_grid_row_into_one_plot(aggregate_viewer):
    viewer, src, stub = aggregate_viewer
    stub.setConfig("plotViewMode", "aggregate")      # the ⋯ popup button
    assert viewer.property("researchViewMode") == "aggregate"
    assert viewer.property("aggregateView") is True
    assert viewer.property("averageView") is False
    # Rows follow the Individual grid: 4+5 on top, 1+8 at the bottom;
    # left module col 0, right col 2, the side-break spacer in col 1.
    assert _layout(viewer) == sorted(
        (side, AGGREGATE_CAM_BASE + p, 3 - p, col)
        for side, col in (("left", 0), ("right", 2)) for p in range(4))
    assert len(_cells(viewer)) == 8
    assert viewer.property("_sideGapActive") is True
    assert viewer.property("_sideGapCol") == 1
    assert viewer.property("showCellValues") is True
    assert viewer.property("_showClinicalPanels") is False
    # Global autoscale refits to the pair traces immediately.
    assert (viewer.property("primaryYMin"), viewer.property("primaryYMax")) \
        == AGGREGATE["bfi"]
    assert (viewer.property("secondaryYMin"),
            viewer.property("secondaryYMax")) == AGGREGATE["bvi"]


def test_aggregate_view_follows_the_masks(aggregate_viewer):
    """0x66 = cameras 2, 3, 6, 7 → pairs 2+7 and 3+6, compacted to the
    top; a single-module scan drops the spacer column."""
    viewer, src, stub = aggregate_viewer
    stub.setConfig("plotViewMode", "aggregate")
    viewer.setProperty("leftMask", 0x66)
    viewer.setProperty("rightMask", 0x00)
    assert _layout(viewer) == [("left", PAIR_27, 1, 0), ("left", PAIR_36, 0, 0)]
    assert viewer.property("_sideGapActive") is False


def test_a_pair_with_one_enabled_camera_keeps_its_plot(aggregate_viewer):
    viewer, src, stub = aggregate_viewer
    stub.setConfig("plotViewMode", "aggregate")
    viewer.setProperty("leftMask", 0x01)          # camera 1 only
    viewer.setProperty("rightMask", 0x80)         # camera 8 only
    assert _layout(viewer) == [("left", PAIR_18, 0, 0),
                               ("right", PAIR_18, 0, 2)]


def test_pair_cells_are_labelled_with_both_cameras(aggregate_viewer):
    viewer, src, stub = aggregate_viewer
    stub.setConfig("plotViewMode", "aggregate")
    labels = sorted(o.property("label") for o in _walk(viewer)
                    if o.metaObject().className().startswith("PlotCell"))
    assert labels == sorted(f"{s} {a}+{b}" for s in ("LEFT", "RIGHT")
                            for a, b in ((1, 8), (2, 7), (3, 6), (4, 5)))


def test_individual_cells_keep_their_camera_label(aggregate_viewer):
    viewer, src, stub = aggregate_viewer
    labels = {o.property("label") for o in _walk(viewer)
              if o.metaObject().className().startswith("PlotCell")}
    assert labels == {f"{s} {c}" for s in ("LEFT", "RIGHT") for c in range(1, 9)}


def test_pair_badge_shows_when_either_camera_is_lost(aggregate_viewer):
    viewer, src, stub = aggregate_viewer
    stub.setConfig("plotViewMode", "aggregate")

    def lost():
        return sorted((o.property("side"), int(o.property("camId")))
                      for o in _walk(viewer)
                      if o.metaObject().className().startswith("PlotCell")
                      and o.property("connectionLost"))

    assert lost() == []
    viewer.setProperty("_lostCameras", {"left:7": True})   # camera 8
    assert lost() == [("left", PAIR_18)]
    viewer.setProperty("_lostCameras", {"right:3": True})  # camera 4
    assert lost() == [("right", PAIR_45)]


def test_per_plot_scale_fits_each_pair_cell(aggregate_viewer):
    viewer, src, stub = aggregate_viewer
    stub.setConfig("plotViewMode", "aggregate")
    stub.setConfig("autoScalePerPlot", True)
    _pump()
    bounds = viewer.property("_cellBounds")
    bounds = bounds.toVariant() if hasattr(bounds, "toVariant") else bounds
    assert set(bounds) == {f"{s}:{AGGREGATE_CAM_BASE + p}"
                           for s in ("left", "right") for p in range(4)}


def test_switching_back_restores_the_camera_grid(aggregate_viewer):
    viewer, src, stub = aggregate_viewer
    stub.setConfig("plotViewMode", "aggregate")
    stub.setConfig("plotViewMode", "individual")
    assert len(_cells(viewer)) == 16
    assert viewer.property("_sideGapCol") == 2
    assert (viewer.property("primaryYMin"), viewer.property("primaryYMax")) \
        == PER_CAM["bfi"]


def test_aggregate_to_average_and_back(aggregate_viewer):
    viewer, src, stub = aggregate_viewer
    stub.setConfig("plotViewMode", "aggregate")
    stub.setConfig("plotViewMode", "average")
    assert _cell_model(viewer) == [("left", -1), ("right", -1)]
    stub.setConfig("plotViewMode", "aggregate")
    assert len(_cells(viewer)) == 8


def test_clinical_ignores_a_stale_aggregate_preference(aggregate_viewer):
    viewer, src, stub = aggregate_viewer
    viewer.setProperty("clinicalMode", True)
    clinical_cells = _cell_model(viewer)
    stub.setConfig("plotViewMode", "aggregate")
    assert viewer.property("aggregateView") is False
    assert viewer.property("averageView") is True
    assert _cell_model(viewer) == clinical_cells
    # Still the per-camera fit. Checked on this viewer's own range: the
    # module-scoped stub is shared with earlier tests' viewers, so the
    # source's call log is not.
    assert (viewer.property("primaryYMin"), viewer.property("primaryYMax"))         == PER_CAM["bfi"]


def test_unknown_view_mode_reads_as_individual(aggregate_viewer):
    viewer, src, stub = aggregate_viewer
    stub.setConfig("plotViewMode", "bogus")
    assert viewer.property("researchViewMode") == "individual"
    assert len(_cells(viewer)) == 16


def test_qml_pair_numbering_matches_the_sources(aggregate_viewer):
    viewer, _src, _stub = aggregate_viewer
    assert viewer.property("_aggregateCamBase") == AGGREGATE_CAM_BASE


def test_view_menu_offers_aggregate_between_individual_and_average():
    """Static guard on the ⋯ popup (a Popup needs a window to open)."""
    qml = PLOT_VIEWER_QML.read_text(encoding="utf-8")
    i = qml.index('value: "individual"')
    g = qml.index('value: "aggregate"')
    a = qml.index('value: "average"')
    assert i < g < a
