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


FAR_MASK = 0xC3  # cams 1,2,7,8 (1-based) → camIds 0,1,6,7


class _StubPastScanSource(QObject):
    """Minimal past-scan source exposing the camera-config masks the way
    data_sources.PastScanSource does. live=False marks it as a replayed
    scan; leftMask/rightMask carry the configuration that scan recorded.
    user_label/date_time back the viewer's "Viewing" badge (issue #245);
    pass live=True to stand in for a live source (badge hidden)."""

    _neverEmitted = pyqtSignal()

    def __init__(self, left_mask: int, right_mask: int, parent=None,
                 user_label: str = "", date_time: str = "",
                 live: bool = False):
        super().__init__(parent)
        self._left = int(left_mask)
        self._right = int(right_mask)
        self._user_label = str(user_label)
        self._date_time = str(date_time)
        self._live = bool(live)

    @pyqtProperty(bool, notify=_neverEmitted)
    def live(self):
        return self._live

    @pyqtProperty(int, notify=_neverEmitted)
    def leftMask(self):
        return self._left

    @pyqtProperty(int, notify=_neverEmitted)
    def rightMask(self):
        return self._right

    @pyqtProperty(str, notify=_neverEmitted)
    def userLabel(self):
        return self._user_label

    @pyqtProperty(str, notify=_neverEmitted)
    def dateTime(self):
        return self._date_time

    @pyqtProperty(float, notify=_neverEmitted)
    def liveEdge(self):
        return 0.0

    # Slots PlotViewer / PlotCell invoke during paint. Neutral returns —
    # this stub has no data; the test only inspects the grid model.
    @pyqtSlot(str, result="QVariantMap")
    @pyqtSlot(str, float, float, float, result="QVariantMap")
    def compute_bounds_for_metric(self, metric, lo=2.0, hi=98.0, pad=0.25):
        return {"yMin": 0.0, "yMax": 1.0}

    @pyqtSlot(str, int, str, float, float, int, result="QVariantList")
    def points_for_window(self, side, cam_id, metric, t_lo, t_hi, max_points):
        return []

    @pyqtSlot(str, int, str, float, result=float)
    def value_at(self, side, cam_id, metric, t):
        return float("nan")

    @pyqtSlot(str, int, int, str, float, float, int, result="QVariantList")
    def pair_points_for_window(self, side, cam_a, cam_b, metric,
                               t_lo, t_hi, max_points):
        return []

    @pyqtSlot(str, int, int, str, float, result=float)
    def pair_value_at(self, side, cam_a, cam_b, metric, t):
        return float("nan")

    @pyqtSlot(str, int, result=float)
    def dropped_at_for(self, side, cam_id):
        return float("nan")


class _StubMotionInterface(QObject):
    """Minimal MotionInterface stand-in covering every property/slot
    PlotViewer.qml (and its child components) actually reference."""

    currentScanSourceChanged = pyqtSignal()
    connectionStatusChanged = pyqtSignal()
    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scan_source = None
        # Both sensors present by default so the legacy grid tests (which
        # predate the issue-#298 connection gate) still render both sides
        # on the live-fallback path. The #298 tests flip these.
        self._left_connected = True
        self._right_connected = True

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        # Persisted defaults deliberately pinned to the middle-4 mask:
        # the issue-#150 bug was the grid reading these instead of the
        # live leftMask/rightMask properties. If the grid follows
        # appConfig again, the 0xFF assertions below fail.
        return {
            "leftMask": MIDDLE_MASK,
            "rightMask": MIDDLE_MASK,
            "engineeringMode": False,
            "showProfiling": False,
        }

    @pyqtProperty(QObject, notify=currentScanSourceChanged)
    def currentScanSource(self):
        return self._scan_source

    def setScanSource(self, source):
        self._scan_source = source
        self.currentScanSourceChanged.emit()

    @pyqtProperty(bool, notify=_neverEmitted)
    def liveSourceAvailable(self):
        return False

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def leftSensorConnected(self):
        return self._left_connected

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def rightSensorConnected(self):
        return self._right_connected

    def setSensorsConnected(self, left, right):
        self._left_connected = bool(left)
        self._right_connected = bool(right)
        self.connectionStatusChanged.emit()

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

    # Tests that need to drive currentScanSource reach the singleton stub
    # through the factory; reset it to None in a finally so the shared
    # module-scoped singleton can't leak a source into other tests.
    make.stub = stub

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


