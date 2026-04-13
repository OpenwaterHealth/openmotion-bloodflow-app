import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    AppTheme { id: theme }

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
            spacing: 20

            Text {
                text: "Scan Duration"
                color: theme.textPrimary
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
                    color: !root.freeRun ? theme.accentBlue : theme.textSecondary
                    font.pixelSize: 16
                    font.weight: !root.freeRun ? Font.Bold : Font.Normal
                }

                Switch {
                    id: modeSwitch
                    checked: root.freeRun
                    onCheckedChanged: root.freeRun = checked
                }

                Text {
                    text: "Continuous"
                    color: root.freeRun ? theme.accentBlue : theme.textSecondary
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
                    color: theme.textPrimary
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 60
                    Layout.preferredHeight: 48
                    background: Rectangle {
                        color: theme.bgInput; radius: 6
                        border.color: theme.borderSubtle; border.width: 1
                    }
                    onEditingFinished: {
                        var v = parseInt(text)
                        if (isNaN(v)) v = 0
                        root.hours = Math.max(0, Math.min(99, v))
                        text = String(root.hours)
                    }
                }

                Text { text: ":"; color: theme.textSecondary; font.pixelSize: 24 }

                TextField {
                    id: minutesField
                    text: String(root.minutes).padStart(2, '0')
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: IntValidator { bottom: 0; top: 59 }
                    font.pixelSize: 24
                    color: theme.textPrimary
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 60
                    Layout.preferredHeight: 48
                    background: Rectangle {
                        color: theme.bgInput; radius: 6
                        border.color: theme.borderSubtle; border.width: 1
                    }
                    onEditingFinished: {
                        var v = parseInt(text)
                        if (isNaN(v)) v = 0
                        root.minutes = Math.max(0, Math.min(59, v))
                        text = String(root.minutes).padStart(2, '0')
                    }
                }

                Text { text: ":"; color: theme.textSecondary; font.pixelSize: 24 }

                TextField {
                    id: secondsField
                    text: String(root.seconds).padStart(2, '0')
                    inputMethodHints: Qt.ImhDigitsOnly
                    validator: IntValidator { bottom: 0; top: 59 }
                    font.pixelSize: 24
                    color: theme.textPrimary
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: 60
                    Layout.preferredHeight: 48
                    background: Rectangle {
                        color: theme.bgInput; radius: 6
                        border.color: theme.borderSubtle; border.width: 1
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
                    color: theme.textTertiary
                    font.pixelSize: 11
                    Layout.alignment: Qt.AlignBottom
                }
            }

            // Free run info text
            Text {
                visible: root.freeRun
                text: "Scan will run indefinitely until stopped."
                color: theme.textTertiary
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
