import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Reusable password prompt modal. Checks against the developer password
// and emits accepted() on success. Caller sets title, description, and
// confirmLabel to customise the appearance.
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 10000

    AppTheme { id: theme }

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
        if (MOTIONInterface.checkDeveloperPassword(pwField.text)) {
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
        MouseArea { anchors.fill: parent; onClicked: root.close() }
    }

    // Panel
    Rectangle {
        width: 360
        height: contentCol.implicitHeight + 48
        radius: 14
        color: theme.bgContainer
        border.color: theme.borderStrong
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
                color: theme.textPrimary
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }

            Text {
                text: root.description
                color: theme.textSecondary
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
                color: theme.textPrimary
                placeholderTextColor: theme.textSecondary
                font.pixelSize: 14
                verticalAlignment: TextInput.AlignVCenter
                leftPadding: 10
                rightPadding: 10
                topPadding: 0
                bottomPadding: 0
                background: Rectangle {
                    color: theme.bgInput
                    radius: 4
                    border.color: pwField.activeFocus ? theme.accentBlue : theme.borderSoft
                    border.width: 1
                }
                onAccepted: root._submit()
            }

            Text {
                id: errorLabel
                text: "Incorrect password"
                color: theme.accentRed
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
                        color: parent.hovered ? Qt.lighter(theme.accentBlue, 1.1) : theme.accentBlue
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
