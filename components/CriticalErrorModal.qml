import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  CriticalErrorModal — blocking, dismissible alert for showstopper
 *  conditions carrying a stable error code (see error_codes.py /
 *  docs/ERROR_CODES.md).
 *
 *  Host once, top-level, above everything else:
 *
 *      CriticalErrorModal { id: criticalErrorModal; anchors.fill: parent; z: 100000 }
 *
 *  It listens to MotionInterface.criticalErrorRaised and shows itself. If
 *  several fire close together they are queued and shown one at a time.
 *  Dismissible via the Dismiss button, the backdrop, or Esc.
 */
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 100000

    AppTheme { id: theme }

    // Current error fields (bound into the panel).
    property string code: ""
    property string title: ""
    property string message: ""
    property string suggestedAction: ""
    property string detail: ""

    // Pending errors waiting their turn.
    property var _queue: []
    property bool _detailExpanded: false

    function _enqueue(code, title, message, action, detail) {
        var q = root._queue.slice()
        q.push({code: code, title: title, message: message,
                action: action, detail: detail})
        root._queue = q
        if (!root.visible)
            root._showNext()
    }

    function _showNext() {
        if (root._queue.length === 0) {
            root.visible = false
            return
        }
        var q = root._queue.slice()
        var e = q.shift()
        root._queue = q
        root.code = e.code
        root.title = e.title
        root.message = e.message
        root.suggestedAction = e.action
        root.detail = e.detail
        root._detailExpanded = false
        root.visible = true
        root.forceActiveFocus()
    }

    function dismiss() {
        root._showNext()  // advances to the next queued error, or hides
    }

    function _reportText() {
        return "Code: " + root.code
            + "\nTitle: " + root.title
            + "\nMessage: " + root.message
            + (root.detail ? "\nDetail: " + root.detail : "")
    }

    Connections {
        target: MotionInterface
        function onCriticalErrorRaised(code, title, message, action, detail) {
            root._enqueue(code, title, message, action, detail)
        }
    }

    // Backdrop — click outside dismisses.
    Rectangle {
        anchors.fill: parent
        color: "#000000C0"
        // Capture ALL pointer input so scroll/hover can't fall through to
        // the interactive plot viewer behind the modal (issue #214).
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onClicked: root.dismiss()
            onWheel: function(wheel) { wheel.accepted = true }
        }
    }

    // Panel
    Rectangle {
        width: 520
        height: contentCol.implicitHeight + headerBar.height + 16 + 20
        radius: 14
        color: theme.bgContainer
        border.color: theme.accentRed
        border.width: 1
        anchors.centerIn: parent

        // Absorb clicks so they don't reach the backdrop.
        MouseArea { anchors.fill: parent }

        // Red header bar with code badge.
        Rectangle {
            id: headerBar
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            height: 44
            radius: 14
            color: theme.accentRed
            // square off the bottom corners so only the top is rounded
            Rectangle {
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                height: parent.radius
                color: theme.accentRed
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                spacing: 10

                Rectangle {
                    Layout.preferredHeight: 24
                    Layout.preferredWidth: codeText.implicitWidth + 16
                    radius: 4
                    color: "#FFFFFF"
                    Text {
                        id: codeText
                        anchors.centerIn: parent
                        text: root.code
                        color: theme.accentRed
                        font.pixelSize: 13
                        font.weight: Font.Bold
                        font.family: "Consolas, monospace"
                    }
                }

                Text {
                    text: "Critical Error"
                    color: "#FFFFFF"
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                    Layout.fillWidth: true
                }

                Text {
                    visible: root._queue.length > 0
                    text: root._queue.length + " more"
                    color: "#FFFFFFCC"
                    font.pixelSize: 12
                }
            }
        }

        ColumnLayout {
            id: contentCol
            anchors.top: headerBar.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.topMargin: 16
            anchors.leftMargin: 20
            anchors.rightMargin: 20
            spacing: 12

            Text {
                text: root.title
                color: theme.textPrimary
                font.pixelSize: 17
                font.weight: Font.DemiBold
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Text {
                text: root.message
                color: theme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // Suggested action callout.
            Rectangle {
                visible: root.suggestedAction !== ""
                Layout.fillWidth: true
                radius: 6
                color: theme.bgInput
                Layout.preferredHeight: actionRow.implicitHeight + 16
                RowLayout {
                    id: actionRow
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 8
                    Text {
                        text: "→"
                        color: theme.accentBlue
                        font.pixelSize: 14
                        font.weight: Font.Bold
                    }
                    Text {
                        text: root.suggestedAction
                        color: theme.textPrimary
                        font.pixelSize: 13
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                }
            }

            // Collapsible technical detail.
            Text {
                visible: root.detail !== ""
                text: (root._detailExpanded ? "▼ " : "▶ ") + "Details"
                color: theme.textTertiary
                font.pixelSize: 12
                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root._detailExpanded = !root._detailExpanded
                }
            }
            Text {
                visible: root.detail !== "" && root._detailExpanded
                text: root.detail
                color: theme.textTertiary
                font.pixelSize: 12
                font.family: "Consolas, monospace"
                wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                Layout.fillWidth: true
            }

            // Buttons
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 4
                spacing: 10

                Button {
                    text: "Copy details"
                    Layout.preferredHeight: 32
                    onClicked: MotionInterface.copyToClipboard(root._reportText())
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.bgHover : theme.bgInput
                        radius: 4
                        border.color: theme.borderSoft; border.width: 1
                    }
                }

                Button {
                    text: "Send Bug Report to Openwater"
                    Layout.preferredHeight: 32
                    onClicked: MotionInterface.sendBugReport(root.code)
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: theme.textPrimary
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.bgHover : theme.bgInput
                        radius: 4
                        border.color: theme.borderSubtle; border.width: 1
                    }
                }

                Item { Layout.fillWidth: true }

                Button {
                    text: "Dismiss"
                    Layout.preferredHeight: 32
                    onClicked: root.dismiss()
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: "#FFFFFF"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? Qt.lighter(theme.accentRed, 1.1) : theme.accentRed
                        radius: 4
                    }
                }
            }
        }
    }

    Keys.onReleased: function(event) {
        if (event.key === Qt.Key_Escape) { root.dismiss(); event.accepted = true }
    }
}
