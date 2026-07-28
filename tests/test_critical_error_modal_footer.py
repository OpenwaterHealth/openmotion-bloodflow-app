"""Unit tests for the footer button row in components/CriticalErrorModal.qml.

Left to size themselves, the three footer buttons came out 119 / 142 / 92 px
(label width plus the Material style's 24px-a-side padding), which made the
primary action the narrowest control in the row and gave the longest label a
content box smaller than the label itself. These tests pin the geometry
contract that replaced that: one shared width for all three, and a row that
still fits inside the card's fixed 520px.

Assertions are deliberately font-independent — the offscreen platform
resolves a fallback font whose metrics are ~2x the real Segoe UI ones, so
anything compared against a measured label width would be meaningless here.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import contextlib
import os
import sys
from pathlib import Path

# A QGuiApplication must exist before any QML Quick item is created, and it
# must be constructed before conftest's session-scoped autouse fixture
# instantiates a bare QCoreApplication. Module import runs at collection
# time — ahead of every fixture — so create it here. The offscreen platform
# is passed via argv, NOT via QT_QPA_PLATFORM, so the process environment is
# untouched (see test_logs_modal_filters.py).
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

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
ERROR_MODAL_QML = REPO_ROOT / "components" / "CriticalErrorModal.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"

FOOTER_BUTTONS = ("errCopyDetailsBtn", "errContactSupportBtn",
                  "errDismissBtn")

# Mirrors the panel geometry in CriticalErrorModal.qml: a fixed 520px card
# with 24px content margins and 10px between footer buttons.
CARD_WIDTH = 520
CARD_MARGIN = 24
FOOTER_SPACING = 10


class _StubMotionInterface(QObject):
    """Minimal MotionInterface stand-in covering every member
    CriticalErrorModal.qml (and AppTheme.qml) references."""

    criticalErrorRaised = pyqtSignal(str, str, str, str, str)
    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dismissed_count = 0

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        return {"darkMode": True}

    @pyqtSlot()
    def criticalErrorsDismissed(self):
        self.dismissed_count += 1

    @pyqtSlot(str)
    def copyToClipboard(self, _text):
        pass

    @pyqtSlot(str)
    def sendBugReport(self, _code):
        pass

    @pyqtSlot(str, str, int, bool, str)
    def notify(self, *_args):
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


@pytest.fixture(scope="module")
def modal():
    """Compile CriticalErrorModal.qml once and raise one error into it."""
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0,
        "AppTheme",
    )
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(ERROR_MODAL_QML))
        )
    if component.isError():
        raise RuntimeError(
            "CriticalErrorModal.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    obj = component.create()
    if obj is None:
        raise RuntimeError(
            "CriticalErrorModal.qml failed to instantiate:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    # Populate it the way the connector does. (root.visible still reads
    # False afterwards — the item is parentless in a bare engine, so it has
    # no effective visibility; the layout below is computed regardless.)
    stub.criticalErrorRaised.emit(
        "E-304", "Device disconnected during scan",
        "A console or sensor was disconnected while the scan was running.",
        "Check the USB cables and power, then start a new scan.",
        "ConnectionError: sensor LEFT vanished from USB",
    )

    yield obj

    obj.deleteLater()
    del engine
    del stub


def _button(modal_obj, object_name):
    btn = modal_obj.findChild(QObject, object_name)
    assert btn is not None, f"footer button {object_name!r} not found"
    return btn


def test_all_footer_buttons_share_one_width(modal):
    """The row reads as a set, and the primary action is never the runt."""
    natural = {
        name: _button(modal, name).property("implicitWidth")
        for name in FOOTER_BUTTONS
    }
    # Sanity check that the test isn't tautological: the labels differ in
    # length, so left to themselves these buttons would be ragged.
    assert len(set(natural.values())) == len(FOOTER_BUTTONS), (
        f"expected three different natural widths, got {natural}"
    )

    laid_out = {
        name: _button(modal, name).property("width")
        for name in FOOTER_BUTTONS
    }
    expected = float(modal.property("_footerButtonWidth"))
    assert set(laid_out.values()) == {expected}, (
        f"footer buttons must all be {expected}px, got {laid_out}"
    )


def test_footer_row_fits_inside_the_fixed_card(modal):
    """Guards the clipping mode that forced the card's fixed 520px width:
    three buttons plus their spacing must fit the card's content box."""
    width = modal.property("_footerButtonWidth")
    assert width is not None, "_footerButtonWidth missing from the modal"
    row_width = 3 * width + 2 * FOOTER_SPACING
    content_width = CARD_WIDTH - 2 * CARD_MARGIN
    assert row_width <= content_width, (
        f"footer row needs {row_width}px but the card only offers "
        f"{content_width}px — it will clip"
    )


def test_footer_padding_is_applied_as_left_and_right(modal):
    """The Material style assigns leftPadding/rightPadding directly, so a
    horizontalPadding override is silently ignored — assert the padding
    actually landed, and that the label keeps a usable content box."""
    pad = modal.property("_footerButtonPad")
    assert pad is not None, "_footerButtonPad missing from the modal"
    for name in FOOTER_BUTTONS:
        btn = _button(modal, name)
        assert btn.property("leftPadding") == pad, name
        assert btn.property("rightPadding") == pad, name
    content_box = modal.property("_footerButtonWidth") - 2 * pad
    assert content_box >= 100, (
        f"only {content_box}px left for the label — the longest "
        f'("Contact Support") needs ~94px at 13px Segoe UI'
    )
