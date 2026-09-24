"""
Issue #606 — research "Average" plot view.

Research users get the clinical-style view back as a display choice in
the plot's ⋯ menu: Individual (one plot per camera, unchanged) or Average
(one plot per sensor module showing the average of that module's enabled
cameras, plus the large readout panels). Research scans keep their
per-camera record, so the average is derived on the display side and
stored under cam_id=-1, the key the clinical SDK stream already uses.

Covered here:
  - the bulk derivation (past scans, the sample CSV, the live DB tail):
    NaN-aware per-capture mean, grouping by frame_id, clinical recordings
    left alone;
  - the live accumulator in LiveScanSource, including the scan-end flush
    and the BVI low-pass applied to the averaged stream;
  - autoscale: the Individual fit ignores the derived streams, the Average
    fit uses only them;
  - PlotViewer.qml: the view switch re-lays the grid, clinical is
    unaffected and ignores a stale preference;
  - the connector and config gates that keep clinical builds untouched.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import contextlib
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace

# A QGuiApplication must exist before any QML Quick item is created, and
# before conftest's session fixture creates a bare QCoreApplication (see
# test_plot_viewer_autoscale.py).
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

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PyQt6.QtQml import (  # noqa: E402
    QQmlComponent,
    QQmlEngine,
    qmlRegisterSingletonInstance,
)
from PyQt6.QtQuick import QQuickItem  # noqa: E402

from config import app_config  # noqa: E402
from data_sources import (  # noqa: E402
    LiveScanSource,
    PastScanSource,
    _CameraBuffer,
    bvi_lpf_alpha,
    derive_side_average_buffers,
    load_csv_scan_buffers,
)
from motion_connector import MotionConnector, _LivePlotSink  # noqa: E402

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
PLOT_VIEWER_QML = REPO_ROOT / "components" / "PlotViewer.qml"
SAMPLE_SCAN_CSV = REPO_ROOT / "resources" / "sample_scan.csv"
NAN = float("nan")


def _buf(samples, derived=False):
    """Unbounded buffer from [(t, v, frame_id), ...]."""
    b = _CameraBuffer(max_capacity=None)
    for t, v, fid in samples:
        b.append(t=t, v=v, frame_id=fid)
    b.derived = derived
    return b


def _arr(buf):
    return (buf.t[:buf.n].tolist(), buf.v[:buf.n].tolist(),
            buf.frame_id[:buf.n].tolist())


# ── Bulk derivation ─────────────────────────────────────────────────────


def test_derive_averages_each_capture_across_cameras():
    buffers = {
        ("left", 0, "bfi"): _buf([(0.000, 1.0, 10), (0.025, 3.0, 11)]),
        ("left", 3, "bfi"): _buf([(0.001, 3.0, 10), (0.026, 5.0, 11)]),
        ("right", 7, "bfi"): _buf([(0.002, 8.0, 10)]),
    }
    out = derive_side_average_buffers(buffers)
    assert set(out) == {("left", -1, "bfi"), ("right", -1, "bfi")}
    t, v, fid = _arr(out[("left", -1, "bfi")])
    assert fid == [10, 11]
    assert v == pytest.approx([2.0, 4.0])
    # Stamped with the capture's earliest camera timestamp.
    assert t == pytest.approx([0.000, 0.025])
    assert _arr(out[("right", -1, "bfi")])[1] == pytest.approx([8.0])
    assert all(b.derived for b in out.values())


def test_derive_skips_unlit_cameras_and_keeps_all_unlit_as_nan():
    """NaN-aware like the SDK's spatial_side_average: an unlit camera
    drops out of the mean instead of blanking it; a capture where every
    camera is unlit stays NaN so the trace shows a gap."""
    buffers = {
        ("left", 0, "bvi"): _buf([(0.0, NAN, 1), (0.025, NAN, 2)]),
        ("left", 1, "bvi"): _buf([(0.0, 6.0, 1), (0.025, NAN, 2)]),
    }
    _t, v, _f = _arr(derive_side_average_buffers(buffers)[("left", -1, "bvi")])
    assert v[0] == pytest.approx(6.0)
    assert math.isnan(v[1])


def test_derive_output_is_time_ordered():
    """window_indices needs non-decreasing t even if frame_id order and
    time order disagree (timestamp-repaired capture)."""
    buffers = {("left", 2, "mean"): _buf([(0.05, 1.0, 5), (0.10, 2.0, 4)])}
    t, v, fid = _arr(derive_side_average_buffers(buffers)[("left", -1, "mean")])
    assert t == sorted(t)
    assert fid == [5, 4]


def test_derive_ignores_existing_side_average_streams():
    buffers = {
        ("left", 0, "bfi"): _buf([(0.0, 2.0, 1)]),
        ("left", -1, "bfi"): _buf([(0.0, 100.0, 1)]),
    }
    v = _arr(derive_side_average_buffers(buffers)[("left", -1, "bfi")])[1]
    assert v == pytest.approx([2.0])


class _FakeScanDB:
    """Enough of omotion.ScanDatabase for the bulk loader."""

    def __init__(self, rows):
        self._rows = rows

    def iter_session_data(self, session_id, t_lo=None, t_hi=None):
        for r in self._rows:
            if t_lo is not None and r["timestamp_s"] < t_lo:
                continue
            if t_hi is not None and r["timestamp_s"] > t_hi:
                continue
            yield r


def _row(side, cam_id, fid, t, bfi, bvi=5.0, mean=100.0, contrast=0.3):
    return {"side": side, "cam_id": cam_id, "frame_id": fid,
            "timestamp_s": t, "bfi": bfi, "bvi": bvi, "mean": mean,
            "contrast": contrast}


def test_past_research_scan_gets_side_average_but_keeps_its_mode():
    db = _FakeScanDB([
        _row(0, 0, 1, 0.0, 1.0), _row(0, 7, 1, 0.0, 3.0),
        _row(0, 0, 2, 0.025, 2.0), _row(0, 7, 2, 0.025, 4.0),
    ])
    src = PastScanSource(scan_db=db, session_id=1)
    assert ("left", -1, "bfi") not in src.buffers   # loader flag off

    from data_sources import load_past_scan_buffers
    buffers, _ = load_past_scan_buffers(db, 1, derive_side_average=True)
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers=buffers)
    try:
        assert src.clinicalMode == 0          # still a per-camera recording
        assert src.leftMask == 0x81           # derived streams don't widen it
        assert src.value_at("left", -1, "bfi", 0.025) == pytest.approx(3.0)
        for m in ("bfi", "bvi", "mean", "contrast"):
            assert ("left", -1, m) in src.buffers
    finally:
        src.release()


def test_clinical_recording_is_left_alone():
    """A clinical scan already stores only the SDK side average; deriving
    must not touch (or duplicate) it."""
    from data_sources import load_past_scan_buffers
    db = _FakeScanDB([_row(0, -1, 1, 0.0, 7.0)])
    buffers, _ = load_past_scan_buffers(db, 1, derive_side_average=True)
    b = buffers[("left", -1, "bfi")]
    assert not b.derived
    assert _arr(b)[1] == pytest.approx([7.0])


@pytest.mark.skipif(not SAMPLE_SCAN_CSV.exists(), reason="no sample scan")
def test_sample_scan_offers_the_average_view():
    buffers = load_csv_scan_buffers(str(SAMPLE_SCAN_CSV),
                                    derive_side_average=True)
    per_cam = [k for k in buffers if k[1] >= 0 and k[2] == "bfi"]
    assert per_cam
    for side in {k[0] for k in per_cam}:
        avg = buffers[(side, -1, "bfi")]
        assert avg.n > 0 and avg.derived


# ── Autoscale ───────────────────────────────────────────────────────────


def test_individual_fit_ignores_derived_streams_average_fit_uses_only_them():
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers={
        ("left", 0, "bfi"): _buf([(i * 0.025, float(v), i)
                                  for i, v in enumerate(range(0, 100))]),
        ("left", -1, "bfi"): _buf([(i * 0.025, 1000.0 + i, i)
                                   for i in range(100)], derived=True),
    })
    try:
        indiv = src.compute_bounds_for_metric("bfi")
        assert indiv["yMax"] < 200.0        # the 1000+ average never leaks in
        avg = src.compute_bounds_for_side_average("bfi")
        assert avg["yMin"] > 900.0
    finally:
        src.release()


# ── Live accumulator ────────────────────────────────────────────────────


def _live(derive=True, cutoff=0.0):
    return LiveScanSource(plot_t0=0.0, derive_side_average=derive,
                          bvi_lpf_cutoff_hz=cutoff)


def test_live_average_emits_when_the_next_capture_starts_and_at_flush():
    src = _live()
    try:
        src.append_uncorrected("left", 0, 1, 0.000, bfi=1.0, bvi=4.0,
                               mean=100.0, contrast=0.2)
        src.append_uncorrected("left", 7, 1, 0.001, bfi=3.0, bvi=6.0,
                               mean=200.0, contrast=0.4)
        assert ("left", -1, "bfi") not in src.buffers  # capture still open
        src.append_uncorrected("left", 0, 2, 0.025, bfi=5.0, bvi=8.0)
        avg = src.buffers[("left", -1, "bfi")]
        assert _arr(avg) == ([0.0], [2.0], [1])
        assert src.value_at("left", -1, "bvi", 0.0) == pytest.approx(5.0)
        assert src.value_at("left", -1, "mean", 0.0) == pytest.approx(150.0)
        assert src.value_at("left", -1, "contrast", 0.0) == pytest.approx(0.3)
        assert avg.derived

        src.flush_side_average()   # scan end: the open capture lands
        assert _arr(avg)[1] == pytest.approx([2.0, 5.0])
        # Capture 2 had no mean/contrast from any camera → none appended.
        assert src.buffers[("left", -1, "mean")].n == 1
        src.flush_side_average()   # idempotent
        assert avg.n == 2
    finally:
        src.release()


def test_live_average_is_per_side_and_nan_aware():
    src = _live()
    try:
        src.append_uncorrected("left", 0, 1, 0.0, bfi=NAN, bvi=NAN)
        src.append_uncorrected("left", 1, 1, 0.0, bfi=4.0, bvi=2.0)
        src.append_uncorrected("right", 0, 1, 0.0, bfi=NAN, bvi=NAN)
        src.flush_side_average()
        assert src.value_at("left", -1, "bfi", 0.0) == pytest.approx(4.0)
        assert math.isnan(src.value_at("right", -1, "bfi", 0.0))
        # The all-unlit capture still advanced the side's stream.
        assert src.buffers[("right", -1, "bfi")].n == 1
    finally:
        src.release()


def test_live_average_bvi_is_averaged_raw_then_low_passed():
    src = _live(cutoff=20.0)
    alpha = bvi_lpf_alpha(20.0)
    try:
        for fid, (a, b) in enumerate([(2.0, 4.0), (6.0, 8.0)]):
            src.append_uncorrected("left", 0, fid, fid * 0.025, bfi=1.0, bvi=a)
            src.append_uncorrected("left", 1, fid, fid * 0.025, bfi=1.0, bvi=b)
        src.flush_side_average()
        v = _arr(src.buffers[("left", -1, "bvi")])[1]
        assert v[0] == pytest.approx(3.0)               # seeds the filter
        assert v[1] == pytest.approx(3.0 + alpha * (7.0 - 3.0))
    finally:
        src.release()


def test_clinical_live_source_never_derives():
    src = _live(derive=False)
    try:
        src.append_uncorrected("left", 0, 1, 0.0, bfi=1.0, bvi=1.0)
        src.append_uncorrected("left", 0, 2, 0.025, bfi=1.0, bvi=1.0)
        src.flush_side_average()
        assert not any(k[1] == -1 for k in src.buffers)
    finally:
        src.release()


def test_plot_sink_flushes_the_average_at_scan_end():
    calls = []
    sink = _LivePlotSink(
        connector=SimpleNamespace(_app_config={}), plot_t0=0.0,
        live_source=SimpleNamespace(
            flush_side_average=lambda: calls.append("flush")))
    sink.on_complete()
    assert calls == ["flush"]


# ── Connector + config gates ────────────────────────────────────────────


@pytest.mark.parametrize("clinical, expected", [(False, True), (True, False)])
def test_connector_derives_only_in_research_builds(clinical, expected):
    conn = SimpleNamespace(_app_config={"clinicalMode": clinical})
    assert MotionConnector._derive_side_average(conn) is expected


def test_plot_view_mode_is_a_persisted_preference_defaulting_to_individual():
    assert app_config.APP_CONFIG["plotViewMode"] == "individual"
    assert app_config.tier_of("plotViewMode") == app_config.PREFERENCE


# ── PlotViewer.qml ──────────────────────────────────────────────────────

ALL_MASK = 0xFF
PER_CAM = {"bfi": (0.0, 100.0), "bvi": (0.0, 200.0)}
AVERAGE = {"bfi": (40.0, 60.0), "bvi": (90.0, 110.0)}


class _StubLiveSource(QObject):
    """Live-looking source (masks/clinicalMode -1: the grid follows the
    viewer inputs) with distinct per-camera and side-average fits."""

    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.average_calls = []

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
        y_min, y_max = PER_CAM.get(metric, (0.0, 1.0))
        return {"yMin": y_min, "yMax": y_max}

    @pyqtSlot(str, result="QVariantMap")
    def compute_bounds_for_side_average(self, metric):
        self.average_calls.append(metric)
        y_min, y_max = AVERAGE.get(metric, (0.0, 1.0))
        return {"yMin": y_min, "yMax": y_max}

    @pyqtSlot(str, int, str, result="QVariantMap")
    @pyqtSlot(str, int, str, float, float, result="QVariantMap")
    def compute_bounds_for_cell(self, side, cam_id, metric, t_lo=None, t_hi=None):
        return {"yMin": 0.0, "yMax": 1.0}

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
            "plotViewMode": "individual",
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
def research_viewer(viewer_factory):
    stub = viewer_factory.stub
    stub.setConfig("plotViewMode", "individual")
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
        stub.setConfig("plotViewMode", "individual")


def _walk(obj):
    yield obj
    kids = obj.childItems() if isinstance(obj, QQuickItem) else obj.children()
    for k in kids:
        yield from _walk(k)


def _cells(viewer):
    return sorted(
        (o.property("side"), int(o.property("camId")))
        for o in _walk(viewer)
        if o.metaObject().className().startswith("PlotCell")
    )


def _cell_model(viewer):
    val = viewer.property("_activeCellModel")
    val = val.toVariant() if hasattr(val, "toVariant") else val
    return sorted((c["side"], int(c["camId"])) for c in val)


def test_individual_view_is_unchanged(research_viewer):
    viewer, src, stub = research_viewer
    assert viewer.property("averageView") is False
    assert len(_cells(viewer)) == 16
    assert viewer.property("_showClinicalPanels") is False
    assert viewer.property("showCellValues") is True
    assert (viewer.property("primaryYMin"), viewer.property("primaryYMax")) \
        == PER_CAM["bfi"]
    assert src.average_calls == []


def test_average_view_draws_one_averaged_plot_per_module(research_viewer):
    viewer, src, stub = research_viewer
    stub.setConfig("plotViewMode", "average")   # the ⋯ popup button
    assert viewer.property("researchAverage") is True
    assert viewer.property("averageView") is True
    assert _cell_model(viewer) == [("left", -1), ("right", -1)]
    assert _cells(viewer) == [("left", -1), ("right", -1)]
    assert viewer.property("_showClinicalPanels") is True
    assert viewer.property("_sideGapActive") is False
    assert viewer.property("showCellValues") is False
    # Autoscale refits to the averaged traces immediately.
    assert (viewer.property("primaryYMin"), viewer.property("primaryYMax")) \
        == AVERAGE["bfi"]
    assert (viewer.property("secondaryYMin"),
            viewer.property("secondaryYMax")) == AVERAGE["bvi"]


def test_average_view_follows_the_module_masks(research_viewer):
    viewer, src, stub = research_viewer
    stub.setConfig("plotViewMode", "average")
    viewer.setProperty("leftMask", 0x00)
    assert _cell_model(viewer) == [("right", -1)]


def test_switching_back_restores_the_camera_grid(research_viewer):
    viewer, src, stub = research_viewer
    stub.setConfig("plotViewMode", "average")
    stub.setConfig("plotViewMode", "individual")
    assert len(_cells(viewer)) == 16
    assert (viewer.property("primaryYMin"), viewer.property("primaryYMax")) \
        == PER_CAM["bfi"]


def test_clinical_ignores_a_stale_average_preference(research_viewer):
    """Clinical is the averaged layout already; the preference must not
    change anything there (no research autoscale path, same cells)."""
    viewer, src, stub = research_viewer
    viewer.setProperty("clinicalMode", True)
    clinical_cells = _cell_model(viewer)
    stub.setConfig("plotViewMode", "average")
    assert viewer.property("researchAverage") is False
    assert viewer.property("averageView") is True
    assert _cell_model(viewer) == clinical_cells
    # Still the pre-#606 fit (the research-average slot is not consulted).
    # Checked on this viewer's own range: the module-scoped stub is shared
    # with earlier tests' viewers, so the source's call log is not.
    assert (viewer.property("primaryYMin"), viewer.property("primaryYMax")) \
        == PER_CAM["bfi"]


def test_view_buttons_are_hidden_in_clinical():
    """Static guard on the ⋯ popup row (a Popup needs a window to open,
    which this harness does without): same gate as researchAverage."""
    qml = PLOT_VIEWER_QML.read_text(encoding="utf-8")
    idx = qml.index("id: viewModeRow")
    row = qml[idx:idx + 200]
    assert "visible: !viewer.clinicalMode && !viewer.effectiveClinical" in row
    assert 'MotionInterface.setConfig("plotViewMode", modelData.value)' in qml
