"""
Issue #452 — per-plot autoscale.

``autoScalePerPlot`` had been a persisted key since the Saleae-style
viewer landed (#142) but nothing read it: SettingsModal mirrored it to
``autoScale`` and PlotViewer pushed one shared range per metric
(``compute_bounds_for_metric`` over every buffer) to every cell. These
tests load the real ``components/PlotViewer.qml`` offscreen with a stub
``MotionInterface`` (appConfig carries the flag and notifies on change,
like the connector) and a stub scan source whose per-cell bounds differ
per camera, then assert:

  - global mode: every PlotCell gets the aggregate range;
  - per-plot mode (flag flipped through appConfig, the way the ⋯ popup's
    setConfig lands): each cell gets its own camera's range;
  - autoScale off: per-plot has no effect (manual bounds win);
  - clinical: the flag is ignored even when the settings table holds it;
  - #591: the per-cell fit covers the visible window the cells draw, and
    a pan / zoom / window-length change refits (coalesced into the
    33 ms paint tick, so the event loop is pumped for those);
  - #591 smoothing: the periodic (Timer) evaluation lets each cell's
    range follow the fresh fit with an asymmetric lag (outward fast,
    inward slow) while a window change snaps it; the range is applied
    directly (no per-frame glide: that forced 30 Hz repaints of every
    cell and made the UI sluggish), so per-plot mode must not keep the
    paint tick running while idle;
  - #613: a new source (scan start) inherits no range from the previous
    one, so a cell's first real fit snaps instead of lagging away from
    the previous scan's range with the traces off-axis; and while the
    window is still filling (tLo < 0) outward moves are adopted at once.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import contextlib
import os
import sys
import time
from pathlib import Path

# A QGuiApplication must exist before any QML Quick item is created, and
# it must be constructed before conftest's session-scoped autouse fixture
# instantiates a bare QCoreApplication. Module import runs at collection
# time — ahead of every fixture — so create it here. The offscreen
# platform is passed via argv, NOT via QT_QPA_PLATFORM, so the process
# environment is untouched (see test_plot_viewer_masks.py).
from PyQt6.QtCore import (  # noqa: E402
    QCoreApplication,
    QMetaObject,
    QObject,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QGuiApplication  # noqa: E402

if QCoreApplication.instance() is None:
    _qt_app = QGuiApplication([sys.argv[0], "-platform", "offscreen"])
else:
    _qt_app = QCoreApplication.instance()

import pytest  # noqa: E402
from PyQt6.QtQml import (  # noqa: E402
    QQmlComponent,
    QQmlEngine,
    qmlRegisterSingletonInstance,
)
from PyQt6.QtQuick import QQuickItem  # noqa: E402

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
PLOT_VIEWER_QML = REPO_ROOT / "components" / "PlotViewer.qml"

ALL_MASK = 0xFF
GLOBAL = {"bfi": (0.0, 100.0), "bvi": (0.0, 200.0)}
LIVE_EDGE = 100.0   # the stub source's liveEdge, so windows are non-trivial


def _cell_range(side, cam_id, metric):
    """Distinct per (side, camera, metric) so a cell fitted to its own
    trace is distinguishable from one that got the shared range."""
    # Offset by 2 so no cell ever reports exactly (0, 1): the viewer
    # treats that pair as the source's "no usable samples" fallback.
    base = 2.0 + float(cam_id) * 10.0 + (1000.0 if side == "right" else 0.0)
    return (base, base + 1.0) if metric == "bfi" else (base + 0.5, base + 1.5)


class _StubLiveSource(QObject):
    """Live-looking source: masks/clinicalMode -1 (grid follows the live
    selection and the viewer's clinicalMode input), aggregate bounds
    fixed per metric, per-cell bounds from _cell_range."""

    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cell_calls = []
        self.window_calls = []   # (t_lo, t_hi) per per-cell call (#591)
        self.offset = 0.0        # added to every per-cell range (settling tests)
        self.neutral = False     # True: no usable samples yet (fresh scan, #613)

    @pyqtProperty(bool, notify=_neverEmitted)
    def live(self):
        return True

    @pyqtProperty(int, notify=_neverEmitted)
    def leftMask(self):
        return -1

    @pyqtProperty(int, notify=_neverEmitted)
    def rightMask(self):
        return -1

    @pyqtProperty(int, notify=_neverEmitted)
    def clinicalMode(self):
        return -1

    @pyqtProperty(str, notify=_neverEmitted)
    def userLabel(self):
        return ""

    @pyqtProperty(str, notify=_neverEmitted)
    def dateTime(self):
        return ""

    @pyqtProperty(float, notify=_neverEmitted)
    def liveEdge(self):
        return LIVE_EDGE

    @pyqtSlot(str, result="QVariantMap")
    @pyqtSlot(str, float, float, float, result="QVariantMap")
    def compute_bounds_for_metric(self, metric, lo=2.0, hi=98.0, pad=0.25):
        y_min, y_max = GLOBAL.get(metric, (0.0, 1.0))
        return {"yMin": y_min, "yMax": y_max}

    @pyqtSlot(str, int, str, result="QVariantMap")
    @pyqtSlot(str, int, str, float, float, result="QVariantMap")
    def compute_bounds_for_cell(self, side, cam_id, metric, t_lo=None, t_hi=None):
        self.cell_calls.append((side, int(cam_id), metric))
        self.window_calls.append((t_lo, t_hi))
        if self.neutral:
            return {"yMin": 0.0, "yMax": 1.0}
        y_min, y_max = _cell_range(side, int(cam_id), metric)
        return {"yMin": y_min + self.offset, "yMax": y_max + self.offset}

    @pyqtSlot(str, int, str, float, float, int, result="QVariantList")
    def points_for_window(self, side, cam_id, metric, t_lo, t_hi, max_points):
        return []

    @pyqtSlot(str, int, str, float, result=float)
    def value_at(self, side, cam_id, metric, t):
        return float("nan")

    @pyqtSlot(str, int, result=float)
    def dropped_at_for(self, side, cam_id):
        return float("nan")


class _StubMotionInterface(QObject):
    """MotionInterface stand-in covering what PlotViewer.qml (and its
    child components) reference. appConfig notifies on setConfig, which
    is exactly the path the ⋯ popup's per-plot switch takes."""

    appConfigChanged = pyqtSignal()
    currentScanSourceChanged = pyqtSignal()
    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scan_source = None
        self._config = {
            "leftMask": ALL_MASK,
            "rightMask": ALL_MASK,
            "engineeringMode": False,
            "showProfiling": False,
            "showAxisLabels": True,
            "autoScalePerPlot": False,
        }

    @pyqtProperty("QVariantMap", notify=appConfigChanged)
    def appConfig(self):
        return dict(self._config)

    @pyqtSlot(str, "QVariant")
    def setConfig(self, key, value):
        self._config[key] = value
        self.appConfigChanged.emit()

    @pyqtProperty(QObject, notify=currentScanSourceChanged)
    def currentScanSource(self):
        return self._scan_source

    def setScanSource(self, source):
        self._scan_source = source
        self.currentScanSourceChanged.emit()

    @pyqtProperty(bool, notify=_neverEmitted)
    def liveSourceAvailable(self):
        return True

    @pyqtProperty(bool, notify=_neverEmitted)
    def leftSensorConnected(self):
        return True

    @pyqtProperty(bool, notify=_neverEmitted)
    def rightSensorConnected(self):
        return True

    @pyqtSlot()
    def showLiveSource(self):
        pass


@contextlib.contextmanager
def _basic_controls_style():
    """Force the always-available Basic Controls style for QML
    compilation only (see test_plot_viewer_masks.py for why)."""
    prev = os.environ.get("QT_QUICK_CONTROLS_STYLE")
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Basic"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("QT_QUICK_CONTROLS_STYLE", None)
        else:
            os.environ["QT_QUICK_CONTROLS_STYLE"] = prev


@pytest.fixture(scope="module")
def viewer_factory():
    """Compile components/PlotViewer.qml once; hand out fresh instances."""
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(PLOT_VIEWER_QML))
        )
    if component.isError():
        raise RuntimeError(
            "PlotViewer.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors())
        )

    created = []

    def make():
        obj = component.create()
        if obj is None:
            raise RuntimeError(
                "PlotViewer.qml failed to instantiate:\n"
                + "\n".join(e.toString() for e in component.errors())
            )
        created.append(obj)
        return obj

    make.stub = stub

    yield make

    for obj in created:
        obj.deleteLater()
    del engine
    del stub


