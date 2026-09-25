"""
Issue #622 — the plot's ⋯ menu as segmented switches.

The popup holds three segmented "bubble" switches — View (Individual |
Aggregate | Average), Scale (Fixed | Global | Per Plot) and Metrics
(BFI / BVI | Mean / Contrast) — plus the engineering-only Profiler
switch. The axis-labels toggle is gone; labels stay on, with the
plumbing kept so the toggle can come back.

Covered here:
  - the Scale mapping both ways (scaleMode / setScaleMode), including
    the write order and what each choice leaves alone;
  - the switches themselves, driven through their picked signal (the
    Popup needs a window to open, but its content exists without one);
  - axis labels: always on, whatever the persisted preference says;
  - static guards: research-only rows, no axis-label writer, and the
    host persisting the popup's autoscale and metric choices.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import re
from pathlib import Path

# test_average_view_mode creates the offscreen QGuiApplication at import
# (it must exist before conftest's bare QCoreApplication); reuse its stubs.
from test_average_view_mode import (  # noqa: E402
    ALL_MASK,
    PLOT_VIEWER_QML,
    _StubLiveSource,
    _StubMotionInterface,
    _basic_controls_style,
)

import pytest  # noqa: E402
from PyQt6.QtCore import (  # noqa: E402
    Q_ARG,
    QCoreApplication,
    QEvent,
    QMetaObject,
    Qt,
    QUrl,
    pyqtSlot,
)
from PyQt6.QtQml import (  # noqa: E402
    QQmlComponent,
    QQmlEngine,
    qmlRegisterSingletonInstance,
)
from PyQt6 import sip  # noqa: E402
from PyQt6.QtQuick import QQuickItem  # noqa: E402

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
BLOODFLOW_QML = REPO_ROOT / "pages" / "BloodFlow.qml"


class _RecordingStub(_StubMotionInterface):
    """Records every setConfig write, in order."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.writes = []

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

    make.stub = stub
    yield make
    # Destroy the viewers while their engine is still alive (see the same
    # teardown in test_aggregate_view_mode): left to a bare deleteLater
    # they outlive it into the next module's event pump.
    for obj in created:
        obj.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    del engine
    del stub


@pytest.fixture
def menu_viewer(viewer_factory):
    stub = viewer_factory.stub
    stub.setConfig("plotViewMode", "individual")
    stub.setConfig("autoScalePerPlot", False)
    viewer = viewer_factory()
    viewer.setProperty("clinicalMode", False)
    viewer.setProperty("leftMask", ALL_MASK)
    viewer.setProperty("rightMask", ALL_MASK)
    viewer.setProperty("autoScale", False)
    src = _StubLiveSource()
    stub.setScanSource(src)
    requests = {"autoScale": [], "bfiBvi": []}
    viewer.autoScaleToggleRequested.connect(requests["autoScale"].append)
    viewer.displayModeToggleRequested.connect(requests["bfiBvi"].append)
    stub.writes.clear()
    try:
        yield viewer, stub, requests
    finally:
        stub.setScanSource(None)
        stub.setConfig("plotViewMode", "individual")
        stub.setConfig("autoScalePerPlot", False)
        stub.setConfig("showAxisLabels", True)


def _all_objects(obj, seen=None):
    """Every QObject below obj — QObject children and visual children —
    so the Popup (not an Item) and its content are reached too. Deduped
    by the C++ pointer: PyQt hands out short-lived wrappers, so Python
    id()s get reused and would prune whole subtrees."""
    seen = set() if seen is None else seen
    ptr = sip.unwrapinstance(obj)
    if ptr in seen:
        return
    seen.add(ptr)
    yield obj
    kids = list(obj.children())
    if isinstance(obj, QQuickItem):
        kids += obj.childItems()
    for k in kids:
        yield from _all_objects(k, seen)


def _segmented(viewer):
    """The popup's segmented switches, keyed by their first option."""
    out = {}
    for o in _all_objects(viewer):
        if o.metaObject().indexOfSignal("picked(QString)") < 0:
            continue
        opts = o.property("options")
        opts = opts.toVariant() if hasattr(opts, "toVariant") else opts
        out[opts[0]["value"]] = o
    return out


