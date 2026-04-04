import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Rectangle {
    id: panel
    width: 80
    color: "#1A1A1C"
    radius: 12
    border.color: "#2A2A2E"
    border.width: 1

    property bool scanning: false
    property bool waiting: false       // true while cameras are flashing / scan is arming
    property bool camerasReady: false  // true when camera flash is complete

    // Status color logic
    property bool allConnected: MOTIONInterface.consoleConnected &&
        (MOTIONInterface.leftSensorConnected || MOTIONInterface.rightSensorConnected) &&
        !MOTIONInterface.safetyFailure
    property color statusColor: {
        if (MOTIONInterface.safetyFailure) return "#F1C40F"  // yellow - safety fault
        if (!MOTIONInterface.consoleConnected ||
            (!MOTIONInterface.leftSensorConnected && !MOTIONInterface.rightSensorConnected))
            return "#7F8C8D"  // grey - disconnected
        if (scanning) return "#3498DB"  // blue - scanning
        return "#2ECC71"  // green - all good
    }

    signal startStopClicked()
    signal scanSettingsClicked()
    signal sessionClicked()
    signal historyClicked()
    signal logClicked()
    signal settingsClicked()

    FontLoader {
        id: iconFont
        source: "../assets/fonts/keenicons-outline.ttf"
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 6
        spacing: 4

        // ===== BOX 1: Scan Controls =====
        // Start/Stop — coloured circle badge behind the icon
        Item {
            Layout.preferredWidth: 68
            Layout.preferredHeight: 68
            Layout.alignment: Qt.AlignHCenter

            ColumnLayout {
                anchors.centerIn: parent
                spacing: 3

                // Coloured circle
                Rectangle {
                    id: startStopCircle
                    Layout.alignment: Qt.AlignHCenter
                    width: 36; height: 36; radius: 18
                    color: panel.scanning ? "#E74C3C"
                         : panel.waiting  ? "#F1C40F"
                         :                  "#2ECC71"
                    Behavior on color { ColorAnimation { duration: 150 } }

                    // Play triangle (start / waiting)
                    Canvas {
                        anchors.centerIn: parent
                        width: 16; height: 16
                        visible: !panel.scanning
                        onPaint: {
                            var ctx = getContext("2d")
                            ctx.clearRect(0, 0, width, height)
                            ctx.fillStyle = "#FFFFFF"
                            ctx.beginPath()
                            ctx.moveTo(3, 1); ctx.lineTo(15, 8); ctx.lineTo(3, 15)
                            ctx.closePath(); ctx.fill()
                        }
                    }

                    // Stop square
                    Rectangle {
                        anchors.centerIn: parent
                        width: 11; height: 11
                        color: "#FFFFFF"
                        visible: panel.scanning
                    }
                }

                Text {
                    text: panel.scanning ? "Stop" : "Start"
                    font.pixelSize: 10
                    color: (panel.camerasReady && panel.allConnected) ? "#BDC3C7" : "#555555"
                    horizontalAlignment: Text.AlignHCenter
                    Layout.alignment: Qt.AlignHCenter
                }
            }

            // Hover / press highlight background
            Rectangle {
                anchors.fill: parent
                radius: 10
                color: ssArea.containsMouse ? "#2E2E33" : "transparent"
                border.color: ssArea.containsMouse ? "#5A6B8C" : "transparent"
                border.width: 1
                z: -1
                Behavior on color { ColorAnimation { duration: 150 } }
            }

            MouseArea {
                id: ssArea
                anchors.fill: parent
                hoverEnabled: panel.camerasReady && panel.allConnected
                enabled: panel.camerasReady && panel.allConnected
                cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                onClicked: panel.startStopClicked()
            }
        }

        // Scan Settings (camera + duration)
        PanelButton {
            iconText: "\uea48"  // camera/aperture icon
            label: "Scan\nSettings"
            onClicked: panel.scanSettingsClicked()
        }

        // --- Divider ---
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            Layout.topMargin: 4
            Layout.bottomMargin: 4
            color: "#3E4E6F"
        }

        // ===== BOX 2: Session Info =====
        // Session (ID + Notes)
        PanelButton {
            iconText: "\ueb1f"  // person/user icon
            label: "Session"
            onClicked: panel.sessionClicked()
        }

        // --- Divider ---
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            Layout.topMargin: 4
            Layout.bottomMargin: 4
            color: "#3E4E6F"
        }

        // ===== BOX 3: System =====
        // History
        PanelButton {
            iconText: "\uea7f"  // history/clock-arrow icon
            label: "History"
            onClicked: panel.historyClicked()
        }

        // Log viewer
        PanelButton {
            iconText: "\uea65"  // list/log icon
            label: "Log"
            onClicked: panel.logClicked()
        }

        // ── spacer pushes Box 4 to the bottom ──
        Item { Layout.fillHeight: true }

        // --- Divider ---
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            Layout.topMargin: 4
            Layout.bottomMargin: 4
            color: "#3E4E6F"
        }

        // ===== BOX 4: Bottom controls =====
        // Status indicator (not clickable)
        Item {
            Layout.preferredWidth: 68
            Layout.preferredHeight: 68
            Layout.alignment: Qt.AlignHCenter

            ColumnLayout {
                anchors.centerIn: parent
                spacing: 4

                Rectangle {
                    width: 20; height: 20; radius: 10
                    color: panel.statusColor
                    border.color: Qt.darker(panel.statusColor, 1.3)
                    border.width: 1
                    Layout.alignment: Qt.AlignHCenter

                    // Pulse animation when scanning
                    SequentialAnimation on opacity {
                        running: panel.scanning
                        loops: Animation.Infinite
                        NumberAnimation { from: 1.0; to: 0.4; duration: 800 }
                        NumberAnimation { from: 0.4; to: 1.0; duration: 800 }
                    }
                }

                Text {
                    text: "Status"
                    color: "#7F8C8D"
                    font.pixelSize: 10
                    horizontalAlignment: Text.AlignHCenter
                    Layout.alignment: Qt.AlignHCenter
                }
            }
        }

        // Settings
        PanelButton {
            iconText: "\ueabf"  // gear icon
            label: "Settings"
            onClicked: panel.settingsClicked()
        }
    }

    // Reusable panel button component
    component PanelButton: Item {
        id: btnItem
        property string iconText: ""
        property string label: ""
        property bool highlighted: false
        property color highlightColor: "#4A90E2"
        Layout.preferredWidth: 68
        Layout.preferredHeight: 68
        Layout.alignment: Qt.AlignHCenter

        signal clicked()

        Rectangle {
            anchors.fill: parent
            radius: 10
            color: btnMouseArea.containsMouse
                ? (btnItem.highlighted ? Qt.lighter(btnItem.highlightColor, 1.2) : "#2E2E33")
                : (btnItem.highlighted ? btnItem.highlightColor : "transparent")
            border.color: btnMouseArea.containsMouse ? "#5A6B8C" : "transparent"
            border.width: 1

            Behavior on color { ColorAnimation { duration: 150 } }
        }

        ColumnLayout {
            anchors.centerIn: parent
            spacing: 2

            Text {
                text: btnItem.iconText
                font.family: iconFont.name
                font.pixelSize: 26
                color: btnItem.enabled ? (btnItem.highlighted ? "white" : "#BDC3C7") : "#555555"
                horizontalAlignment: Text.AlignHCenter
                Layout.alignment: Qt.AlignHCenter
            }

            Text {
                text: btnItem.label
                font.pixelSize: 10
                color: btnItem.enabled ? (btnItem.highlighted ? "white" : "#7F8C8D") : "#555555"
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                Layout.preferredWidth: 64
                Layout.alignment: Qt.AlignHCenter
            }
        }

        MouseArea {
            id: btnMouseArea
            anchors.fill: parent
            hoverEnabled: btnItem.enabled
            enabled: btnItem.enabled
            cursorShape: btnItem.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: btnItem.clicked()
        }
    }
}
