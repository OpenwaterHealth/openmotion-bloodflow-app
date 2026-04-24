import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  NotificationCenter — bottom-right toast stack.
 *
 *  Mount once in main.qml:
 *      NotificationCenter { id: notificationCenter; anchors.fill: parent; z: 99999 }
 *
 *  Fire a notification from QML:
 *      MOTIONInterface.notify("Note saved.", "success", 4000, true)
 *  Or from Python:
 *      self.notify("Scan complete.", "success")
 */
Item {
    id: root

    // Visible only via its toasts; root itself is transparent and doesn't
    // capture mouse events on empty space.

    AppTheme { id: theme }

    FontLoader {
        id: iconFont
        source: "../assets/fonts/keenicons-outline.ttf"
    }

    // Maximum number of toasts visible at once. When exceeded, the oldest
    // (model index 0) is removed.
    readonly property int maxVisible: 5

    // Monotonically increasing id assigned to each notification.
    property int _nextId: 1

    function _accentColor(type) {
        switch (type) {
            case "success": return theme.accentGreen
            case "warning": return theme.accentYellow
            case "error":   return theme.accentRed
            case "info":
            default:        return theme.accentBlue
        }
    }

    function _glyph(type) {
        switch (type) {
            case "success": return "\ue99c"  // check-circle
            case "warning": return "\uea82"  // notification-bing
            case "error":   return "\ue9b2"  // cross-circle
            case "info":
            default:        return "\uea43"  // information-2
        }
    }

    ListModel { id: model_ }

    /* ── public API ────────────────────────────────────────────────── */

    // Append a notification. `request` is a JS object with at least a `text`
    // field; other fields fall back to defaults defined here.
    //
    // If `request.tag` is a non-empty string and an existing notification has
    // the same tag, the existing one is removed first (instant) so the new
    // one slides in at the bottom — preventing duplicate "Connecting..." style
    // toasts from stacking.
    //
    // If `request.id` is provided (the Python `notify` slot supplies one so
    // it can return the assigned id to its caller), it's used; otherwise an
    // id is generated locally so QML-only callers still get uniqueness.
    function notify(request) {
        var tag = request.tag || ""
        if (tag !== "") {
            for (var i = 0; i < model_.count; ++i) {
                if (model_.get(i).tag === tag) {
                    model_.remove(i)
                    break
                }
            }
        }
        var nid = (request.id !== undefined) ? request.id : (root._nextId++)
        var entry = {
            id: nid,
            tag: tag,
            text: request.text || "",
            type: request.type || "info",
            durationMs: (request.durationMs !== undefined) ? request.durationMs : 4000,
            dismissible: (request.dismissible !== undefined) ? request.dismissible : true
        }
        model_.append(entry)
        // Cap-eviction is intentionally NOT animated — when the user fires
        // many toasts quickly, the oldest just disappears. Animating it would
        // visually compete with the new toast sliding in at the bottom.
        while (model_.count > root.maxVisible) {
            model_.remove(0)
        }
        return nid
    }

    // Internal: direct model removal, no animation. Called by the wrapper's
    // exit-animation `onStopped` to finalize a dismiss after the slide-out
    // completes. Public callers should use `dismiss()` instead.
    function _removeById(id) {
        for (var i = 0; i < model_.count; ++i) {
            if (model_.get(i).id === id) {
                model_.remove(i)
                return
            }
        }
    }

    // Public: dismiss a notification by id, animated. Safe with unknown ids.
    function dismiss(id) {
        for (var i = 0; i < model_.count; ++i) {
            if (model_.get(i).id === id) {
                _animateAtIndex(i)
                return
            }
        }
    }

    // Public: dismiss every notification with the given tag, animated.
    function dismissByTag(tag) {
        if (!tag) return
        // Iterate from the end so we can dismiss multiple matches (rare —
        // tags are typically unique by convention, but the loop is cheap).
        for (var i = model_.count - 1; i >= 0; --i) {
            if (model_.get(i).tag === tag) {
                _animateAtIndex(i)
            }
        }
    }

    // Public: dismiss every active notification, animated.
    function dismissAll() {
        for (var i = model_.count - 1; i >= 0; --i) {
            _animateAtIndex(i)
        }
    }

    // Internal helper. Looks up the wrapper Item at `index` and triggers its
    // exit animation. Falls back to direct model removal if the delegate
    // hasn't been instantiated yet (shouldn't happen in practice).
    function _animateAtIndex(index) {
        var w = repeater_.itemAt(index)
        if (w && w.dismissAnimated) {
            w.dismissAnimated()
        } else if (index >= 0 && index < model_.count) {
            model_.remove(index)
        }
    }

    /* ── Python bridge ─────────────────────────────────────────────── */

    Connections {
        target: MOTIONInterface
        function onNotificationRequested(payload) {
            root.notify(payload)
        }
        function onNotificationDismissByIdRequested(id) {
            root.dismiss(id)
        }
        function onNotificationDismissByTagRequested(tag) {
            root.dismissByTag(tag)
        }
        function onNotificationDismissAllRequested() {
            root.dismissAll()
        }
    }

    /* ── visual stack (placeholder delegate; replaced in Task 3) ──── */

    Column {
        id: stack
        spacing: 10
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.rightMargin: 24
        anchors.bottomMargin: 24

        add: Transition {
            // The wrapper's internal enter animation handles fade/slide; we only
            // animate the y-position here so existing toasts shift up smoothly.
            NumberAnimation { properties: "y"; duration: 180; easing.type: Easing.OutCubic }
        }
        move: Transition {
            NumberAnimation { properties: "y"; duration: 180; easing.type: Easing.OutCubic }
        }
        // (No `populate` transition — we don't want the entire stack to animate
        // on initial load; only newly-added toasts should slide in.)

        Repeater {
            id: repeater_
            model: model_
            delegate: Item {
                id: wrapper
                width: 340
                height: toast.height
                opacity: 0

                // Slide is driven by a Translate transform rather than the wrapper's
                // own `x` property, because Column's positioner can interrupt
                // animations bound to a child's `x`/`y` (e.g. when the 5-toast cap
                // removes an item mid-animation, leaving the slide stranded at 60).
                // Transforms are pure visual offsets — the positioner never touches
                // them — so the slide always completes cleanly.
                transform: Translate { id: slide; x: 60 }

                // Enter animation runs on completion.
                Component.onCompleted: enterAnim.start()

                // Auto-dismiss timer. Disabled when durationMs == 0 (sticky).
                Timer {
                    id: autoDismiss
                    interval: model.durationMs
                    repeat: false
                    running: model.durationMs > 0
                    onTriggered: wrapper.dismissAnimated()
                }

                ParallelAnimation {
                    id: enterAnim
                    NumberAnimation { target: slide;   property: "x";       to: 0; duration: 180; easing.type: Easing.OutCubic }
                    NumberAnimation { target: wrapper; property: "opacity"; to: 1; duration: 180; easing.type: Easing.OutCubic }
                }

                // Exit: triggered by `dismissAnimated()` (called by timer, the
                // close click, or a programmatic dismiss). When the parallel
                // animation finishes, remove the model entry directly so the
                // Column's `move` transition can collapse the empty slot. We
                // call `_removeById` (not `dismiss`) here to avoid re-entering
                // the animated path — the animation already played.
                function dismissAnimated() {
                    if (exitAnim.running) return  // already dismissing
                    exitAnim.start()
                }

                ParallelAnimation {
                    id: exitAnim
                    NumberAnimation { target: slide;   property: "x";       to: 60; duration: 160; easing.type: Easing.InCubic }
                    NumberAnimation { target: wrapper; property: "opacity"; to: 0;  duration: 160; easing.type: Easing.InCubic }
                    onStopped: root._removeById(model.id)
                }

                Rectangle {
                    id: toast
                    anchors.left: parent.left
                    anchors.right: parent.right
                    implicitHeight: contentRow.implicitHeight + 24
                    height: implicitHeight
                    radius: 10
                    color: theme.bgElevated
                    border.color: theme.borderSubtle
                    border.width: 1

                    // Hovering anywhere on the toast pauses the auto-dismiss timer.
                    HoverHandler {
                        id: toastHover
                        onHoveredChanged: {
                            if (hovered) {
                                autoDismiss.stop()
                            } else if (model.durationMs > 0) {
                                autoDismiss.restart()
                            }
                        }
                    }

                    // Left accent stripe
                    Rectangle {
                        width: 3
                        height: parent.height - 8
                        radius: 1.5
                        color: root._accentColor(model.type)
                        anchors.left: parent.left
                        anchors.leftMargin: 4
                        anchors.verticalCenter: parent.verticalCenter
                    }

                    RowLayout {
                        id: contentRow
                        anchors.fill: parent
                        anchors.leftMargin: 16
                        anchors.rightMargin: 12
                        anchors.topMargin: 12
                        anchors.bottomMargin: 12
                        spacing: 12

                        Text {
                            text: root._glyph(model.type)
                            font.family: iconFont.name
                            font.pixelSize: 24
                            color: root._accentColor(model.type)
                            Layout.alignment: Qt.AlignTop
                        }

                        Text {
                            text: model.text
                            color: theme.textPrimary
                            font.pixelSize: 13
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                        }

                        Item {
                            Layout.alignment: Qt.AlignTop
                            width: 20
                            height: 20
                            visible: model.dismissible
                            Text {
                                anchors.centerIn: parent
                                text: "\ue9b4"
                                font.family: iconFont.name
                                font.pixelSize: 14
                                color: closeArea.containsMouse ? theme.textPrimary : theme.textTertiary
                            }
                            MouseArea {
                                id: closeArea
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: wrapper.dismissAnimated()
                            }
                        }
                    }
                }
            }
        }
    }
}