@pytest.fixture
def live_viewer(viewer_factory):
    """A research viewer with autoscale on, All cameras on both sides,
    and a live stub source bound — the state the ⋯ popup is used in."""
    stub = viewer_factory.stub
    stub.setConfig("autoScalePerPlot", False)
    viewer = viewer_factory()
    viewer.setProperty("clinicalMode", False)
    viewer.setProperty("leftMask", ALL_MASK)
    viewer.setProperty("rightMask", ALL_MASK)
    viewer.setProperty("autoScale", True)
    src = _StubLiveSource()
    stub.setScanSource(src)
    try:
        yield viewer, src, stub
    finally:
        stub.setScanSource(None)
        stub.setConfig("autoScalePerPlot", False)


def _walk(obj):
    yield obj
    kids = obj.childItems() if isinstance(obj, QQuickItem) else obj.children()
    for k in kids:
        yield from _walk(k)


def _plot_cells(viewer):
    cells = [
        o for o in _walk(viewer)
        if o.metaObject().className().startswith("PlotCell")
    ]
    assert cells, "no PlotCell delegates were instantiated"
    return cells


def _ranges(viewer):
    """{(side, camId): (yMin, yMax, secondaryYMin, secondaryYMax)}."""
    out = {}
    for c in _plot_cells(viewer):
        key = (c.property("side"), int(c.property("camId")))
        out[key] = tuple(
            float(c.property(p))
            for p in ("yMin", "yMax", "secondaryYMin", "secondaryYMax")
        )
    return out


