"""Optimize Laser QML: every touched file compiles, the progress modal
follows the connector's signals, and the Settings section shows only on a
research build and saves / restores through the connector.

Bare offscreen QQmlEngine with a stubbed ``MotionInterface`` singleton
(the test_settings_modal_mode_gates.py pattern, whose stub this extends).
Unit-marked: no app launch, no hardware.
"""

import pytest

# Creates the offscreen QGuiApplication before conftest's QCoreApplication.
import test_settings_modal_mode_gates as base  # noqa: E402
from PyQt6.QtCore import (  # noqa: E402
    Q_ARG, QCoreApplication, QMetaObject, QObject, Qt, QUrl, pyqtProperty, pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtQml import QQmlComponent, QQmlEngine, qmlRegisterSingletonInstance  # noqa: E402
from PyQt6.QtQuick import QQuickWindow  # noqa: E402

pytestmark = pytest.mark.unit

REPO_ROOT = base.REPO_ROOT
FORM = {"method": "fit", "targetMean": 200.0, "passingRange": 20.0, "startPoint": "max",
        "order": "gain,pulse_width,current", "gainBounds": [[1.0, 16.0]] * 4,
        "pulseWidthBounds": [100.0, 2096.0], "currentBounds": [2500.0, 5000.0],
        "gainMode": "individual", "gainPriority": "near", "framesPerIteration": 1,
        "inRangePoints": 1}


class _Stub(base._StubMotionInterface):
    optimizeLaserStateChanged = pyqtSignal()
    optimizeLaserStarted = pyqtSignal()
    optimizeLaserProgress = pyqtSignal("QVariantMap")
    optimizeLaserFinished = pyqtSignal(bool, str, "QVariantMap")
    optimizeLaserDecisionNeeded = pyqtSignal("QVariantMap")
    laserDefaultsRestored = pyqtSignal(bool, str)

    def __init__(self):
        super().__init__()
        self.available = True
        self.saved_forms = []
        self.restores = 0
        self.answers = []
        self.current = {"source": "default", "gainBounds": ["16×", "4×", "2×", "1×"],
                        "pulseWidthBounds": "530 µs", "currentBounds": "5000 mA",
                        "exposure": "648 µs"}

    @pyqtProperty("QVariantMap", notify=optimizeLaserStateChanged)
    def optimizeLaserCurrentValues(self):
        return self.current

    @pyqtSlot(str)
    def answerOptimizeLaser(self, answer):
        self.answers.append(answer)

    @pyqtProperty(bool, notify=optimizeLaserStateChanged)
    def optimizeLaserAvailable(self):
        return self.available

    @pyqtProperty(bool, notify=optimizeLaserStateChanged)
    def optimizeLaserActive(self):
        return False

    @pyqtProperty(bool, notify=optimizeLaserStateChanged)
    def optimizeLaserRunning(self):
        return False

    @pyqtProperty("QVariantMap", notify=optimizeLaserStateChanged)
    def optimizeLaserSummary(self):
        return {}

    @pyqtSlot(result="QVariantMap")
    def optimizeLaserSettings(self):
        return {"values": dict(FORM), "problems": [], "defaults": dict(FORM),
                "method": [{"value": "fit", "label": "Fit"}, {"value": "basic", "label": "Basic"}],
                "startPoint": [], "order": [], "gainMode": [], "gainPriority": [],
                "limits": {"gain": [1, 16], "pulseWidthUs": [100, 2096],
                           "currentMa": [2500, 5000], "exposureUs": 2196}}

    @pyqtSlot("QVariantMap", result="QVariantList")
    def setOptimizeLaserSettings(self, form):
        self.saved_forms.append(dict(form))
        return [] if form.get("targetMean") != -1 else ["target mean is nonsense"]

    @pyqtSlot(result=bool)
    def restoreDefaultLaserSettings(self):
        self.restores += 1
        return True

    # The section calls notify(text, type, ms); the base stub has only the
    # five-argument form.
    @pyqtSlot(str, str, int)
    @pyqtSlot(str, str, int, bool, str)
    def notify(self, text, kind="info", ms=0, sticky=True, tag=""):
        pass


@pytest.fixture(scope="module")
def qml():
    stub = _Stub()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    engine = QQmlEngine()
    window = QQuickWindow()
    window.resize(1400, 900)
    window.show()
    created = []

    def compile_(*parts):
        with base._basic_controls_style():
            component = QQmlComponent(engine, QUrl.fromLocalFile(str(REPO_ROOT.joinpath(*parts))))
        return component

    def make(*parts):
        component = compile_(*parts)
        assert not component.isError(), "\n".join(e.toString() for e in component.errors())
        obj = component.create()
        assert obj is not None, "\n".join(e.toString() for e in component.errors())
        obj.setParentItem(window.contentItem())
        created.append(obj)
        return obj

    make.compile = compile_
    make.stub = stub
    yield make
    for obj in created:
        obj.setParentItem(None)
        obj.deleteLater()
    window.close()


@pytest.mark.parametrize("parts", [
    ("components", "OptimizeLaserModal.qml"),
    ("components", "ButtonPanel.qml"),
    ("components", "SettingsModal.qml"),
    ("pages", "scan", "OptimizeLaserTask.qml"),
    ("pages", "scan", "ScanRunner.qml"),
    ("pages", "BloodFlow.qml"),
])
def test_files_compile(qml, parts):
    component = qml.compile(*parts)
    assert not component.isError(), "\n".join(e.toString() for e in component.errors())


def test_modal_follows_progress_and_result(qml):
    modal = qml("components", "OptimizeLaserModal.qml")
    base._invoke(modal, "reset")
    assert modal.property("running") is True
    assert modal.property("dismissable") is False
    qml.stub.optimizeLaserProgress.emit({
        "iteration": 2, "pulseWidthUs": 1901.0, "emittedPulseWidthUs": 1883.7,
        "currentMa": 5000.0, "done": False, "status": "",
        "next": {"pulseWidthUs": 1800.0, "currentMa": 5000.0},
        "cameras": [{"camera": "L1", "side": "left", "position": 1, "gain": 16.0,
                     "gainCode": 256, "mean": 328.0, "signal": 200.0, "dark": 128.0,
                     "sat": 0.0, "onTarget": True, "status": "on_target"},
                    {"camera": "R4", "side": "right", "position": 4, "gain": 1.0,
                     "gainCode": 16, "mean": 1023.0, "signal": None, "dark": 128.0,
                     "sat": 1.0, "onTarget": False, "status": "adjusting"}]})
    assert modal.property("iteration") == 2
    assert len(_py(modal.property("cameras"))) == 2
    assert "Iteration 2" in modal.property("stage")
    qml.stub.optimizeLaserFinished.emit(True, "The priority cameras are on target.", {
        "alerts": ["R4: too bright at minimum gain"], "csvName": "optimize-x.csv",
        "cameras": [], "pulseWidthUs": 1901.0, "emittedPulseWidthUs": 1883.7,
        "currentMa": 5000.0, "iterations": 3})
    assert modal.property("finished") is True and modal.property("ok") is True
    assert modal.property("dismissable") is True
    assert _py(modal.property("alerts")) == ["R4: too bright at minimum gain"]
    assert modal.property("csvName") == "optimize-x.csv"


def test_modal_table_fits_the_card(qml):
    """Bench, 2026-10-28: the right sensor's status text ran past the card."""
    modal = qml("components", "OptimizeLaserModal.qml")
    # The app's window size; a test-created modal has no parent to fill.
    modal.setProperty("width", 1400)
    modal.setProperty("height", 900)
    base._invoke(modal, "reset")
    base._invoke(modal, "open")
    rows = [{"camera": f"{s[0].upper()}{p}", "side": s, "position": p, "gain": 13.0625,
             "gainCode": 209, "mean": 710.0, "signal": 582.0, "dark": 128.0, "sat": 0.07,
             "onTarget": False, "status": "too_bright"}
            for s in ("left", "right") for p in range(1, 9)]
    qml.stub.optimizeLaserProgress.emit({
        "iteration": 3, "pulseWidthUs": 2096.0, "emittedPulseWidthUs": 2078.7,
        "currentMa": 5000.0, "done": True, "status": "partial",
        "next": {"pulseWidthUs": 2096.0, "currentMa": 5000.0}, "cameras": rows})
    for _ in range(5):          # let the layouts polish
        QCoreApplication.processEvents()
    card = modal.findChild(QObject, "optimizeLaserCard")
    table = modal.findChild(QObject, "optimizeLaserTable")
    assert table.property("implicitWidth") > 400     # really laid out
    assert table.property("implicitWidth") <= card.property("width") - 48, (
        table.property("implicitWidth"), card.property("width"))
    modal.setProperty("running", False)
    base._invoke(modal, "close")


def test_modal_shows_errors_from_the_runner(qml):
    modal = qml("components", "OptimizeLaserModal.qml")
    base._invoke(modal, "reset")
    QMetaObject.invokeMethod(modal, "showError", Qt.ConnectionType.DirectConnection,
                             Q_ARG("QVariant", "Flash step timed out"))
    assert modal.property("running") is False
    assert modal.property("message") == "Flash step timed out"


def _py(value):
    """A QML ``var`` property read from Python (QJSValue -> plain value)."""
    return value.toVariant() if hasattr(value, "toVariant") else value


def _section(modal):
    section = modal.findChild(QObject, "optimizeLaserSection")
    assert section is not None
    return section


def test_settings_section_is_research_only(qml):
    stub = qml.stub
    modal = qml("components", "SettingsModal.qml")
    stub.setFlags(clinical=False, engineering=True)
    base._invoke(modal, "open")
    try:
        assert _section(modal).property("visible") is True
        stub.setFlags(clinical=True, engineering=True)
        assert _section(modal).property("visible") is False
        stub.setFlags(clinical=False, engineering=True)
        stub.available = False
        stub.optimizeLaserStateChanged.emit()
        assert _section(modal).property("visible") is False
    finally:
        stub.available = True
        stub.optimizeLaserStateChanged.emit()
        base._invoke(modal, "close")


def test_settings_section_saves_and_restores_through_the_connector(qml):
    stub = qml.stub
    modal = qml("components", "SettingsModal.qml")
    stub.setFlags(clinical=False, engineering=True)
    base._invoke(modal, "open")
    try:
        section = _section(modal)
        base._invoke(section, "load")
        assert section.property("dirty") is False
        section.setProperty("form", {**FORM, "targetMean": 250.0})
        assert section.property("dirty") is True
        _save(section)
        assert stub.saved_forms[-1]["targetMean"] == 250.0
        # A refused save keeps the errors on screen and the edits in the form.
        section.setProperty("form", {**FORM, "targetMean": -1})
        _save(section)
        assert _py(section.property("errors")) == ["target mean is nonsense"]
        assert section.property("dirty") is True
        button = modal.findChild(QObject, "restoreDefaultLaserButton")
        assert button is not None
        base._invoke(button, "clicked")
        assert stub.restores == 1
    finally:
        base._invoke(modal, "close")


def _find_item(root, name):
    """An item by objectName through the visual tree: Repeater delegates
    aren't QObject children, so findChild() misses them."""
    from PyQt6 import sip
    from PyQt6.QtQuick import QQuickItem
    stack = [sip.cast(root, QQuickItem)]
    while stack:
        item = stack.pop()
        if item.objectName() == name:
            return item
        stack.extend(item.childItems())
    return None


def _save(section, closing=False):
    QMetaObject.invokeMethod(section, "save", Qt.ConnectionType.DirectConnection,
                             Q_ARG("QVariant", closing))


def test_settings_section_autosaves_on_close(qml):
    stub = qml.stub
    modal = qml("components", "SettingsModal.qml")
    stub.setFlags(clinical=False, engineering=True)
    base._invoke(modal, "open")
    section = _section(modal)
    base._invoke(section, "load")
    n = len(stub.saved_forms)
    section.setProperty("form", {**FORM, "inRangePoints": 7})
    base._invoke(modal, "close")
    assert len(stub.saved_forms) == n + 1 and stub.saved_forms[-1]["inRangePoints"] == 7
    # Unchanged: closing saves nothing.
    base._invoke(modal, "open")
    base._invoke(section, "load")
    base._invoke(modal, "close")
    assert len(stub.saved_forms) == n + 1


def test_settings_section_shows_the_current_values(qml):
    stub = qml.stub
    modal = qml("components", "SettingsModal.qml")
    stub.setFlags(clinical=False, engineering=True)
    base._invoke(modal, "open")
    try:
        def text(name):
            return _find_item(modal, name).property("text")
        assert text("optimizeLaserCurrentHeader") == "Current (default)"
        assert text("optimizeLaserCurrent_gainBounds0") == "16×"
        assert text("optimizeLaserCurrent_pulseWidthBounds-1") == "530 µs"
        assert text("optimizeLaserCurrent_exposure") == "648 µs"
        stub.current = {"source": "optimized", "gainBounds": ["12.125–16×", "1.5–1.6875×",
                                                              "1×", "1×"],
                        "pulseWidthBounds": "2096 µs", "currentBounds": "5000 mA",
                        "exposure": "2196 µs"}
        stub.optimizeLaserStateChanged.emit()
        assert text("optimizeLaserCurrentHeader") == "Current (optimized)"
        assert text("optimizeLaserCurrent_gainBounds0") == "12.125–16×"
        assert text("optimizeLaserCurrent_pulseWidthBounds-1") == "2096 µs"
        # The yellow status block and Revert are gone.
        assert modal.findChild(QObject, "optimizeLaserActiveText") is None
    finally:
        base._invoke(modal, "close")


def test_modal_asks_keep_iterating_or_accept(qml):
    stub = qml.stub
    modal = qml("components", "OptimizeLaserModal.qml")
    base._invoke(modal, "reset")
    stub.optimizeLaserDecisionNeeded.emit({
        "iteration": 20, "pulseWidthUs": 2096.0, "emittedPulseWidthUs": 2078.7,
        "currentMa": 5000.0, "done": True, "status": "not_converged",
        "next": {"pulseWidthUs": 2096.0, "currentMa": 5000.0}, "cameras": [],
        "question": "The cameras have not settled ... Keep iterating, or accept?"})
    assert modal.property("deciding") is True
    assert modal.property("dismissable") is False
    assert "Keep iterating" in modal.findChild(QObject, "optimizeLaserQuestion").property("text")
    for choice in ("continue", "accept", "stop"):
        assert _find_item(modal, f"optimizeLaserAnswer_{choice}") is not None
    base._invoke(_find_item(modal, "optimizeLaserAnswer_accept"), "clicked")
    assert stub.answers[-1] == "accept" and modal.property("deciding") is False


def test_modal_shows_the_total_time(qml):
    stub = qml.stub
    modal = qml("components", "OptimizeLaserModal.qml")
    base._invoke(modal, "reset")
    modal.setProperty("startedAt", modal.property("startedAt") - 119_000)   # 119 s ago
    stub.optimizeLaserFinished.emit(True, "Every camera is on target.", {
        "alerts": [], "csvName": "", "cameras": [], "pulseWidthUs": 1000.0,
        "emittedPulseWidthUs": 982.7, "currentMa": 5000.0, "iterations": 3,
        "runSeconds": 14.2})
    assert modal.findChild(QObject, "optimizeLaserTime").property("text") == (
        "Total time: 1 min 59 s (camera setup 1 min 45 s, optimizing 14 s)")


def test_button_panel_shows_the_button_only_when_asked(qml):
    panel = qml("components", "ButtonPanel.qml")
    button = panel.findChild(QObject, "optimizeLaserButton")
    assert button is not None and button.property("visible") is False
    panel.setProperty("optimizeLaserVisible", True)
    assert button.property("visible") is True
