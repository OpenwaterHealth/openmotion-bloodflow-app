"""Unit tests for the contact-quality modal and its BloodFlow.qml wiring.

BloodFlow.qml can't be instantiated standalone (it needs the whole app), so
its handler contracts are pinned as source-structure assertions. The modal
itself compiles fine in a bare engine, so its signal contract — including the
#492 Force Dismiss behavior — and the #709 build/engineering gate on that
button are exercised for real in an offscreen QML harness (pattern:
test_critical_error_modal_footer.py).
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
    Q_ARG,
    Q_RETURN_ARG,
    QCoreApplication,
    QMetaObject,
    QObject,
    Qt,
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
from PyQt6.QtQuick import QQuickWindow  # noqa: E402

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

    appConfigChanged = pyqtSignal()
    _neverEmitted = pyqtSignal()
    # The connector's dropout watchdog signals (#731 feeds the modal's
    # no-data dots from them): side, cam_id (0-7), elapsed.
    cameraDropoutDetected = pyqtSignal(str, int, str)
    cameraDropoutRecovered = pyqtSignal(str, int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clinical = False
        self._engineering = True

    def setFlags(self, clinical: bool, engineering: bool):
        self._clinical = bool(clinical)
        self._engineering = bool(engineering)
        self.appConfigChanged.emit()

    @pyqtProperty("QVariantMap", notify=appConfigChanged)
    def appConfig(self):
        return {
            "engineeringMode": self._engineering,
            "clinicalMode": self._clinical,
            "darkMode": True,
        }

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
def cq_harness():
    """Compile ContactQualityModal.qml once for this module; hand out
    instances. ``make(in_window=True)`` parents the instance into a shown
    offscreen window — effective item visibility only resolves there (an
    unattached item tree reads visible=false everywhere). The stub is
    shared, so tests flip the mode flags through ``make.stub.setFlags``."""
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

    window = QQuickWindow()
    window.resize(1400, 820)
    window.show()
    created = []

    def make(in_window=False):
        obj = component.create()
        if obj is None:
            raise RuntimeError(
                "ContactQualityModal.qml failed to instantiate:\n"
                + "\n".join(e.toString() for e in component.errors())
            )
        if in_window:
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


@pytest.fixture(scope="module")
def cq_modal(cq_harness):
    """One shared, unparented instance for the signal-contract tests."""
    return cq_harness()


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


# ── Force Dismiss availability + look (#709) ────────────────────────────────

def _show_live_warning(modal):
    """Drive the modal through its public API into the live-scan
    poor-contact state BloodFlow produces on a mid-scan latch (reset(true)
    + addWarning), where the footer is up for every build variant."""
    QMetaObject.invokeMethod(modal, "reset", Q_ARG("QVariant", True))
    QMetaObject.invokeMethod(
        modal, "addWarning",
        Q_ARG("QVariant", "L1"), Q_ARG("QVariant", "contact"),
        Q_ARG("QVariant", "Poor contact"), Q_ARG("QVariant", 12.0))
    assert modal.property("state_") == "warnings"
    assert modal.property("visible") is True


@pytest.mark.parametrize(
    "clinical, engineering, expected",
    [
        (False, False, True),   # Research build: always offered (#709)
        (False, True, True),
        (True, False, False),   # plain clinical build: hidden, as before
        (True, True, True),     # clinical + engineering unlock
    ],
)
def test_force_dismiss_visibility_follows_build_and_engineering(
        cq_harness, clinical, engineering, expected):
    """#709: the button is gated ``!clinicalMode || engineeringMode`` (the
    #234 raw-CSV pattern) instead of engineering mode alone."""
    cq_harness.stub.setFlags(clinical, engineering)
    try:
        modal = cq_harness(in_window=True)
        _show_live_warning(modal)
        btn = modal.findChild(QObject, "cqForceDismissBtn")
        assert btn is not None
        assert btn.property("visible") is expected
        # The rest of the live footer is unaffected by the gate.
        stop = [b for b in _footer_buttons(modal)
                if b.property("text") == "Stop scan"]
        assert len(stop) == 1 and stop[0].property("visible") is True
        modal.setProperty("visible", False)
    finally:
        cq_harness.stub.setFlags(False, True)


