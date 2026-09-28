"""Settings dropdowns close when the wheel turns outside them (#478).

Loads the real components/SettingsModal.qml offscreen in a shown
QQuickWindow (a Popup only opens inside a window) with the stubbed
``MotionInterface`` from test_settings_modal_mode_gates, opens a
dropdown, sends a mouse-wheel event over the Settings page outside the
open list, and asserts that:

- the dropdown closed (Popup.closePolicy only reacts to presses and
  releases outside it, never to the wheel, so before the fix it stayed
  open), and
- the same wheel event still scrolled the Settings page.

A wheel over the open list itself must keep scrolling the list, not
close it.

Unit-marked: no app launch, no hardware, offscreen Qt platform.
"""

import time

import pytest
from PyQt6.QtCore import (
    QCoreApplication, QEvent, QObject, QPoint, QPointF, Qt, QUrl)
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from PyQt6.QtQml import QQmlComponent, QQmlEngine, qmlRegisterSingletonInstance
from PyQt6.QtQuick import QQuickWindow

# Importing the mode-gates harness creates the offscreen QGuiApplication
# ahead of conftest's QCoreApplication (see that module's header).
from test_settings_modal_mode_gates import (  # noqa: E402
    SETTINGS_MODAL_QML,
    _basic_controls_style,
    _find_item,
    _invoke,
    _StubMotionInterface,
)

pytestmark = pytest.mark.unit


def _pump(ms=150):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def _scene_y(item):
    """Item's y in scene coordinates. Summed up the parent chain — the
    mapToItem family crashes natively from Python on offscreen."""
    y = 0.0
    while item is not None:
        y += item.y()
        item = item.parentItem()
    return y


def _scene_x(item):
    x = 0.0
    while item is not None:
        x += item.x()
        item = item.parentItem()
    return x


def _flickable_of(item):
    """The Settings ScrollView's Flickable: the combo's nearest
    Flickable ancestor."""
    item = item.parentItem()
    while item is not None:
        if item.inherits("QQuickFlickable"):
            return item
        item = item.parentItem()
    raise AssertionError("combo is not inside a Flickable")


def _click(window, x, y):
    pos = QPointF(x, y)
    for typ, buttons in (
        (QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton),
    ):
        ev = QMouseEvent(typ, pos, pos, pos, Qt.MouseButton.LeftButton,
                         buttons, Qt.KeyboardModifier.NoModifier)
        QCoreApplication.sendEvent(window, ev)
        _pump()


def _popup_of(combo):
    """The combo's Popup. ``combo.property("popup")`` hands PyQt a bare
    voidptr, so find it among the combo's QObject children instead."""
    for child in combo.findChildren(QObject):
        if child.inherits("QQuickPopup"):
            return child
    raise AssertionError("combo has no Popup child")


def _wheel(window, x, y, dy=-120):
    pos = QPointF(x, y)
    ev = QWheelEvent(
        pos, pos, QPoint(0, 0), QPoint(0, dy),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QCoreApplication.sendEvent(window, ev)
    _pump()


@pytest.fixture(scope="module")
def modal():
    stub = _StubMotionInterface()
    stub.setFlags(clinical=False, engineering=True)
    stub.setAltCameraEnabled(True)
    qmlRegisterSingletonInstance("OpenMotion", 1, 0, "MotionInterface", stub)
    engine = QQmlEngine()
    with _basic_controls_style():
        component = QQmlComponent(
            engine, QUrl.fromLocalFile(str(SETTINGS_MODAL_QML))
        )
    if component.isError():
        raise RuntimeError(
            "SettingsModal.qml failed to compile:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    window = QQuickWindow()
    window.resize(1200, 800)
    window.show()
    obj = component.create()
    if obj is None:
        raise RuntimeError(
            "SettingsModal.qml failed to instantiate:\n"
            + "\n".join(e.toString() for e in component.errors())
        )
    obj.setParentItem(window.contentItem())
    obj.window_ = window
    _invoke(obj, "open")
    _pump()
    yield obj
    _invoke(obj, "close")
    obj.setParentItem(None)
    obj.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    window.close()
    del engine


def _open_combo_in_view(modal, object_name):
    """Scroll the Settings page so the combo sits near the top of the
    viewport, then open its dropdown."""
    combo = _find_item(modal, object_name)
    assert combo is not None, f"No combo named {object_name!r}"
    flick = _flickable_of(combo)
    content = flick.property("contentItem")
    y_in_content = 0.0
    item = combo
    while item is not None and item != content:
        y_in_content += item.y()
        item = item.parentItem()
    flick.setProperty("contentY", max(0.0, y_in_content - 60))
    _pump()
    # Open it the way a user does — a click. ComboBox.popup is a deferred
    # property: the Popup object does not exist until first needed.
    _click(modal.window_,
           _scene_x(combo) + combo.width() / 2,
           _scene_y(combo) + combo.height() / 2)
    popup = _popup_of(combo)
    assert popup.property("visible") is True, "dropdown did not open"
    return combo, popup, flick


@pytest.mark.parametrize(
    "object_name", ["altCameraExposureCombo", "altCameraGainCombo1"])
def test_wheel_outside_closes_dropdown_and_scrolls_page(modal, object_name):
    combo, popup, flick = _open_combo_in_view(modal, object_name)
    y_before = flick.property("contentY")

    # Over the Settings page, left of the combo row (the card margin),
    # a little below the combo — outside both the combo and its list.
    x = _scene_x(flick) + 8
    y = _scene_y(combo) + combo.height() + 20
    _wheel(modal.window_, x, y, dy=-120)

    assert popup.property("visible") is False, \
        "dropdown stayed open after a wheel outside it (#478)"
    assert flick.property("contentY") > y_before, \
        "the wheel did not scroll the Settings page"


def test_wheel_over_the_backdrop_closes_dropdown(modal):
    """Outside the Settings card entirely (the dimmed backdrop): the
    dropdown closes and the backdrop still swallows the wheel (#214)."""
    combo, popup, flick = _open_combo_in_view(modal, "altCameraExposureCombo")
    y_before = flick.property("contentY")

    _wheel(modal.window_, 20, 400, dy=-120)   # left of the card

    assert popup.property("visible") is False
    assert flick.property("contentY") == y_before


def test_wheel_over_the_open_list_keeps_it_open(modal):
    combo, popup, flick = _open_combo_in_view(modal, "altCameraExposureCombo")
    list_view = popup.property("contentItem")
    y_before = list_view.property("contentY")

    x = _scene_x(combo) + combo.width() / 2
    y = _scene_y(combo) + combo.height() + 60   # inside the 320 px list
    _wheel(modal.window_, x, y, dy=-120)

    assert popup.property("visible") is True
    assert list_view.property("contentY") > y_before, \
        "the wheel over the list did not scroll the list"
    _invoke(popup, "close")
    _pump()
