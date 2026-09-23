"""UpdatesModal.qml + UpdateBanner.qml presentation logic (#514).

Loads the real components in an offscreen QQmlEngine against a stubbed
``MotionInterface`` and checks: a new offer raises the modal (once per
item+version), the raise waits while a scan runs, rows list only pending
items in "Update all" order, the confirm step routes to the right slot,
and clinical builds never show either piece.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""
import time

from PyQt6.QtCore import (
    Q_ARG,
    QMetaObject,
    QObject,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)

# Importing the harness creates the offscreen QGuiApplication first.
from test_settings_modal_mode_gates import (  # noqa: F401
    REPO_ROOT,
    _basic_controls_style,
    _qt_app,
)

import pytest
from PyQt6.QtQml import QQmlComponent, QQmlEngine, qmlRegisterSingletonInstance
from PyQt6.QtQuick import QQuickWindow

pytestmark = pytest.mark.unit

MODAL_QML = REPO_ROOT / "components" / "UpdatesModal.qml"
BANNER_QML = REPO_ROOT / "components" / "UpdateBanner.qml"


class _Stub(QObject):
    appConfigChanged = pyqtSignal()
    fwInfoChanged = pyqtSignal()
    appInfoChanged = pyqtSignal()
    busyChanged = pyqtSignal()

    updateAvailable = pyqtSignal(str, str)
    updateNotAvailable = pyqtSignal()
    updateCheckFailed = pyqtSignal(str)
    updateProgress = pyqtSignal(str)
    firmwareUpdateAvailable = pyqtSignal(str, str, str)
    firmwareUpdateProgress = pyqtSignal(str, str, int, str)
    firmwareUpdateFinished = pyqtSignal(str, bool, str)
    batchUpdateStep = pyqtSignal(str)
    batchUpdateFinished = pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self.reset()

    def reset(self):
        self.clinical = False
        self.fw = {d: {"cur": "", "latest": "", "avail": False}
                   for d in ("console", "left", "right")}
        self.app_latest, self.app_url = "", ""
        self.busy = False
        self.calls = []

    def offer_fw(self, dev, cur, latest):
        self.fw[dev] = {"cur": cur, "latest": latest, "avail": True}
        self.fwInfoChanged.emit()
        self.firmwareUpdateAvailable.emit(dev, cur, latest)

    def offer_app(self, version):
        self.app_latest, self.app_url = version, f"https://x/{version}.exe"
        self.appInfoChanged.emit()
        self.updateAvailable.emit(version, self.app_url)

    def set_clinical(self, value):
        self.clinical = value
        self.appConfigChanged.emit()

    @pyqtProperty("QVariantMap", notify=appConfigChanged)
    def appConfig(self):
        return {"clinicalMode": self.clinical}

    def _fw(dev, field):
        return lambda self: self.fw[dev][field]

    consoleFirmwareVersion = pyqtProperty(str, _fw("console", "cur"), notify=fwInfoChanged)
    leftSensorFirmwareVersion = pyqtProperty(str, _fw("left", "cur"), notify=fwInfoChanged)
    rightSensorFirmwareVersion = pyqtProperty(str, _fw("right", "cur"), notify=fwInfoChanged)
    consoleFirmwareLatest = pyqtProperty(str, _fw("console", "latest"), notify=fwInfoChanged)
    leftSensorFirmwareLatest = pyqtProperty(str, _fw("left", "latest"), notify=fwInfoChanged)
    rightSensorFirmwareLatest = pyqtProperty(str, _fw("right", "latest"), notify=fwInfoChanged)
    consoleFirmwareUpdateAvailable = pyqtProperty(bool, _fw("console", "avail"), notify=fwInfoChanged)
    leftSensorFirmwareUpdateAvailable = pyqtProperty(bool, _fw("left", "avail"), notify=fwInfoChanged)
    rightSensorFirmwareUpdateAvailable = pyqtProperty(bool, _fw("right", "avail"), notify=fwInfoChanged)
    del _fw

    @pyqtProperty(bool, notify=fwInfoChanged)
    def anyFirmwareUpdateAvailable(self):
        return any(v["avail"] for v in self.fw.values())

    @pyqtProperty(bool, notify=appInfoChanged)
    def appUpdateAvailable(self):
        return bool(self.app_url)

    @pyqtProperty(str, notify=appInfoChanged)
    def appUpdateLatest(self):
        return self.app_latest

    @pyqtProperty(str, notify=appInfoChanged)
    def appUpdateUrl(self):
        return self.app_url

    @pyqtProperty(bool, notify=busyChanged)
    def updateBusy(self):
        return self.busy

    @pyqtProperty(bool, notify=busyChanged)
    def batchUpdateRunning(self):
        return self.busy

    @pyqtSlot(result=bool)
    def startUpdateAll(self):
        self.calls.append(("all",))
        return True

    @pyqtSlot(str)
    def applyUpdate(self, url):
        self.calls.append(("app", url))

    @pyqtSlot(str, result=bool)
    def startFirmwareUpdate(self, dev):
        self.calls.append(("fw", dev))
        return True

    @pyqtSlot()
    def checkForUpdates(self):
        self.calls.append(("check",))


@pytest.fixture(scope="module")
def env():
    stub = _Stub()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    engine = QQmlEngine()
    comps = {}
    with _basic_controls_style():
        for name, path in (("modal", MODAL_QML), ("banner", BANNER_QML)):
            comp = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
            if comp.isError():
                raise RuntimeError("\n".join(e.toString() for e in comp.errors()))
            comps[name] = comp
    window = QQuickWindow()
    window.resize(1200, 800)
    window.show()
    created = []

    def make(name):
        obj = comps[name].create()
        assert obj is not None, "\n".join(e.toString() for e in comps[name].errors())
        obj.setParentItem(window.contentItem())
        created.append(obj)
        return obj

    yield stub, make
    for obj in created:
        obj.setParentItem(None)
        obj.deleteLater()
    window.close()
    del engine


@pytest.fixture
def stub_make(env):
    stub, make = env
    stub.reset()
    stub.set_clinical(False)
    stub.fwInfoChanged.emit()
    stub.appInfoChanged.emit()
    stub.busyChanged.emit()
    return stub, make


def _pump(ms=30):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        _qt_app.processEvents()
        time.sleep(0.002)


def _call(obj, name, *args):
    QMetaObject.invokeMethod(
        obj, name, Qt.ConnectionType.DirectConnection,
        *[Q_ARG("QVariant", a) for a in args])


def _row_keys(modal):
    rows = modal.property("rows")
    rows = rows.toVariant() if hasattr(rows, "toVariant") else rows
    return [r["key"] for r in rows]


def test_new_offer_raises_modal_once_per_version(stub_make):
    stub, make = stub_make
    modal = make("modal")
    assert modal.property("visible") is False

    stub.offer_fw("console", "1.0.0", "1.1.0")
    assert modal.property("visible") is True

    _call(modal, "close")
    stub.offer_fw("console", "1.0.0", "1.1.0")   # re-detection, same version
    assert modal.property("visible") is False

    stub.offer_app("2.0.0")                        # a new item
    assert modal.property("visible") is True


def test_raise_waits_for_running_scan(stub_make):
    stub, make = stub_make
    modal = make("modal")
    modal.setProperty("deferAutoOpen", True)
    stub.offer_app("2.0.0")
    assert modal.property("visible") is False
    modal.setProperty("deferAutoOpen", False)
    assert modal.property("visible") is True


def test_rows_list_pending_items_in_update_all_order(stub_make):
    stub, make = stub_make
    modal = make("modal")
    stub.offer_app("2.0.0")
    stub.offer_fw("console", "1.0.0", "1.1.0")
    stub.offer_fw("left", "1.0.0", "1.1.0")
    assert _row_keys(modal) == ["left", "console", "app"]
    assert modal.property("pendingCount") == 3


def test_update_all_confirm_routes_to_connector(stub_make):
    stub, make = stub_make
    modal = make("modal")
    stub.offer_app("2.0.0")
    stub.offer_fw("right", "1.0.0", "1.1.0")
    _call(modal, "_run", "all")
    assert stub.calls == [("all",)]


def test_single_item_routes(stub_make):
    stub, make = stub_make
    modal = make("modal")
    stub.offer_app("2.0.0")
    stub.offer_fw("left", "1.0.0", "1.1.0")
    _call(modal, "_run", "left")
    _call(modal, "_run", "app")
    assert stub.calls == [("fw", "left"), ("app", "https://x/2.0.0.exe")]


def test_flashed_row_stays_listed_after_offer_withdrawn(stub_make):
    """The connector withdraws a flashed device's offer; the row must stay
    so the operator sees the power-cycle instruction."""
    stub, make = stub_make
    modal = make("modal")
    stub.offer_fw("left", "1.0.0", "1.1.0")
    stub.firmwareUpdateFinished.emit("left", True, "Firmware written.")
    stub.fw["left"]["avail"] = False
    stub.fwInfoChanged.emit()
    assert _row_keys(modal) == ["left"]
    assert modal.property("pendingCount") == 0


def test_cannot_dismiss_while_updating(stub_make):
    stub, make = stub_make
    modal = make("modal")
    stub.offer_app("2.0.0")
    stub.busy = True
    stub.busyChanged.emit()
    _call(modal, "close")
    assert modal.property("visible") is True
    stub.busy = False
    stub.busyChanged.emit()
    _call(modal, "close")
    assert modal.property("visible") is False


def test_clinical_never_opens(stub_make):
    stub, make = stub_make
    stub.set_clinical(True)
    modal = make("modal")
    banner = make("banner")
    stub.offer_app("2.0.0")
    stub.offer_fw("console", "1.0.0", "1.1.0")
    _call(modal, "open")
    assert modal.property("visible") is False
    assert banner.property("visible") is False


def test_banner_summarises_everything_pending(stub_make):
    stub, make = stub_make
    banner = make("banner")
    assert banner.property("visible") is False
    stub.offer_app("2.0.0")
    stub.offer_fw("right", "1.0.0", "1.1.0")
    assert banner.property("visible") is True
    summary = banner.property("summary")
    assert "Application 2.0.0" in summary and "right sensor" in summary