def _visible_footer_order(modal):
    """Texts of the shown footer buttons, left to right as laid out."""
    _qt_app.processEvents()     # let the RowLayout place its children
    shown = [b for b in _footer_buttons(modal) if b.property("visible")]
    return [b.property("text") for b in sorted(shown, key=lambda b: b.property("x"))]


def test_live_footer_puts_continue_after_hide_for_this_scan(cq_harness):
    """Continue resumes the scan, so it sits last, at the right edge, with
    "Hide for this scan" before it (Ethan, 2026-10-07)."""
    cq_harness.stub.setFlags(False, False)     # Research, no engineering
    try:
        modal = cq_harness(in_window=True)
        _show_live_warning(modal)
        assert _visible_footer_order(modal) == [
            "Stop scan", "Hide for this scan", "Continue"]
        modal.setProperty("visible", False)
    finally:
        cq_harness.stub.setFlags(False, True)


def test_quick_check_footer_order_is_unchanged(cq_harness):
    """Outside a live scan there is no Continue; Hide stays last."""
    cq_harness.stub.setFlags(False, False)
    try:
        modal = cq_harness(in_window=True)
        QMetaObject.invokeMethod(modal, "reset", Q_ARG("QVariant", False))
        QMetaObject.invokeMethod(
            modal, "addWarning",
            Q_ARG("QVariant", "L1"), Q_ARG("QVariant", "contact"),
            Q_ARG("QVariant", "Poor contact"), Q_ARG("QVariant", 12.0))
        assert _visible_footer_order(modal) == [
            "Dismiss", "Retest", "Hide for this scan"]
        modal.setProperty("visible", False)
    finally:
        cq_harness.stub.setFlags(False, True)


def test_force_dismiss_reads_as_a_user_facing_control(cq_modal):
    """#709: plain-language label, plus help text that says how long the
    hide lasts — and stays true outside a scan, where the click is a
    one-shot dismiss."""
    btn = cq_modal.findChild(QObject, "cqForceDismissBtn")
    assert btn.property("text") == "Hide for this scan"
    help_text = btn.property("helpText")
    assert "If a scan is running" in help_text
    assert "The next scan warns again" in help_text


# ── No-data cameras: pre-scan no_signal and mid-scan dropouts (#731) ───────

HOVER_LINE = "Hover over orange cameras for details."
HOVER_LINE_RED = "Hover over orange or red cameras for details."
DISMISS_LINE = "All contact quality issues are currently inactive. You may dismiss."
RED = "#e74c3c"             # AppTheme.accentRed
ORANGE = "#e67e22"          # the operator's single "bad" orange
GREEN = "#a3e4a1"


def _add_warning(modal, camera, type_key, type_text, value):
    QMetaObject.invokeMethod(
        modal, "addWarning",
        Q_ARG("QVariant", camera), Q_ARG("QVariant", type_key),
        Q_ARG("QVariant", type_text), Q_ARG("QVariant", value))


def _show_check_result(modal, warnings):
    """The quick-check shape BloodFlow produces from
    contactQualityCheckFinished: reset(false), then one addWarning per
    entry the connector mapped."""
    QMetaObject.invokeMethod(modal, "reset", Q_ARG("QVariant", False))
    for w in warnings:
        _add_warning(modal, *w)
    assert modal.property("state_") == "warnings"


def _show_live_scan_warning(modal, warnings):
    """The live-scan shape: BloodFlow's onContactQualityWarning resets the
    modal for a live scan, then adds the latched warning."""
    QMetaObject.invokeMethod(modal, "reset", Q_ARG("QVariant", True))
    for w in warnings:
        _add_warning(modal, *w)
    assert modal.property("state_") == "warnings"


