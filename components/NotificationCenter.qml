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
                id: toast
                width: 340
                implicitHeight: contentRow.implicitHeight + 24  // 12px padding top+bottom
                height: implicitHeight
                radius: 10
                color: theme.bgElevated
                border.color: theme.borderSubtle
                border.width: 1

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
                    anchors.leftMargin: 16     // leave room for stripe (4 + 3 + ~9)
                    anchors.rightMargin: 12
                    anchors.topMargin: 12
                    anchors.bottomMargin: 12
                    spacing: 12

                    // Type icon
                    Text {
                        text: root._glyph(model.type)
                        font.family: iconFont.name
                        font.pixelSize: 24
                        color: root._accentColor(model.type)
                        Layout.alignment: Qt.AlignTop
                    }

                    // Message
                    Text {
                        text: model.text
                        color: theme.textPrimary
                        font.pixelSize: 13
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignVCenter
                    }

                    // Close button (only when dismissible)
                    Item {
                        Layout.alignment: Qt.AlignTop
                        width: 20
                        height: 20
                        visible: model.dismissible
                        Text {
                            id: closeGlyph
                            anchors.centerIn: parent
                            text: "\ue9b4"   // cross
                            font.family: iconFont.name
                            font.pixelSize: 14
                            color: closeArea.containsMouse ? theme.textPrimary : theme.textTertiary
                        }
                        MouseArea {
                            id: closeArea
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.dismiss(model.id)
                        }
                    }
                }
            }
        }
    }
}
