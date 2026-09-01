"""Hardware serial rows in the Settings -> About card (#529).

Loads the real components/SettingsModal.qml offscreen with a stubbed
``MotionInterface`` (extends the stub from test_settings_modal_mode_gates)
that exposes the connector's serial-number properties, and asserts the
About card renders one selectable serial row per device (console + the two
sensor modules — no per-camera UIDs) with a Copy chip:

- connected + programmed -> the serial itself, Copy chip shown
- connected + unprogrammed -> "Not programmed", no chip
- disconnected -> "Not connected", no chip (never a stale serial)

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import pytest
from PyQt6.QtCore import (
    QMetaObject,
    QObject,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtQml import QQmlComponent, QQmlEngine, qmlRegisterSingletonInstance
from PyQt6.QtQuick import QQuickWindow

# Importing the mode-gates harness creates the offscreen QGuiApplication
# ahead of conftest's QCoreApplication (see that module's header).
from test_settings_modal_mode_gates import (  # noqa: E402
    SETTINGS_MODAL_QML,
    _basic_controls_style,
    _find_card,
    _invoke,
    _StubMotionInterface,
)

pytestmark = pytest.mark.unit

ROW_LABELS = {"Console SN", "Left Sensor SN", "Right Sensor SN"}


class _IdentityStub(_StubMotionInterface):
    """Mode-gates stub plus the #529 identity surface of MotionConnector."""

    connectionStatusChanged = pyqtSignal()
    deviceIdentityChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._connected = {"console": False, "left": False, "right": False}
        self._serials = {"console": "", "left": "", "right": ""}
        self.copied: list[str] = []

    @pyqtSlot(str)
    def copyToClipboard(self, text):
        self.copied.append(text)

    def setIdentity(self, connected, serials):
        self._connected.update(connected)
        self._serials.update(serials)
        self.connectionStatusChanged.emit()
        self.deviceIdentityChanged.emit()

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def consoleConnected(self):
        return self._connected["console"]

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def leftSensorConnected(self):
        return self._connected["left"]

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def rightSensorConnected(self):
        return self._connected["right"]

    @pyqtProperty(str, notify=deviceIdentityChanged)
    def consoleSerialNumber(self):
        return self._serials["console"]

    @pyqtProperty(str, notify=deviceIdentityChanged)
    def leftSensorSerialNumber(self):
        return self._serials["left"]

    @pyqtProperty(str, notify=deviceIdentityChanged)
    def rightSensorSerialNumber(self):
        return self._serials["right"]


@pytest.fixture(scope="module")
def modal():
    stub = _IdentityStub()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(SETTINGS_MODAL_QML))
        )
    if component.isError():
        raise RuntimeError(
            "SettingsModal.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    window = QQuickWindow()
    window.resize(1200, 800)
    window.show()
    obj = component.create()
    if obj is None:
        raise RuntimeError(
            "SettingsModal.qml failed to instantiate:\n"
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


def _serial_rows(card):
    """SerialRow instances inside ``card`` (they carry ``serial``), by label."""
    out = {}
    for child in card.findChildren(QObject):
        try:
            if child.property("serial") is not None and child.property("label"):
                out[child.property("label")] = child
        except RuntimeError:
            continue
    return out


def _value_edit(row):
    """The value TextEdit of a serial row — the one selectable child (the
    label Text and the Copy chip's caption are plain Text)."""
    for child in row.findChildren(QObject):
        try:
            if child.property("selectByMouse") is True:
                return child
        except RuntimeError:
            continue
    raise AssertionError(f"No selectable value under row {row.property('label')!r}")


def _copy_chip(row):
    for child in row.findChildren(QObject):
        try:
            if child.property("copyText") is not None:
                return child
        except RuntimeError:
            continue
    raise AssertionError(f"No Copy chip under row {row.property('label')!r}")


def test_serial_rows_reflect_connection_and_programming(modal):
    modal.stub.setIdentity(
        connected={"console": True, "left": True, "right": False},
        serials={"console": "WWW04Q40005", "left": "", "right": "STALE"},
    )
    _invoke(modal, "open")
    try:
        card = _find_card(modal, "About")
        rows = _serial_rows(card)
        assert set(rows) == ROW_LABELS
        assert _value_edit(rows["Console SN"]).property("text") == "WWW04Q40005"
        assert _value_edit(rows["Left Sensor SN"]).property("text") == "Not programmed"
        # A disconnected device never shows a (stale) serial.
        assert _value_edit(rows["Right Sensor SN"]).property("text") == "Not connected"
        for row in rows.values():
            assert row.property("visible") is True
            assert _value_edit(row).property("readOnly") is True
        # No per-camera UID rows anywhere in the card.
        labels = {
            c.property("label") for c in card.findChildren(QObject)
            if c.property("label")
        }
        assert not any("Camera" in str(lbl) for lbl in labels)
    finally:
        _invoke(modal, "close")


def test_copy_chip_only_for_real_values_and_pushes_serial(modal):
    modal.stub.setIdentity(
        connected={"console": True, "left": True, "right": False},
        serials={"console": "WWW04Q40005", "left": "", "right": "STALE"},
    )
    modal.stub.copied.clear()
    _invoke(modal, "open")
    try:
        rows = _serial_rows(_find_card(modal, "About"))
        # Chip only where there is a real value: not for a placeholder
        # ("Not programmed") and never for a disconnected device's serial.
        assert _copy_chip(rows["Console SN"]).property("visible") is True
        assert _copy_chip(rows["Left Sensor SN"]).property("visible") is False
        assert _copy_chip(rows["Right Sensor SN"]).property("visible") is False

        QMetaObject.invokeMethod(
            _copy_chip(rows["Console SN"]), "copy",
            Qt.ConnectionType.DirectConnection)
        assert modal.stub.copied == ["WWW04Q40005"]
    finally:
        _invoke(modal, "close")
