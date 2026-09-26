"""
Issue #635 — the plot viewer's Statistics pane.

A Statistics switch in the plot's ⋯ menu adds a pane right of the plots:
one row per plot the current view draws, grouped LEFT / RIGHT, each with
the plotted pair's live value and the same stream through a 0.5 Hz
low-pass, then the LEFT − RIGHT differential against the opposite side's
plot. The modules are rotated 180° on the body, so camera N faces camera
9 − N (L1 ↔ R8); Aggregate pairs face the same pair (L1+8 ↔ R1+8) and the
Average view pairs L ↔ R. While the pane is on, the per-cell value labels
(and the Average view's side panels) are off.

Covered here:
  - the low-pass: lowpass_last against a running IIR (NaN samples hold the
    state), its window length, and statistics_at on past and live sources;
  - PlotViewer.qml with a real PastScanSource: gating (research only), the
    cell labels / side panels giving way, the pane's rows and differentials
    in the Individual, Aggregate and Average views, a missing opposite
    plot, display clamping, the ⋯ switch and the overlay inset;
  - the config key.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import math
import re
from pathlib import Path

# test_average_view_mode creates the offscreen QGuiApplication at import
# (it must exist before conftest's bare QCoreApplication); reuse its stubs.
from test_average_view_mode import (  # noqa: E402
    PLOT_VIEWER_QML,
    _StubMotionInterface,
    _basic_controls_style,
)
from test_plot_menu import _all_objects, _pick, _segmented  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PyQt6.QtCore import (  # noqa: E402
    Q_ARG,
    Q_RETURN_ARG,
    QCoreApplication,
    QEvent,
    QMetaObject,
    QUrl,
    pyqtSlot,
)
from PyQt6.QtQml import (  # noqa: E402
    QQmlComponent,
    QQmlEngine,
    qmlRegisterSingletonInstance,
)
from PyQt6 import sip  # noqa: E402

from config import app_config  # noqa: E402
from data_sources import (  # noqa: E402
    LiveScanSource,
    PastScanSource,
    _CameraBuffer,
    _add_derived_side_averages,
    bvi_lpf_alpha,
    lowpass_last,
    lowpass_window_sec,
)

pytestmark = pytest.mark.unit

NAN = float("nan")
HZ = 40.0
ALPHA = bvi_lpf_alpha(0.5)


def _running_iir(values, alpha):
    """Reference: the filter run sample by sample, as _bvi_lpf does."""
    y = None
    for x in values:
        if not math.isfinite(x):
            continue
        y = x if y is None else y + alpha * (x - y)
    return NAN if y is None else y


# ── The low-pass ────────────────────────────────────────────────────────


def test_lowpass_last_matches_the_running_filter_across_nan_gaps():
    rng = np.random.default_rng(635)
    x = 5.0 + np.sin(np.arange(400) / 7.0) + rng.normal(0.0, 0.3, 400)
    x[[0, 50, 51, 52, 300]] = np.nan
    assert lowpass_last(x, ALPHA) == pytest.approx(_running_iir(x, ALPHA), rel=1e-12)


@pytest.mark.parametrize("values, expected", [
    ([], NAN),
    ([NAN, NAN], NAN),
    ([3.0], 3.0),
    ([NAN, 2.0, NAN], 2.0),
])
def test_lowpass_last_edge_cases(values, expected):
    got = lowpass_last(np.array(values, dtype=float), ALPHA)
    if math.isnan(expected):
        assert math.isnan(got)
    else:
        assert got == pytest.approx(expected)


def test_lowpass_last_with_the_filter_off_is_the_last_finite_value():
    assert lowpass_last(np.array([1.0, 4.0, NAN]), 1.0) == 4.0


def test_window_is_long_enough_to_forget_the_seed():
    """The windowed filter is seeded with the window's first sample; the
    window must be long enough that the seed no longer shows."""
    window = lowpass_window_sec(ALPHA)
    n = round(window * HZ)              # samples the window always holds
    assert (1.0 - ALPHA) ** (n - 1) < 1e-6
    assert 4.0 < window < 5.0           # 0.5 Hz: ~4.6 s of samples


def _buf(values, t0=0.0):
    b = _CameraBuffer(max_capacity=None)
    for i, v in enumerate(values):
        b.append(t=t0 + i / HZ, v=float(v), frame_id=i)
    return b


def test_statistics_at_matches_a_filter_run_from_scan_start():
    """20 s of a pulsing trace: the windowed low-pass at the live edge
    equals the filter run over the whole scan (to float32 storage)."""
    t = np.arange(800) / HZ
    bfi = 4.0 + np.sin(2 * np.pi * 1.2 * t) + 0.02 * t
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers={
        ("left", 2, "bfi"): _buf(bfi),
        ("left", 2, "bvi"): _buf(np.full(800, 3.0)),
    })
    try:
        edge = src.liveEdge
        out = src.statistics_at(["left:2", "right:5"], ["bfi", "bvi"], edge, 0.5)
        row = out["left:2"]
        stored = src.buffers[("left", 2, "bfi")].v[:800].astype(float)
        assert row["bfi_lpf"] == pytest.approx(_running_iir(stored, ALPHA), abs=1e-5)
        assert row["bfi"] == pytest.approx(src.value_at("left", 2, "bfi", edge))
        # The slow filter visibly smooths the 1.2 Hz pulse away from the
        # latest sample; a constant stream passes through unchanged.
        assert abs(row["bfi_lpf"] - row["bfi"]) > 0.05
        assert row["bvi_lpf"] == pytest.approx(3.0)
        # A cell with no stream reads NaN rather than disappearing.
        assert all(math.isnan(v) for v in out["right:5"].values())
    finally:
        src.release()


def test_statistics_at_parses_derived_cam_ids_and_skips_bad_keys():
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers={
        ("left", -1, "bfi"): _buf([2.0] * 40),
        ("right", 8, "bfi"): _buf([6.0] * 40),
    })
    try:
        out = src.statistics_at(["left:-1", "right:8", "junk", "left:x"],
                                ["bfi"], src.liveEdge, 0.5)
        assert set(out) == {"left:-1", "right:8"}
        assert out["left:-1"]["bfi_lpf"] == pytest.approx(2.0)
        assert out["right:8"]["bfi"] == pytest.approx(6.0)
    finally:
        src.release()


def test_statistics_at_on_a_live_source_reads_the_displayed_stream():
    src = LiveScanSource(plot_t0=0.0)
    try:
        for i in range(200):
            src.append_uncorrected("right", 7, i, i / HZ,
                                   bfi=1.0 if i < 100 else 3.0, bvi=2.0)
        out = src.statistics_at(["right:7"], ["bfi", "bvi"], src.liveEdge, 0.5)
        assert out["right:7"]["bfi"] == pytest.approx(3.0)
        # 100 samples after a 1 → 3 step: 1 + 2 (1 - (1 - a)^100).
        expected = 1.0 + 2.0 * (1.0 - (1.0 - ALPHA) ** 100)
        assert out["right:7"]["bfi_lpf"] == pytest.approx(expected, abs=1e-6)
    finally:
        src.release()


# ── Config ──────────────────────────────────────────────────────────────


def test_show_statistics_is_a_persisted_preference_defaulting_off():
    assert app_config.APP_CONFIG["showStatistics"] is False
    assert app_config.tier_of("showStatistics") == app_config.PREFERENCE


# ── PlotViewer.qml ──────────────────────────────────────────────────────


def _left_bfi(c):
    return 1.0 + 0.1 * c * c          # 1.0 … 5.9, nonlinear so pairs differ


def _right_bfi(c):
    return 2.0 + 0.25 * c             # 2.0 … 3.75


def _bvi(side, c):
    return 0.5 + 0.2 * c if side == "left" else 1.0 + 0.1 * c


def _scan_source(left_cams=range(8), right_cams=range(8),
                 left_bfi=_left_bfi, right_bfi=_right_bfi):
    """A research past scan with one constant value per camera stream,
    plus the derived Average / Aggregate streams, as the loader builds
    it. Constant streams make the low-pass exact, so both columns carry
    the same known number."""
    buffers = {}
    for side, cams, bfi in (("left", left_cams, left_bfi),
                            ("right", right_cams, right_bfi)):
        for c in cams:
            buffers[(side, c, "bfi")] = _buf([bfi(c)] * 80)
            buffers[(side, c, "bvi")] = _buf([_bvi(side, c)] * 80)
    _add_derived_side_averages(buffers)
    return PastScanSource(scan_db=None, session_id=1, preloaded_buffers=buffers)


class _RecordingStub(_StubMotionInterface):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.writes = []
        self._config["showStatistics"] = False

    @pyqtSlot(str, "QVariant")
    def setConfig(self, key, value):
        self.writes.append((key, value))
        super().setConfig(key, value)


@pytest.fixture(scope="module")
def viewer_factory():
    stub = _RecordingStub()
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

    def destroy(obj):
        created.remove(obj)
        obj.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    make.stub = stub
    make.destroy = destroy
    yield make
    # Destroy the viewers while their engine is still alive (see
    # test_plot_menu's teardown).
    for obj in created:
        obj.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    del engine
    del stub


@pytest.fixture
def stats_viewer(viewer_factory):
    """A research viewer with statistics off, and a helper that shows a
    scan source on it."""
    stub = viewer_factory.stub
    stub.setConfig("plotViewMode", "individual")
    stub.setConfig("showStatistics", False)
    viewer = viewer_factory()
    viewer.setProperty("clinicalMode", False)
    sources = []

    def show(src):
        sources.append(src)
        stub.setScanSource(src)
        return src

    stub.writes.clear()
    try:
        yield viewer, stub, show
    finally:
        # Destroyed first: a viewer left alive until module teardown would
        # re-run its bindings on every later test's config and source
        # changes, against real sources, making the module quadratic.
        viewer_factory.destroy(viewer)
        stub.setScanSource(None)
        stub.setConfig("plotViewMode", "individual")
        stub.setConfig("showStatistics", False)
        for src in sources:
            src.release()


def _panel(viewer):
    found = [o for o in _all_objects(viewer)
             if o.metaObject().className().startswith("StatisticsPanel")]
    assert len(found) == 1
    return found[0]


def _variant(v):
    return v.toVariant() if hasattr(v, "toVariant") else v


def _row_values(panel, row):
    return _variant(QMetaObject.invokeMethod(
        panel, "rowValues", Q_RETURN_ARG("QVariant"), Q_ARG("QVariant", row)))


def _sections(viewer):
    """The pane after a fresh poll, as {title: [(label, v…)]}."""
    panel = _panel(viewer)
    QMetaObject.invokeMethod(panel, "poll")
    out = {}
    for sec in _variant(panel.property("model")):
        out[sec["title"]] = [(r["label"], *_row_values(panel, r))
                             for r in sec["rows"]]
    return out


def _value_texts(panel):
    """The value cells' Text items, keyed by C++ pointer."""
    return {sip.unwrapinstance(o): o.property("text")
            for o in _all_objects(panel) if o.objectName() == "statValue"}


