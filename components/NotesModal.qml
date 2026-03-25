import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    function open() {
        notesArea.text = MOTIONInterface.scanNotes
        root.visible = true
        notesArea.forceActiveFocus()
    }
    function close() {
        MOTIONInterface.scanNotes = notesArea.text
        root.visible = false
    }

    // Dimmed backdrop
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        MouseArea { anchors.fill: parent; onClicked: {} }
    }

    Rectangle {
        width: 600
        height: 450
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
                text: "Session Notes"
                color: "#FFFFFF"
                font.pixelSize: 20
                font.weight: Font.Bold
                Layout.alignment: Qt.AlignHCenter
            }

            Rectangle {
                color: "#2E2E33"
                radius: 6
                border.color: "#3E4E6F"
                border.width: 1
                Layout.fillWidth: true
                Layout.fillHeight: true

                ScrollView {
                    anchors.fill: parent
                    anchors.margins: 8

                    TextArea {
                        id: notesArea
                        font.pixelSize: 14
                        color: "white"
                        wrapMode: Text.Wrap
                        placeholderText: "Enter notes for this session..."
                        placeholderTextColor: "#7F8C8D"
                        background: null
                    }
                }
            }

            Button {
                text: "Close"
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