def test_global_mode_shares_one_range_per_metric(live_viewer):
    viewer, src, stub = live_viewer
    assert viewer.property("autoScalePerPlot") is False
    ranges = _ranges(viewer)
    assert len(ranges) == 16
    for key, (y0, y1, s0, s1) in ranges.items():
        assert (y0, y1) == GLOBAL["bfi"], key
        assert (s0, s1) == GLOBAL["bvi"], key
    assert src.cell_calls == []   # per-cell slot never consulted


def test_per_plot_mode_fits_each_cell_to_its_own_camera(live_viewer):
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)   # the ⋯ popup switch
    assert viewer.property("autoScalePerPlot") is True
    ranges = _ranges(viewer)
    assert len(ranges) == 16
    for (side, cam), (y0, y1, s0, s1) in ranges.items():
        assert (y0, y1) == _cell_range(side, cam, "bfi"), (side, cam)
        assert (s0, s1) == _cell_range(side, cam, "bvi"), (side, cam)
    assert len({r[:2] for r in ranges.values()}) == 16   # all distinct
    # Every active cell was fitted from its own buffer, both metrics.
    assert {(s, c, m) for (s, c, m) in src.cell_calls} == {
        (side, cam, metric)
        for (side, cam) in ranges for metric in ("bfi", "bvi")
    }


def test_switching_back_to_global_restores_the_shared_range(live_viewer):
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    stub.setConfig("autoScalePerPlot", False)
    for key, (y0, y1, s0, s1) in _ranges(viewer).items():
        assert (y0, y1) == GLOBAL["bfi"], key
        assert (s0, s1) == GLOBAL["bvi"], key


def test_per_plot_is_inert_while_autoscale_is_off(live_viewer):
    """Per-plot refines autoscale; it must never override the manual
    bounds an operator chose by turning autoscale off."""
    viewer, src, stub = live_viewer
    viewer.setProperty("autoScale", False)
    stub.setConfig("autoScalePerPlot", True)
    manual = (
        float(viewer.property("settingBfiMin")),
        float(viewer.property("settingBfiMax")),
    )
    for key, (y0, y1, s0, s1) in _ranges(viewer).items():
        assert (y0, y1) == manual, key