def _cells(viewer):
    return [o for o in _all_objects(viewer)
            if o.metaObject().className().startswith("PlotCell")]


def test_statistics_are_off_by_default(stats_viewer):
    viewer, stub, show = stats_viewer
    show(_scan_source())
    assert viewer.property("statsActive") is False
    assert viewer.property("_showStatsPanel") is False
    assert _panel(viewer).property("visible") is False
    assert viewer.property("_statsInsetPx") == 0
    assert all(c.property("showValueLabels") is True for c in _cells(viewer))


def test_turning_statistics_on_moves_the_numbers_into_the_pane(stats_viewer):
    viewer, stub, show = stats_viewer
    show(_scan_source())
    stub.setConfig("showStatistics", True)
    assert viewer.property("statsActive") is True
    assert viewer.property("showCellValues") is False
    cells = _cells(viewer)
    assert len(cells) == 16
    assert all(c.property("showValueLabels") is False for c in cells)
    panel = _panel(viewer)
    assert panel.property("visible") is True
    # The plot overlays move left by the pane plus the row spacing.
    assert viewer.property("_statsInsetPx") == pytest.approx(
        panel.property("width") + 8)


def test_individual_rows_and_mirrored_differentials(stats_viewer):
    viewer, stub, show = stats_viewer
    show(_scan_source())
    stub.setConfig("showStatistics", True)
    s = _sections(viewer)
    assert list(s) == ["LEFT", "RIGHT", "LEFT − RIGHT"]
    assert [r[0] for r in s["LEFT"]] == [f"L{c + 1}" for c in range(8)]
    assert [r[0] for r in s["RIGHT"]] == [f"R{c + 1}" for c in range(8)]
    for c, (_label, bfi, bfi_lpf, bvi, bvi_lpf) in enumerate(s["LEFT"]):
        assert (bfi, bfi_lpf) == pytest.approx((_left_bfi(c),) * 2, abs=1e-5)
        assert (bvi, bvi_lpf) == pytest.approx((_bvi("left", c),) * 2, abs=1e-5)
    # Camera N faces camera 9 − N: L1 − R8, L2 − R7, …, L8 − R1.
    diffs = s["LEFT − RIGHT"]
    assert [r[0] for r in diffs] == [f"L{c + 1} − R{8 - c}" for c in range(8)]
    for c, (_label, dbfi, dbfi_lpf, dbvi, dbvi_lpf) in enumerate(diffs):
        o = 7 - c
        assert (dbfi, dbfi_lpf) == pytest.approx(
            (_left_bfi(c) - _right_bfi(o),) * 2, abs=1e-5)
        assert (dbvi, dbvi_lpf) == pytest.approx(
            (_bvi("left", c) - _bvi("right", o),) * 2, abs=1e-5)


