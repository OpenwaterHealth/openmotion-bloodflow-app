"""Unit tests for the contact-quality modal and its BloodFlow.qml wiring.

BloodFlow.qml can't be instantiated standalone (it needs the whole app), so
its handler contracts are pinned as source-structure assertions. The modal
itself compiles fine in a bare engine, so its signal contract — including the
#492 Force Dismiss behavior — is exercised for real in an offscreen QML
harness (pattern: test_critical_error_modal_footer.py).
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
    QMetaObject,
    QObject,
    QUrl,
    pyqtProperty,
    pyqtSignal,
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
CQ_MODAL_QML = REPO_ROOT / "components" / "ContactQualityModal.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"


def _bloodflow_text():
    qml = REPO_ROOT / "pages" / "BloodFlow.qml"
    return qml.read_text(encoding="utf-8")


# ── BloodFlow.qml handler contracts (source-structure) ──────────────────────

def test_contact_quality_warnings_take_precedence_over_generic_failure():
    text = _bloodflow_text()
    handler_start = text.index("function onContactQualityCheckFinished")
    handler_end = text.index("// Live-scan warnings", handler_start)
    handler = text[handler_start:handler_end]

    warning_branch = handler.index("warnings.length")
    generic_failure_branch = handler.index("if (!ok)")

    assert warning_branch < generic_failure_branch


def test_force_dismiss_arms_scan_scoped_suppression():
    """#492: Force Dismiss must set the existing suppressLiveCqModal flag —
    but only while a scan is running, so a force-dismissed quick check or
    pre-scan gate can never arm suppression outside a scan."""
    text = _bloodflow_text()
    start = text.index("onForceDismissed:")
    handler = text[start:text.index("onContinueRequested:", start)]
    assert "bloodFlow.suppressLiveCqModal = true" in handler
    assert "bloodFlow.scanning" in handler


def test_both_live_cq_entry_points_honor_the_suppress_flag():
    """The flag only means "stays dismissed" (#492) if every path that can
    re-open the modal mid-scan checks it: onContactQualityWarning
    (reset/addWarning both self-open) and onContactQualityIssueStateChanged
    (clearWarning re-opens on the last clear during a live scan)."""
    text = _bloodflow_text()
    for fn in ("function onContactQualityWarning",
               "function onContactQualityIssueStateChanged"):
        start = text.index(fn)
        assert "suppressLiveCqModal" in text[start:start + 300], fn


def test_suppression_ends_with_the_scan_at_both_boundaries():
    """"Until the end of that session" (#492): the flag resets when a new
    scan starts AND when the running one finishes, so the next scan warns
    again no matter which boundary is crossed first."""
    text = _bloodflow_text()
    begin = text.index("function beginScanNow")
    assert "suppressLiveCqModal = false" in text[begin:begin + 500]
    finished = text.index("onScanFinished:", text.index("id: scanRunner"))
    assert "suppressLiveCqModal = false" in text[finished:finished + 500]


# ── ContactQualityModal.qml signal contract (offscreen harness) ─────────────

class _StubMotionInterface(QObject):
    """Minimal MotionInterface stand-in covering every member the
    ContactQualityModal.qml tree (and AppTheme.qml) references."""

    _neverEmitted = pyqtSignal()

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        return {"engineeringMode": True, "darkMode": True}

    @pyqtProperty(bool, notify=_neverEmitted)
    def leftSensorConnected(self):
        return True

    @pyqtProperty(bool, notify=_neverEmitted)
    def rightSensorConnected(self):
        return True


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
def cq_modal():
    """Compile ContactQualityModal.qml once for this module."""
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0,
        "AppTheme",
    )
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(CQ_MODAL_QML)))
    if component.isError():
        raise RuntimeError(
            "ContactQualityModal.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    obj = component.create()
    if obj is None:
        raise RuntimeError(
            "ContactQualityModal.qml failed to instantiate:\n"
            + "\n".join(e.toString() for e in component.errors())
        )

    yield obj

    obj.deleteLater()
    del engine
    del stub


def _is_quick_button(obj):
    """A QML `Button {}` instance reports a synthetic className like
    Button_QMLTYPE_12 — walk the superclass chain to the C++ base."""
    mo = obj.metaObject()
    while mo is not None:
        if mo.className() in ("QQuickButton", "QQuickAbstractButton"):
            return True
        mo = mo.superClass()
    return False


def _footer_buttons(modal_obj):
    return [c for c in modal_obj.findChildren(QObject) if _is_quick_button(c)]


def test_force_dismiss_emits_force_dismissed_and_still_dismissed(cq_modal):
    """#492: the button must fire the new forceDismissed() AND keep firing
    dismissed() — BloodFlow's onDismissed still unwinds clinicalStartPending
    and preScanMode on a force dismiss."""
    fired = {"force": 0, "dismissed": 0}
    cq_modal.forceDismissed.connect(
        lambda: fired.__setitem__("force", fired["force"] + 1))
    cq_modal.dismissed.connect(
        lambda: fired.__setitem__("dismissed", fired["dismissed"] + 1))

    btn = cq_modal.findChild(QObject, "cqForceDismissBtn")
    assert btn is not None, "Force Dismiss button not found by objectName"
    # Emitting the clicked() signal runs the QML onClicked handler; PyQt6's
    # invokeMethod returns the (void) result, so success is asserted via
    # the observed emissions below, not the return value.
    QMetaObject.invokeMethod(btn, "clicked")

    assert fired == {"force": 1, "dismissed": 1}


def test_no_other_footer_button_emits_force_dismissed(cq_modal):
    """Guards the #492 hazard of forceDismissed leaking into the shared
    dismiss path: only the engineering escape hatch may arm scan-long
    suppression. Dismiss / Retest / Start Scan / Stop scan / Continue must
    all stay soft (warn again on the next latch)."""
    counts = {"force": 0, "root": 0}
    cq_modal.forceDismissed.connect(
        lambda: counts.__setitem__("force", counts["force"] + 1))
    # Every non-force footer button fires at least one of these root
    # signals from its onClicked — used as per-click proof that the
    # invokeMethod dispatch actually ran the handler.
    for sig in (cq_modal.dismissed, cq_modal.retestRequested,
                cq_modal.stopScanRequested, cq_modal.continueRequested):
        sig.connect(lambda: counts.__setitem__("root", counts["root"] + 1))

    others = [b for b in _footer_buttons(cq_modal)
              if b.objectName() != "cqForceDismissBtn"]
    # preScan Dismiss/Retest/Start Scan, Stop scan, Continue,
    # quick-check Dismiss/Retest — anything fewer means the sweep is
    # not actually covering the footer.
    assert len(others) >= 7, [b.property("text") for b in others]
    for b in others:
        before = counts["root"]
        QMetaObject.invokeMethod(b, "clicked")
        assert counts["root"] > before, b.property("text")

    assert counts["force"] == 0