def test_display_mode_change_refits_per_cell(live_viewer):
    """Flipping BFI/BVI ↔ Mean/Contrast recomputes per-cell ranges for
    the new pair (the global path already did this, #142)."""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    src.cell_calls.clear()
    viewer.setProperty("displayMode", "mean_contrast")
    assert {m for (_s, _c, m) in src.cell_calls} == {"mean", "contrast"}


def _pump(ms=150):
    """Run the event loop long enough for the viewer's 33 ms paint
    timer to fire a few times (the per-plot refit is coalesced into it)."""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        _qt_app.processEvents()
        time.sleep(0.005)


def test_per_plot_fits_the_visible_window_when_following_live(live_viewer):
    """#591: the per-cell fit covers the window the cells draw — the last
    windowSeconds up to the live edge — never the whole scan."""
    viewer, src, stub = live_viewer
    viewer.setProperty("windowSeconds", 15.0)
    stub.setConfig("autoScalePerPlot", True)     # immediate refit
    assert src.window_calls
    assert set(src.window_calls) == {(LIVE_EDGE - 15.0, LIVE_EDGE)}
    assert (None, None) not in src.window_calls


def test_pan_zoom_and_window_length_refit_per_plot(live_viewer):
    """#591: a pan/zoom (pinned window) and a window-length change each
    refit against the new visible window, coalesced so a burst of
    changes fits only the final window; back-to-live refits at the
    live edge again."""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    _pump()                                       # drain the timer start
    src.window_calls.clear()
    # What setWindow() does for a drag/wheel, as three property writes.
    viewer.setProperty("followLive", False)
    viewer.setProperty("windowStartT", 20.0)
    viewer.setProperty("windowSeconds", 5.0)
    assert src.window_calls == []                 # nothing until the tick
    _pump()
    assert src.window_calls and set(src.window_calls) == {(20.0, 25.0)}
    src.window_calls.clear()
    viewer.setProperty("followLive", True)        # back to live
    _pump()
    assert src.window_calls and set(src.window_calls) == {(LIVE_EDGE - 5.0, LIVE_EDGE)}


def _refit(viewer):
    """A snapping refit (what a window change, source change or mode
    flip does), synchronously."""
    QMetaObject.invokeMethod(viewer, "_recomputeAutoscale")


def _tick(viewer):
    """What the 0.5 s autoscale Timer does: a lagging evaluation."""
    QMetaObject.invokeMethod(viewer, "_autoscaleTick")


def _slide(viewer, n):
    """Advance the followed window the way n evaluations of a live scan
    would (the paint tick refreshes liveEdgeSnapshot from the source);
    the Timer path only lags while the window is actually sliding."""
    viewer.setProperty("liveEdgeSnapshot", LIVE_EDGE + 0.5 * n)


def _paint_ticks_during(viewer, ms):
    """How many paint ticks the viewer runs on its own over `ms` of
    event-loop time (nothing else dirtying it)."""
    start = int(viewer.property("paintTick"))
    _pump(ms)
    return int(viewer.property("paintTick")) - start


KEY = ("left", 0)


def _target(viewer, key="left:0"):
    """The smoothed range of one cell as the viewer holds it (same object
    the cells bind), updated synchronously by a refit or tick."""
    v = viewer.property("_cellTargets")
    if hasattr(v, "toVariant"):
        v = v.toVariant()
    t = v[key]
    return tuple(float(t[f]) for f in ("pMin", "pMax", "sMin", "sMax"))


