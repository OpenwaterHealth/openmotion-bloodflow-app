import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Rectangle {
    id: panel

    AppTheme { id: theme }

    width: 80
    color: theme.bgPanel
    radius: 12
    border.color: theme.borderStrong
    border.width: 1

    property bool scanning: false
    property bool waiting: false       // true while cameras are flashing / scan is arming
    property bool camerasReady: false  // true when camera flash is complete
    property bool reducedMode: false       // FDA mode hides scan-settings button

    // Connection state — drives start button icon and enablement
    property bool allConnected: MOTIONInterface.consoleConnected &&
        (MOTIONInterface.leftSensorConnected || MOTIONInterface.rightSensorConnected) &&
        !MOTIONInterface.safetyFailure
    signal startStopClicked()
    signal scanSettingsClicked()
    signal notesClicked()
    signal checkClicked()
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
                    color: !panel.allConnected ? theme.textDisabled
                         : panel.scanning ? "#E74C3C"
                         : panel.waiting  ? "#F1C40F"
                         :                  "#2ECC71"
                    Behavior on color { ColorAnimation { duration: 150 } }

                    // Disconnect icon (shown when not connected)
                    Text {
                        anchors.centerIn: parent
                        text: "\ue9ce"
                        font.family: iconFont.name
                        font.pixelSize: 20
                        color: "#FFFFFF"
                        visible: !panel.allConnected && !panel.scanning
                    }

                    // Play triangle (shown when connected and not scanning)
                    Canvas {
                        anchors.centerIn: parent
                        width: 16; height: 16
                        visible: panel.allConnected && !panel.scanning
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
                    text: !panel.allConnected ? "Disconnected" : panel.scanning ? "Stop" : "Start"
                    font.pixelSize: 10
                    color: (panel.camerasReady && panel.allConnected) ? theme.textSecondary : theme.textDisabled
                    horizontalAlignment: Text.AlignHCenter
                    Layout.alignment: Qt.AlignHCenter
                }
            }

            // Hover / press highlight background
            Rectangle {
                anchors.fill: parent
                radius: 10
                color: ssArea.containsMouse ? theme.bgHover : "transparent"
                border.color: ssArea.containsMouse ? theme.borderHover : "transparent"
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

        // Divider between Start and Scan Settings (or between Start and Notes
        // in reduced mode where Scan Settings is hidden).
        Rectangle {
            Layout.preferredWidth: 52; Layout.preferredHeight: 1
            Layout.topMargin: 4; Layout.bottomMargin: 4
            Layout.alignment: Qt.AlignHCenter; color: theme.borderSubtle
        }

        // Scan Settings (camera + duration)
        PanelButton {
            visible: !panel.reducedMode
            enabled: !panel.scanning
            iconText: "\ueabf"  // setting-3 icon
            label: "Scan\nSettings"
            onClicked: panel.scanSettingsClicked()
        }

        // Divider between Scan Settings and Notes — only in normal mode so
        // reduced mode doesn't get two consecutive dividers.
        Rectangle {
            visible: !panel.reducedMode
            Layout.preferredWidth: 52; Layout.preferredHeight: 1
            Layout.topMargin: 4; Layout.bottomMargin: 4
            Layout.alignment: Qt.AlignHCenter; color: theme.borderSubtle
        }

        // Notes
        PanelButton {
            iconText: "\uea7f"  // notes/document icon
            label: "Notes"
            onClicked: panel.notesClicked()
        }

        Rectangle {
            Layout.preferredWidth: 52; Layout.preferredHeight: 1
            Layout.topMargin: 4; Layout.bottomMargin: 4
            Layout.alignment: Qt.AlignHCenter; color: theme.borderSubtle
        }

        // Check (contact quality quick-check)
        PanelButton {
            enabled: !panel.scanning && panel.camerasReady
            iconText: "\uea31"  // graph-3 icon
            label: "Check"
            onClicked: panel.checkClicked()
        }

        // Log viewer (developer mode only)
        PanelButton {
            visible: MOTIONInterface.appConfig.developerMode ? true : false
            iconText: "\uea65"  // list/log icon
            label: "Log"
            onClicked: panel.logClicked()
        }

        // ── spacer pushes bottom controls down ──
        Item { Layout.fillHeight: true }

        // History
        PanelButton {
            enabled: !panel.scanning
            iconText: "\ue96b"  // book icon
            label: "History"
            onClicked: panel.historyClicked()
        }

        Rectangle { Layout.preferredWidth: 52; Layout.preferredHeight: 1; Layout.topMargin: 4; Layout.bottomMargin: 4; Layout.alignment: Qt.AlignHCenter; color: theme.borderSubtle }

        // Settings
        PanelButton {
            enabled: !panel.scanning
            iconText: "\ueabe"  // setting-2 icon
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
                ? (btnItem.highlighted ? Qt.lighter(btnItem.highlightColor, 1.2) : theme.bgHover)
                : (btnItem.highlighted ? btnItem.highlightColor : "transparent")
            border.color: btnMouseArea.containsMouse ? theme.borderHover : "transparent"
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
                color: btnItem.enabled ? (btnItem.highlighted ? "white" : theme.textSecondary) : theme.textDisabled
                horizontalAlignment: Text.AlignHCenter
                Layout.alignment: Qt.AlignHCenter
            }

            Text {
                text: btnItem.label
                font.pixelSize: 10
                color: btnItem.enabled ? (btnItem.highlighted ? "white" : theme.textTertiary) : theme.textDisabled
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