# ── Issue #175 — replayed scan keeps its own camera configuration ──────
# Loading a past scan must lay the grid out from the camera config that
# scan recorded, not the operator's current Scan Settings selection.
# PlotViewer reads the active source's leftMask/rightMask when the source
# exposes them (a past scan does); a live source reports -1 so the grid
# keeps following the live selection.


def test_past_scan_config_overrides_live_mask_selection(viewer_factory):
    viewer = viewer_factory()
    try:
        # Operator's live selection is All → 16-cell grid.
        viewer.setProperty("leftMask", ALL_MASK)
        viewer.setProperty("rightMask", ALL_MASK)
        assert len(_cells(viewer)) == 16

        # Replay a Far scan; the grid must adopt the Far config (cams
        # 1,2,7,8 → camIds 0,1,6,7, 8 cells), not stay at All.
        past = _StubPastScanSource(FAR_MASK, FAR_MASK)
        viewer_factory.stub.setScanSource(past)

        cells = _cells(viewer)
        assert _cam_ids(cells, "left") == [0, 1, 6, 7]
        assert _cam_ids(cells, "right") == [0, 1, 6, 7]
        assert len(cells) == 8
    finally:
        viewer_factory.stub.setScanSource(None)


# ── Issue #245 — "Viewing" badge names the replayed scan ───────────────
# The viewer shows a top-center badge identifying the loaded past scan
# (user label · date trimmed to minutes). It is hidden during live
# monitoring and when no scan is loaded.


def test_viewing_badge_names_loaded_past_scan(viewer_factory):
    viewer = viewer_factory()
    try:
        past = _StubPastScanSource(
            FAR_MASK, FAR_MASK,
            user_label="Patient A", date_time="2026-06-23 11:19:35")
        viewer_factory.stub.setScanSource(past)
        assert viewer.property("_showScanBadge") is True
        assert viewer.property("_scanBadgeText") == "Patient A · 2026-06-23 11:19"
    finally:
        viewer_factory.stub.setScanSource(None)


def test_viewing_badge_collapses_to_date_when_unlabeled(viewer_factory):
    viewer = viewer_factory()
    try:
        past = _StubPastScanSource(
            FAR_MASK, FAR_MASK,
            user_label="", date_time="2026-06-23 11:19:35")
        viewer_factory.stub.setScanSource(past)
        assert viewer.property("_showScanBadge") is True
        assert viewer.property("_scanBadgeText") == "2026-06-23 11:19"
    finally:
        viewer_factory.stub.setScanSource(None)


def test_viewing_badge_hidden_during_live(viewer_factory):
    viewer = viewer_factory()
    try:
        live = _StubPastScanSource(
            ALL_MASK, ALL_MASK,
            user_label="ignored", date_time="2026-06-23 11:19:35", live=True)
        viewer_factory.stub.setScanSource(live)
        assert viewer.property("_showScanBadge") is False
    finally:
        viewer_factory.stub.setScanSource(None)


def test_viewing_badge_hidden_without_source(viewer_factory):
    viewer = viewer_factory()
    viewer_factory.stub.setScanSource(None)
    assert viewer.property("_showScanBadge") is False


def test_unknown_source_masks_fall_back_to_live_selection(viewer_factory):
    """A source reporting -1 masks (e.g. a live source) leaves the grid
    following the operator's live Scan Settings selection."""
    viewer = viewer_factory()
    try:
        viewer.setProperty("leftMask", ALL_MASK)
        viewer.setProperty("rightMask", ALL_MASK)
        unknown = _StubPastScanSource(-1, -1)
        viewer_factory.stub.setScanSource(unknown)
        assert len(_cells(viewer)) == 16
    finally:
        viewer_factory.stub.setScanSource(None)


