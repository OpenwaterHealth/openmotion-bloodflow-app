"""Compile + binding checks for components/TestResultsWindow.qml (#469).

QML doesn't fail at import time — a typo'd role name in the table
delegate only shows up as a runtime warning in a hardware session. This
compiles the window against a stub MotionInterface and exercises the
row bindings and the Copy export, so the new BFI/BVI columns are covered
without a bench.

Follows the offscreen-Qt pattern established in
test_critical_error_modal_footer.py / test_calibration_override_modal.py.
"""

import contextlib
import os
import sys
from pathlib import Path

from PyQt6.QtCore import (  # noqa: E402
    QCoreApplication,
    QMetaObject,
    QObject,
    Qt,
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
    qmlRegisterSingletonType,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_WINDOW_QML = REPO_ROOT / "components" / "TestResultsWindow.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"

# One row per interesting shape: an all-PASS camera, and a camera whose
# BFI/BVI are out of band while the Test acceptance gate (mean + contrast
# + dark) still passes — the #469 report's exact signature.
_ROWS = [
    {"side": "left", "cam": 1, "light_mean": 120.0, "min_mean": 80.0,
     "mean_pf": "PASS", "dark_mean": 1.0, "max_dark": 3.0,
     "dark_pf": "PASS", "contrast": 0.31234, "min_contrast": 0.25,
     "contrast_pf": "PASS", "bfi": 0.042, "bfi_pf": "PASS",
     "bvi": 4.987, "bvi_pf": "PASS", "overall": "PASS"},
    {"side": "left", "cam": 2, "light_mean": 157.6, "min_mean": 80.0,
     "mean_pf": "PASS", "dark_mean": 0.9, "max_dark": 3.0,
     "dark_pf": "PASS", "contrast": 0.34884, "min_contrast": 0.25,
     "contrast_pf": "PASS", "bfi": 1.058, "bfi_pf": "FAIL",
     "bvi": -17.327, "bvi_pf": "FAIL", "overall": "PASS"},
]


class _StubMotionInterface(QObject):
    """Every member TestResultsWindow.qml (and AppTheme.qml) touches."""

    testScanStateChanged = pyqtSignal()
    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.clipboard_texts = []
        self._rows = list(_ROWS)
        self._status = "passed"
        self._reason = ""

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        return {"darkMode": True}

    @pyqtProperty("QVariantList", notify=testScanStateChanged)
    def testScanRows(self):
        return self._rows

    @pyqtProperty(str, notify=testScanStateChanged)
    def testScanStatus(self):
        return self._status

    @pyqtProperty(str, notify=testScanStateChanged)
    def testScanFailureReason(self):
        return self._reason

    @pyqtProperty(bool, notify=testScanStateChanged)
    def testScanRunning(self):
        return self._status == "running"

    @pyqtSlot(str)
    def copyToClipboard(self, text):
        self.clipboard_texts.append(text)


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


@pytest.fixture
def window():
    stub = _StubMotionInterface()
    try:
        qmlRegisterSingletonInstance(
            "OpenMotion", 1, 0, "MotionInterface", stub)
        qmlRegisterSingletonType(
            QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0,
            "AppTheme",
        )
    except Exception:
        pass

    # The window is a Qt.Window root; a wrapper Item declares it as a
    # sibling type (base URL = components dir) without showing it.
    wrapper_qml = b"""
import QtQuick 6.0
Item {
    property alias win: w
    TestResultsWindow { id: w }
}
"""
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(engine)
        component.setData(
            wrapper_qml,
            QUrl.fromLocalFile(str(TEST_WINDOW_QML.parent / "_probe.qml")),
        )
    if component.isError():
        raise AssertionError(
            "TestResultsWindow.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    root = component.create()
    if root is None:
        raise AssertionError(
            "TestResultsWindow.qml failed to instantiate:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    obj = root.property("win")
    assert obj is not None, "wrapper did not expose the window"
    yield obj, stub
    root.deleteLater()


def test_window_compiles_and_binds_rows(window):
    obj, _ = window
    rows = obj.property("rows")
    assert len(rows) == 2
    assert rows[0]["side"] == "left"
    assert rows[1]["bfi_pf"] == "FAIL"


def test_copy_exports_bfi_bvi_columns(window):
    obj, stub = window
    # PyQt6's invokeMethod returns the method's own return value (None
    # for this void QML function) — success is judged by the stub below.
    QMetaObject.invokeMethod(
        obj, "_copyToClipboard", Qt.ConnectionType.DirectConnection)
    assert len(stub.clipboard_texts) == 1, "_copyToClipboard did not run"
    lines = stub.clipboard_texts[0].split("\n")
    header = lines[0].split("\t")
    assert header == [
        "Side", "Cam", "LightMean", "MinMean", "MeanPF",
        "DarkMean", "MaxDark", "DarkPF",
        "Contrast", "MinContrast", "ContrastPF",
        "BFI", "BFIPF", "BVI", "BVIPF", "Overall",
    ]
    assert len(lines) == 3
    cam2 = lines[2].split("\t")
    assert len(cam2) == len(header)
    # BFI/BVI value + verdict land in their columns, Overall stays last.
    assert cam2[header.index("BFI")] == "1.058"
    assert cam2[header.index("BFIPF")] == "FAIL"
    assert cam2[header.index("BVI")] == "-17.327"
    assert cam2[header.index("BVIPF")] == "FAIL"
    assert cam2[header.index("Overall")] == "PASS"


def test_header_reads_fail_with_reason(window):
    obj, stub = window
    stub._status = "failed"
    stub._reason = "no camera data captured"
    stub.testScanStateChanged.emit()
    assert obj.property("status") == "failed"
    assert obj.property("failureReason") == "no camera data captured"