def _pick(seg, value):
    QMetaObject.invokeMethod(seg, "picked", Qt.ConnectionType.DirectConnection,
                             Q_ARG(str, value))


def _options(seg):
    opts = seg.property("options")
    opts = opts.toVariant() if hasattr(opts, "toVariant") else opts
    return [(o["value"], o["label"]) for o in opts]


# ── Scale mapping ───────────────────────────────────────────────────────


@pytest.mark.parametrize("auto, per_plot, mode", [
    (False, False, "fixed"),
    (False, True, "fixed"),
    (True, False, "global"),
    (True, True, "perPlot"),
])
def test_scale_mode_reads_the_two_flags(menu_viewer, auto, per_plot, mode):
    viewer, stub, _ = menu_viewer
    stub.setConfig("autoScalePerPlot", per_plot)
    viewer.setProperty("autoScale", auto)
    assert viewer.property("scaleMode") == mode


def test_fixed_to_per_plot_sets_the_flag_then_turns_autoscale_on(menu_viewer):
    viewer, stub, req = menu_viewer
    QMetaObject.invokeMethod(viewer, "setScaleMode", Q_ARG("QVariant", "perPlot"))
    assert stub.writes == [("autoScalePerPlot", True)]
    assert req["autoScale"] == [True]


def test_per_plot_to_global_only_clears_the_flag(menu_viewer):
    viewer, stub, req = menu_viewer
    stub.setConfig("autoScalePerPlot", True)
    viewer.setProperty("autoScale", True)
    stub.writes.clear()
    QMetaObject.invokeMethod(viewer, "setScaleMode", Q_ARG("QVariant", "global"))
    assert stub.writes == [("autoScalePerPlot", False)]
    assert req["autoScale"] == []


def test_fixed_turns_autoscale_off_and_keeps_the_per_plot_choice(menu_viewer):
    viewer, stub, req = menu_viewer
    stub.setConfig("autoScalePerPlot", True)
    viewer.setProperty("autoScale", True)
    stub.writes.clear()
    QMetaObject.invokeMethod(viewer, "setScaleMode", Q_ARG("QVariant", "fixed"))
    assert stub.writes == []
    assert req["autoScale"] == [False]


def test_reselecting_the_current_scale_writes_nothing(menu_viewer):
    viewer, stub, req = menu_viewer
    viewer.setProperty("autoScale", True)
    QMetaObject.invokeMethod(viewer, "setScaleMode", Q_ARG("QVariant", "global"))
    assert stub.writes == [] and req["autoScale"] == []


# ── The switches ────────────────────────────────────────────────────────


def test_popup_has_the_three_segmented_switches(menu_viewer):
    viewer, _stub, _req = menu_viewer
    seg = _segmented(viewer)
    assert _options(seg["individual"]) == [
        ("individual", "Individual"), ("aggregate", "Aggregate"),
        ("average", "Average")]
    assert _options(seg["fixed"]) == [
        ("fixed", "Fixed"), ("global", "Global"), ("perPlot", "Per Plot")]
    assert _options(seg["bfi_bvi"]) == [
        ("bfi_bvi", "BFI / BVI"), ("mean_contrast", "Mean / Contrast")]


def test_switches_show_the_current_state(menu_viewer):
    viewer, stub, _req = menu_viewer
    seg = _segmented(viewer)
    assert seg["individual"].property("current") == "individual"
    assert seg["fixed"].property("current") == "fixed"
    assert seg["bfi_bvi"].property("current") == "bfi_bvi"
    stub.setConfig("plotViewMode", "aggregate")
    stub.setConfig("autoScalePerPlot", True)
    viewer.setProperty("autoScale", True)
    viewer.setProperty("displayMode", "mean_contrast")
    assert seg["individual"].property("current") == "aggregate"
    assert seg["fixed"].property("current") == "perPlot"
    assert seg["bfi_bvi"].property("current") == "mean_contrast"


