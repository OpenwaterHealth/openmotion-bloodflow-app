"""Unit tests for the toast stack order in components/NotificationCenter.qml.

What this exercises
-------------------
Re-firing a *tagged* notification while its previous card is still visible
must REPLACE the card at its current stack position — not remove it there
and append the new one at the bottom. The old behavior made cards leapfrog
each other whenever the same tags re-fired in quick succession: during
rapid system power cycles the three ``disconnect_*`` tags (6 s cards)
re-fire while the previous set is still on screen, and the whole stack
appeared to shuffle (issue #489).

Also pinned: untagged/new-tag notifications still append at the bottom, the
oldest-first cap eviction still works, and the Python signal bridge
(``notificationRequested``) still lands payloads in the stack.

Marker
------
``pytest.mark.unit`` — no app launch, no hardware, offscreen Qt platform
(same harness pattern as test_critical_error_modal_footer.py).

Run with:  pytest tests/test_notification_center_stack.py -v
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

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTIFICATION_CENTER_QML = REPO_ROOT / "components" / "NotificationCenter.qml"
APP_THEME_QML = REPO_ROOT / "components" / "AppTheme.qml"


class _StubMotionInterface(QObject):
    """Minimal MotionInterface stand-in covering every member
    NotificationCenter.qml (and AppTheme.qml) references."""

    notificationRequested = pyqtSignal("QVariant")
    notificationDismissByIdRequested = pyqtSignal(int)
    notificationDismissByTagRequested = pyqtSignal(str)
    notificationDismissAllRequested = pyqtSignal()
    _neverEmitted = pyqtSignal()

    @pyqtProperty("QVariantMap", notify=_neverEmitted)
    def appConfig(self):
        return {"darkMode": True}


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


@pytest.fixture()
def center():
    """Compile and instantiate a fresh NotificationCenter per test."""
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0,
        "AppTheme",
    )
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(NOTIFICATION_CENTER_QML))
        )
    if component.isError():
        raise RuntimeError(
            "NotificationCenter.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    obj = component.create()
    if obj is None:
        raise RuntimeError(
            "NotificationCenter.qml failed to instantiate:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    obj.stub = stub  # keep the singleton alive alongside the item

    yield obj

    obj.deleteLater()
    del engine
    del stub


def _notify(center_obj, **request):
    QMetaObject.invokeMethod(
        center_obj, "notify", Qt.ConnectionType.DirectConnection,
        Q_RETURN_ARG("QVariant"), Q_ARG("QVariant", request),
    )


def _stack(center_obj):
    """[{id, tag, text, type}] top→bottom via the QML snapshot helper."""
    result = QMetaObject.invokeMethod(
        center_obj, "stackSnapshot", Qt.ConnectionType.DirectConnection,
        Q_RETURN_ARG("QVariant"),
    )
    # A QML js function hands its return value back as a QJSValue.
    return result.toVariant() if hasattr(result, "toVariant") else result


def _tags(center_obj):
    return [e["tag"] for e in _stack(center_obj)]


def test_tagged_refire_keeps_stack_position(center):
    """The issue-#489 shuffle: re-firing an existing tag must refresh the
    card in place, not move it to the bottom of the stack."""
    _notify(center, text="Console disconnected", tag="disconnect_console")
    _notify(center, text="Left sensor disconnected", tag="disconnect_left")
    _notify(center, text="Right sensor disconnected", tag="disconnect_right")
    assert _tags(center) == [
        "disconnect_console", "disconnect_left", "disconnect_right",
    ]
    first_ids = {e["tag"]: e["id"] for e in _stack(center)}

    # Next power cycle: the same tags re-fire (in a different detection
    # order, as USB enumeration order varies) while the first set is still
    # visible.
    _notify(center, text="Left sensor disconnected", tag="disconnect_left")
    _notify(center, text="Console disconnected", tag="disconnect_console")

    # Order unchanged — no leapfrogging.
    assert _tags(center) == [
        "disconnect_console", "disconnect_left", "disconnect_right",
    ]
    # But the re-fired entries are FRESH (new ids → new delegates, so the
    # auto-dismiss clock restarts).
    refreshed = {e["tag"]: e["id"] for e in _stack(center)}
    assert refreshed["disconnect_console"] != first_ids["disconnect_console"]
    assert refreshed["disconnect_left"] != first_ids["disconnect_left"]
    assert refreshed["disconnect_right"] == first_ids["disconnect_right"]


def test_new_tags_and_untagged_append_at_bottom(center):
    _notify(center, text="a", tag="one")
    _notify(center, text="b")            # untagged
    _notify(center, text="c", tag="two")
    assert [e["text"] for e in _stack(center)] == ["a", "b", "c"]


def test_replacement_updates_text_and_type_in_place(center):
    """The progress→result pattern (e.g. debug-bundle 'Preparing…' →
    'saved') now updates where the card already is."""
    _notify(center, text="Preparing debug logs…", type="info",
            tag="debug-bundle")
    _notify(center, text="anchor below", tag="other")
    _notify(center, text="Debug logs saved.", type="success",
            tag="debug-bundle")

    stack = _stack(center)
    assert [e["tag"] for e in stack] == ["debug-bundle", "other"]
    assert stack[0]["text"] == "Debug logs saved."
    assert stack[0]["type"] == "success"


def test_cap_evicts_oldest_first(center):
    for i in range(7):
        _notify(center, text=f"t{i}", tag=f"tag{i}")
    max_visible = center.property("maxVisible")
    assert max_visible == 5
    assert [e["text"] for e in _stack(center)] == [
        "t2", "t3", "t4", "t5", "t6",
    ]


def test_python_bridge_lands_payloads_in_the_stack(center):
    """The Connections handler on notificationRequested still feeds
    notify() — the path the connector's notify slot uses."""
    center.stub.notificationRequested.emit({
        "id": 42, "tag": "disconnect_console",
        "text": "Console disconnected", "type": "warning",
        "durationMs": 6000, "dismissible": True,
    })
    stack = _stack(center)
    assert len(stack) == 1
    assert stack[0]["id"] == 42
    assert stack[0]["tag"] == "disconnect_console"