def test_text_is_as_large_as_fits_the_pane(stats_viewer):
    """The font grows until every line fills the pane's height, within
    its bounds, and the columns widen with it. (The layout sets the
    pane's height in the app; this harness has no window, so the test
    sets it.)"""
    viewer, stub, show = stats_viewer
    show(_scan_source())
    stub.setConfig("showStatistics", True)
    panel = _panel(viewer)
    lo, hi = panel.property("_minFontPx"), panel.property("_maxFontPx")
    # 2 header lines + 3 section titles + 8 + 8 + 8 rows.
    assert panel.property("_lineCount") == 29
    panel.setProperty("height", 300)
    assert panel.property("_fontPx") == lo        # too short: floor, scrolls
    panel.setProperty("height", 820)
    font = panel.property("_fontPx")
    assert lo < font < hi
    fixed = 2 * 12 + 9 + 2 * 10                   # padding, divider, gaps
    assert 29 * panel.property("_rowHeight") <= 820 - fixed
    width = panel.property("width")
    stub.setConfig("plotViewMode", "average")     # 2 + 3 titles + 3 rows
    assert panel.property("_fontPx") == hi
    assert panel.property("width") > width


def test_a_refresh_updates_the_rows_in_place(stats_viewer):
    """Polling at 10 Hz must not rebuild the rows (a new model array would
    make the Repeaters recreate every Text each time): the same Text items
    show the new numbers."""
    viewer, stub, show = stats_viewer
    ramp = {("left", 0, "bfi"): _buf(np.linspace(1.0, 5.0, 400)),
            ("left", 0, "bvi"): _buf(np.full(400, 2.0)),
            ("right", 7, "bfi"): _buf(np.full(400, 1.0)),
            ("right", 7, "bvi"): _buf(np.full(400, 2.0))}
    show(PastScanSource(scan_db=None, session_id=1, preloaded_buffers=ramp))
    stub.setConfig("showStatistics", True)
    panel = _panel(viewer)
    viewer.setProperty("liveEdgeSnapshot", 2.0)
    QMetaObject.invokeMethod(panel, "poll")
    before = _value_texts(panel)
    assert len(before) == 3 * 4           # L1, R8 and L1 − R8
    viewer.setProperty("liveEdgeSnapshot", 9.0)
    QMetaObject.invokeMethod(panel, "poll")
    after = _value_texts(panel)
    assert set(after) == set(before)
    assert after != before                # same items, new numbers
    assert "+" in "".join(after.values())  # a differential carries its sign


