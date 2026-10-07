"""
Issue #708 — the plot's bottom-right controls before a scan starts.

The time-window pill and the ⋯ settings button used to appear only once
a scan source was bound, so the window and the display options could not
be set until data was already streaming. They now show from app start:

  - the row is visible with no source; the ⋯ button keeps its clinical
    gate (hidden in clinical unless engineering mode is on), the pill
    shows in every mode;
  - the pill's menu opens and a window picked with no source sticks, and
    the first scan opens at that window (live-follow or paused);
  - the ⋯ popup opens with no source, every switch in it is a no-op
    (no refit, no warning) until a source is bound, and the choices
    apply to the first scan;
  - geometry: with the scrubber hidden the row keeps the usual 12 px rim
    above the plots, and it clears the empty-state placeholder;
  - no QML warnings on any of these paths (Qt messages are captured from
    load on).

The real PlotViewer.qml runs in an offscreen QQuickView (Popup and Menu
only open inside a window) with a stub MotionInterface and the real
AppTheme singleton, driven by mouse clicks where a user would click.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

from pathlib import Path

# test_average_view_mode creates the offscreen QGuiApplication at import
# (it must exist before conftest's bare QCoreApplication); reuse its stubs.
from test_average_view_mode import (  # noqa: E402
    AVERAGE,
    PLOT_VIEWER_QML,
    _StubLiveSource,
    _StubMotionInterface,
    _basic_controls_style,
)
from test_plot_menu import _all_objects, _pick, _segmented  # noqa: E402

import pytest  # noqa: E402
from PyQt6.QtCore import (  # noqa: E402
    QCoreApplication,
    QElapsedTimer,
    QEvent,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QtMsgType,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
    qInstallMessageHandler,
)
from PyQt6.QtQml import (  # noqa: E402
    QQmlEngine,
    qmlRegisterSingletonInstance,
    qmlRegisterSingletonType,
)
from PyQt6.QtQuick import QQuickItem, QQuickView  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"
VIEW_SIZE = (1400, 820)
SIDE_MASK = 0x99   # cameras 1, 4, 5, 8 per side: a 2-row grid


class _Stub(_StubMotionInterface):
    """Records setConfig writes; sensor connection is switchable."""

    # The connector's per-camera dropout signals. PlotViewer binds handlers
    # to them and warns at load when they are missing, which would make a
    # clean-log assertion meaningless.
    cameraDropoutDetected = pyqtSignal(str, int, float)
    cameraDropoutRecovered = pyqtSignal(str, int, float)
    sensorsChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.writes = []
        self._sensors = True

    @pyqtSlot(str, "QVariant")
    def setConfig(self, key, value):
        self.writes.append((key, value))
        super(_Stub, self).setConfig(key, value)

    def setSensorsConnected(self, connected):
        self._sensors = bool(connected)
        self.sensorsChanged.emit()

    @pyqtProperty(bool, notify=sensorsChanged)
    def leftSensorConnected(self):
        return self._sensors

    @pyqtProperty(bool, notify=sensorsChanged)
    def rightSensorConnected(self):
        return self._sensors


def _pump(ms=150):
    """Let bindings, polish and QML Timers run."""
    t = QElapsedTimer()
    t.start()
    while t.elapsed() < ms:
        QCoreApplication.processEvents()


class _Harness:
    def __init__(self, stub, view, messages):
        self.stub = stub
        self.view = view
        self.messages = messages

    @property
    def root(self):
        return self.view.rootObject()

    def item(self, qml_id):
        """An object of PlotViewer.qml by its id."""
        obj = QQmlEngine.contextForObject(self.root).objectForName(qml_id)
        assert obj is not None, qml_id
        return obj

    def click(self, item):
        c = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        QTest.mouseClick(self.view, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         QPoint(round(c.x()), round(c.y())))
        _pump()

    def bind(self, source):
        self.stub.setScanSource(source)
        _pump()

    def warnings(self):
        bad = (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg,
               QtMsgType.QtFatalMsg)
        return [msg for kind, msg in self.messages
                if kind in bad and not msg.startswith(_ENV_NOISE)]


# Host-environment messages that say nothing about the QML under test:
# the offscreen platform's font database finds no font directory in the
# PyQt6 wheel the first time text is laid out.
_ENV_NOISE = ("QFontDatabase: Cannot find font directory",)


def _variant(v):
    return v.toVariant() if hasattr(v, "toVariant") else v


def _scene_rect(item):
    return item.mapRectToScene(QRectF(0, 0, item.width(), item.height()))


def _menu_item(h, label):
    """An open menu's item by its text. Popup content lives under the
    window's overlay, not under the viewer, so search from the window."""
    for o in _all_objects(h.view.contentItem()):
        if (isinstance(o, QQuickItem)
                and o.metaObject().indexOfProperty("highlighted") >= 0
                and o.property("text") == label):
            return o
    raise AssertionError("no menu item " + label)


