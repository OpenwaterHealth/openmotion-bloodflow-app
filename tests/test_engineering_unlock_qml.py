"""QML side of the engineering unlock (#706).

Loads the real components/EngineeringUnlockModal.qml and
PasswordPromptModal.qml in a bare offscreen QQmlEngine with a stubbed
MotionInterface singleton and pins the QML -> Python contract:

- the unlock prompt hands the typed password to ``unlockEngineeringMode``
  (the check and the flag live in Python) and never sets the flag itself;
- it brings its own double-click area and puts it on the header logo, so a
  build without the file has no logo double-click handler;
- PasswordPromptModal has no built-in password any more: without a
  submitHandler every password is refused;
- ConfirmModal is the plain "are you sure?" dialog (Calibrate, Delete): no
  field, confirm accepts, cancel doesn't.

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
    QMetaObject,
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
from PyQt6.QtQuick import QQuickItem  # noqa: E402

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = REPO_ROOT / "components"
UNLOCK_QML = COMPONENTS / "EngineeringUnlockModal.qml"
APP_THEME_QML = COMPONENTS / "AppTheme.qml"
GOOD = "right-password"


class _StubMotionInterface(QObject):
    """What EngineeringUnlockModal / PasswordPromptModal / AppTheme use."""

    directoryChanged = pyqtSignal()
    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.unlock_calls = []
        self.notifications = []

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        return {"darkMode": True}

    @pyqtProperty(str, notify=directoryChanged)
    def directory(self):
        return ""

    @pyqtSlot(str, result=bool)
    def unlockEngineeringMode(self, password):
        self.unlock_calls.append(password)
        return password == GOOD

    @pyqtSlot(str, str, int, bool, str)
    def notify(self, message, level, duration, sticky, key):
        self.notifications.append((message, key))


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
def qml():
    """One engine for the module; ``qml.make(url_or_source)`` instances."""
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0, "AppTheme",
    )
    engine = QQmlEngine()
    created = []

    def make(source=None):
        with _basic_controls_style():
            if source is None:
                component = QQmlComponent(engine, QUrl.fromLocalFile(str(UNLOCK_QML)))
            else:
                # Inline QML resolved against components/, so the
                # directory's types (PasswordPromptModal) are in scope.
                component = QQmlComponent(engine)
                component.setData(source.encode("utf-8"),
                                  QUrl.fromLocalFile(str(COMPONENTS / "_inline.qml")))
        obj = component.create()
        if obj is None:
            raise RuntimeError("\n".join(e.toString() for e in component.errors()))
        created.append(obj)
        stub.unlock_calls.clear()
        stub.notifications.clear()
        return obj

    make.stub = stub
    yield make
    for obj in created:
        obj.deleteLater()
    del engine
    del stub


def _password_field(modal):
    for child in modal.findChildren(QObject):
        if child.metaObject().indexOfProperty("echoMode") >= 0:
            return child
    raise AssertionError("password field not found")


def _submit(modal, password=None):
    accepted = []
    modal.accepted.connect(lambda: accepted.append(1))
    QMetaObject.invokeMethod(modal, "open")
    if password is not None:
        _password_field(modal).setProperty("text", password)
    QMetaObject.invokeMethod(modal, "_submit")
    return bool(accepted)


# ── EngineeringUnlockModal ───────────────────────────────────────────────

def test_unlock_prompt_asks_for_the_engineering_password(qml):
    modal = qml()
    assert modal.property("description") == (
        "Enter the engineering password to enable engineering mode.")


def test_unlock_prompt_hands_the_password_to_python(qml):
    modal = qml()
    assert _submit(modal, "wrong") is False
    assert qml.stub.unlock_calls == ["wrong"]
    assert qml.stub.notifications == []

    assert _submit(modal, GOOD) is True
    assert qml.stub.unlock_calls == ["wrong", GOOD]
    assert qml.stub.notifications == [("Engineering mode enabled.", "engineering-mode")]


def test_unlock_prompt_puts_its_double_click_area_on_the_logo(qml):
    modal = qml()
    logo = QQuickItem()
    assert not logo.childItems()
    modal.setProperty("logoItem", logo)
    kids = logo.childItems()
    assert len(kids) == 1
    assert kids[0].metaObject().className().startswith("QQuickMouseArea")


def test_unlock_source_never_sets_the_flag_itself():
    text = UNLOCK_QML.read_text(encoding="utf-8")
    assert "setConfig" not in text
    assert "MotionInterface.unlockEngineeringMode(pw)" in text


# ── PasswordPromptModal ──────────────────────────────────────────────────

def test_password_prompt_has_no_default_password(qml):
    modal = qml("import QtQuick 6.0\nPasswordPromptModal {}\n")
    for pw in ("", "anything"):
        assert _submit(modal, pw) is False


def test_password_prompt_uses_the_submit_handler(qml):
    modal = qml(
        "import QtQuick 6.0\n"
        "PasswordPromptModal { submitHandler: function(pw) { return pw === 'ok' } }\n")
    assert _submit(modal, "nope") is False
    assert _submit(modal, "ok") is True


# ── ConfirmModal (plain "are you sure?", e.g. Calibrate and Delete) ───────

def test_confirm_modal_has_no_password_field(qml):
    modal = qml('import QtQuick 6.0\nConfirmModal { description: "Sure?" }\n')
    assert not [c for c in modal.findChildren(QObject)
                if c.metaObject().indexOfProperty("echoMode") >= 0]


def test_confirm_modal_confirm_accepts_and_cancel_does_not(qml):
    modal = qml('import QtQuick 6.0\nConfirmModal { description: "Sure?" }\n')
    accepted = []
    modal.accepted.connect(lambda: accepted.append(1))

    QMetaObject.invokeMethod(modal, "open")
    QMetaObject.invokeMethod(modal, "close")        # Cancel / Esc / backdrop
    assert accepted == []

    QMetaObject.invokeMethod(modal, "open")
    QMetaObject.invokeMethod(modal, "confirm")
    assert accepted == [1]
    assert qml.stub.unlock_calls == []
