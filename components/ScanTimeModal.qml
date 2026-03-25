import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    property bool freeRun: false
    property int hours: 1
    property int minutes: 0
    property int seconds: 0

    // Duration in seconds
    property int durationSec: freeRun ? 0 : (hours * 3600 + minutes * 60 + seconds)

    signal accepted()

    function open() { root.visible = true }
    function close() {
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
        width: 400
        height: 280
        radius: 12
        color: "#1E1E20"
        border.color: "#3E4E6F"
        border.width: 2
        anchors.centerIn: parent

        // X close button
        Rectangle {
            width: 28; height: 28; radius: 14
            color: xArea.containsMouse ? "#C0392B" : "#2A2A2E"
            border.color: "#5A6B8C"; border.width: 1
            anchors.top: parent.top; anchors.right: parent.right
            anchors.topMargin: 10; anchors.rightMargin: 10
            z: 10
            Behavior on color { ColorAnimation { duration: 120 } }
            Text { anchors.centerIn: parent; text: "✕"; color: "#FFFFFF"; font.pixelSize: 13 }
            MouseArea {
                id: xArea; anchors.fill: parent; hoverEnabled: true
                cursorShape: Qt.PointingHandCursor; onClicked: root.close()
            }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 20

            Text {
                text: "Scan Duration"
                color: "#FFFFFF"
                font.pixelSize: 20
                font.weight: Font.Bold
                Layout.alignment: Qt.AlignHCenter
            }

            // Timed / Free Run switch
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 16

                Text {
                    text: "Timed"
                    color: !root.freeRun ? "#4A90E2" : "#BDC3C7"
                    font.pixelSize: 16
                    font.weight: !root.freeRun ? Font.Bold : Font.Normal
                }

                Switch {
                    id: modeSwitch
                    checked: root.freeRun
                    onCheckedChanged: root.freeRun = checked
                }

                Text {
                    text: "Free Run"
                    color: root.freeRun ? "#4A90E2" : "#BDC3C7"
                    font.pixelSize: 16
                    font.weight: root.freeRun ? Font.Bold : Font.Normal
                }
            }

            // Time entry (only when timed)
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 8
                visible: !root.freeRun
                opacity: !root.freeRun ? 1.0 : 0.3

                TextField {
                    id: hoursField
                    text: String(root.hours)
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: IntValidator { bottom: 0; top: 99 }
                    font.pixelSize: 24
                    color: "white"
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 60
                    Layout.preferredHeight: 48
                    background: Rectangle {
                        color: "#2E2E33"; radius: 6
                        border.color: "#3E4E6F"; border.width: 1
                    }
                    onEditingFinished: {
                        var v = parseInt(text)
                        if (isNaN(v)) v = 0
                        root.hours = Math.max(0, Math.min(99, v))
                        text = String(root.hours)
                    }
                }

                Text { text: ":"; color: "#BDC3C7"; font.pixelSize: 24 }

                TextField {
                    id: minutesField
                    text: String(root.minutes).padStart(2, '0')
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: IntValidator { bottom: 0; top: 59 }
                    font.pixelSize: 24
                    color: "white"
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 60
                    Layout.preferredHeight: 48
                    background: Rectangle {
                        color: "#2E2E33"; radius: 6
                        border.color: "#3E4E6F"; border.width: 1
                    }
                    onEditingFinished: {
                        var v = parseInt(text)
                        if (isNaN(v)) v = 0
                        root.minutes = Math.max(0, Math.min(59, v))
                        text = String(root.minutes).padStart(2, '0')
                    }
                }

                Text { text: ":"; color: "#BDC3C7"; font.pixelSize: 24 }

                TextField {
                    id: secondsField
                    text: String(root.seconds).padStart(2, '0')
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: IntValidator { bottom: 0; top: 59 }
                    font.pixelSize: 24
                    color: "white"
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 60
                    Layout.preferredHeight: 48
                    background: Rectangle {
                        color: "#2E2E33"; radius: 6
                        border.color: "#3E4E6F"; border.width: 1
                    }
                    onEditingFinished: {
                        var v = parseInt(text)
                        if (isNaN(v)) v = 0
                        root.seconds = Math.max(0, Math.min(59, v))
                        text = String(root.seconds).padStart(2, '0')
                    }
                }

                Text {
                    text: "H : M : S"
                    color: "#7F8C8D"
                    font.pixelSize: 11
                    Layout.alignment: Qt.AlignBottom
                }
            }

            // Free run info text
            Text {
                visible: root.freeRun
                text: "Scan will run indefinitely until stopped."
                color: "#7F8C8D"
                font.pixelSize: 14
                Layout.alignment: Qt.AlignHCenter
            }

            Item { Layout.fillHeight: true }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
