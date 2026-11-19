import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Reusable "are you sure?" dialog: title, message, Cancel and a confirm
// button. accepted() fires on confirm; Cancel, Escape or a click outside
// closes it without. destructive colours the confirm button red (Delete).
// Same look as PasswordPromptModal, which is for real password checks only.
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 10000

    property string title: "Are you sure?"
    property string description: ""
    property string confirmLabel: "Confirm"
    property bool destructive: false

    signal accepted()

    function open() {
        root.visible = true
        panel.forceActiveFocus()
    }
    function close() {
        root.visible = false
    }
    function confirm() {
        root.accepted()
        root.close()
    }

    // Backdrop — click outside closes.
    Rectangle {
        anchors.fill: parent
        color: "#000000B0"
        // Capture ALL pointer input so scroll/hover can't fall through to
        // the interactive plot viewer behind the modal (issue #214).
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onClicked: root.close()
            onWheel: function(wheel) { wheel.accepted = true }
        }
    }

    // Panel
    Rectangle {
        id: panel
        width: 360
        height: contentCol.implicitHeight + 48
        radius: 14
        color: AppTheme.sheetBg
        border.color: AppTheme.borderStrong
        border.width: 1
        anchors.centerIn: parent

        // Absorb clicks so they don't reach the backdrop.
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            id: contentCol
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            Text {
                text: root.title
                color: AppTheme.textPrimary
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }

            Text {
                text: root.description
                visible: root.description !== ""
                color: AppTheme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Item { Layout.fillWidth: true }

                Button {
                    text: "Cancel"
                    Layout.preferredHeight: 32
                    onClicked: root.close()
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
                    text: root.confirmLabel
                    Layout.preferredHeight: 32
                    onClicked: root.confirm()
                    readonly property color base: root.destructive ? AppTheme.accentRed
                                                                   : AppTheme.accentInteractive
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: "#FFFFFF"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? Qt.lighter(parent.base, 1.1) : parent.base
                        radius: 4
                    }
                }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