def test_per_plot_timer_evaluation_lags_outward_fast_inward_slow(live_viewer):
    """While the followed window slides, after the whole fit shifts up
    by half a span one Timer evaluation moves the top bound (outward) by
    dt/(tauUp+dt) of the way and the bottom bound (inward) by only
    dt/(tauDown+dt); after five, the top is >80% there and the bottom
    lags behind it."""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    t0 = _target(viewer)
    dt = float(viewer.property("_perPlotEvalSec"))
    a_up = dt / (float(viewer.property("_perPlotTauUpSec")) + dt)
    a_dn = dt / (float(viewer.property("_perPlotTauDownSec")) + dt)
    assert a_dn < a_up
    src.offset = 0.5
    _slide(viewer, 1)
    _tick(viewer)
    t1 = _target(viewer)
    assert t1[1] == pytest.approx(t0[1] + a_up * 0.5, rel=1e-3)    # pMax: outward
    assert t1[0] == pytest.approx(t0[0] + a_dn * 0.5, rel=1e-3)    # pMin: inward
    assert _ranges(viewer)[KEY][:2] == pytest.approx(t1[:2])       # applied directly
    for n in range(2, 6):
        _slide(viewer, n)
        _tick(viewer)
    t5 = _target(viewer)
    up_progress = (t5[1] - t0[1]) / 0.5
    down_progress = (t5[0] - t0[0]) / 0.5
    assert up_progress > 0.8
    assert down_progress < up_progress
    assert down_progress == pytest.approx(1 - (1 - a_dn) ** 5, rel=1e-3)
    assert t0[1] < t1[1] < t5[1] < t0[1] + 0.5 + 1e-9                # monotonic approach


def test_per_plot_static_window_snaps_on_the_timer(live_viewer):
    """When the window has not moved since the last evaluation (scan
    stopped, view paused) the fresh fit is final: the Timer path snaps
    to it instead of creeping there for seconds."""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    t0 = _target(viewer)
    _tick(viewer)                       # same window as the enabling fit
    src.offset = 0.5
    _tick(viewer)                       # still the same window -> snap
    t1 = _target(viewer)
    for i in range(4):
        assert t1[i] == pytest.approx(t0[i] + 0.5), i


def test_per_plot_window_change_snaps_the_target(live_viewer):
    """A pan (new window content) snaps the target to the fresh fit
    instead of lagging toward it."""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    t0 = _target(viewer)
    src.offset = 0.5
    viewer.setProperty("followLive", False)
    viewer.setProperty("windowStartT", 20.0)
    _pump()                                   # coalesced refit = snap
    t1 = _target(viewer)
    for i in range(4):
        assert t1[i] == pytest.approx(t0[i] + 0.5), i


def test_per_plot_refit_applies_at_once_and_does_not_keep_painting(live_viewer):
    """A snapped refit lands on the new range immediately, and per-plot
    mode adds no repaints of its own: after the one paint the refit
    marks dirty, the paint tick stays idle. (An earlier per-frame glide
    kept every cell repainting at 30 Hz and made the UI sluggish.)"""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    _pump(200)                            # drain the timer start + that paint
    before = _ranges(viewer)[KEY]
    src.offset = 0.5
    _refit(viewer)
    after = _ranges(viewer)[KEY]
    for i in range(4):
        assert after[i] == pytest.approx(before[i] + 0.5), i
    ticks = _paint_ticks_during(viewer, 400)
    assert ticks <= 2, f"per-plot mode kept the paint tick running: {ticks} ticks in 400 ms"


def test_per_plot_first_fit_snaps_without_a_glide(live_viewer):
    """Enabling per-plot lands on the fitted ranges immediately — the
    existing exact-range assertions rely on it; stated here explicitly."""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    assert _ranges(viewer)[KEY][:2] == _cell_range(*KEY, "bfi")


def test_per_plot_neutral_fit_holds_the_axis(live_viewer):
    """A window with no usable samples (covered camera) reports the
    0..1 neutral fit; the axis must hold rather than collapse."""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    before = _ranges(viewer)[KEY]
    real = src.compute_bounds_for_cell
    src.compute_bounds_for_cell = lambda *a, **k: {"yMin": 0.0, "yMax": 1.0}
    try:
        _refit(viewer)
        _pump(300)
        assert _ranges(viewer)[KEY] == before
    finally:
        src.compute_bounds_for_cell = real


