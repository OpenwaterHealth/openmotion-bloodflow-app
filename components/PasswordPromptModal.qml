import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Reusable password prompt modal. Checks against the engineering password
// and emits accepted() on success. Caller sets title, description, and
// confirmLabel to customise the appearance.
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 10000


    property string title: "Password Required"
    property string description: "Enter the password to continue."
    property string confirmLabel: "Confirm"

    signal accepted()

    function open() {
        pwField.text = ""
        errorLabel.visible = false
        root.visible = true
        pwField.forceActiveFocus()
    }
    function close() {
        root.visible = false
    }

    function _submit() {
        if (MotionInterface.checkEngineeringPassword(pwField.text)) {
            root.accepted()
            root.close()
        } else {
            errorLabel.visible = true
            pwField.text = ""
            pwField.forceActiveFocus()
        }
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
        width: 360
        height: contentCol.implicitHeight + 48
        radius: 14
        color: AppTheme.bgContainer
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
                color: AppTheme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            TextField {
                id: pwField
                Layout.fillWidth: true
                Layout.preferredHeight: 38
                echoMode: TextInput.Password
                placeholderText: ""
                color: AppTheme.textPrimary
                placeholderTextColor: AppTheme.textSecondary
                font.pixelSize: 14
                verticalAlignment: TextInput.AlignVCenter
                leftPadding: 10
                rightPadding: 10
                topPadding: 0
                bottomPadding: 0
                background: Rectangle {
                    color: AppTheme.bgInput
                    radius: 4
                    border.color: pwField.activeFocus ? AppTheme.accentBlue : AppTheme.borderSoft
                    border.width: 1
                }
                onAccepted: root._submit()
            }

            Text {
                id: errorLabel
                text: "Incorrect password"
                color: AppTheme.accentRed
                font.pixelSize: 12
                visible: false
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
                    onClicked: root._submit()
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: "#FFFFFF"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? Qt.lighter(AppTheme.accentBlue, 1.1) : AppTheme.accentBlue
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
