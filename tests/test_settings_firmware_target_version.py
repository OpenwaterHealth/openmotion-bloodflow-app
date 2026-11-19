"""Settings -> About firmware rows name the version an update installs (#743).

Loads the real components/SettingsModal.qml offscreen with the #529 identity
stub plus the connector's firmware-version surface, and asserts each device
row reads "<installed>  →  <latest>" while an update is offered, the plain
installed version otherwise, and never a target in a clinical build.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import pytest
from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal
from PyQt6.QtQml import QQmlComponent, QQmlEngine, qmlRegisterSingletonInstance
from PyQt6.QtQuick import QQuickWindow
from PyQt6.QtCore import QUrl

from test_settings_modal_mode_gates import (  # noqa: E402
    SETTINGS_MODAL_QML,
    _basic_controls_style,
    _find_card,
    _invoke,
)
from test_settings_about_serials import _IdentityStub  # noqa: E402

pytestmark = pytest.mark.unit


class _FirmwareStub(_IdentityStub):
    firmwareUpdateInfoChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fw = {d: {"cur": "", "latest": "", "avail": False}
                   for d in ("console", "left", "right")}

    def setFirmware(self, dev, cur, latest, avail):
        self.fw[dev] = {"cur": cur, "latest": latest, "avail": avail}
        self.firmwareUpdateInfoChanged.emit()

    def _fw(dev, field):
        return lambda self: self.fw[dev][field]

    consoleFirmwareVersion = pyqtProperty(str, _fw("console", "cur"), notify=firmwareUpdateInfoChanged)
    leftSensorFirmwareVersion = pyqtProperty(str, _fw("left", "cur"), notify=firmwareUpdateInfoChanged)
    rightSensorFirmwareVersion = pyqtProperty(str, _fw("right", "cur"), notify=firmwareUpdateInfoChanged)
    consoleFirmwareLatest = pyqtProperty(str, _fw("console", "latest"), notify=firmwareUpdateInfoChanged)
    leftSensorFirmwareLatest = pyqtProperty(str, _fw("left", "latest"), notify=firmwareUpdateInfoChanged)
    rightSensorFirmwareLatest = pyqtProperty(str, _fw("right", "latest"), notify=firmwareUpdateInfoChanged)
    consoleFirmwareUpdateAvailable = pyqtProperty(bool, _fw("console", "avail"), notify=firmwareUpdateInfoChanged)
    leftSensorFirmwareUpdateAvailable = pyqtProperty(bool, _fw("left", "avail"), notify=firmwareUpdateInfoChanged)
    rightSensorFirmwareUpdateAvailable = pyqtProperty(bool, _fw("right", "avail"), notify=firmwareUpdateInfoChanged)
    del _fw


@pytest.fixture(scope="module")
def modal():
    stub = _FirmwareStub()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(SETTINGS_MODAL_QML)))
    if component.isError():
        raise RuntimeError("\n".join(e.toString() for e in component.errors()))
    window = QQuickWindow()
    window.resize(1200, 800)
    window.show()
    obj = component.create()
    assert obj is not None, "\n".join(e.toString() for e in component.errors())
    obj.setParentItem(window.contentItem())
    obj.stub = stub
    yield obj
    obj.setParentItem(None)
    obj.deleteLater()
    window.close()
    del engine
    del stub


def _row_texts(card):
    """The version text of each firmware DeviceRow, by its label."""
    out = {}
    for row in card.findChildren(QObject):
        try:
            if row.property("dev") and row.property("latest") is not None:
                for child in row.findChildren(QObject):
                    if child.property("font") is not None and \
                            child.property("font").family() == "Consolas":
                        out[row.property("label")] = child.property("text")
        except RuntimeError:
            continue
    return out


def test_rows_show_target_version_while_an_update_is_offered(modal):
    stub = modal.stub
    stub.setIdentity(connected={"console": True, "left": True, "right": True},
                     serials={})
    stub.setFirmware("console", "1.8.2", "1.9.0", True)
    stub.setFirmware("left", "1.8.3", "1.8.3", False)
    stub.setFirmware("right", "1.8.2", "1.8.3", True)
    _invoke(modal, "open")
    try:
        texts = _row_texts(_find_card(modal, "About"))
        assert texts["Console FW"] == "1.8.2  →  1.9.0"
        assert texts["Left Sensor FW"] == "1.8.3"
        assert texts["Right Sensor FW"] == "1.8.2  →  1.8.3"
    finally:
        _invoke(modal, "close")
