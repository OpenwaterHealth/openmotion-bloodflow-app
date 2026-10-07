"""Unit tests: a toast in components/NotificationCenter.qml is opaque to input.

What this exercises
-------------------
The toast card must swallow the pointer input that lands on it. Before
issue #661 its body carried only a passive ``HoverHandler``, so clicks,
wheel and hover went straight through to whatever sat underneath: in
History, "Export CSV" and "Load in viewer" stayed clickable under the
"Exported to <path>" toast, so a user could start a second export or load
a scan through a notification they were still reading.

Pinned here, with real events routed by Qt's scene graph into a window
that has a counting MouseArea standing in for the controls behind:

* a click, a wheel notch and hover moves on the toast body never reach it;
* the empty part of the overlay (it fills the window) still passes input
  through, so the toast layer doesn't freeze the rest of the UI;
* the close button still dismisses, and hovering still pauses the
  auto-dismiss timer.

Marker
------
``pytest.mark.unit`` — no app launch, no hardware, offscreen Qt platform
(same harness pattern as test_notification_center_stack.py).

Run with:  pytest tests/test_notification_center_input.py -v
"""

import sys
import time

# A QGuiApplication must exist before any QML Quick item is created; see
# test_notification_center_stack.py for why it is built at import time.
from PyQt6.QtCore import (  # noqa: E402
    Q_ARG,
    Q_RETURN_ARG,
    QCoreApplication,
    QEvent,
    QMetaObject,
    QPoint,
    QPointF,
    Qt,
    QUrl,
)
from PyQt6.QtGui import QGuiApplication, QMouseEvent, QWheelEvent  # noqa: E402

if QCoreApplication.instance() is None:
    _qt_app = QGuiApplication([sys.argv[0], "-platform", "offscreen"])
else:
    _qt_app = QCoreApplication.instance()

import pytest  # noqa: E402
from PyQt6.QtQml import (  # noqa: E402
    QQmlComponent,
    qmlRegisterSingletonInstance,
    qmlRegisterSingletonType,
)
from PyQt6.QtQuick import QQuickView  # noqa: E402

from test_notification_center_stack import (  # noqa: E402
    APP_THEME_QML,
    NOTIFICATION_CENTER_QML,
    _basic_controls_style,
    _StubMotionInterface,
)

pytestmark = pytest.mark.unit

# Hosts the real NotificationCenter over a stand-in for the controls behind
# it. The base URL is set to components/, so NotificationCenter resolves as
# a same-directory type with no import line.
_DRIVER_QML = b"""
import QtQuick

Item {
    width: 800; height: 600

    property int behindPresses: 0
    property int behindWheels: 0
    property int behindHovers: 0
    property alias center: nc

    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        onPressed: parent.behindPresses += 1
        onWheel: function(wheel) { parent.behindWheels += 1; wheel.accepted = true }
        onPositionChanged: function(mouse) { if (!pressed) parent.behindHovers += 1 }
    }

    NotificationCenter { id: nc; anchors.fill: parent; z: 99999 }
}
"""

TOAST_WIDTH = 340        # the delegate wrapper's fixed width
ENTER_SETTLE_MS = 300    # enter slide is 180 ms
EXIT_SETTLE_MS = 400     # exit slide is 160 ms


def _pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        _qt_app.processEvents()
        time.sleep(0.005)