def test_aggregate_pairs_face_the_same_pair(stats_viewer):
    viewer, stub, show = stats_viewer
    show(_scan_source())
    stub.setConfig("plotViewMode", "aggregate")
    stub.setConfig("showStatistics", True)
    s = _sections(viewer)
    pairs = [f"{p + 1}+{8 - p}" for p in range(4)]
    assert [r[0] for r in s["LEFT"]] == ["L" + p for p in pairs]
    assert [r[0] for r in s["LEFT − RIGHT"]] == [f"L{p} − R{p}" for p in pairs]
    for p, row in enumerate(s["LEFT − RIGHT"]):
        left = (_left_bfi(p) + _left_bfi(7 - p)) / 2
        right = (_right_bfi(p) + _right_bfi(7 - p)) / 2
        assert row[1] == pytest.approx(left - right, abs=1e-5)


def test_average_view_pairs_the_sides_and_drops_the_side_panels(stats_viewer):
    viewer, stub, show = stats_viewer
    show(_scan_source())
    stub.setConfig("plotViewMode", "average")
    assert viewer.property("_showClinicalPanels") is True
    assert [c.property("showLabel") for c in _cells(viewer)] == [False, False]
    stub.setConfig("showStatistics", True)
    assert viewer.property("_showClinicalPanels") is False
    # With the side panels gone, the averaged plots name their own side.
    cells = _cells(viewer)
    assert all(c.property("showLabel") is True for c in cells)
    assert sorted(c.property("label") for c in cells) == ["LEFT AVG", "RIGHT AVG"]
    s = _sections(viewer)
    assert s["LEFT"][0][0] == "AVG" and s["RIGHT"][0][0] == "AVG"
    dbfi = (np.mean([_left_bfi(c) for c in range(8)])
            - np.mean([_right_bfi(c) for c in range(8)]))
    dbvi = (np.mean([_bvi("left", c) for c in range(8)])
            - np.mean([_bvi("right", c) for c in range(8)]))
    [(label, *values)] = s["LEFT − RIGHT"]
    assert label == "L − R"
    assert values == pytest.approx([dbfi, dbfi, dbvi, dbvi], abs=1e-5)