def _segment(seg, label):
    """One bubble of a PopupSegmented switch."""
    for o in _all_objects(seg):
        if (isinstance(o, QQuickItem) and o is not seg
                and o.metaObject().indexOfProperty("selected") >= 0
                and any(k.property("text") == label for k in o.childItems())):
            return o
    raise AssertionError("no segment " + label)


def _plot_cells(h):
    return [o for o in _all_objects(h.root)
            if o.metaObject().className().startswith("PlotCell")]


@pytest.fixture(scope="module")
def harness_view():
    messages = []

    def handler(kind, _context, msg):
        messages.append((kind, msg))

    prev = qInstallMessageHandler(handler)
    stub = _Stub()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(QUrl.fromLocalFile(str(APP_THEME_QML)),
                             "OpenMotion", 1, 0, "AppTheme")
    view = QQuickView()
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.resize(*VIEW_SIZE)
    view.show()
    try:
        yield stub, view, messages
    finally:
        stub.setScanSource(None)
        view.close()
        view.setSource(QUrl())
        view.deleteLater()
        # Destroy everything while the engine is still alive (see
        # test_plot_menu): a deferred delete left for a later module's
        # event pump crashes natively.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qInstallMessageHandler(prev)


@pytest.fixture
def h(harness_view):
    """A fresh research PlotViewer with sensors connected and NO scan
    source, its messages captured from load on."""
    stub, view, messages = harness_view
    view.setSource(QUrl())           # drop the previous test's viewer
    stub.setScanSource(None)
    stub.setSensorsConnected(True)
    stub.setConfig("engineeringMode", False)
    stub.setConfig("plotViewMode", "individual")
    stub.setConfig("autoScalePerPlot", False)
    stub.setConfig("showStatistics", False)
    view.resize(*VIEW_SIZE)
    _pump(50)
    messages.clear()
    with _basic_controls_style():
        view.setSource(QUrl.fromLocalFile(str(PLOT_VIEWER_QML)))
    assert view.status() == QQuickView.Status.Ready, \
        [e.toString() for e in view.errors()]
    root = view.rootObject()
    root.setProperty("clinicalMode", False)
    root.setProperty("leftMask", SIDE_MASK)
    root.setProperty("rightMask", SIDE_MASK)
    root.setProperty("autoScale", False)
    # The host's wiring (BloodFlow.qml): the popup's autoscale and metric
    # requests come back as the viewer's inputs.
    root.autoScaleToggleRequested.connect(
        lambda on: root.setProperty("autoScale", on))
    root.displayModeToggleRequested.connect(
        lambda bfi: root.setProperty(
            "displayMode", "bfi_bvi" if bfi else "mean_contrast"))
    _pump(200)
    stub.writes.clear()
    yield _Harness(stub, view, messages)
    stub.setScanSource(None)


# ── Visibility ──────────────────────────────────────────────────────────


def test_controls_show_with_no_scan_source(h):
    assert h.stub.currentScanSource is None
    for qml_id in ("bottomRightOverlay", "windowSecondsPill",
                   "settingsMenuButton"):
        assert h.item(qml_id).isVisible(), qml_id
    assert h.item("windowSecondsText").property("text") == "15 s"
    assert h.warnings() == []


@pytest.mark.parametrize("clinical, engineering, menu_shown", [
    (False, False, True),
    (False, True, True),
    (True, False, False),
    (True, True, True),
])
def test_settings_button_keeps_its_clinical_gate(
        h, clinical, engineering, menu_shown):
    h.stub.setConfig("engineeringMode", engineering)
    h.root.setProperty("clinicalMode", clinical)
    _pump(50)
    assert h.item("windowSecondsPill").isVisible()
    assert h.item("settingsMenuButton").isVisible() is menu_shown
    assert h.warnings() == []


# ── The window pill ─────────────────────────────────────────────────────


def test_window_menu_opens_and_picks_with_no_source(h):
    menu = h.item("windowSecondsMenu")
    h.click(h.item("windowSecondsPill"))
    assert menu.property("opened") is True
    h.click(_menu_item(h, "1 min"))
    assert menu.property("opened") is False
    assert h.root.property("windowSeconds") == 60
    assert h.item("windowSecondsText").property("text") == "1 min"
    assert h.warnings() == []


def test_window_picked_before_a_scan_is_the_first_scans_window(h):
    h.click(h.item("windowSecondsPill"))
    h.click(_menu_item(h, "30 s"))
    h.bind(_StubLiveSource())
    assert h.root.property("windowSeconds") == 30
    assert h.root.property("followLive") is True
    assert h.item("windowSecondsText").property("text") == "30 s"
    cells = _plot_cells(h)
    assert cells and all(c.property("windowSeconds") == 30 for c in cells)
    assert h.warnings() == []