def test_zero_masks_render_empty_grid(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", 0x00)
    viewer.setProperty("rightMask", 0x00)
    assert _cells(viewer) == []


# ── Issue #164 — grid mirrors the physical U-shape ─────────────────────
# A sensor module arranges cameras 1-8 in a U: 1 and 8 at the top of the
# U, 4 and 5 at the bottom. The grid displays an upside-down U — row 0
# pairs cams 4|5, then 3|6, 2|7, and 1|8 at the bottom. Left module owns
# columns 0-1. When BOTH sides have active cameras a fixed side-break
# spacer occupies column 2 and the right module sits at columns 3-4; when
# only one side is active there is no spacer and that side starts at
# column 0. Rows with no selected camera on either side are dropped (the
# remaining rows compact upward).


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
    # Both sides active → the side-break spacer occupies column 2, so the
    # right module sits at columns 3-4 (left stays at 0-1).
    for r in range(4):
        assert _pos(cells, "left", 3 - r) == (r, 0)
        assert _pos(cells, "left", 4 + r) == (r, 1)
        assert _pos(cells, "right", 3 - r) == (r, 3)
        assert _pos(cells, "right", 4 + r) == (r, 4)


def test_middle_masks_compact_to_4x2(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", MIDDLE_MASK)
    viewer.setProperty("rightMask", MIDDLE_MASK)
    cells = _cells(viewer)
    assert len(cells) == 8
    # Middle = cams 3,6 (U row 1) and 2,7 (U row 2) — rows compact to 0,1.
    # Both sides active → right module shifts to columns 3-4 (spacer at 2).
    assert _pos(cells, "left", 2) == (0, 0)
    assert _pos(cells, "left", 5) == (0, 1)
    assert _pos(cells, "right", 2) == (0, 3)
    assert _pos(cells, "right", 5) == (0, 4)
    assert _pos(cells, "left", 1) == (1, 0)
    assert _pos(cells, "left", 6) == (1, 1)
    assert _pos(cells, "right", 1) == (1, 3)
    assert _pos(cells, "right", 6) == (1, 4)


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
    # Both sides active → spacer at col 2 pushes the right module to col 3-4.
    assert _pos(cells, "right", 4) == (0, 4)


def test_row_compaction_is_shared_across_modules(viewer_factory):
    viewer = viewer_factory()
    # Left "Far" 0xC3 = cams 1,2,7,8 → U rows 2 and 3.
    # Right "Middle" 0x66 = cams 3,6 and 2,7 → U rows 1 and 2.
    # Union {1,2,3} compacts to display rows 0,1,2 shared by both sides.
    viewer.setProperty("leftMask", 0xC3)
    viewer.setProperty("rightMask", MIDDLE_MASK)
    cells = _cells(viewer)
    assert len(cells) == 8
    # Both sides have active cameras → spacer at col 2, right module at 3-4.
    # U row 1 (cams 3|6) → display row 0: right side only.
    assert _pos(cells, "right", 2) == (0, 3)
    assert _pos(cells, "right", 5) == (0, 4)
    # U row 2 (cams 2|7) → display row 1: both sides.
    assert _pos(cells, "left", 1) == (1, 0)
    assert _pos(cells, "left", 6) == (1, 1)
    assert _pos(cells, "right", 1) == (1, 3)
    assert _pos(cells, "right", 6) == (1, 4)
    # U row 3 (cams 1|8) → display row 2: left side only.
    assert _pos(cells, "left", 0) == (2, 0)
    assert _pos(cells, "left", 7) == (2, 1)


# ── Issue #298 — single sensor draws only its own side ─────────────────
# The live leftMask/rightMask carry the config default (e.g. 0x99 = 4
# cams) regardless of which sensor ports are populated. On the live
# fallback (no past-scan source, so masks read -1) PlotViewer must gate
# each side on its sensor actually being connected, or a lone right-port
# sensor draws 4 phantom left plots. The gate re-flows on connect/
# disconnect because leftSensorConnected/rightSensorConnected notify.


def _clinical_cells(viewer):
    """Read the clinical (side-averaged) grid model."""
    val = viewer.property("_clinicalCellModel")
    if hasattr(val, "toVariant"):
        val = val.toVariant()
    assert isinstance(val, list)
    return val


def test_right_only_sensor_hides_left_plots(viewer_factory):
    """The reported bug: only the right sensor is connected, but the live
    leftMask default (0x99, 4 cams) would draw 4 left plots. The connection
    gate must drop them, leaving only the right side."""
    viewer = viewer_factory()
    try:
        viewer_factory.stub.setSensorsConnected(left=False, right=True)
        # BloodFlow.qml never clears leftMask when the left sensor is
        # absent — it keeps the config default. Reproduce that here.
        viewer.setProperty("leftMask", 0x99)
        viewer.setProperty("rightMask", 0x99)
        cells = _cells(viewer)
        assert _cam_ids(cells, "left") == []
        assert _cam_ids(cells, "right") == [0, 3, 4, 7]
        # Single side active ⇒ no side-break spacer, right module at col 0-1.
        assert viewer.property("_sideGapActive") is False
    finally:
        viewer_factory.stub.setSensorsConnected(left=True, right=True)


def test_left_only_sensor_hides_right_plots(viewer_factory):
    viewer = viewer_factory()
    try:
        viewer_factory.stub.setSensorsConnected(left=True, right=False)
        viewer.setProperty("leftMask", 0x99)
        viewer.setProperty("rightMask", 0x99)
        cells = _cells(viewer)
        assert _cam_ids(cells, "right") == []
        assert _cam_ids(cells, "left") == [0, 3, 4, 7]
        assert viewer.property("_sideGapActive") is False
    finally:
        viewer_factory.stub.setSensorsConnected(left=True, right=True)


def test_both_sensors_connected_render_both_sides(viewer_factory):
    viewer = viewer_factory()
    try:
        viewer_factory.stub.setSensorsConnected(left=True, right=True)
        viewer.setProperty("leftMask", 0x99)
        viewer.setProperty("rightMask", 0x99)
        cells = _cells(viewer)
        assert _cam_ids(cells, "left") == [0, 3, 4, 7]
        assert _cam_ids(cells, "right") == [0, 3, 4, 7]
        assert viewer.property("_sideGapActive") is True
    finally:
        viewer_factory.stub.setSensorsConnected(left=True, right=True)


def test_grid_reflows_on_left_sensor_connect_midsession(viewer_factory):
    """Re-flow on connect/disconnect: start right-only (left hidden), then
    the left sensor enumerates → left plots appear without re-touching the
    masks. Proves the binding tracks connectionStatusChanged."""
    viewer = viewer_factory()
    try:
        viewer.setProperty("leftMask", 0x99)
        viewer.setProperty("rightMask", 0x99)
        viewer_factory.stub.setSensorsConnected(left=False, right=True)
        assert _cam_ids(_cells(viewer), "left") == []

        # Left sensor plugs in mid-session.
        viewer_factory.stub.setSensorsConnected(left=True, right=True)
        assert _cam_ids(_cells(viewer), "left") == [0, 3, 4, 7]

        # And unplugs again — plots must disappear.
        viewer_factory.stub.setSensorsConnected(left=False, right=True)
        assert _cam_ids(_cells(viewer), "left") == []
    finally:
        viewer_factory.stub.setSensorsConnected(left=True, right=True)


def test_past_scan_ignores_connection_gate(viewer_factory):
    """A replayed past scan lays out from its recorded masks even with the
    matching sensor disconnected — you can review a two-sided scan with no
    hardware attached (issue #175 must survive the #298 gate)."""
    viewer = viewer_factory()
    try:
        viewer_factory.stub.setSensorsConnected(left=False, right=False)
        past = _StubPastScanSource(FAR_MASK, FAR_MASK)
        viewer_factory.stub.setScanSource(past)
        cells = _cells(viewer)
        assert _cam_ids(cells, "left") == [0, 1, 6, 7]
        assert _cam_ids(cells, "right") == [0, 1, 6, 7]
    finally:
        viewer_factory.stub.setScanSource(None)
        viewer_factory.stub.setSensorsConnected(left=True, right=True)


# ── Issue #289 — pair mode grid model ───────────────────────────────────
# Pair mode collapses each U row's two cameras (1-based 4-r | 5+r → pairs
# 4/5, 3/6, 2/7, 1/8 top-to-bottom, same order as the dev grid) into ONE
# cell per side rendering their averaged trace — ≤4 panels per side. A
# pair member missing from the mask falls the cell back to the single
# remaining camera (camIdB == -1 → plain single-cam PlotCell path); a
# row with neither camera selected is dropped. Left module owns column
# 0, right column 2 (column 1 is the side-break spacer). The averaging
# itself is Python-side — tests/test_plot_pair_mode.py.


def _pair_cells(viewer):
    val = viewer.property("_pairCellModel")
    if hasattr(val, "toVariant"):
        val = val.toVariant()
    assert isinstance(val, list)
    return val


def _active_cells(viewer):
    val = viewer.property("_activeCellModel")
    if hasattr(val, "toVariant"):
        val = val.toVariant()
    assert isinstance(val, list)
    return val


def _pair_pos(cells, side, cam_id, cam_id_b):
    matches = [(c["row"], c["col"]) for c in cells
               if c["side"] == side and c["camId"] == cam_id
               and c["camIdB"] == cam_id_b]
    assert len(matches) == 1, \
        f"expected exactly one cell for {side} pair {cam_id}/{cam_id_b}"
    return matches[0]


def test_pair_mode_all_masks_collapse_to_four_rows_per_side(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", ALL_MASK)
    viewer.setProperty("rightMask", ALL_MASK)
    viewer.setProperty("pairMode", True)
    cells = _pair_cells(viewer)
    assert len(cells) == 8  # 4 rows × 2 sides
    # Row r pairs zero-based camIds (3-r, 4+r) — same U order as the
    # dev grid (row 0 = 4/5 … row 3 = 1/8). Left col 0, right col 2.
    for r in range(4):
        assert _pair_pos(cells, "left", 3 - r, 4 + r) == (r, 0)
        assert _pair_pos(cells, "right", 3 - r, 4 + r) == (r, 2)


def test_pair_mode_rows_compact_like_dev_grid(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", MIDDLE_MASK)
    viewer.setProperty("rightMask", MIDDLE_MASK)
    viewer.setProperty("pairMode", True)
    cells = _pair_cells(viewer)
    assert len(cells) == 4
    # Middle = U rows 1 (cams 3|6) and 2 (cams 2|7), compacted to 0,1.
    assert _pair_pos(cells, "left", 2, 5) == (0, 0)
    assert _pair_pos(cells, "right", 2, 5) == (0, 2)
    assert _pair_pos(cells, "left", 1, 6) == (1, 0)
    assert _pair_pos(cells, "right", 1, 6) == (1, 2)


def test_pair_mode_masked_partner_falls_back_to_single_camera(viewer_factory):
    """One camera of a pair masked out → the cell carries only the
    remaining camera (camIdB -1), i.e. the plain single-camera render
    path, labeled with just that camera."""
    viewer = viewer_factory()
    # Left: cam 4 only (camId 3, U row 0) — partner cam 5 masked out.
    viewer.setProperty("leftMask", 0x08)
    viewer.setProperty("rightMask", 0x00)
    viewer.setProperty("pairMode", True)
    cells = _pair_cells(viewer)
    assert len(cells) == 1
    assert cells[0]["side"] == "left"
    assert cells[0]["camId"] == 3
    assert cells[0]["camIdB"] == -1
    assert (cells[0]["row"], cells[0]["col"]) == (0, 0)


def test_pair_mode_single_side_takes_first_column(viewer_factory):
    viewer = viewer_factory()
    viewer.setProperty("leftMask", 0x00)
    viewer.setProperty("rightMask", ALL_MASK)
    viewer.setProperty("pairMode", True)
    cells = _pair_cells(viewer)
    assert len(cells) == 4
    assert all(c["side"] == "right" and c["col"] == 0 for c in cells)


def test_active_model_follows_pair_mode_toggle(viewer_factory):
    """Toggling pair mode mid-session swaps the active grid model between
    the 16-cell dev grid and the 8-cell pair grid (and back)."""
    viewer = viewer_factory()
    viewer.setProperty("leftMask", ALL_MASK)
    viewer.setProperty("rightMask", ALL_MASK)
    viewer.setProperty("pairMode", False)
    assert len(_active_cells(viewer)) == 16
    viewer.setProperty("pairMode", True)
    cells = _active_cells(viewer)
    assert len(cells) == 8
    assert all(c["camIdB"] >= 0 for c in cells)
    viewer.setProperty("pairMode", False)
    assert len(_active_cells(viewer)) == 16


def test_pair_mode_defaults_off(viewer_factory):
    viewer = viewer_factory()
    assert viewer.property("pairMode") is False


def test_clinical_mode_wins_over_pair_mode(viewer_factory):
    """Clinical mode already averages (whole side, cam_id -1) — pair mode
    must not replace its 2-cell layout."""
    viewer = viewer_factory()
    try:
        viewer.setProperty("clinicalMode", True)
        viewer.setProperty("leftMask", ALL_MASK)
        viewer.setProperty("rightMask", ALL_MASK)
        viewer.setProperty("pairMode", True)
        cells = _active_cells(viewer)
        assert [c["camId"] for c in cells] == [-1, -1]
    finally:
        viewer.setProperty("clinicalMode", False)
        viewer.setProperty("pairMode", False)


def test_pair_mode_respects_connection_gate(viewer_factory):
    """The issue-#298 per-side connection gate applies unchanged: a lone
    right sensor must not draw phantom left pair panels."""
    viewer = viewer_factory()
    try:
        viewer_factory.stub.setSensorsConnected(left=False, right=True)
        viewer.setProperty("leftMask", 0x99)
        viewer.setProperty("rightMask", 0x99)
        viewer.setProperty("pairMode", True)
        cells = _pair_cells(viewer)
        assert all(c["side"] == "right" for c in cells)
        # 0x99 = cams 1,4,5,8 → U rows 0 (4/5) and 3 (1/8), compacted.
        assert _pair_pos(cells, "right", 3, 4) == (0, 0)
        assert _pair_pos(cells, "right", 0, 7) == (1, 0)
    finally:
        viewer_factory.stub.setSensorsConnected(left=True, right=True)
        viewer.setProperty("pairMode", False)


def test_pair_mode_replay_uses_recorded_masks(viewer_factory):
    """Pair mode on a replayed past scan lays out from the scan's own
    recorded masks (issue #175 semantics carry over)."""
    viewer = viewer_factory()
    try:
        viewer.setProperty("leftMask", ALL_MASK)
        viewer.setProperty("rightMask", ALL_MASK)
        viewer.setProperty("pairMode", True)
        past = _StubPastScanSource(FAR_MASK, FAR_MASK)
        viewer_factory.stub.setScanSource(past)
        cells = _pair_cells(viewer)
        # Far = cams 1,2,7,8 → U rows 2 (2/7) and 3 (1/8), both sides.
        assert len(cells) == 4
        assert _pair_pos(cells, "left", 1, 6) == (0, 0)
        assert _pair_pos(cells, "right", 1, 6) == (0, 2)
        assert _pair_pos(cells, "left", 0, 7) == (1, 0)
        assert _pair_pos(cells, "right", 0, 7) == (1, 2)
    finally:
        viewer_factory.stub.setScanSource(None)
        viewer.setProperty("pairMode", False)


def test_clinical_model_drops_disconnected_side(viewer_factory):
    """Clinical mode: a right-only sensor shows only the RIGHT side-average
    cell, compacted to row 0 — no blank LEFT panel/row."""
    viewer = viewer_factory()
    try:
        viewer.setProperty("clinicalMode", True)
        viewer.setProperty("leftMask", 0xC3)
        viewer.setProperty("rightMask", 0xC3)
        viewer_factory.stub.setSensorsConnected(left=False, right=True)
        cells = _clinical_cells(viewer)
        assert len(cells) == 1
        assert cells[0]["side"] == "right"
        assert cells[0]["row"] == 0

        # Both connected → both cells, stacked.
        viewer_factory.stub.setSensorsConnected(left=True, right=True)
        cells = _clinical_cells(viewer)
        assert [c["side"] for c in cells] == ["left", "right"]
        assert [c["row"] for c in cells] == [0, 1]
    finally:
        viewer_factory.stub.setSensorsConnected(left=True, right=True)
        viewer.setProperty("clinicalMode", False)