def _subtitle(modal):
    texts = [o.property("text") for o in modal.findChildren(QObject)
             if isinstance(o.property("text"), str)
             and ("Hover over" in o.property("text")
                  or DISMISS_LINE in o.property("text"))]
    assert len(texts) == 1, texts
    return texts[0]


def _tooltip(modal, side, cam_index1):
    return QMetaObject.invokeMethod(
        modal, "cameraTooltip", Qt.ConnectionType.DirectConnection,
        Q_RETURN_ARG("QVariant"),
        Q_ARG("QVariant", side), Q_ARG("QVariant", cam_index1))


def _dot(modal, side, cam_index1):
    dots = [o for o in modal.findChildren(QObject)
            if o.property("camIndex1") == cam_index1
            and o.property("side") == side]
    assert len(dots) == 1
    return dots[0]


def _dot_color(modal, side, cam_index1):
    return _dot(modal, side, cam_index1).property("singleColor").name()


NO_SIGNAL_L7 = ("L7", "no_signal", "No data from camera", 0.0)
POOR_CONTACT_R2 = ("R2", "poor_contact", "Poor sensor contact", 1.5)
POOR_CONTACT_L2 = ("L2", "poor_contact", "Poor sensor contact", 10.3)


def test_no_signal_adds_the_remedy_line_to_the_subtitle(cq_harness):
    """An operator never sees the per-type dot colours, so the remedy for a
    camera with no data is spelled out once, above the hover hint, and the
    hint names the red dot too."""
    cq_harness.stub.setFlags(True, False)      # plain clinical build
    try:
        modal = cq_harness(in_window=True)
        _show_check_result(modal, [NO_SIGNAL_L7, POOR_CONTACT_R2])
        assert modal.property("hasNoSignal") is True
        hint = modal.property("noSignalHint")
        assert hint and "contact" not in hint.lower()
        assert _subtitle(modal) == hint + "\n" + HOVER_LINE_RED
        modal.setProperty("visible", False)
    finally:
        cq_harness.stub.setFlags(False, True)


def test_poor_contact_alone_keeps_the_plain_subtitle(cq_harness):
    modal = cq_harness(in_window=True)
    _show_check_result(modal, [POOR_CONTACT_R2])
    assert modal.property("hasNoSignal") is False
    assert _subtitle(modal) == HOVER_LINE
    modal.setProperty("visible", False)


def test_no_signal_tooltip_shows_no_dn_reading(cq_harness):
    """Engineering mode appends the DN reading to each tooltip line; a
    camera with no data has none (its value is a 0.0 filler)."""
    modal = cq_harness(in_window=True)          # engineering mode on
    _show_check_result(modal, [NO_SIGNAL_L7, POOR_CONTACT_R2])
    assert _tooltip(modal, "left", 7) == "L7\nNo data from camera"
    assert _tooltip(modal, "right", 2) == "R2\nPoor sensor contact (1.5 DN)"
    modal.setProperty("visible", False)


def test_no_signal_dot_is_red_in_every_mode(cq_harness):
    """Red means "no frames from this camera" for operators and engineers
    alike (Ethan, 2026-10-26). Other warnings keep their colours: the
    operator's single orange, engineering's contact orange."""
    modal = cq_harness(in_window=True)          # engineering mode on
    _show_check_result(modal, [NO_SIGNAL_L7, POOR_CONTACT_R2])
    assert _dot_color(modal, "left", 7) == RED
    assert _dot_color(modal, "right", 2) == "#f4a460"    # accentOrangeContact
    cq_harness.stub.setFlags(True, False)
    try:
        assert _dot_color(modal, "left", 7) == RED
        assert _dot_color(modal, "right", 2) == ORANGE
    finally:
        cq_harness.stub.setFlags(False, True)
    modal.setProperty("visible", False)


