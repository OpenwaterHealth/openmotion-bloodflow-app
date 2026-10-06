"""Scan deletion asks "are you sure", with no password, in every build (#703).

Loads the real components/HistoryModal.qml in a bare offscreen QQmlEngine
with a stubbed MotionInterface (a clinical build, so nothing depends on the
Research variant) and checks the delete prompt: no password field, and the
wording follows the selection count.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import contextlib
import os
import sys
from pathlib import Path

# A QGuiApplication must exist before any QML Quick item is created (see
# test_logs_modal_filters.py for why it is made at import time, via argv).
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
HISTORY_QML = REPO_ROOT / "components" / "HistoryModal.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"


class _StubMotionInterface(QObject):
    """What HistoryModal's bindings (and AppTheme) read at creation."""

    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.password_checks = []

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        return {"darkMode": True, "clinicalMode": True, "engineeringMode": False}

    @pyqtProperty(str, notify=_neverEmitted)
    def directory(self):
        return ""

    @pyqtProperty(int, notify=_neverEmitted)
    def state(self):
        return 0

    # Tripwire: the delete confirm must never check a password.
    @pyqtSlot(str, result=bool)
    def checkEngineeringPassword(self, pw):
        self.password_checks.append(pw)
        return False


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
def history():
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0, "AppTheme",
    )
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(HISTORY_QML)))
        obj = component.create()
    if obj is None:
        raise RuntimeError("\n".join(e.toString() for e in component.errors()))
    obj.stub = stub
    yield obj
    obj.deleteLater()
    del engine


def _prompt(history):
    p = history.findChild(QObject, "deleteScansPrompt")
    assert p is not None, "deleteScansPrompt not found"
    return p


def test_delete_prompt_has_no_password_field(history):
    prompt = _prompt(history)
    assert prompt.property("requirePassword") is False
    assert "password" not in prompt.property("description").lower()


@pytest.mark.parametrize("count, phrase", [(1, "this scan"), (3, "these 3 scans")])
def test_delete_prompt_asks_are_you_sure(history, count, phrase):
    history.setProperty("checkedCount", count)
    text = _prompt(history).property("description")
    assert text.startswith("Are you sure you want to permanently delete ")
    assert phrase in text
    assert text.endswith("This cannot be undone.")


def test_confirming_never_checks_a_password(history):
    from PyQt6.QtCore import QMetaObject
    prompt = _prompt(history)
    accepted = []
    prompt.accepted.connect(lambda: accepted.append(1))
    QMetaObject.invokeMethod(prompt, "open")
    QMetaObject.invokeMethod(prompt, "_submit")
    assert accepted == [1]
    assert history.stub.password_checks == []
