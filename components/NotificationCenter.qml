import QtQuick 6.0
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

    // Maximum number of toasts visible at once. When exceeded, the oldest
    // (model index 0) is removed.
    readonly property int maxVisible: 5

    // Monotonically increasing id assigned to each notification.
    property int _nextId: 1

    ListModel { id: model_ }

    /* ── public API ────────────────────────────────────────────────── */

    // Append a notification. `request` is a JS object with at least a `text`
    // field; other fields fall back to defaults defined here.
    function notify(request) {
        var entry = {
            id: root._nextId++,
            text: request.text || "",
            type: request.type || "info",
            durationMs: (request.durationMs !== undefined) ? request.durationMs : 4000,
            dismissible: (request.dismissible !== undefined) ? request.dismissible : true
        }
        model_.append(entry)
        while (model_.count > root.maxVisible) {
            model_.remove(0)
        }
    }

    // Remove a notification by id. Used by the toast delegate when its timer
    // fires or the user clicks ✕.
    function dismiss(id) {
        for (var i = 0; i < model_.count; ++i) {
            if (model_.get(i).id === id) {
                model_.remove(i)
                return
            }
        }
    }

    /* ── Python bridge ─────────────────────────────────────────────── */

    Connections {
        target: MOTIONInterface
        function onNotificationRequested(payload) {
            root.notify(payload)
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

        Repeater {
            model: model_
            delegate: Rectangle {
                width: 340
                height: 48
                radius: 10
                color: theme.bgElevated
                border.color: theme.borderSubtle
                border.width: 1
                Text {
                    anchors.fill: parent
                    anchors.margins: 12
                    text: "[" + model.type + "] " + model.text
                    color: theme.textPrimary
                    elide: Text.ElideRight
                }
                // Temporary: click anywhere to dismiss while we're building.
                MouseArea {
                    anchors.fill: parent
                    onClicked: root.dismiss(model.id)
                }
            }
        }
    }
}