def test_view_switch_writes_the_view_mode(menu_viewer):
    viewer, stub, _req = menu_viewer
    _pick(_segmented(viewer)["individual"], "aggregate")
    assert stub.writes == [("plotViewMode", "aggregate")]
    assert viewer.property("aggregateView") is True


def test_scale_switch_goes_through_set_scale_mode(menu_viewer):
    viewer, stub, req = menu_viewer
    _pick(_segmented(viewer)["fixed"], "perPlot")
    assert stub.writes == [("autoScalePerPlot", True)]
    assert req["autoScale"] == [True]


def test_metrics_switch_requests_the_display_pair(menu_viewer):
    viewer, _stub, req = menu_viewer
    _pick(_segmented(viewer)["bfi_bvi"], "mean_contrast")
    assert req["bfiBvi"] == [False]


def test_switches_share_one_center_line(menu_viewer):
    """Each switch sits centered in a slot as wide as the widest one, so
    they all line up on one center whatever their option lengths (the
    three above plus Statistics, #635)."""
    viewer, _stub, _req = menu_viewer
    seg = list(_segmented(viewer).values())
    widths = [s.property("width") for s in seg]
    assert len(seg) == 4
    assert len(set(widths)) == 4                   # genuinely different
    slots = [s.parentItem() for s in seg]
    assert {sl.property("width") for sl in slots} == {max(widths)}
    for s, sl in zip(seg, slots):
        assert s.property("x") + s.property("width") / 2             == pytest.approx(sl.property("width") / 2)


def test_no_on_off_switches_left_but_the_profiler():
    """Static: the only PopupPillSwitch instance is the Profiler's."""
    qml = PLOT_VIEWER_QML.read_text(encoding="utf-8")
    uses = re.findall(r"PopupPillSwitch \{\s*id: (\w+)", qml)
    assert uses == ["profilerSwitch"]


# ── Axis labels ─────────────────────────────────────────────────────────


def test_axis_labels_stay_on_whatever_the_preference_says(menu_viewer):
    """A user who had switched labels off before the toggle went away
    must not be stranded without them."""
    viewer, stub, _req = menu_viewer
    stub.setConfig("showAxisLabels", False)
    assert viewer.property("showAxisLabels") is True
    cells = [o for o in _all_objects(viewer)
             if o.metaObject().className().startswith("PlotCell")]
    assert cells and all(c.property("showAxisLabels") is True for c in cells)


def test_axis_labels_can_still_be_switched_in_code(menu_viewer):
    viewer, _stub, _req = menu_viewer
    viewer.setProperty("showAxisLabels", False)
    cells = [o for o in _all_objects(viewer)
             if o.metaObject().className().startswith("PlotCell")]
    assert cells and all(c.property("showAxisLabels") is False for c in cells)


def test_nothing_in_the_popup_writes_show_axis_labels():
    qml = PLOT_VIEWER_QML.read_text(encoding="utf-8")
    assert 'setConfig("showAxisLabels"' not in qml


# ── Static gates ────────────────────────────────────────────────────────


@pytest.mark.parametrize("row_id, gate", [
    ("viewModeRow", "visible: !viewer.clinicalMode && !viewer.effectiveClinical"),
    ("scaleRow", "visible: !viewer.effectiveClinical"),
    ("metricsRow", "visible: !viewer.effectiveClinical"),
])
def test_research_rows_are_hidden_in_clinical(row_id, gate):
    qml = PLOT_VIEWER_QML.read_text(encoding="utf-8")
    idx = qml.index("id: " + row_id)
    assert gate in qml[idx:idx + 120]


def test_host_persists_the_popup_autoscale_and_metric_choices():
    """BloodFlow.qml applies both requests to settingsModal AND writes
    them through setConfig, so they survive the Settings modal reloading
    from config on open, and a restart."""
    qml = BLOODFLOW_QML.read_text(encoding="utf-8")
    a = qml.index("onAutoScaleToggleRequested")
    assert 'MotionInterface.setConfig("autoScale", enabled)' in qml[a:a + 200]
    d = qml.index("onDisplayModeToggleRequested")
    assert 'MotionInterface.setConfig("showBfiBvi", bfiBviMode)' in qml[d:d + 200]