def test_window_picked_while_paused_with_no_source(h):
    """A pan before the first scan (arrow keys work then) pauses
    live-follow, so the pick goes through setWindow, which clamps the
    start against the empty live edge. The first scan still opens
    following live, at the picked window."""
    h.root.setProperty("followLive", False)
    h.root.setProperty("windowStartT", 5.0)
    h.click(h.item("windowSecondsPill"))
    h.click(_menu_item(h, "5 min"))
    assert h.root.property("windowSeconds") == 300
    assert h.root.property("windowStartT") == 0
    h.bind(_StubLiveSource())
    assert h.root.property("windowSeconds") == 300
    assert h.root.property("followLive") is True
    assert h.warnings() == []


# ── The ⋯ popup ─────────────────────────────────────────────────────────


def test_settings_popup_opens_with_no_source(h):
    popup = h.item("settingsPopup")
    h.click(h.item("settingsMenuButton"))
    assert popup.property("opened") is True
    for row in ("viewModeRow", "scaleRow", "metricsRow", "statsRow"):
        assert h.item(row).isVisible(), row
    h.click(h.item("settingsMenuButton"))
    assert popup.property("opened") is False
    assert h.warnings() == []


def test_every_popup_switch_is_a_no_op_without_a_source(h):
    """Each choice lands in config / the viewer inputs, but nothing refits
    (there is nothing to fit) and nothing warns — per-plot included, whose
    window-change refit stays pending until a source arrives."""
    h.stub.setConfig("engineeringMode", True)   # the Profiler row too
    h.click(h.item("settingsMenuButton"))
    seg = _segmented(h.root)
    for key, values in (
            ("individual", ["aggregate", "average", "individual"]),
            ("fixed", ["global", "perPlot", "fixed", "perPlot"]),
            ("bfi_bvi", ["mean_contrast", "bfi_bvi"]),
            ("off", ["on", "off"])):
        for value in values:
            _pick(seg[key], value)
            _pump(30)
    h.click(h.item("profilerSwitch"))
    assert h.root.property("showProfiling") is True
    assert h.root.property("perPlotActive") is True
    h.click(h.item("settingsMenuButton"))
    h.click(h.item("windowSecondsPill"))
    h.click(_menu_item(h, "5 s"))
    assert (h.root.property("_autoPrimaryYMin"),
            h.root.property("_autoPrimaryYMax")) == (0.0, 10.0)
    assert _variant(h.root.property("_cellBounds")) == {}
    h.bind(_StubLiveSource())
    assert h.root.property("windowSeconds") == 5
    assert h.warnings() == []


def test_popup_choices_made_before_a_scan_apply_to_it(h):
    h.click(h.item("settingsMenuButton"))
    seg = _segmented(h.root)
    h.click(_segment(seg["fixed"], "Global"))       # a real click
    _pick(seg["individual"], "average")
    assert h.stub.writes == [("plotViewMode", "average")]
    assert h.root.property("scaleMode") == "global"
    assert h.root.property("averageView") is True
    # Nothing to fit yet: the autoscaled range is still the default.
    assert (h.root.property("primaryYMin"),
            h.root.property("primaryYMax")) == (0.0, 10.0)
    h.bind(_StubLiveSource())
    assert (h.root.property("primaryYMin"),
            h.root.property("primaryYMax")) == AVERAGE["bfi"]
    assert (h.root.property("secondaryYMin"),
            h.root.property("secondaryYMax")) == AVERAGE["bvi"]
    assert sorted((c.property("side"), int(c.property("camId")))
                  for c in _plot_cells(h)) == [("left", -1), ("right", -1)]
    assert h.warnings() == []


# ── Geometry ────────────────────────────────────────────────────────────


def test_controls_keep_the_rim_margin_above_the_plots(h):
    """With no source the scrubber is hidden and the grid reaches down
    into its strip; the row drops with it, keeping the same 12 px rim
    above the bottom plot row as during a scan."""
    def gap():
        row = _scene_rect(h.item("bottomRightOverlay"))
        grid = _scene_rect(h.item("grid"))
        assert grid.contains(row)
        return grid.bottom() - row.bottom()

    assert not h.item("scrubber").isVisible()
    assert gap() == pytest.approx(12)
    h.bind(_StubLiveSource())
    assert h.item("scrubber").isVisible()
    assert gap() == pytest.approx(12)


@pytest.mark.parametrize("size", [VIEW_SIZE, (560, 420)])
def test_controls_clear_the_empty_state(h, size):
    """No device: the "No active cameras selected" placeholder fills the
    viewer; the row stays clear of it, and of the (hidden) scan badge."""
    h.stub.setSensorsConnected(False)
    h.view.resize(*size)
    _pump()
    text = next(o for o in _all_objects(h.root)
                if isinstance(o, QQuickItem)
                and o.property("text") == "No active cameras selected")
    assert text.isVisible()
    row = _scene_rect(h.item("bottomRightOverlay"))
    assert h.item("bottomRightOverlay").isVisible()
    assert not _scene_rect(text).intersects(row)
    assert QRectF(0, 0, *size).contains(row)
    assert not h.item("scanBadge").isVisible()
    assert h.warnings() == []
