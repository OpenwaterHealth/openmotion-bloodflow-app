"""Regression tests for issue #229 — "Clamp the manual plot bounds to
sensible numbers".

The Manual Plot Bounds fields in ``components/SettingsModal.qml`` used to
accept anything the decimal regex allowed: absurd magnitudes
(``999999999``), inverted pairs (min >= max — which flips the axis, and
at min == max divides by zero in ``PlotCell._drawTrace``), and the
persisted config values were trusted blindly on load, so a hand-edited
``app_config.json`` broke the plots without the UI ever being touched.
In clinical mode the manual bounds are always active (the autoscale
toggle is hidden), so bad bounds bricked the clinical plots with no
escape hatch.

The fix (SettingsModal's ``clampBound``/``sanitizeBoundPair``) coerces
every committed bound into a per-metric sane window and keeps min one
display-step clear of max; ``_loadFromConfig`` sanitizes persisted pairs
the same way. These tests load the real ``components/SettingsModal.qml``
in a bare QQmlEngine with a stubbed ``MotionInterface`` singleton, drive
the actual bound TextFields (set ``text`` + emit ``editingFinished``)
and the config-load path, and pin the coercion behavior.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import contextlib
import os
import sys
from pathlib import Path

# A QGuiApplication must exist before any QML Quick item is created, and
# it must be constructed before conftest's session-scoped autouse
# fixture instantiates a bare QCoreApplication. Module import runs at
# collection time — ahead of every fixture — so create it here (same
# pattern as test_plot_viewer_masks.py). Offscreen via argv, not env.
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
    QQmlExpression,
    qmlRegisterSingletonInstance,
    qmlRegisterSingletonType,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_MODAL_QML = REPO_ROOT / "components" / "SettingsModal.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"


class _StubMotionInterface(QObject):
    """Minimal MotionInterface stand-in covering the properties and slots
    SettingsModal.qml binds at creation. ``config`` is swappable per test
    (a fresh modal instance re-reads it in ``_loadFromConfig``)."""

    appConfigChanged = pyqtSignal()
    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._config = {}
        self.saved = None  # last saveConfigs payload

    def setConfig(self, config):
        self._config = dict(config)
        self.appConfigChanged.emit()

    @pyqtProperty("QVariantMap", notify=appConfigChanged)
    def appConfig(self):
        return self._config

    @pyqtProperty(str, notify=_neverEmitted)
    def directory(self):
        return "C:/nonexistent"

    @directory.setter
    def directory(self, value):
        pass

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
    def calibrationFailureReason(self):
        return ""

    @pyqtProperty(int, notify=_neverEmitted)
    def maxCalibrationTimeSec(self):
        return 60

    @pyqtProperty(bool, notify=_neverEmitted)
    def leftSensorConnected(self):
        return False

    @pyqtProperty(bool, notify=_neverEmitted)
    def rightSensorConnected(self):
        return False

    @pyqtProperty(str, notify=_neverEmitted)
    def consoleFirmwareVersion(self):
        return ""

    @pyqtProperty(str, notify=_neverEmitted)
    def leftSensorFirmwareVersion(self):
        return ""

    @pyqtProperty(str, notify=_neverEmitted)
    def rightSensorFirmwareVersion(self):
        return ""

    @pyqtProperty(bool, notify=_neverEmitted)
    def consoleFirmwareUpdateAvailable(self):
        return False

    @pyqtProperty(bool, notify=_neverEmitted)
    def leftSensorFirmwareUpdateAvailable(self):
        return False

    @pyqtProperty(bool, notify=_neverEmitted)
    def rightSensorFirmwareUpdateAvailable(self):
        return False

    @pyqtSlot(result=str)
    def get_sdk_version(self):
        return "0.0.0"

    @pyqtSlot("QVariantMap")
    def saveConfigs(self, cfg):
        self.saved = dict(cfg)

    @pyqtSlot(bool)
    def setWriteRawCsv(self, value):
        pass

    @pyqtSlot("QVariant")
    def setRawCsvDurationSec(self, value):
        pass


@contextlib.contextmanager
def _basic_controls_style():
    """Force the always-available Basic Controls style during QML
    compilation (see test_plot_viewer_masks.py for the rationale)."""
    prev = os.environ.get("QT_QUICK_CONTROLS_STYLE")
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Basic"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("QT_QUICK_CONTROLS_STYLE", None)
        else:
            os.environ["QT_QUICK_CONTROLS_STYLE"] = prev


# Baseline config carrying only sane, non-default bound values so tests
# can tell "loaded from config" apart from "fell back to defaults".
_BASE_CONFIG = {
    "engineeringMode": False,
    "clinicalMode": False,
    "darkMode": True,
    "bfiMin": 0.0,
    "bfiMax": 10.0,
    "bviMin": 0.0,
    "bviMax": 10.0,
    "meanMin": 0.0,
    "meanMax": 500.0,
    "contrastMin": 0.0,
    "contrastMax": 1.0,
}


@pytest.fixture(scope="module")
def modal_factory():
    """Compile components/SettingsModal.qml once; hand out fresh
    instances, optionally with per-test appConfig overrides."""
    stub = _StubMotionInterface()
    stub.setConfig(_BASE_CONFIG)
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    # AppTheme the same way main.py registers it — SettingsModal's color
    # tokens resolve through it (it reads MotionInterface.appConfig, so
    # it must be registered after the stub).
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0, "AppTheme"
    )
    engine = QQmlEngine()
    engine.rootContext().setContextProperty("appVersion", "0.0.0-test")
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

    def make(config_overrides=None):
        cfg = dict(_BASE_CONFIG)
        cfg.update(config_overrides or {})
        stub.setConfig(cfg)
        stub.saved = None
        obj = component.create()
        if obj is None:
            raise RuntimeError(
                "SettingsModal.qml failed to instantiate:\n"
                + "\n".join(e.toString() for e in component.errors())
            )
        created.append(obj)
        return obj

    make.stub = stub

    yield make

    for obj in created:
        obj.deleteLater()
    del engine
    del stub


def _commit(modal, field_object_name, text):
    """Type `text` into a bound field and commit it the way the user
    does — emit ``editingFinished`` so the field's onEditingFinished
    handler runs the clamp. Emitted via a QML-scope expression because
    PyQt6 exposes no binding for QQuickTextField's C++ signals."""
    field = modal.findChild(QObject, field_object_name)
    assert field is not None, f"no field named {field_object_name}"
    field.setProperty("text", text)
    expr = QQmlExpression(
        QQmlEngine.contextForObject(field), field, "editingFinished()"
    )
    expr.evaluate()
    assert not expr.hasError(), expr.error().toString()
    return field


