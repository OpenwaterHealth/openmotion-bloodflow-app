"""Mode-flag gating in components/SettingsModal.qml (issues #233/#234).

Loads the real SettingsModal.qml in a bare offscreen QQmlEngine with a
stubbed ``MotionInterface`` singleton (same pattern as
test_plot_viewer_masks.py) and asserts, in terms of the two config flags
``clinicalMode`` / ``engineeringMode`` (per #231's resolution, there is
no derived mode abstraction — the flags ARE the model):

- #234: the Save raw CSV toggle + duration rows (Data Output card) are
  visible when NOT clinicalMode, or when engineeringMode is unlocked;
  hidden on a plain clinical build.
- The "Engineering" card shows only when engineeringMode is on.
- #233: ``close()`` never persists ``clinicalMode`` through
  ``saveConfigs()`` — clinical selection is build-time/env-only.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import contextlib
import os
import sys
from pathlib import Path

# A QGuiApplication must exist before any QML Quick item is created, and
# it must be constructed before conftest's session-scoped autouse
# fixture instantiates a bare QCoreApplication. Module import runs at
# collection time — ahead of every fixture — so create it here. The
# offscreen platform is passed via argv, NOT via QT_QPA_PLATFORM, so
# HIL subprocesses launched later in the session can't inherit it.
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
)
from PyQt6.QtQuick import QQuickWindow  # noqa: E402

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_MODAL_QML = REPO_ROOT / "components" / "SettingsModal.qml"


class _StubMotionInterface(QObject):
    """Minimal MotionInterface stand-in for SettingsModal.qml.

    Only what the modal's bindings and open()/close() paths touch is
    implemented; everything else it references (firmware versions, fan
    control, calibration slots, ...) degrades to undefined/no-op binding
    warnings, which QML tolerates.
    """

    appConfigChanged = pyqtSignal()
    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clinical = False
        self._engineering = False
        self._directory = "C:/tmp"
        self.saved_configs: list[dict] = []
        self.write_raw_csv_calls: list[bool] = []

    # ── Mode flags (the gates under test) ────────────────────────────
    def setFlags(self, clinical: bool, engineering: bool):
        self._clinical = bool(clinical)
        self._engineering = bool(engineering)
        self.appConfigChanged.emit()

    def setAltCameraEnabled(self, enabled: bool):
        self._alt_camera_enabled = bool(enabled)
        self.appConfigChanged.emit()

    def setAltLaserEnabled(self, enabled: bool):
        self._alt_laser_enabled = bool(enabled)
        self.appConfigChanged.emit()

    @pyqtProperty("QVariantMap", notify=appConfigChanged)
    def appConfig(self):
        return {
            "engineeringMode": self._engineering,
            "clinicalMode": self._clinical,
            "darkMode": True,
            "altCameraSettingsEnabled": getattr(
                self, "_alt_camera_enabled", False),
            "altLaserPulseWidthEnabled": getattr(
                self, "_alt_laser_enabled", False),
        }

    # ── open()/close() dependencies ──────────────────────────────────
    @pyqtProperty(str, notify=_neverEmitted)
    def directory(self):
        return self._directory

    @directory.setter
    def directory(self, value):
        self._directory = value

    @pyqtSlot("QVariantMap")
    def saveConfigs(self, configs):
        self.saved_configs.append(dict(configs))

    @pyqtSlot(bool)
    def setWriteRawCsv(self, enabled):
        self.write_raw_csv_calls.append(bool(enabled))

    @pyqtSlot("QVariant")
    def setRawCsvDurationSec(self, value):
        pass

    @pyqtSlot(str, "QVariant")
    def setConfig(self, key, value):
        pass

    @pyqtSlot(str, str, int, bool, str)
    def notify(self, text, kind, ms, sticky, tag):
        pass

    # ── Referenced by bindings; neutral values ────────────────────────
    @pyqtProperty(bool, notify=_neverEmitted)
    def consoleConnected(self):
        return False

    @pyqtProperty(bool, notify=_neverEmitted)
    def consoleFanOn(self):
        return False

    @pyqtProperty(bool, notify=_neverEmitted)
    def calibrationRunning(self):
        return False

    @pyqtProperty(bool, notify=_neverEmitted)
    def testScanRunning(self):
        return False

    @pyqtProperty(str, notify=_neverEmitted)
    def calibrationStatus(self):
        return "idle"

    @pyqtProperty(str, notify=_neverEmitted)
    def testScanStatus(self):
        return "idle"

    @pyqtProperty(str, notify=_neverEmitted)
    def calibrationFailureReason(self):
        return ""

    @pyqtProperty(int, notify=_neverEmitted)
    def maxCalibrationTimeSec(self):
        return 600

    @pyqtSlot(result=str)
    def get_sdk_version(self):
        return "0.0.0"


@contextlib.contextmanager
def _basic_controls_style():
    """Force the always-available Basic Controls style during compile —
    see test_plot_viewer_masks.py for the rationale."""
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
def modal_factory():
    """Compile components/SettingsModal.qml; hand out fresh instances.

    Module-scoped: the singleton registration only takes effect for the
    first engine that resolves it, so all tests share one stub and flip
    its flags via setFlags — which doubles as coverage for the live
    rebinding the runtime engineering unlock relies on.
    """
    stub = _StubMotionInterface()
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

    created = []
    # Effective item visibility only resolves inside a shown window —
    # an unattached item tree reads visible=false everywhere. The
    # offscreen platform renders nothing but keeps the visibility
    # propagation logic intact.
    window = QQuickWindow()
    window.resize(1200, 800)
    window.show()

    def make():
        obj = component.create()
        if obj is None:
            raise RuntimeError(
                "SettingsModal.qml failed to instantiate:\n"
                + "\n".join(e.toString() for e in component.errors())
            )
        obj.setParentItem(window.contentItem())
        created.append(obj)
        return obj

    make.stub = stub
    yield make
    for obj in created:
        obj.setParentItem(None)
        obj.deleteLater()
    window.close()
    del engine
    del stub


def _find_card(modal, title):
    """Return the SectionCard child whose ``title`` matches."""
    for child in modal.findChildren(QObject):
        try:
            if child.property("title") == title:
                return child
        except RuntimeError:
            continue
    raise AssertionError(f"No SectionCard titled {title!r} found")


def _invoke(obj, name):
    ok = QMetaObject.invokeMethod(
        obj, name, Qt.ConnectionType.DirectConnection)
    assert ok is not False, f"could not invoke {name}()"


def _card_visible(modal, title):
    """Effective visibility of a card while the modal itself is open."""
    _invoke(modal, "open")
    try:
        return bool(_find_card(modal, title).property("visible"))
    finally:
        _invoke(modal, "close")


def _control_visible(modal, object_name):
    """Effective visibility of a named control while the modal is open."""
    control = modal.findChild(QObject, object_name)
    assert control is not None, f"No control named {object_name!r} found"
    _invoke(modal, "open")
    try:
        return bool(control.property("visible"))
    finally:
        _invoke(modal, "close")


def test_raw_csv_controls_visible_for_research_build(modal_factory):
    """#234: research (non-clinical) users get the raw-CSV toggle
    without the engineering unlock."""
    modal_factory.stub.setFlags(clinical=False, engineering=False)
    modal = modal_factory()
    assert _control_visible(modal, "saveRawCsvSwitch") is True
    assert _control_visible(modal, "rawCsvDurationField") is True


def test_raw_csv_controls_visible_with_engineering_unlock(modal_factory):
    """Engineering unlock shows them even on a clinical build (matches
    the pre-#234 engineering-only behavior)."""
    modal_factory.stub.setFlags(clinical=True, engineering=True)
    modal = modal_factory()
    assert _control_visible(modal, "saveRawCsvSwitch") is True
    assert _control_visible(modal, "rawCsvDurationField") is True


def test_raw_csv_controls_hidden_on_plain_clinical_build(modal_factory):
    """#43: clinical users must not see (or reach) raw-CSV output."""
    modal_factory.stub.setFlags(clinical=True, engineering=False)
    modal = modal_factory()
    assert _control_visible(modal, "saveRawCsvSwitch") is False
    assert _control_visible(modal, "rawCsvDurationField") is False


def test_engineering_card_follows_engineering_flag(modal_factory):
    modal_factory.stub.setFlags(clinical=False, engineering=True)
    modal = modal_factory()
    assert _card_visible(modal, "Engineering") is True

    # Live flip — the same path the runtime unlock/disable exercises.
    modal_factory.stub.setFlags(clinical=False, engineering=False)
    assert _card_visible(modal, "Engineering") is False


def _find_item(root, object_name):
    """Find a named QQuickItem by walking the VISUAL tree.

    findChild() only traverses QObject parentage, which misses
    Repeater-created delegates (their parent is the visual container,
    not the component root) — e.g. the per-camera gain combos (#446).
    """
    if root.objectName() == object_name:
        return root
    for child in root.childItems():
        found = _find_item(child, object_name)
        if found is not None:
            return found
    return None


def test_alt_camera_controls_gate_on_enable_toggle(modal_factory):
    """#446: the exposure + gain dropdowns stay greyed out until the
    'Enable Alternative camera settings?' toggle is on, and the exposure
    list holds the ~100 µs-spaced usability subset of valid 9 µs-row
    values (22 targets snapped to whole rows + the 648 µs firmware
    default = 23 entries) with the default preselected."""
    stub = modal_factory.stub
    stub.setFlags(clinical=False, engineering=True)
    stub.setAltCameraEnabled(False)
    modal = modal_factory()

    exposure = _find_item(modal, "altCameraExposureCombo")
    assert exposure is not None
    assert exposure.property("count") == 23
    assert exposure.property("displayText") == "648 µs (default)"
    assert exposure.property("enabled") is False
    for cam in (1, 8):
        gain = _find_item(modal, f"altCameraGainCombo{cam}")
        assert gain is not None, f"gain combo {cam} not instantiated"
        assert gain.property("count") == 5
        assert gain.property("enabled") is False

    stub.setAltCameraEnabled(True)
    assert exposure.property("enabled") is True
    assert _find_item(
        modal, "altCameraGainCombo8").property("enabled") is True


def test_alt_laser_pulse_width_gates_on_its_own_toggle(modal_factory):
    """#449: the laser pulse-width dropdown gates on its OWN enable —
    turning on the camera settings must never enable it (a camera
    experiment must not silently change laser emission). List = 22
    entries, 100–2200 µs in 100 µs steps, 500 µs default labeled."""
    stub = modal_factory.stub
    stub.setFlags(clinical=False, engineering=True)
    stub.setAltCameraEnabled(True)
    stub.setAltLaserEnabled(False)
    modal = modal_factory()

    combo = _find_item(modal, "altLaserPulseWidthCombo")
    assert combo is not None
    assert combo.property("count") == 22
    assert combo.property("displayText") == "500 µs (default)"
    assert combo.property("enabled") is False  # camera toggle ON, laser OFF

    stub.setAltLaserEnabled(True)
    assert combo.property("enabled") is True


def test_close_never_persists_clinical_mode(modal_factory):
    """#233: the Settings modal must not write clinicalMode back through
    saveConfigs — the last UI path that could persist a mode switch."""
    stub = modal_factory.stub
    stub.setFlags(clinical=False, engineering=False)
    stub.saved_configs.clear()
    modal = modal_factory()
    _invoke(modal, "open")
    _invoke(modal, "close")
    assert stub.saved_configs, "close() should call saveConfigs()"
    saved = stub.saved_configs[-1]
    assert "clinicalMode" not in saved
    assert "plotWindowSec" in saved  # sanity: the payload is the real one
