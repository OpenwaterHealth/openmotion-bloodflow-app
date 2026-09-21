"""Scan Settings commits the User Label when the modal closes (#551).

The label field only wrote ``MotionInterface.userLabel`` from
``onEditingFinished`` (Enter / Tab / focus loss). Qt Quick lets a hidden
item keep keyboard focus, so typing a new subject name and dismissing the
modal with Done / X / Esc never fired it: the next scan was still named
after the previous subject.

Loads the real components/ScanSettingsModal.qml offscreen with a stubbed
``MotionInterface`` whose ``userLabel`` setter mirrors
``MotionConnector.setUserLabel``. Setting the field's ``text`` stands in
for typing — like typing, it does not fire ``editingFinished``.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import pytest
from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtQml import QQmlComponent, QQmlEngine, qmlRegisterSingletonInstance
from PyQt6.QtQuick import QQuickWindow

# Importing the mode-gates harness creates the offscreen QGuiApplication
# ahead of conftest's QCoreApplication (see that module's header).
from test_settings_modal_mode_gates import (  # noqa: E402
    REPO_ROOT,
    _basic_controls_style,
    _find_item,
    _invoke,
)

pytestmark = pytest.mark.unit

SCAN_SETTINGS_MODAL_QML = REPO_ROOT / "components" / "ScanSettingsModal.qml"


class _LabelStub(QObject):
    userLabelChanged = pyqtSignal()
    appConfigChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._user_label = "owSUBJECT9"

    def _get(self):
        return self._user_label

    def _set(self, value):
        # Mirrors MotionConnector.setUserLabel: empty ignored, normalized
        # to "ow" + uppercase alphanumerics.
        if not value:
            return
        rest = value[2:] if value.startswith("ow") else value
        rest = "".join(ch for ch in rest.upper() if ch.isalnum())
        new_val = "ow" + rest
        if new_val != self._user_label:
            self._user_label = new_val
            self.userLabelChanged.emit()

    userLabel = pyqtProperty(str, fget=_get, fset=_set, notify=userLabelChanged)

    @pyqtProperty("QVariantMap", notify=appConfigChanged)
    def appConfig(self):
        return {"engineeringMode": False, "clinicalMode": False}

    @pyqtSlot(str, str, int, bool, str)
    def notify(self, *_args):
        pass


@pytest.fixture(scope="module")
def modal():
    stub = _LabelStub()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(SCAN_SETTINGS_MODAL_QML))
        )
    if component.isError():
        raise RuntimeError(
            "ScanSettingsModal.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    window = QQuickWindow()
    window.resize(1200, 800)
    window.show()
    obj = component.create()
    if obj is None:
        raise RuntimeError(
            "ScanSettingsModal.qml failed to instantiate:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    obj.setParentItem(window.contentItem())
    obj.stub = stub
    yield obj
    obj.setParentItem(None)
    obj.deleteLater()
    window.close()
    del engine
    del stub


def _type_label(modal, text):
    _invoke(modal, "open")
    field = _find_item(modal, "userLabelField")
    assert field is not None
    field.forceActiveFocus()
    field.setProperty("text", text)
    return field


def test_close_commits_a_typed_label(modal):
    modal.stub._user_label = "owSUBJECT9"
    _type_label(modal, "subject 6")
    assert modal.stub._user_label == "owSUBJECT9"  # nothing committed yet

    _invoke(modal, "close")

    assert modal.property("visible") is False
    assert modal.stub._user_label == "owSUBJECT6"


def test_close_reflects_normalization_back_into_the_field(modal):
    modal.stub._user_label = "owSUBJECT9"
    field = _type_label(modal, "subject 6")
    _invoke(modal, "close")
    assert field.property("text") == "owSUBJECT6"


def test_close_with_an_emptied_field_keeps_the_label(modal):
    modal.stub._user_label = "owSUBJECT9"
    field = _type_label(modal, "")
    _invoke(modal, "close")
    assert modal.stub._user_label == "owSUBJECT9"
    assert field.property("text") == "owSUBJECT9"