# ── Load-time sanitize — persisted config values are untrusted ─────────


def test_valid_config_bounds_load_unchanged(modal_factory):
    modal = modal_factory({"bfiMin": 1.5, "bfiMax": 8.5,
                           "meanMin": 10, "meanMax": 200})
    assert modal.property("bfiMin") == 1.5
    assert modal.property("bfiMax") == 8.5
    assert modal.property("meanMin") == 10.0
    assert modal.property("meanMax") == 200.0


def test_inverted_config_pair_resets_to_defaults(modal_factory):
    modal = modal_factory({"bfiMin": 50.0, "bfiMax": 2.0})
    assert modal.property("bfiMin") == 0.0
    assert modal.property("bfiMax") == 10.0


def test_equal_config_pair_resets_to_defaults(modal_factory):
    # min == max would make PlotCell divide by zero.
    modal = modal_factory({"bviMin": 5.0, "bviMax": 5.0})
    assert modal.property("bviMin") == 0.0
    assert modal.property("bviMax") == 10.0


def test_absurd_config_magnitudes_clamp_to_metric_windows(modal_factory):
    modal = modal_factory({
        "bviMin": -1e9, "bviMax": 1e9,
        "meanMin": -99999, "meanMax": 99999,
        "contrastMin": -50, "contrastMax": 50,
    })
    assert modal.property("bviMin") == 0.0
    assert modal.property("bviMax") == 10.0
    assert modal.property("meanMin") == 0.0
    assert modal.property("meanMax") == 1024.0
    assert modal.property("contrastMin") == 0.0
    assert modal.property("contrastMax") == 1.0


