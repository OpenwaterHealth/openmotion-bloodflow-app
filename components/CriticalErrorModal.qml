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
            if (root.visible) {
                // Last queued error dismissed — let the connector stop the
                // console error-LED blink and restore idle green (#257).
                MotionInterface.criticalErrorsDismissed()
            }
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
        color: "#000000AA"
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
        // Fixed width, centred on the full window — deliberately NOT the
        // Math.min(parent.width - iconBarInset - 40, …) clamp the other modals
        // use. This modal's footer carries three buttons whose combined
        // minimum is ~466px, so a clamp that shrinks the card below ~514px
        // makes the row overflow the card and clip (verified: fine at 640px,
        // broken at 560px). A fixed 520 has the room at every window size the
        // clamp would have shrunk for. The icon-bar inset is unnecessary here
        // too — at z:100000 this modal is above ButtonPanel, so the bar sits
        // dimmed behind the backdrop rather than overlapping the card.
        width: 520
        height: contentCol.implicitHeight + 48
        radius: 12
        color: AppTheme.sheetBg
        border.color: AppTheme.borderSubtle
        border.width: 2
        anchors.centerIn: parent

        // Absorb clicks so they don't reach the backdrop.
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            id: contentCol
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            // Eyebrow row — code badge, label, queued-error counter. Replaces
            // the old solid header bar: a coloured header bar is not a pattern
            // used anywhere else in the app, and the saturated red read as an
            // alarm for conditions that are often recoverable.
            RowLayout {
                Layout.fillWidth: true
                spacing: 9

                Rectangle {
                    Layout.preferredHeight: 24
                    Layout.preferredWidth: codeText.implicitWidth + 16
                    radius: 4
                    color: AppTheme.accentInteractive
                    Text {
                        id: codeText
                        anchors.centerIn: parent
                        text: root.code
                        color: "#FFFFFF"
                        font.pixelSize: 13
                        font.weight: Font.Bold
                        font.family: "Consolas, monospace"
                    }
                }

                Text {
                    text: "CRITICAL ERROR"
                    color: AppTheme.textTertiary
                    font.pixelSize: 11
                    font.letterSpacing: 0.5
                    Layout.fillWidth: true
                }

                Text {
                    visible: root._queue.length > 0
                    text: root._queue.length + " more"
                    color: AppTheme.textTertiary
                    font.pixelSize: 12
                }
            }

            Text {
                text: root.title
                color: AppTheme.textPrimary
                font.pixelSize: 18
                font.weight: Font.DemiBold
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Text {
                text: root.message
                color: AppTheme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // Suggested action callout.
            Rectangle {
                visible: root.suggestedAction !== ""
                Layout.fillWidth: true
                radius: 6
                color: AppTheme.bgInput
                Layout.preferredHeight: actionRow.implicitHeight + 16
                RowLayout {
                    id: actionRow
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 8
                    Text {
                        text: "→"
                        color: AppTheme.accentBlue
                        font.pixelSize: 14
                        font.weight: Font.Bold
                    }
                    Text {
                        text: root.suggestedAction
                        color: AppTheme.textPrimary
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
                color: AppTheme.textTertiary
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
                color: AppTheme.textTertiary
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
                    // Confirm the copy — copyToClipboard succeeds silently, so
                    // without a toast this button is indistinguishable from a
                    // dead one. The tag keeps repeat clicks from stacking.
                    onClicked: {
                        MotionInterface.copyToClipboard(root._reportText())
                        MotionInterface.notify(
                            "Error details copied to clipboard.",
                            "success", 3000, true, "critical-error-copy")
                    }
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: AppTheme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.bgHover : AppTheme.bgInput
                        radius: 4
                        border.color: AppTheme.borderSoft; border.width: 1
                    }
                }

                Button {
                    text: "Send Bug Report to Openwater"
                    Layout.preferredHeight: 32
                    onClicked: MotionInterface.sendBugReport(root.code)
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: AppTheme.textPrimary
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.bgHover : AppTheme.bgInput
                        radius: 4
                        border.color: AppTheme.borderSubtle; border.width: 1
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
                        color: parent.hovered ? Qt.lighter(AppTheme.accentInteractive, 1.1)
                                              : AppTheme.accentInteractive
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
