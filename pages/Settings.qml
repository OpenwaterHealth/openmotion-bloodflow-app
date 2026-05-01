import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0
import "../components"

Rectangle {
    id: settings
    width: parent.width
    height: parent.height
    color: theme.bgElevated
    radius: 20
    opacity: 0.95 // Slight transparency for the content area

    AppTheme { id: theme }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 20
        spacing: 15

        // Title
        Text {
            text: "Settings"
            font.pixelSize: 20
            font.weight: Font.Bold
            color: theme.textPrimary
            horizontalAlignment: Text.AlignHCenter
            Layout.alignment: Qt.AlignHCenter
        }

        // Calibration section
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 80
            color: theme.bgContainer
            radius: 10
            border.color: theme.borderSubtle
            border.width: 2

            RowLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 12

                Button {
                    id: runCalibrationButton
                    text: "Run Calibration"
                    enabled: MOTIONInterface.consoleConnected && !MOTIONInterface.calibrationRunning
                    onClicked: MOTIONInterface.runCalibration()
                }

                // Indicator light
                Rectangle {
                    id: calibLight
                    width: 14
                    height: 14
                    radius: 7
                    border.width: 1
                    border.color: "#444"
                    color: {
                        switch (MOTIONInterface.calibrationStatus) {
                        case "running": return "#2196F3"  // blue
                        case "passed":  return "#4CAF50"  // green
                        case "failed":  return "#F44336"  // red
                        case "aborted": return "#FF9800"  // orange
                        default:        return "#9E9E9E"  // gray
                        }
                    }
                }

                // Status text
                Label {
                    id: calibStatusLabel
                    color: theme.textPrimary
                    text: {
                        switch (MOTIONInterface.calibrationStatus) {
                        case "running":
                            return "Calibrating... (" + calibTimer.elapsedSec
                                   + "s / " + MOTIONInterface.maxCalibrationTimeSec + "s)"
                        case "passed":  return "Calibration Passed"
                        case "failed":  return "Calibration Failed"
                        case "aborted": return "Calibration Aborted"
                        default:        return ""
                        }
                    }
                }

                // 1 Hz tick driving the elapsed counter while running.
                Timer {
                    id: calibTimer
                    property int elapsedSec: 0
                    interval: 1000
                    repeat: true
                    running: MOTIONInterface.calibrationRunning
                    onTriggered: elapsedSec += 1
                }

                Connections {
                    target: MOTIONInterface
                    function onCalibrationStateChanged() {
                        if (MOTIONInterface.calibrationStatus === "running") {
                            calibTimer.elapsedSec = 0
                        }
                    }
                }

                Item { Layout.fillWidth: true }   // push everything left
            }
        }

        // (Reserved for future settings sections)
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: theme.bgContainer
            radius: 10
            border.color: theme.borderSubtle
            border.width: 2

            Text {
                text: "More settings coming soon"
                font.pixelSize: 14
                color: theme.textSecondary
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                anchors.centerIn: parent
            }
        }
    }
}
