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
  - clinical: the flag is ignored even when the settings table holds it.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import contextlib
import os
import sys
from pathlib import Path

# A QGuiApplication must exist before any QML Quick item is created, and
# it must be constructed before conftest's session-scoped autouse fixture
# instantiates a bare QCoreApplication. Module import runs at collection
# time — ahead of every fixture — so create it here. The offscreen
# platform is passed via argv, NOT via QT_QPA_PLATFORM, so the process
# environment is untouched (see test_plot_viewer_masks.py).
from PyQt6.QtCore import (  # noqa: E402
    QCoreApplication,
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


def _cell_range(side, cam_id, metric):
    """Distinct per (side, camera, metric) so a cell fitted to its own
    trace is distinguishable from one that got the shared range."""
    base = float(cam_id) * 10.0 + (1000.0 if side == "right" else 0.0)
    return (base, base + 1.0) if metric == "bfi" else (base + 0.5, base + 1.5)


class _StubLiveSource(QObject):
    """Live-looking source: masks/clinicalMode -1 (grid follows the live
    selection and the viewer's clinicalMode input), aggregate bounds
    fixed per metric, per-cell bounds from _cell_range."""

    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cell_calls = []

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
        return 0.0

    @pyqtSlot(str, result="QVariantMap")
    @pyqtSlot(str, float, float, float, result="QVariantMap")
    def compute_bounds_for_metric(self, metric, lo=2.0, hi=98.0, pad=0.25):
        y_min, y_max = GLOBAL.get(metric, (0.0, 1.0))
        return {"yMin": y_min, "yMax": y_max}

    @pyqtSlot(str, int, str, result="QVariantMap")
    def compute_bounds_for_cell(self, side, cam_id, metric):
        self.cell_calls.append((side, int(cam_id), metric))
        y_min, y_max = _cell_range(side, int(cam_id), metric)
        return {"yMin": y_min, "yMax": y_max}

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