def test_new_source_does_not_inherit_the_previous_range(live_viewer):
    """#613: a scan starts on a fresh source with no samples, so the
    source-change refit sees only neutral fits. Those used to hold the
    PREVIOUS source's ranges, and every Timer evaluation then lagged
    away from them — the new scan's traces sat off-axis for seconds.
    The first real fit on the new source must snap."""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    old = _target(viewer)
    new_src = _StubLiveSource()
    new_src.neutral = True                   # scan just started: no samples
    stub.setScanSource(new_src)
    new_src.neutral = False                  # first frames arrive,
    new_src.offset = 50.0                    # on a very different range
    _slide(viewer, 1)                        # live window sliding
    _tick(viewer)                            # a lagging (Timer) evaluation
    first = _target(viewer)
    want = _cell_range(*KEY, "bfi") + _cell_range(*KEY, "bvi")
    for i in range(4):
        assert first[i] == pytest.approx(want[i] + 50.0), i
        assert first[i] != pytest.approx(old[i]), i


def test_outward_moves_are_immediate_while_the_window_fills(live_viewer):
    """#613: in a live scan younger than the window (tLo < 0) the fitted
    set grows rather than slides, so a wider fit is adopted at once
    instead of clipping the early beats; inward moves keep the lag."""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    viewer.setProperty("liveEdgeSnapshot", 4.0)     # 4 s into a 15 s window
    _refit(viewer)
    t0 = _target(viewer)
    src.offset = 0.5                                # whole fit moves up
    viewer.setProperty("liveEdgeSnapshot", 4.5)
    _tick(viewer)
    t1 = _target(viewer)
    dt = float(viewer.property("_perPlotEvalSec"))
    a_dn = dt / (float(viewer.property("_perPlotTauDownSec")) + dt)
    assert t1[1] == pytest.approx(t0[1] + 0.5)              # pMax: outward, at once
    assert t1[0] == pytest.approx(t0[0] + a_dn * 0.5, rel=1e-3)  # pMin: inward, lagged
    # Once the window is full the outward lag is back (the lag test
    # above covers it at LIVE_EDGE; this pins the boundary).
    viewer.setProperty("liveEdgeSnapshot", 15.5)
    src.offset = 1.0
    _tick(viewer)
    t2 = _target(viewer)
    assert t2[1] < t1[1] + 0.5 - 1e-6


def test_trace_pen_is_at_most_one_device_pixel(live_viewer):
    """#616: QPainter strokes a pen of at most one device pixel with its
    fast stroker; the old 1.5 px trace pen cost ~270 ms per 8-cell
    repaint on a 3440 px screen (vs ~6 ms), which held the plots to ~2
    repaints/s and starved every other control. Canvas scales by the
    device pixel ratio, so the width must be in device pixels."""
    viewer, src, stub = live_viewer
    for c in _plot_cells(viewer):
        width = float(c.property("_tracePenWidth"))
        dpr = c.window().devicePixelRatio() if c.window() else 1.0
        assert 0.0 < width * dpr <= 1.0
    qml = (REPO_ROOT / "components" / "PlotCell.qml").read_text(encoding="utf-8")
    start = qml.index("function _drawTrace")
    body = qml[start:qml.index("return pts.length", start)]
    assert "ctx.lineWidth = cell._tracePenWidth" in body


def test_global_mode_ignores_window_changes(live_viewer):
    """Global autoscale keeps fitting the whole scan: a pan must not
    consult the per-cell slot at all."""
    viewer, src, stub = live_viewer
    viewer.setProperty("followLive", False)
    viewer.setProperty("windowStartT", 20.0)
    _pump()
    assert src.cell_calls == []


def test_popup_switch_row_is_research_only():
    """The ⋯ popup row carrying the per-plot switch is gated on
    !effectiveClinical (and on autoscale being on, under which it is
    disclosed). Static guard on the QML: opening a Popup needs a
    window, which this harness deliberately does without."""
    qml = PLOT_VIEWER_QML.read_text(encoding="utf-8")
    idx = qml.index("id: perPlotSwitch")
    row = qml[qml.rfind("Row {", 0, idx):idx]
    assert "visible: !viewer.effectiveClinical && viewer.autoScale" in row


def test_clinical_ignores_the_persisted_flag(live_viewer):
    """A clinical build has neither switch; a stale true in the settings
    table must not leak through."""
    viewer, src, stub = live_viewer
    stub.setConfig("autoScalePerPlot", True)
    assert viewer.property("autoScalePerPlot") is True
    viewer.setProperty("clinicalMode", True)
    assert viewer.property("autoScalePerPlot") is False