def test_non_numeric_config_bounds_fall_back_to_defaults(modal_factory):
    cfg = dict(_BASE_CONFIG)
    del cfg["bfiMax"]  # missing key
    cfg["bfiMin"] = "garbage"  # wrong type
    modal = modal_factory(cfg)
    assert modal.property("bfiMin") == 0.0
    assert modal.property("bfiMax") == 10.0


def test_close_persists_sanitized_bounds(modal_factory):
    """A bad legacy config heals: the sanitized pair is what gets saved
    back when Settings closes."""
    modal = modal_factory({"bfiMin": 50.0, "bfiMax": 2.0,
                           "meanMax": 99999})
    QMetaObject.invokeMethod(
        modal, "close", Qt.ConnectionType.DirectConnection
    )
    saved = modal_factory.stub.saved
    assert saved is not None
    assert saved["bfiMin"] == 0.0
    assert saved["bfiMax"] == 10.0
    assert saved["meanMax"] == 1024.0


# ── Commit-time coercion — driving the real TextFields ─────────────────


def test_huge_max_coerces_to_window_edge(modal_factory):
    modal = modal_factory()
    field = _commit(modal, "bfiMaxField", "999999999")
    assert modal.property("bfiMax") == 10.0
    # The correction is visible — the field re-displays the coerced value.
    assert field.property("text") == "10.0"


def test_min_at_or_above_max_coerces_one_step_below(modal_factory):
    modal = modal_factory({"bfiMin": 0.0, "bfiMax": 10.0})
    field = _commit(modal, "bfiMinField", "50")
    assert modal.property("bfiMin") == 9.9
    assert modal.property("bfiMax") == 10.0
    assert field.property("text") == "9.9"


def test_max_at_or_below_min_coerces_one_step_above(modal_factory):
    modal = modal_factory({"bfiMin": 0.0, "bfiMax": 10.0})
    field = _commit(modal, "bfiMaxField", "-5")
    assert modal.property("bfiMax") == 0.1
    assert modal.property("bfiMin") == 0.0
    assert field.property("text") == "0.1"


def test_garbage_text_reverts_to_previous_value(modal_factory):
    modal = modal_factory({"bviMin": 2.0})
    field = _commit(modal, "bviMinField", "abc")
    assert modal.property("bviMin") == 2.0
    assert field.property("text") == "2.0"


def test_empty_text_reverts_to_previous_value(modal_factory):
    modal = modal_factory({"bviMax": 8.0})
    field = _commit(modal, "bviMaxField", "")
    assert modal.property("bviMax") == 8.0
    assert field.property("text") == "8.0"


def test_mean_bounds_clamp_and_keep_integer_step(modal_factory):
    modal = modal_factory({"meanMin": 0.0, "meanMax": 500.0})
    _commit(modal, "meanMaxField", "99999")
    assert modal.property("meanMax") == 1024.0
    # Now try to invert min against the new max — coerces one unit below.
    _commit(modal, "meanMinField", "5000")
    assert modal.property("meanMin") == 1023.0


def test_contrast_min_coerces_below_its_max(modal_factory):
    modal = modal_factory({"contrastMin": 0.0, "contrastMax": 1.0})
    field = _commit(modal, "contrastMinField", "5")
    assert modal.property("contrastMin") == 0.99
    assert field.property("text") == "0.99"


def test_in_window_values_commit_unchanged(modal_factory):
    modal = modal_factory({"bfiMin": 0.0, "bfiMax": 10.0})
    _commit(modal, "bfiMinField", "2.5")
    assert modal.property("bfiMin") == 2.5
    _commit(modal, "bfiMaxField", "8.5")
    assert modal.property("bfiMax") == 8.5
