"""
Regression test for issue #150 — "All camera selection not reflected in
UI after updating Scan Settings".

Root cause: PlotViewer.qml's ``_devCellModel`` used to read the camera
masks from ``MOTIONInterface.appConfig.leftMask/rightMask`` (the
*persisted defaults*, which Scan Settings never touches and which carry
no change notification) instead of the live ``leftMask``/``rightMask``
selection that BloodFlow.qml owns. Selecting "All" (0xFF) in Scan
Settings updated the capture mask (the scan really did record all 16
cameras) but the plot grid stayed pinned at the config default 0x66 —
the middle 4 cameras per sensor.

These tests load the real ``components/PlotViewer.qml`` in a bare
QQmlEngine with a stubbed ``MOTIONInterface`` singleton whose appConfig
deliberately carries the middle-4 masks, then drive the viewer's
``leftMask``/``rightMask`` properties exactly the way BloodFlow.qml's
bindings do after Scan Settings closes. If the grid model ever regresses
to reading appConfig (or stops re-evaluating on mask change), the
"All → 16 cells" assertion fails.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import contextlib
import os
import sys
from pathlib import Path

# A QGuiApplication must exist before any QML Quick item is created, and
# it must be constructed before conftest's session-scoped autouse
# fixture instantiates a bare QCoreApplication (a QGuiApplication cannot
# be created once a plain QCoreApplication exists). Module import runs
# at collection time — ahead of every fixture — so create it here. The
# offscreen platform is passed via argv, NOT via QT_QPA_PLATFORM, so the
# process environment is untouched (HIL tests in the same session launch
# the real app as a subprocess and must not inherit an offscreen
# platform).
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

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
PLOT_VIEWER_QML = REPO_ROOT / "components" / "PlotViewer.qml"

MIDDLE_MASK = 0x66  # cams 2,3,6,7 (1-based) — the persisted config default
ALL_MASK = 0xFF


class _StubMotionInterface(QObject):
    """Minimal MOTIONInterface stand-in covering every property/slot
    PlotViewer.qml (and its child components) actually reference."""

    _neverEmitted = pyqtSignal()

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        # Persisted defaults deliberately pinned to the middle-4 mask:
        # the issue-#150 bug was the grid reading these instead of the
        # live leftMask/rightMask properties. If the grid follows
        # appConfig again, the 0xFF assertions below fail.
        return {
            "leftMask": MIDDLE_MASK,
            "rightMask": MIDDLE_MASK,
            "developerMode": False,
            "showProfiling": False,
        }

    @pyqtProperty(QObject, notify=_neverEmitted)
    def currentScanSource(self):
        return None

    @pyqtProperty(bool, notify=_neverEmitted)
    def liveSourceAvailable(self):
        return False

    @pyqtSlot()
    def showLiveSource(self):
        pass


@contextlib.contextmanager
def _basic_controls_style():
    """Temporarily force the always-available Basic Controls style.

    The platform-native "Windows" style needs a style-impl plugin DLL
    that fails to load in a bare offscreen test process. PyQt6 doesn't
    ship the QtQuickControls2 binding (QQuickStyle), so the env var is
    the only knob — set it just for QML compilation and restore right
    after, so HIL app subprocesses launched later in the same pytest
    session can't inherit it.
    """
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
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MOTIONInterface", stub)
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

    yield make

    for obj in created:
        obj.deleteLater()
    # Keep stub/engine alive until after teardown.
    del engine
    del stub


def _cells(viewer):
    """Read the viewer's grid model as a list of {side, camId, row, col}."""
    val = viewer.property("_devCellModel")
    if hasattr(val, "toVariant"):  # var properties may surface as QJSValue
        val = val.toVariant()
    assert isinstance(val, list)
    return val


def _cam_ids(cells, side):
    return sorted(c["camId"] for c in cells if c["side"] == side)


def test_default_masks_render_middle_four_per_side(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", MIDDLE_MASK)
    viewer.setProperty("rightMask", MIDDLE_MASK)
    cells = _cells(viewer)
    assert len(cells) == 8
    assert _cam_ids(cells, "left") == [1, 2, 5, 6]
    assert _cam_ids(cells, "right") == [1, 2, 5, 6]


def test_all_selection_immediately_expands_grid_to_16_cells(viewer_factory):
    """Issue #150 repro: middle-4 grid, then Scan Settings switches both
    sides to All. The grid model must re-evaluate from the live mask
    properties (16 cells) even though appConfig still says 0x66."""
    viewer = viewer_factory()
    viewer.setProperty("leftMask", MIDDLE_MASK)
    viewer.setProperty("rightMask", MIDDLE_MASK)
    assert len(_cells(viewer)) == 8

    # BloodFlow.qml's bindings push the new Scan Settings selection in.
    viewer.setProperty("leftMask", ALL_MASK)
    viewer.setProperty("rightMask", ALL_MASK)

    cells = _cells(viewer)
    assert len(cells) == 16
    assert _cam_ids(cells, "left") == list(range(8))
    assert _cam_ids(cells, "right") == list(range(8))


def test_single_side_all_keeps_other_side_unchanged(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", ALL_MASK)
    viewer.setProperty("rightMask", MIDDLE_MASK)
    cells = _cells(viewer)
    assert _cam_ids(cells, "left") == list(range(8))
    assert _cam_ids(cells, "right") == [1, 2, 5, 6]
    assert len(cells) == 12


def test_zero_masks_render_empty_grid(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", 0x00)
    viewer.setProperty("rightMask", 0x00)
    assert _cells(viewer) == []
