import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    AppTheme { id: theme }

    property string sessionId: MOTIONInterface.subjectId

    signal accepted()

    function open() {
        sessionIdField.text = MOTIONInterface.subjectId
        root.visible = true
        sessionIdField.forceActiveFocus()
    }
    function close() {
        MOTIONInterface.subjectId = sessionIdField.text
        sessionId = sessionIdField.text
        accepted()
        root.visible = false
    }

    // Dimmed backdrop
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        MouseArea { anchors.fill: parent; onClicked: {} }
    }

    Rectangle {
        width: 380
        height: 200
        radius: 12
        color: theme.bgContainer
        border.color: theme.borderSubtle
        border.width: 2
        anchors.centerIn: parent

        // X close button
        Rectangle {
            width: 28; height: 28; radius: 14
            color: xArea.containsMouse ? "#C0392B" : theme.borderStrong
            border.color: theme.borderHover; border.width: 1
            anchors.top: parent.top; anchors.right: parent.right
            anchors.topMargin: 10; anchors.rightMargin: 10
            z: 10
            Behavior on color { ColorAnimation { duration: 120 } }
            Text { anchors.centerIn: parent; text: "✕"; color: theme.textPrimary; font.pixelSize: 13 }
            MouseArea {
                id: xArea; anchors.fill: parent; hoverEnabled: true
                cursorShape: Qt.PointingHandCursor; onClicked: root.close()
            }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            Text {
                text: "Session Settings"
                color: theme.textPrimary
                font.pixelSize: 20
                font.weight: Font.Bold
                Layout.alignment: Qt.AlignHCenter
            }

            RowLayout {
                spacing: 10
                Layout.fillWidth: true

                Text {
                    text: "Session ID:"
                    font.pixelSize: 16
                    color: theme.textSecondary
                    Layout.alignment: Qt.AlignVCenter
                }

                TextField {
                    id: sessionIdField
                    text: root.sessionId
                    font.pixelSize: 16
                    color: theme.textPrimary
                    Layout.fillWidth: true
                    background: Rectangle {
                        color: theme.bgInput; radius: 4
                        border.color: theme.borderSubtle; border.width: 1
                    }
                }
            }

            Item { Layout.fillHeight: true }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
