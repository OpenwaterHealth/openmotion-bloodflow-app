"""Unit tests for the audit-log filter bar in components/LogsModal.qml
(#226).

Loads the real LogsModal.qml in a bare QQmlEngine with a stubbed
MotionInterface singleton, then drives the filter controls the way the
UI does and asserts the filter map handed to
``filteredAuditLogEntries`` — the Python side of that slot is covered by
tests/test_audit_connector.py; these tests pin the QML → slot contract.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import contextlib
import os
import sys
from pathlib import Path

# A QGuiApplication must exist before any QML Quick item is created, and
# it must be constructed before conftest's session-scoped autouse
# fixture instantiates a bare QCoreApplication (a QGuiApplication cannot
# be created once a plain QCoreApplication exists). Module import runs
# at collection time — ahead of every fixture — so create it here. The
# offscreen platform is passed via argv, NOT via QT_QPA_PLATFORM, so the
# process environment is untouched (see test_plot_viewer_masks.py).
from PyQt6.QtCore import (  # noqa: E402
    QCoreApplication,
    QMetaObject,
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
LOGS_MODAL_QML = REPO_ROOT / "components" / "LogsModal.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"

_ROWS = [
    {"id": 2, "ts_epoch": 2.0, "ts_iso": "2026-06-15T09:00:02-07:00",
     "event_type": "scan_started", "details": '{"label":"owABC"}'},
    {"id": 1, "ts_epoch": 1.0, "ts_iso": "2026-06-15T09:00:01-07:00",
     "event_type": "system_startup", "details": "{}"},
]


class _StubMotionInterface(QObject):
    """Minimal MotionInterface stand-in covering every property/slot
    LogsModal.qml (and AppTheme.qml) actually reference."""

    directoryChanged = pyqtSignal()
    _neverEmitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.filter_calls = []          # every map handed to the slot
        self.viewed_count = 0
        self.event_types = ["scan_started", "system_startup"]

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        return {"darkMode": True}

    @pyqtProperty(str, notify=directoryChanged)
    def directory(self):
        return ""

    @pyqtSlot()
    def recordAuditLogViewed(self):
        self.viewed_count += 1

    @pyqtSlot(result="QVariantList")
    def auditEventTypes(self):
        return list(self.event_types)

    @pyqtSlot("QVariantMap", result="QVariantList")
    def filteredAuditLogEntries(self, filters):
        self.filter_calls.append(dict(filters))
        return list(_ROWS)


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
def modal_factory():
    """Compile components/LogsModal.qml once; hand out fresh instances."""
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0,
        "AppTheme",
    )
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(LOGS_MODAL_QML))
        )
    if component.isError():
        raise RuntimeError(
            "LogsModal.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors())
        )

    created = []

    def make():
        obj = component.create()
        if obj is None:
            raise RuntimeError(
                "LogsModal.qml failed to instantiate:\n"
                + "\n".join(e.toString() for e in component.errors())
            )
        created.append(obj)
        stub.filter_calls.clear()
        return obj

    make.stub = stub

    yield make

    for obj in created:
        obj.deleteLater()
    del engine
    del stub


def _call(obj, name):
    """Invoke a parameterless JS function declared on the modal root.
    PyQt6's invokeMethod returns None for void QML functions, so
    success is asserted by the callers' downstream checks."""
    QMetaObject.invokeMethod(obj, name)


def _child(obj, object_name):
    c = obj.findChild(QObject, object_name)
    assert c is not None, f"child {object_name!r} not found"
    return c


def test_open_records_view_and_loads_filtered_entries(modal_factory):
    modal = modal_factory()
    stub = modal_factory.stub
    before = stub.viewed_count
    _call(modal, "open")
    assert stub.viewed_count == before + 1
    # open() loads via the filtered slot with no filters active.
    assert stub.filter_calls
    f = stub.filter_calls[-1]
    assert f["eventType"] == ""
    assert f["dateFrom"] == "" and f["dateTo"] == ""
    assert f["text"] == ""
    assert f["limit"] == 500
    assert len(modal.property("entries")) == len(_ROWS)
    assert modal.property("filtersActive") is False
    # Event types for the dropdown come from the connector.
    assert list(modal.property("eventTypes")) == stub.event_types


def test_event_type_filter_is_passed_to_slot(modal_factory):
    modal = modal_factory()
    stub = modal_factory.stub
    modal.setProperty("filterEventType", "scan_started")
    _call(modal, "refresh")
    assert stub.filter_calls[-1]["eventType"] == "scan_started"
    assert modal.property("filtersActive") is True


def test_date_and_text_fields_are_passed_to_slot(modal_factory):
    modal = modal_factory()
    stub = modal_factory.stub
    _child(modal, "auditFilterFrom").setProperty("text", "2026-06-01")
    _child(modal, "auditFilterTo").setProperty("text", "2026-06-15")
    _child(modal, "auditFilterSearch").setProperty("text", "owABC")
    _call(modal, "refresh")
    f = stub.filter_calls[-1]
    assert f["dateFrom"] == "2026-06-01"
    assert f["dateTo"] == "2026-06-15"
    assert f["text"] == "owABC"
    assert modal.property("filtersActive") is True


def test_date_field_validity_flag(modal_factory):
    modal = modal_factory()
    from_field = _child(modal, "auditFilterFrom")
    from_field.setProperty("text", "garbage")
    assert from_field.property("validDate") is False
    from_field.setProperty("text", "2026-06-15")
    assert from_field.property("validDate") is True
    from_field.setProperty("text", "")
    assert from_field.property("validDate") is True


def test_clear_filters_resets_everything_and_refreshes(modal_factory):
    modal = modal_factory()
    stub = modal_factory.stub
    modal.setProperty("filterEventType", "scan_started")
    _child(modal, "auditFilterFrom").setProperty("text", "2026-06-01")
    _child(modal, "auditFilterSearch").setProperty("text", "x")
    _call(modal, "clearFilters")
    f = stub.filter_calls[-1]
    assert f["eventType"] == ""
    assert f["dateFrom"] == "" and f["dateTo"] == ""
    assert f["text"] == ""
    assert modal.property("filtersActive") is False
    assert modal.property("filterEventType") == ""