@pytest.fixture()
def harness():
    stub = _StubMotionInterface()
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    qmlRegisterSingletonType(
        QUrl.fromLocalFile(str(APP_THEME_QML)), "OpenMotion", 1, 0,
        "AppTheme",
    )
    view = QQuickView()
    view.resize(800, 600)
    with _basic_controls_style():
        component = QQmlComponent(view.engine())
        component.setData(
            _DRIVER_QML,
            QUrl.fromLocalFile(
                str(NOTIFICATION_CENTER_QML.parent / "_input_driver.qml")
            ),
        )
        root = component.create() if not component.isError() else None
    if root is None:
        raise RuntimeError(
            "driver failed to load:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    view.setContent(QUrl(), component, root)
    view.show()
    _pump(100)

    yield view, root

    view.close()
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    del stub


def _center(root):
    return root.property("center")


def _notify(root, **request):
    QMetaObject.invokeMethod(
        _center(root), "notify", Qt.ConnectionType.DirectConnection,
        Q_RETURN_ARG("QVariant"), Q_ARG("QVariant", request),
    )
    _pump(ENTER_SETTLE_MS)


def _stack_len(root):
    result = QMetaObject.invokeMethod(
        _center(root), "stackSnapshot", Qt.ConnectionType.DirectConnection,
        Q_RETURN_ARG("QVariant"),
    )
    result = result.toVariant() if hasattr(result, "toVariant") else result
    return len(result)


def _toast_rect(root):
    """Scene rect (x, y, w, h) of the only visible toast card. Positions are
    summed up the parent chain rather than mapped (mapToItem from Python is
    unreliable on the offscreen platform)."""
    found = []

    def walk(item):
        # The Column and each delegate wrapper are 340 wide too; the card
        # is the Rectangle (the only one of the three with a radius).
        for child in item.childItems():
            if (child.width() == TOAST_WIDTH and child.height() > 0
                    and child.property("radius") is not None):
                found.append(child)
            walk(child)

    walk(_center(root))
    assert len(found) == 1, f"expected one toast, found {len(found)}"
    item = found[0]
    w, h = item.width(), item.height()
    x = y = 0.0
    while item is not None:
        x += item.x()
        y += item.y()
        item = item.parentItem()
    return x, y, w, h


def _body_point(root):
    """A point on the toast's message text, well clear of the close button."""
    x, y, w, h = _toast_rect(root)
    return QPoint(int(x + w * 0.4), int(y + h / 2))


def _close_point(root):
    # Close glyph: 20x20, anchored top-right inside 12 px margins.
    x, y, w, h = _toast_rect(root)
    return QPoint(int(x + w - 12 - 10), int(y + 12 + 10))


def _send_mouse(view, kind, pos, button=Qt.MouseButton.NoButton,
                buttons=Qt.MouseButton.NoButton):
    glob = QPointF(view.mapToGlobal(pos))
    QCoreApplication.sendEvent(view, QMouseEvent(
        kind, QPointF(pos), glob, button, buttons,
        Qt.KeyboardModifier.NoModifier,
    ))
    _qt_app.processEvents()


def _click(view, pos):
    left = Qt.MouseButton.LeftButton
    _send_mouse(view, QEvent.Type.MouseMove, pos)
    _send_mouse(view, QEvent.Type.MouseButtonPress, pos, left, left)
    _send_mouse(view, QEvent.Type.MouseButtonRelease, pos, left)


def _wheel(view, pos):
    glob = QPointF(view.mapToGlobal(pos))
    QCoreApplication.sendEvent(view, QWheelEvent(
        QPointF(pos), glob, QPoint(0, 0), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    ))
    _qt_app.processEvents()


def _hover_moves(view, pos, steps=(-20, -10, 10, 20)):
    for dx in steps:
        _send_mouse(view, QEvent.Type.MouseMove, QPoint(pos.x() + dx, pos.y()))


EXPORT_TOAST = dict(text="Exported to C:\\data\\scan_0001.csv",
                    type="success", durationMs=0)


def test_click_on_toast_does_not_reach_controls_behind(harness):
    """The #661 report: a click on the toast body must not fire the
    History button it covers. It doesn't dismiss the toast either."""
    view, root = harness
    _notify(root, **EXPORT_TOAST)

    _click(view, _body_point(root))

    assert root.property("behindPresses") == 0
    assert _stack_len(root) == 1


def test_wheel_and_hover_on_toast_do_not_reach_controls_behind(harness):
    view, root = harness
    _notify(root, **EXPORT_TOAST)
    body = _body_point(root)

    _wheel(view, body)
    _hover_moves(view, body)

    assert root.property("behindWheels") == 0
    assert root.property("behindHovers") == 0


def test_space_around_the_toast_stays_interactive(harness):
    """The overlay fills the window: only the card may block, never the
    transparent rest of it."""
    view, root = harness
    _notify(root, **EXPORT_TOAST)
    x, y, w, h = _toast_rect(root)
    beside = QPoint(int(x - 40), int(y + h / 2))   # left of the card
    above = QPoint(int(x + w / 2), int(y - 40))    # above the card

    _click(view, beside)
    _click(view, above)
    _wheel(view, beside)

    assert root.property("behindPresses") == 2
    assert root.property("behindWheels") == 1


def test_close_button_still_dismisses(harness):
    view, root = harness
    _notify(root, **EXPORT_TOAST)

    _click(view, _close_point(root))
    _pump(EXIT_SETTLE_MS)

    assert _stack_len(root) == 0
    assert root.property("behindPresses") == 0


def test_hover_still_pauses_auto_dismiss(harness):
    view, root = harness
    _notify(root, text="Exported.", type="success", durationMs=400)
    body = _body_point(root)

    _hover_moves(view, body)
    _pump(900)                       # well past the 400 ms lifetime
    assert _stack_len(root) == 1, "hover did not pause the auto-dismiss"

    _hover_moves(view, QPoint(40, 40), steps=(0, 5))   # leave the card
    _pump(400 + EXIT_SETTLE_MS + 200)
    assert _stack_len(root) == 0, "timer did not resume after hover left"