def test_differentials_only_where_the_opposite_plot_exists(stats_viewer):
    """Right module cameras 1-4 only: their opposites are left 8-5, so
    only those four left cameras get a differential."""
    viewer, stub, show = stats_viewer
    show(_scan_source(right_cams=range(4)))
    stub.setConfig("showStatistics", True)
    s = _sections(viewer)
    assert [r[0] for r in s["LEFT − RIGHT"]] == [
        "L5 − R4", "L6 − R3", "L7 − R2", "L8 − R1"]


def test_single_sided_scan_has_no_differential_section(stats_viewer):
    viewer, stub, show = stats_viewer
    show(_scan_source(right_cams=()))
    stub.setConfig("showStatistics", True)
    assert list(_sections(viewer)) == ["LEFT"]


def test_values_are_clamped_for_display_and_diffs_use_the_shown_numbers(stats_viewer):
    """BFI above the display ceiling (10) shows as 10, like the cell
    labels, and the differential is taken between the shown numbers."""
    viewer, stub, show = stats_viewer
    show(_scan_source(left_bfi=lambda c: 14.0, right_bfi=lambda c: 9.0))
    stub.setConfig("showStatistics", True)
    s = _sections(viewer)
    assert s["LEFT"][0][1:3] == pytest.approx((10.0, 10.0))
    assert s["LEFT − RIGHT"][0][1:3] == pytest.approx((1.0, 1.0))


