"""Hardware serial rows in the Settings -> About card (#529).

Loads the real components/SettingsModal.qml offscreen with a stubbed
``MotionInterface`` (extends the stub from test_settings_modal_mode_gates)
that exposes the connector's device-identity properties, and asserts the
About card renders one serial row per device plus a camera-UID line per
sensor:

- connected + programmed -> the serial itself
- connected + unprogrammed -> "Not programmed"
- disconnected -> "Not connected"
- camera-UID line hidden until UIDs exist; an absent camera shows a dash

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import pytest
from PyQt6.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal
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

UIDS = [f"0x00000000000{i}" for i in range(1, 9)]


class _IdentityStub(_StubMotionInterface):
    """Mode-gates stub plus the #529 identity surface of MotionConnector."""

    connectionStatusChanged = pyqtSignal()
    deviceIdentityChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._connected = {"console": False, "left": False, "right": False}
        self._serials = {"console": "", "left": "", "right": ""}
        self._uids = {"left": [], "right": []}

    def setIdentity(self, connected, serials, uids):
        self._connected.update(connected)
        self._serials.update(serials)
        self._uids.update(uids)
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

    @pyqtProperty("QVariantList", notify=deviceIdentityChanged)
    def leftSensorCameraUids(self):
        return list(self._uids["left"])

    @pyqtProperty("QVariantList", notify=deviceIdentityChanged)
    def rightSensorCameraUids(self):
        return list(self._uids["right"])


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


def _rows(card, prop):
    """FieldRow instances inside ``card`` carrying ``prop`` (SerialRow has
    ``serial``, CameraUidRow has ``uids``), keyed by label."""
    out = {}
    for child in card.findChildren(QObject):
        try:
            if child.property(prop) is not None and child.property("label"):
                out[child.property("label")] = child
        except RuntimeError:
            continue
    return out


def _value_text(row):
    """The value Text of a FieldRow — the Text child that is not the label."""
    label = row.property("label")
    for child in row.findChildren(QObject):
        try:
            text = child.property("text")
        except RuntimeError:
            continue
        if isinstance(text, str) and text != label:
            return text
    raise AssertionError(f"No value Text under row {label!r}")


def test_serial_rows_reflect_connection_and_programming(modal):
    modal.stub.setIdentity(
        connected={"console": True, "left": True, "right": False},
        serials={"console": "WWW04Q40005", "left": "", "right": "STALE"},
        uids={"left": UIDS, "right": []},
    )
    _invoke(modal, "open")
    try:
        card = _find_card(modal, "About")
        rows = _rows(card, "serial")
        assert set(rows) == {"Console S/N", "Left Sensor S/N", "Right Sensor S/N"}
        assert _value_text(rows["Console S/N"]) == "WWW04Q40005"
        assert _value_text(rows["Left Sensor S/N"]) == "Not programmed"
        # A disconnected device never shows a (stale) serial.
        assert _value_text(rows["Right Sensor S/N"]) == "Not connected"
        for row in rows.values():
            assert row.property("visible") is True
    finally:
        _invoke(modal, "close")


def test_camera_uid_lines_hidden_until_read_and_mark_absent_camera(modal):
    uids = list(UIDS)
    uids[2] = ""  # camera 3 read back absent
    modal.stub.setIdentity(
        connected={"console": True, "left": True, "right": True},
        serials={"console": "C", "left": "L", "right": "R"},
        uids={"left": uids, "right": []},
    )
    _invoke(modal, "open")
    try:
        card = _find_card(modal, "About")
        rows = _rows(card, "uids")
        assert set(rows) == {"Left Cameras", "Right Cameras"}
        assert rows["Left Cameras"].property("visible") is True
        text = _value_text(rows["Left Cameras"])
        # "camN" is tied to its UID with a non-breaking space so the line
        # never wraps inside an entry.
        assert text.startswith("cam1\u00a00x000000000001")
        assert "cam3\u00a0—" in text
        assert "cam8\u00a00x000000000008" in text
        assert rows["Right Cameras"].property("visible") is False
    finally:
        _invoke(modal, "close")
