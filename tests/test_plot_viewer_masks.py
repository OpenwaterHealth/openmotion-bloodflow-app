"""
Regression test for issue #150 — "All camera selection not reflected in
UI after updating Scan Settings".

Root cause: PlotViewer.qml's ``_devCellModel`` used to read the camera
masks from ``MotionInterface.appConfig.leftMask/rightMask`` (the
*persisted defaults*, which Scan Settings never touches and which carry
no change notification) instead of the live ``leftMask``/``rightMask``
selection that BloodFlow.qml owns. (The singleton was spelled
``MOTIONInterface`` when the bug shipped; it was renamed in 62195e8.)
Selecting "All" (0xFF) in Scan Settings updated the capture mask (the
scan really did record all 16 cameras) but the plot grid stayed pinned
at the config default 0x66 — the middle 4 cameras per sensor.

These tests load the real ``components/PlotViewer.qml`` in a bare
QQmlEngine with a stubbed ``MotionInterface`` singleton whose appConfig
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
    """Minimal MotionInterface stand-in covering every property/slot
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


# ── Issue #164 — grid mirrors the physical U-shape ─────────────────────
# A sensor module arranges cameras 1-8 in a U: 1 and 8 at the top of the
# U, 4 and 5 at the bottom. The grid displays an upside-down U — row 0
# pairs cams 4|5, then 3|6, 2|7, and 1|8 at the bottom. Left module owns
# columns 0-1, right module columns 2-3; rows with no selected camera on
# either side are dropped (the remaining rows compact upward).


def _pos(cells, side, cam_id):
    matches = [(c["row"], c["col"]) for c in cells
               if c["side"] == side and c["camId"] == cam_id]
    assert len(matches) == 1, \
        f"expected exactly one cell for {side} cam {cam_id}"
    return matches[0]


def test_all_masks_lay_out_as_upside_down_u(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", ALL_MASK)
    viewer.setProperty("rightMask", ALL_MASK)
    cells = _cells(viewer)
    assert len(cells) == 16
    # Row r pairs 1-based cams (4-r | 5+r): zero-based camIds 3-r | 4+r.
    for r in range(4):
        assert _pos(cells, "left", 3 - r) == (r, 0)
        assert _pos(cells, "left", 4 + r) == (r, 1)
        assert _pos(cells, "right", 3 - r) == (r, 2)
        assert _pos(cells, "right", 4 + r) == (r, 3)


def test_middle_masks_compact_to_4x2(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", MIDDLE_MASK)
    viewer.setProperty("rightMask", MIDDLE_MASK)
    cells = _cells(viewer)
    assert len(cells) == 8
    # Middle = cams 3,6 (U row 1) and 2,7 (U row 2) — rows compact to 0,1.
    assert _pos(cells, "left", 2) == (0, 0)
    assert _pos(cells, "left", 5) == (0, 1)
    assert _pos(cells, "right", 2) == (0, 2)
    assert _pos(cells, "right", 5) == (0, 3)
    assert _pos(cells, "left", 1) == (1, 0)
    assert _pos(cells, "left", 6) == (1, 1)
    assert _pos(cells, "right", 1) == (1, 2)
    assert _pos(cells, "right", 6) == (1, 3)


def test_right_module_alone_takes_first_column_pair(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", 0x00)
    viewer.setProperty("rightMask", MIDDLE_MASK)
    cells = _cells(viewer)
    assert len(cells) == 4
    assert _pos(cells, "right", 2) == (0, 0)
    assert _pos(cells, "right", 5) == (0, 1)
    assert _pos(cells, "right", 1) == (1, 0)
    assert _pos(cells, "right", 6) == (1, 1)


def test_unselected_slot_leaves_gap_in_its_row(viewer_factory):
    viewer = viewer_factory()
    # Left: only cam 4 (camId 3). Right: only cam 5 (camId 4). Both live
    # on U row 0; the unselected partners leave their slots empty.
    viewer.setProperty("leftMask", 0x08)
    viewer.setProperty("rightMask", 0x10)
    cells = _cells(viewer)
    assert len(cells) == 2
    assert _pos(cells, "left", 3) == (0, 0)
    assert _pos(cells, "right", 4) == (0, 3)


def test_row_compaction_is_shared_across_modules(viewer_factory):
    viewer = viewer_factory()
    # Left "Far" 0xC3 = cams 1,2,7,8 → U rows 2 and 3.
    # Right "Middle" 0x66 = cams 3,6 and 2,7 → U rows 1 and 2.
    # Union {1,2,3} compacts to display rows 0,1,2 shared by both sides.
    viewer.setProperty("leftMask", 0xC3)
    viewer.setProperty("rightMask", MIDDLE_MASK)
    cells = _cells(viewer)
    assert len(cells) == 8
    # U row 1 (cams 3|6) → display row 0: right side only.
    assert _pos(cells, "right", 2) == (0, 2)
    assert _pos(cells, "right", 5) == (0, 3)
    # U row 2 (cams 2|7) → display row 1: both sides.
    assert _pos(cells, "left", 1) == (1, 0)
    assert _pos(cells, "left", 6) == (1, 1)
    assert _pos(cells, "right", 1) == (1, 2)
    assert _pos(cells, "right", 6) == (1, 3)
    # U row 3 (cams 1|8) → display row 2: left side only.
    assert _pos(cells, "left", 0) == (2, 0)
    assert _pos(cells, "left", 7) == (2, 1)