@pytest.mark.parametrize("v, signed, text", [
    (1.234, False, "1.23"),
    (NAN, False, "--"),
    (0.5, True, "+0.50"),
    (-2.75, True, "-2.75"),
    (-0.001, True, "0.00"),
    (0.001, True, "0.00"),
])
def test_number_format(stats_viewer, v, signed, text):
    viewer, _stub, _show = stats_viewer
    out = QMetaObject.invokeMethod(
        _panel(viewer), "_fmt", Q_RETURN_ARG("QVariant"),
        Q_ARG("QVariant", v), Q_ARG("QVariant", signed))
    assert out == text


@pytest.mark.parametrize("opp_in, opp_out", [
    (0, 7), (7, 0), (3, 4), (-1, -1), (8, 8), (11, 11),
])
def test_opposite_cam_id(stats_viewer, opp_in, opp_out):
    viewer, _stub, _show = stats_viewer
    out = QMetaObject.invokeMethod(
        _panel(viewer), "oppositeCamId", Q_RETURN_ARG("QVariant"),
        Q_ARG("QVariant", opp_in))
    assert out == opp_out


def test_clinical_build_ignores_a_stale_preference(stats_viewer):
    viewer, stub, show = stats_viewer
    show(_scan_source())
    stub.setConfig("showStatistics", True)
    viewer.setProperty("clinicalMode", True)
    assert viewer.property("statsActive") is False
    assert _panel(viewer).property("visible") is False


def test_replayed_clinical_scan_ignores_it(stats_viewer):
    viewer, stub, show = stats_viewer
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers={
        ("left", -1, "bfi"): _buf([3.0] * 40),
        ("left", -1, "bvi"): _buf([2.0] * 40),
    })
    show(src)
    stub.setConfig("showStatistics", True)
    assert viewer.property("effectiveClinical") is True
    assert viewer.property("statsActive") is False


# ── The ⋯ switch ────────────────────────────────────────────────────────


def test_statistics_switch_shows_and_writes_the_preference(stats_viewer):
    viewer, stub, show = stats_viewer
    show(_scan_source())
    seg = _segmented(viewer)["off"]
    opts = _variant(seg.property("options"))
    assert [(o["value"], o["label"]) for o in opts] == [("off", "Off"), ("on", "On")]
    assert seg.property("current") == "off"
    _pick(seg, "on")
    assert stub.writes == [("showStatistics", True)]
    assert seg.property("current") == "on"
    _pick(seg, "off")
    assert stub.writes[-1] == ("showStatistics", False)
    assert viewer.property("statsActive") is False


def test_statistics_row_has_the_view_rows_gate():
    qml = PLOT_VIEWER_QML.read_text(encoding="utf-8")
    idx = qml.index("id: statsRow")
    assert "visible: !viewer.clinicalMode && !viewer.effectiveClinical" \
        in qml[idx:idx + 120]


def test_plot_overlays_clear_the_pane():
    """Back-to-live, the hover tooltip and the bottom-right controls all
    shift by the pane's inset."""
    qml = PLOT_VIEWER_QML.read_text(encoding="utf-8")
    for overlay in ("hoverTooltip", "backToLiveOverlay", "bottomRightOverlay"):
        idx = qml.index("id: " + overlay)
        body = qml[idx:idx + 1200]
        assert re.search(r"anchors\.rightMargin: viewer\._overlayEdgeMarginPx"
                         r" \+ viewer\._statsInsetPx", body), overlay
