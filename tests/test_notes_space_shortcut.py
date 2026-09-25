"""Space opens the notes modal on every press during a scan (#517).

Loads the real pages/BloodFlow.qml in an offscreen QQuickView with a
stubbed ``MotionInterface`` singleton and drives it with real key events,
so the window-level ``Shortcut``, the ModalManager and NotesModal all run
exactly as they do in the app.

The bug: hiding an item does not take its keyboard focus away. After the
first Space → type → Escape, the notes TextArea was invisible but still
held focus, accepted the next Space as text, and the shortcut never fired
again until the operator clicked back into the app. BloodFlow now hands
focus back to the plot viewer whenever the last modal closes.

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
    Q_ARG,
    QPoint,
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
    qmlRegisterSingletonInstance,
    qmlRegisterSingletonType,
)
from PyQt6.QtQuick import QQuickView  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
BLOODFLOW_QML = REPO_ROOT / "pages" / "BloodFlow.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"


class _StubMotionInterface(QObject):
    """What the Space shortcut and NotesModal touch. Everything else
    BloodFlow.qml binds to degrades to undefined-binding warnings."""

    _neverEmitted = pyqtSignal()
    triggerStateChanged = pyqtSignal()
    scanNotesChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._trigger_state = "OFF"
        self._scan_notes = ""

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        return {"clinicalMode": False, "darkMode": True}

    @pyqtProperty(str, notify=_neverEmitted)
    def userLabel(self):
        return "owTEST"

    @pyqtProperty(str, notify=triggerStateChanged)
    def triggerState(self):
        return self._trigger_state

    def set_trigger_state(self, value):
        self._trigger_state = value
        self.triggerStateChanged.emit()

    @pyqtProperty(str, notify=scanNotesChanged)
    def scanNotes(self):
        return self._scan_notes

    @scanNotes.setter
    def scanNotes(self, value):
        self._scan_notes = value or ""
        self.scanNotesChanged.emit()

    @pyqtSlot(result=str)
    def scanElapsedStr(self):
        return "00:01:23"

    @pyqtSlot(result=int)
    def scanElapsedSec(self):
        return 83

    @pyqtSlot(str, str, int, bool)
    def notify(self, message, level, duration_ms, dismissable):
        pass


@contextlib.contextmanager
def _basic_controls_style():
    """Temporarily force the always-available Basic Controls style (the
    platform-native style plugin fails to load in a bare offscreen test
    process; restore right after so HIL subprocesses can't inherit it)."""
    prev = os.environ.get("QT_QUICK_CONTROLS_STYLE")
    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Basic"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("QT_QUICK_CONTROLS_STYLE", None)
        else:
            os.environ["QT_QUICK_CONTROLS_STYLE"] = prev


class _Page:
    """A loaded BloodFlow page plus the handles the tests drive."""

    def __init__(self, stub, view):
        self.stub = stub
        self.view = view
        self.root = view.rootObject()
        self.modal_manager = self.root.property("modalManager")
        self.notes = self._modal("Session Notes")

    def _modal(self, label):
        for m in self.modal_manager.property("modals").toVariant():
            if m.property("label") == label:
                return m
        raise AssertionError(f"no modal labelled {label!r}")

    def button_panel(self):
        """The icon bar, found by its notesClicked signal (file-internal
        QML ids are not reachable from Python)."""
        stack = [self.root]
        while stack:
            item = stack.pop()
            if item.metaObject().indexOfSignal("notesClicked()") >= 0:
                return item
            stack.extend(item.childItems())
        raise AssertionError("no ButtonPanel in BloodFlow.qml")

    def pump(self, ms=50):
        QTest.qWait(ms)

    def key(self, key):
        QTest.keyClick(self.view, key)
        self.pump()

    def type_text(self, text):
        for ch in text:
            QTest.keyClick(self.view, ch)
        self.pump()

    def current_label(self):
        current = self.modal_manager.property("current")
        return None if current is None else current.property("label")

    def focus_item(self):
        return self.view.activeFocusItem()

    def focus_is_inside(self, item):
        node = self.focus_item()
        while node is not None:
            if node is item:
                return True
            node = node.parentItem()
        return False

    def start_scan(self):
        self.root.setProperty("scanning", True)
        self.stub.set_trigger_state("ON")
        self.pump()


def _load_page():
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0,
        "AppTheme",
    )
    view = QQuickView()
    view.rootContext().setContextProperty("appVersion", "test")
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.resize(1200, 800)
    with _basic_controls_style():
        view.setSource(QUrl.fromLocalFile(str(BLOODFLOW_QML)))
    if view.status() != QQuickView.Status.Ready:
        raise RuntimeError(
            "BloodFlow.qml failed to load:\n"
            + "\n".join(e.toString() for e in view.errors())
        )
    view.show()
    view.requestActivate()
    # The Shortcut only fires in the application's focus window.
    assert QTest.qWaitForWindowActive(view, 5000)
    p = _Page(stub, view)
    p.pump(200)
    return p


@pytest.fixture
def page():
    p = _load_page()
    yield p
    p.view.close()
    p.view.deleteLater()
    QTest.qWait(0)


def _open_with_space(page):
    page.key(Qt.Key.Key_Space)
    assert page.current_label() == "Session Notes"
    assert page.focus_item() is not None
    assert page.focus_item().metaObject().className().startswith("TextArea")


def test_space_opens_notes_on_every_press(page):
    """The #517 repro: Space → type → Escape, three times over."""
    page.start_scan()
    for n in range(1, 4):
        _open_with_space(page)
        page.type_text(f"note{n}")
        page.key(Qt.Key.Key_Escape)
        assert page.current_label() is None
        # Focus is back on a visible item, not stranded in the hidden
        # TextArea where the next Space would be typed as text.
        assert page.focus_item() is not None
        assert page.focus_item().isVisible()

    lines = page.stub.scanNotes.splitlines()
    assert [line.rsplit(" ", 1)[-1] for line in lines] == [
        "note1", "note2", "note3"]
    assert all(line.startswith("[00:01:23 / ") for line in lines)


@pytest.mark.parametrize("close_path", ["backdrop", "icon_bar"])
def test_space_reopens_after_any_close_path(page, close_path):
    page.start_scan()
    _open_with_space(page)
    page.type_text("x")
    if close_path == "backdrop":
        # Top-right corner: backdrop, well clear of the centered card.
        QTest.mouseClick(page.view, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(1150, 40))
    else:
        # Clicking the Notes icon a second time closes the modal.
        QMetaObject.invokeMethod(page.button_panel(), "notesClicked")
    page.pump()
    assert page.current_label() is None

    _open_with_space(page)


def test_switching_modals_keeps_the_new_modals_focus(page):
    """Closing Notes on the way to another modal must not pull focus
    back to the viewer after the new modal has taken it."""
    page.start_scan()
    _open_with_space(page)
    offer = page._modal("Sample Dataset")
    QMetaObject.invokeMethod(page.modal_manager, "toggle",
                             Qt.ConnectionType.DirectConnection,
                             Q_ARG("QVariant", offer))
    page.pump()
    assert page.current_label() == "Sample Dataset"
    assert page.focus_is_inside(offer)


def test_space_does_nothing_outside_a_scan(page):
    page.key(Qt.Key.Key_Space)
    assert page.current_label() is None
