"""Compile + render checks for components/CalibrationOverrideModal.qml (#426).

QML doesn't fail at import time — a typo'd property or a shadowed
``modelData`` only shows up as a runtime warning in a hardware session. This
compiles the modal against a stub MotionInterface and drives the two exits,
so the table bindings and the accept/discard wiring are covered without a
bench.

Follows the offscreen-Qt pattern established in
test_critical_error_modal_footer.py.
"""

import contextlib
import os
import sys
from pathlib import Path

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
    qmlRegisterSingletonType,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
OVERRIDE_MODAL_QML = REPO_ROOT / "components" / "CalibrationOverrideModal.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"

_ROWS = [
    {"cam": 1, "side": "L", "mean": "32.40", "meanLimit": "≥ 40.00",
     "meanFail": True, "contrast": "0.0810", "contrastLimit": "≥ 0.0600",
     "contrastFail": False, "bfi": "0.420", "bfiLimit": "0.000–5.000",
     "bfiFail": False, "bvi": "1.100", "bviLimit": "≥ 0.000",
     "bviFail": False},
    {"cam": 2, "side": "R", "mean": "--", "meanLimit": "--",
     "meanFail": False, "contrast": "0.0310", "contrastLimit": "≥ 0.0600",
     "contrastFail": True, "bfi": "0.090", "bfiLimit": "0.000–5.000",
     "bfiFail": False, "bvi": "0.220", "bviLimit": "≥ 0.000",
     "bviFail": False},
]


class _StubMotionInterface(QObject):
    """Every member CalibrationOverrideModal.qml (and AppTheme.qml) touches."""

    calibrationStateChanged = pyqtSignal()
    calibrationOverrideRequested = pyqtSignal()
    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.accepted = 0
        self.dismissed = 0
        self._pending = True

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        return {"darkMode": True}

    @pyqtProperty("QVariantList", notify=calibrationStateChanged)
    def calibrationOverrideRows(self):
        return _ROWS

    @pyqtProperty(bool, notify=calibrationStateChanged)
    def calibrationOverridePending(self):
        return self._pending

    @pyqtSlot()
    def acceptCalibrationOverride(self):
        self.accepted += 1
        self._pending = False

    @pyqtSlot()
    def dismissCalibrationOverride(self):
        self.dismissed += 1
        self._pending = False

    @pyqtSlot(str, result=bool)
    def checkEngineeringPassword(self, pw):
        return pw == "correcthorse"


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
def modal():
    stub = _StubMotionInterface()
    try:
        qmlRegisterSingletonInstance(
            "OpenMotion", 1, 0, "MotionInterface", stub)
        qmlRegisterSingletonType(
            QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0,
            "AppTheme",
        )
    except Exception:
        # Another module in the same process already registered the
        # singleton; the modal only needs *a* MotionInterface to compile.
        pass

    # Instantiated inside a sized, visible parent: QQuickItem.visible is
    # *effective* visibility, so a parentless root always reads false and
    # the show/hide assertions below would be meaningless. The wrapper's
    # base URL is the components dir, which is what lets it name the modal
    # as a sibling type.
    wrapper_qml = b"""
import QtQuick 6.0
Item {
    width: 1200
    height: 800
    visible: true
    property alias modal: m
    CalibrationOverrideModal { id: m }
}
"""
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(engine)
        component.setData(
            wrapper_qml,
            QUrl.fromLocalFile(str(OVERRIDE_MODAL_QML.parent / "_probe.qml")),
        )
    if component.isError():
        raise AssertionError(
            "CalibrationOverrideModal.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    root = component.create()
    if root is None:
        raise AssertionError(
            "CalibrationOverrideModal.qml failed to instantiate:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    obj = root.property("modal")
    assert obj is not None, "wrapper did not expose the modal"
    yield obj, stub
    root.deleteLater()


def test_modal_compiles_and_instantiates(modal):
    obj, _ = modal
    assert obj is not None
    # Starts hidden — it is raised by the connector's signal, not on load.
    assert obj.property("visible") is False


def test_open_shows_the_modal(modal):
    obj, _ = modal
    obj.open()
    assert obj.property("visible") is True


def test_rows_bind_from_the_interface(modal):
    obj, _ = modal
    rows = obj.property("rows")
    assert len(rows) == 2
    assert rows[0]["side"] == "L"
    assert rows[1]["mean"] == "--"


def test_accept_calls_the_slot_and_closes(modal):
    obj, stub = modal
    obj.open()
    obj._accept()
    assert stub.accepted == 1
    assert stub.dismissed == 0
    assert obj.property("visible") is False


def test_discard_calls_the_slot_and_closes(modal):
    obj, stub = modal
    obj.open()
    obj._discard()
    assert stub.dismissed == 1
    assert stub.accepted == 0
    assert obj.property("visible") is False


def test_modal_closes_when_the_pending_override_is_retired(modal):
    """Starting a new calibration clears the pending offer; a stale prompt
    must not stay on screen blocking the Calibrate row."""
    obj, stub = modal
    obj.open()
    assert obj.property("visible") is True

    stub._pending = False
    stub.calibrationStateChanged.emit()

    assert obj.property("visible") is False


def test_modal_stays_open_while_the_override_is_still_pending(modal):
    obj, stub = modal
    obj.open()
    stub.calibrationStateChanged.emit()
    assert obj.property("visible") is True


# ── password gate on the destructive answer ───────────────────────────────

def _password_modal(obj):
    from PyQt6.QtCore import QObject
    pw = obj.findChild(QObject, "overwritePasswordModal")
    assert pw is not None, "overwrite password gate is missing"
    return pw


def test_overwrite_is_password_gated(modal):
    """Overwriting the console must not be one click away from a screen
    that just told the operator the scan is bad."""
    obj, stub = modal
    obj.open()
    pw = _password_modal(obj)

    # Opening the prompt alone authorises nothing.
    pw.open()
    assert pw.property("visible") is True
    assert stub.accepted == 0


def test_correct_password_authorises_the_overwrite(modal):
    obj, stub = modal
    obj.open()
    pw = _password_modal(obj)
    pw.open()

    pw.accepted.emit()      # what PasswordPromptModal emits on a match

    assert stub.accepted == 1
    assert obj.property("visible") is False


def test_password_prompt_starts_hidden_and_resets_between_openings(modal):
    """A prompt left open from a previous gate must not be showing when
    the next one is raised."""
    obj, _ = modal
    pw = _password_modal(obj)
    assert pw.property("visible") is False

    # Parent first: `visible` is effective visibility, so the child reads
    # False while the gate modal itself is hidden regardless of its own
    # flag.
    obj.open()
    pw.open()
    assert pw.property("visible") is True

    # Re-raising the gate must clear a prompt left over from last time.
    obj.open()
    assert pw.property("visible") is False


def test_declining_needs_no_password(modal):
    """Leaving the console alone is the safe direction — don't gate it."""
    obj, stub = modal
    obj.open()
    obj._discard()
    assert stub.dismissed == 1
    assert _password_modal(obj).property("visible") is False
