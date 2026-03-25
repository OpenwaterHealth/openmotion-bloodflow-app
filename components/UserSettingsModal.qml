import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

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
        color: "#1E1E20"
        border.color: "#3E4E6F"
        border.width: 2
        anchors.centerIn: parent

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            Text {
                text: "Session Settings"
                color: "#FFFFFF"
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
                    color: "#BDC3C7"
                    Layout.alignment: Qt.AlignVCenter
                }

                TextField {
                    id: sessionIdField
                    text: root.sessionId
                    font.pixelSize: 16
                    color: "white"
                    Layout.fillWidth: true
                    background: Rectangle {
                        color: "#2E2E33"; radius: 4
                        border.color: "#3E4E6F"; border.width: 1
                    }
                }
            }

            Item { Layout.fillHeight: true }

            Button {
                text: "OK"
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: 100
                Layout.preferredHeight: 36
                hoverEnabled: true
                contentItem: Text {
                    text: parent.text; font.pixelSize: 14; color: "#FFFFFF"
                    horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    color: parent.hovered ? "#4A90E2" : "#3A3F4B"
                    border.color: parent.hovered ? "#FFFFFF" : "#BDC3C7"; radius: 6
                }
                onClicked: root.close()
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
