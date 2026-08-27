"""Standalone-window surfaces stay opaque in liquid-glass mode (#486).

AppTheme's glass branch turns bgBase/bgPlot transparent so the main
window's AmbientBackground shows through every panel — but the two
secondary top-level Windows (LogViewerWindow, TestResultsWindow) render
outside the main window with no ambient behind them, so those tokens
left them see-through to the bare desktop. They now use the
always-opaque windowBg/windowInsetBg tokens, and the IconSmallButton
tooltip moved off bgBase onto overlayBgSolid.

Loads the real QML (with the real AppTheme singleton) in an offscreen
engine and asserts:

- both Windows' root fill is fully opaque with glass on, in dark and
  light mode alike;
- with glass off, windowBg is byte-identical to bgBase (perfect revert —
  the solid theme is unchanged);
- windowInsetBg (the log body) is opaque and overlayBgSolid (the
  tooltip surface) is near-opaque under glass.

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
from PyQt6.QtQuick import QQuickWindow  # noqa: E402,F401  (Window-rooted
# QML instantiates as a bare QWindow unless QtQuick is imported first)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = REPO_ROOT / "components"

# Solid-theme bgBase values — the revert target windowBg must match.
SOLID_BG_DARK = "#1c1c1e"
SOLID_BG_LIGHT = "#f0eee6"

TOKEN_PROBE_QML = b"""
import QtQuick 6.0
import OpenMotion 1.0
QtObject {
    readonly property color bgBase:         AppTheme.bgBase
    readonly property color windowBg:       AppTheme.windowBg
    readonly property color windowInsetBg:  AppTheme.windowInsetBg
    readonly property color overlayBgSolid: AppTheme.overlayBgSolid
}
"""


class _StubMotionInterface(QObject):
    """Just enough MotionInterface for AppTheme + the two Windows."""

    appConfigChanged = pyqtSignal()
    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dark = True
        self._glass = True

    def setTheme(self, dark: bool, glass: bool):
        self._dark = bool(dark)
        self._glass = bool(glass)
        self.appConfigChanged.emit()

    @pyqtProperty("QVariantMap", notify=appConfigChanged)
    def appConfig(self):
        return {
            "darkMode": self._dark,
            "liquidGlass": self._glass,
            "engineeringMode": False,
        }

    # ── LogViewerWindow's poll path (never fires: window stays hidden,
    # but keep it harmless if it ever does) ──────────────────────────
    @pyqtSlot(result=str)
    def appLogPath(self):
        return ""

    @pyqtSlot(result=str)
    def readAppLog(self):
        return ""

    # ── TestResultsWindow bindings ───────────────────────────────────
    @pyqtProperty("QVariantList", notify=_neverEmitted)
    def testScanRows(self):
        return []

    @pyqtProperty(str, notify=_neverEmitted)
    def testScanStatus(self):
        return "idle"

    @pyqtProperty(str, notify=_neverEmitted)
    def testScanFailureReason(self):
        return ""

    @pyqtProperty(bool, notify=_neverEmitted)
    def testScanRunning(self):
        return False

    @pyqtSlot(str)
    def copyToClipboard(self, text):
        pass


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
def probe():
    """One engine, one stub, real AppTheme; hands out live objects.

    Module-scoped because singleton registrations only take effect for
    the first engine that resolves them. Tests flip theme axes through
    ``probe.stub.setTheme`` and read the live bindings back.
    """
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(COMPONENTS / "AppTheme.qml")),
        "OpenMotion", 1, 0, "AppTheme",
    )
    engine = QQmlEngine()

    created = []

    def make(source):
        with _basic_controls_style():
            if isinstance(source, bytes):
                component = QQmlComponent(engine)
                component.setData(source, QUrl())
            else:
                component = QQmlComponent(engine, QUrl.fromLocalFile(source))
        if component.isError():
            raise RuntimeError(
                "QML failed to compile:\n"
                + "\n".join(e.toString() for e in component.errors())
            )
        obj = component.create()
        if obj is None:
            raise RuntimeError(
                "QML failed to instantiate:\n"
                + "\n".join(e.toString() for e in component.errors())
            )
        created.append(obj)
        return obj

    class Probe:
        pass

    p = Probe()
    p.stub = stub
    p.tokens = make(TOKEN_PROBE_QML)
    p.log_win = make(str(COMPONENTS / "LogViewerWindow.qml"))
    p.test_win = make(str(COMPONENTS / "TestResultsWindow.qml"))
    yield p
    for obj in created:
        obj.deleteLater()


def _color(obj, prop):
    return obj.property(prop)


@pytest.mark.parametrize("dark", [True, False])
def test_windows_opaque_under_glass(probe, dark):
    probe.stub.setTheme(dark=dark, glass=True)
    expected = SOLID_BG_DARK if dark else SOLID_BG_LIGHT
    for win in (probe.log_win, probe.test_win):
        c = _color(win, "color")
        assert c.alpha() == 255, f"{win} root fill translucent under glass"
        assert c.name() == expected


@pytest.mark.parametrize("dark", [True, False])
def test_window_tokens_match_solid_bgbase_when_glass_off(probe, dark):
    # Perfect revert: with glass off, the new tokens change nothing —
    # windowBg IS the solid-theme bgBase.
    probe.stub.setTheme(dark=dark, glass=False)
    bg_base = _color(probe.tokens, "bgBase")
    window_bg = _color(probe.tokens, "windowBg")
    assert window_bg == bg_base
    assert bg_base.alpha() == 255


def test_inset_and_overlay_tokens_readable_under_glass(probe):
    for dark in (True, False):
        probe.stub.setTheme(dark=dark, glass=True)
        inset = _color(probe.tokens, "windowInsetBg")
        assert inset.alpha() == 255, "log body translucent under glass"
        overlay = _color(probe.tokens, "overlayBgSolid")
        assert overlay.alphaF() >= 0.9, "tooltip surface too transparent"


def test_glass_on_bgbase_still_transparent(probe):
    # Guard the guard: the fix must NOT have de-glassed bgBase itself —
    # the main window's panels still rely on it being transparent.
    probe.stub.setTheme(dark=True, glass=True)
    assert _color(probe.tokens, "bgBase").alpha() == 0