def test_camera_without_frames_mid_scan_turns_red_and_back(cq_harness):
    """The 2026-10-26 bench case: the live modal is up for L2's poor
    contact while R8 never delivers a frame. The dropout watchdog's signal
    turns R8 red (it used to read as a green, good dot); recovery turns it
    back."""
    cq_harness.stub.setFlags(True, False)      # plain clinical build
    try:
        modal = cq_harness(in_window=True)
        _show_live_scan_warning(modal, [POOR_CONTACT_L2])
        assert _dot_color(modal, "right", 8) == GREEN
        assert _subtitle(modal) == HOVER_LINE

        cq_harness.stub.cameraDropoutDetected.emit("right", 7, "00:00:03")
        assert _dot(modal, "right", 8).property("status") == "nodata"
        assert _dot_color(modal, "right", 8) == RED
        assert _dot_color(modal, "left", 2) == ORANGE
        assert _tooltip(modal, "right", 8) == "R8\nNo data from camera"
        assert _subtitle(modal) == (
            modal.property("noSignalHint") + "\n" + HOVER_LINE_RED)

        cq_harness.stub.cameraDropoutRecovered.emit("right", 7, "00:00:20")
        assert _dot_color(modal, "right", 8) == GREEN
        assert _subtitle(modal) == HOVER_LINE
        modal.setProperty("visible", False)
    finally:
        cq_harness.stub.setFlags(False, True)


def test_no_data_mark_never_opens_or_gates_the_modal(cq_harness):
    """A dead camera stays a warning (#585): marking it must not pop the
    modal mid-scan or hold Continue, which a clinical operator could only
    escape by stopping the scan. The toast is the notice."""
    modal = cq_harness(in_window=True)
    assert modal.property("visible") is False
    cq_harness.stub.cameraDropoutDetected.emit("right", 7, "00:00:03")
    assert modal.property("visible") is False
    assert modal.property("anyCameraWithoutData") is True

    _show_live_scan_warning(modal, [POOR_CONTACT_L2])
    dismissable = modal.property("liveScanDismissable")
    cq_harness.stub.cameraDropoutDetected.emit("left", 0, "00:00:04")
    assert modal.property("liveScanDismissable") == dismissable
    entries = modal.property("entries").toVariant()   # a QML var array
    assert len(entries) == 1 and entries[0]["camera"] == "L2"
    modal.setProperty("visible", False)


def test_reset_keeps_and_clear_drops_cameras_without_data(cq_harness):
    """The live modal resets each time it reopens, which must not forget a
    camera the watchdog already reported dead in this scan. Only the scan
    boundary (BloodFlow's clearCamerasWithoutData) drops the marks."""
    modal = cq_harness(in_window=True)
    cq_harness.stub.cameraDropoutDetected.emit("right", 7, "00:00:03")
    _show_live_scan_warning(modal, [POOR_CONTACT_L2])
    assert _dot_color(modal, "right", 8) == RED
    QMetaObject.invokeMethod(modal, "clearCamerasWithoutData")
    assert modal.property("anyCameraWithoutData") is False
    assert _dot_color(modal, "right", 8) == GREEN
    modal.setProperty("visible", False)


def test_scan_boundaries_clear_cameras_without_data():
    """Both boundaries, so neither the next scan nor the next pre-scan
    check inherits a previous scan's red dot."""
    text = _bloodflow_text()
    begin = text.index("function beginScanNow")
    assert "contactQualityModal.clearCamerasWithoutData()" in text[begin:begin + 900]
    finished = text.index("onScanFinished:", text.index("id: scanRunner"))
    assert "contactQualityModal.clearCamerasWithoutData()" in text[finished:finished + 700]


def test_no_data_text_matches_the_connector_wording(cq_modal):
    """The red dot's tooltip and the pre-scan no_signal warning say the
    same thing; the text lives in two places, so pin them together."""
    from motion_connector import MotionConnector

    assert cq_modal.property("noDataText") == MotionConnector._warning_text("no_signal")
