import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0

Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9999

    AppTheme { id: theme }

    // API
    signal cancelRequested()
    property alias message: titleLabel.text
    property bool done: false            // 
    property int progress: 0            // <-- bind from ScanRunner
    property string stageText: ""       // <-- bind from ScanRunner
    function open()  { root.visible = true }
    function close() { root.visible = false; root.done = false; }  // reset on close
    function appendLog(line) {
        if (!line) return
        if (logArea.text.length > 0) logArea.text += "\n"
        logArea.text += line
        logArea.cursorPosition = logArea.length
    }

    // Dimmed backdrop — click outside to close
    Rectangle {
        anchors.fill: parent
        color: "#00000088"
        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.AllButtons
            hoverEnabled: true
            onClicked: root.close()
        }
    }

    // Dialog panel
    Rectangle {
        id: panel
        width: 920
        height: 640
        radius: 10
        color: theme.bgContainer
        border.color: theme.borderSubtle
        border.width: 2
        anchors.centerIn: parent
        focus: true

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 20
            spacing: 12

            // Title
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Layout.alignment: Qt.AlignHCenter

                Text {
                    id: titleLabel
                    text: "Scanning…"
                    color: theme.textPrimary
                    font.pixelSize: 20
                    Layout.alignment: Qt.AlignVCenter
                }
            }

            // Stage line
            Text {
                id: stageLine
                text: stageText.length ? stageText : "Preparing…"
                color: theme.plotText
                font.pixelSize: 14
                wrapMode: Text.NoWrap
                elide: Text.ElideRight
                Layout.fillWidth: true
            }

            // Progress bar
            ProgressBar {
                id: prog
                from: 0; to: 100
                value: root.progress
                indeterminate: value < 5
                Layout.fillWidth: true
                Layout.preferredHeight: 8
            }

            // Animated dots
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                spacing: 6
                Repeater {
                    model: 3
                    delegate: Rectangle {
                        width: 6; height: 6; radius: 3
                        color: theme.accentBlue
                        opacity: 0.3

                        SequentialAnimation on opacity {
                            running: root.visible
                            loops: Animation.Infinite
                            NumberAnimation { from: 0.3; to: 1.0; duration: 400 }
                            NumberAnimation { from: 1.0; to: 0.3; duration: 400 }
                            onStarted: delay.start()
                        }
                        PauseAnimation { id: delay; duration: index * 120 }
                    }
                }
            }

            // Log area
            Frame {
                Layout.fillWidth: true
                Layout.fillHeight: true
                background: Rectangle { color: theme.bgPlot; radius: 6; border.color: theme.borderSoft }
                ScrollView {
                    anchors.fill: parent
                    TextArea {
                        id: logArea
                        readOnly: true
                        wrapMode: TextEdit.NoWrap
                        text: ""
                        color: theme.plotText
                        font.family: "Consolas"
                        font.pixelSize: 12
                        background: null
                    }
                }
            }

            // Close hint
            Text {
                text: "Click outside to close"
                color: theme.textDisabled
                font.pixelSize: 11
                horizontalAlignment: Text.AlignHCenter
                Layout.alignment: Qt.AlignHCenter
            }
        }

        // ESC to close
        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) {
                root.close()
                event.accepted = true
            }
        }
    }
}
