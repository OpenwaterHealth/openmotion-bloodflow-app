import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Rectangle {
    id: panel


    width: 80
    color: AppTheme.bgPanel
    radius: 12
    border.color: AppTheme.borderStrong
    border.width: 1

    property bool scanning: false
    property bool waiting: false       // true while a scan start is armed (pipeline-idle gate)
    property bool camerasReady: false  // gates Start/Check enablement
    property bool clinicalMode: false       // FDA mode hides scan-settings button

    // Connection state — drives start button icon and enablement.
    // A laser-safety trip is surfaced via a persistent NotificationCenter
    // toast (see motion_connector.safetyFailure setter), not by faking
    // a disconnect here.
    property bool allConnected: MotionInterface.consoleConnected &&
        (MotionInterface.leftSensorConnected || MotionInterface.rightSensorConnected)
    signal startStopClicked()
    signal scanSettingsClicked()
    signal notesClicked()
    signal checkClicked()
    signal historyClicked()
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
                    color: !panel.allConnected ? AppTheme.textDisabled
                         : panel.waiting  ? "#F1C40F"
                         : panel.scanning ? "#E74C3C"
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
                    color: (panel.camerasReady && panel.allConnected) ? AppTheme.textSecondary : AppTheme.textDisabled
                    horizontalAlignment: Text.AlignHCenter
                    Layout.alignment: Qt.AlignHCenter
                }
            }

            // Hover / press highlight background
            Rectangle {
                anchors.fill: parent
                radius: 10
                color: ssArea.containsMouse ? AppTheme.bgHover : "transparent"
                border.color: ssArea.containsMouse ? AppTheme.borderHover : "transparent"
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
        // in clinical mode where Scan Settings is hidden).
        Rectangle {
            Layout.preferredWidth: 52; Layout.preferredHeight: 1
            Layout.topMargin: 4; Layout.bottomMargin: 4
            Layout.alignment: Qt.AlignHCenter; color: AppTheme.borderSubtle
        }

        // Scan Settings (camera + duration)
        PanelButton {
            visible: !panel.clinicalMode
            enabled: !panel.scanning
            iconText: "\ueabf"  // setting-3 icon
            label: "Scan\nSettings"
            onClicked: panel.scanSettingsClicked()
        }

        // Divider between Scan Settings and Notes — only in normal mode so
        // clinical mode doesn't get two consecutive dividers.
        Rectangle {
            visible: !panel.clinicalMode
            Layout.preferredWidth: 52; Layout.preferredHeight: 1
            Layout.topMargin: 4; Layout.bottomMargin: 4
            Layout.alignment: Qt.AlignHCenter; color: AppTheme.borderSubtle
        }

        // Notes
        PanelButton {
            iconText: "\uea7f"  // notes/document icon
            label: "Notes"
            onClicked: panel.notesClicked()
        }

        Rectangle {
            visible: !panel.clinicalMode
            Layout.preferredWidth: 52; Layout.preferredHeight: 1
            Layout.topMargin: 4; Layout.bottomMargin: 4
            Layout.alignment: Qt.AlignHCenter; color: AppTheme.borderSubtle
        }

        // Check (contact quality quick-check)
        PanelButton {
            visible: !panel.clinicalMode
            enabled: !panel.scanning && panel.camerasReady
            iconText: "\uea31"  // graph-3 icon
            label: "Check"
            onClicked: panel.checkClicked()
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

        Rectangle { Layout.preferredWidth: 52; Layout.preferredHeight: 1; Layout.topMargin: 4; Layout.bottomMargin: 4; Layout.alignment: Qt.AlignHCenter; color: AppTheme.borderSubtle }

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
                ? (btnItem.highlighted ? Qt.lighter(btnItem.highlightColor, 1.2) : AppTheme.bgHover)
                : (btnItem.highlighted ? btnItem.highlightColor : "transparent")
            border.color: btnMouseArea.containsMouse ? AppTheme.borderHover : "transparent"
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
                color: btnItem.enabled ? (btnItem.highlighted ? "white" : AppTheme.textSecondary) : AppTheme.textDisabled
                horizontalAlignment: Text.AlignHCenter
                Layout.alignment: Qt.AlignHCenter
            }

            Text {
                text: btnItem.label
                font.pixelSize: 10
                color: btnItem.enabled ? (btnItem.highlighted ? "white" : AppTheme.textTertiary) : AppTheme.textDisabled
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
