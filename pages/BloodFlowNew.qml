import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

import "../components"
import "./scan"

Rectangle {
    id: bloodFlow
    width: parent.width
    height: parent.height
    color: "#1C1C1E"
    radius: 0

    property bool realtimePlotEnabled: (AppFlags && AppFlags.realtimePlotEnabled) ? AppFlags.realtimePlotEnabled : false
    property bool scanning: false
    property bool camerasReady: true  // starts true, goes false when camera selection changes
    property bool configuring: false  // true during camera flash

    // Camera masks (updated by camera selection modal)
    property int leftMask: 0x99   // default "Outer"
    property int rightMask: 0x00

    // Duration from scan time modal
    property bool freeRun: false
    property int durationSec: 3600  // default 1 hour

    // Convert mask to active array for camera selection modal
    function maskToArray(mask) {
        const bitMap = [7, 6, 5, 4, 3, 2, 1, 0];
        var arr = [false, false, false, false, false, false, false, false];
        for (var i = 0; i < 8; i++) {
            if (mask & (1 << bitMap[i])) arr[i] = true;
        }
        return arr;
    }

    // Apply default cameras from config
    function applyDefaultCameras() {
        var defaultIdx = (AppFlags && AppFlags.defaultCameraIndex !== undefined)
            ? AppFlags.defaultCameraIndex : 4;  // "Outer"
        var mask = patternToMask(defaultIdx);
        // Apply default to whichever sensors are connected
        if (MOTIONInterface.leftSensorConnected)  leftMask  = mask;
        if (MOTIONInterface.rightSensorConnected) rightMask = mask;
        // If sensors connected, start flash immediately
        if (MOTIONInterface.leftSensorConnected || MOTIONInterface.rightSensorConnected) {
            flashDefaultCameras();
        }
    }

    function patternToMask(index) {
        switch(index) {
            case 0: return 0x00;
            case 1: return 0x5A;
            case 2: return 0x66;
            case 3: return 0xA5;
            case 4: return 0x99;
            case 5: return 0x0F;
            case 6: return 0xF0;
            case 7: return 0x42;
            case 8: return 0xFF;
            default: return 0x99;
        }
    }

    function flashDefaultCameras() {
        if (configuring || scanning) return;
        camerasReady = false;
        configuring = true;
        console.log("Auto-flashing cameras: left=0x" + leftMask.toString(16) + " right=0x" + rightMask.toString(16));
        MOTIONInterface.startConfigureCameraSensors(leftMask, rightMask);
    }

    // LAYOUT: full-height column → session header bar + (ButtonPanel | DataViewer)
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 8
        spacing: 6

        // ── Session ID header bar ─────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 34
            color: "#252528"
            radius: 8
            border.color: "#3E4E6F"
            border.width: 1

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                spacing: 10

                Text {
                    text: "OpenMotion BloodFlow"
                    color: "#FFFFFF"
                    font.pixelSize: 14
                    font.weight: Font.Bold
                }

                Rectangle { width: 1; height: 18; color: "#3E4E6F" }

                Text { text: "Session:"; color: "#7F8C8D"; font.pixelSize: 13 }
                Text {
                    text: MOTIONInterface.sessionId || "—"
                    color: "#4A90E2"
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                }

                Item { Layout.fillWidth: true }

            }
        }

        // ── ButtonPanel | DataViewer ──────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 8

            ButtonPanel {
                id: buttonPanel
                Layout.fillHeight: true
                Layout.preferredWidth: 80
                scanning: bloodFlow.scanning
                waiting: bloodFlow.configuring
                camerasReady: bloodFlow.camerasReady && !bloodFlow.configuring

                onStartStopClicked: {
                    if (bloodFlow.scanning) {
                        scanRunner.cancel()
                    } else {
                        if (!MOTIONInterface.sessionId || MOTIONInterface.sessionId.length === 0) {
                            sessionModal.open()
                            return
                        }
                        bloodFlow.scanning = true
                        scanDialog.message = "Scanning..."
                        scanDialog.stageText = "Preparing..."
                        scanDialog.progress = 1
                        embeddedPlot.startScan(bloodFlow.leftMask, bloodFlow.rightMask)
                        scanRunner.start()
                    }
                }

                onScanSettingsClicked: {
                    scanSettingsModal.setInitialSelection(
                        maskToArray(leftMask),
                        maskToArray(rightMask)
                    )
                    scanSettingsModal.open()
                }
                onSessionClicked: sessionModal.open()
                onHistoryClicked: historyModal.open()
                onLogClicked: scanDialog.open()
                onSettingsClicked: settingsModal.open()
            }

            EmbeddedRealtimePlot {
                id: embeddedPlot
                Layout.fillWidth: true
                Layout.fillHeight: true
                showBfiBvi: settingsModal.showBfiBvi
                previewLeftMask: bloodFlow.leftMask
                previewRightMask: bloodFlow.rightMask
            }
        }
    }

    // ===== MODALS =====
    ScanSettingsModal {
        id: scanSettingsModal
        onSelectionChanged: function(newLeftMask, newRightMask) {
            bloodFlow.freeRun = scanSettingsModal.freeRun
            bloodFlow.durationSec = scanSettingsModal.freeRun ? 43200 : scanSettingsModal.durationSec
            if (newLeftMask !== bloodFlow.leftMask || newRightMask !== bloodFlow.rightMask) {
                bloodFlow.leftMask = newLeftMask
                bloodFlow.rightMask = newRightMask
                if (!scanning) {
                    flashDefaultCameras()
                }
            }
        }
    }

    SessionModal {
        id: sessionModal
    }

    HistoryModal {
        id: historyModal
    }

    SettingsModal {
        id: settingsModal
        onSettingsChanged: {
            // Apply default camera config if changed
            var newMask = patternToMask(settingsModal.defaultCameraIndex)
            if (newMask !== bloodFlow.leftMask && !scanning) {
                bloodFlow.leftMask = newMask
                flashDefaultCameras()
            }
        }
    }

    ScanProgressDialog {
        id: scanDialog
    }

    // ===== SCAN RUNNER =====
    ScanRunner {
        id: scanRunner
        connector: MOTIONInterface
        leftMask: bloodFlow.leftMask
        rightMask: bloodFlow.rightMask
        durationSec: bloodFlow.durationSec
        subjectId: MOTIONInterface.sessionId
        dataDir: MOTIONInterface.directory
        disableLaser: false
        laserOn: true
        laserPower: 50
        triggerConfig: (typeof appTriggerConfig !== "undefined") ? appTriggerConfig : ({
            "TriggerFrequencyHz": 40,
            "TriggerPulseWidthUsec": 500,
            "LaserPulseDelayUsec": 100,
            "LaserPulseWidthUsec": 500,
            "LaserPulseSkipInterval": 600,
            "LaserPulseSkipDelayUsec": 1800,
            "EnableSyncOut": true,
            "EnableTaTrigger": true
        })

        onStageUpdate: function(txt) {
            scanDialog.stageText = txt
        }
        onProgressUpdate: function(pct) {
            scanDialog.progress = pct
        }
        onMessageOut: function(line) {
            scanDialog.appendLog(line)
            console.log("Scan message: " + line)
        }
        onScanFinished: function(ok, err, left, right) {
            bloodFlow.scanning = false

            if (err === "Canceled") {
                scanDialog.close()
                embeddedPlot.stopScan()
                return
            }

            if (!ok) {
                scanDialog.appendLog("ERROR: " + err)
                scanDialog.stageText = "Error during capture"
                scanDialog.done = true
                embeddedPlot.stopScan()
                return
            }

            scanDialog.stageText = "Capture complete"
            scanDialog.progress = 100
            scanDialog.done = true
            embeddedPlot.stopScan()
        }
    }

    // ===== CONNECTIONS =====
    Connections {
        target: MOTIONInterface

        function onCaptureLog(line) {
            console.log("Capture log: " + line)
        }

        function onSignalConnected(descriptor, port) {
            console.log(descriptor + " connected on " + port)
            // Auto-flash default cameras when sensors connect
            if ((descriptor || "").toUpperCase().indexOf("SENSOR") >= 0) {
                Qt.callLater(function() {
                    if (!bloodFlow.scanning && !bloodFlow.configuring) {
                        // Apply default mask to the newly connected sensor
                        var defaultIdx = (AppFlags && AppFlags.defaultCameraIndex !== undefined)
                            ? AppFlags.defaultCameraIndex : 4;
                        var mask = patternToMask(defaultIdx);
                        if (MOTIONInterface.leftSensorConnected)  bloodFlow.leftMask  = mask;
                        if (MOTIONInterface.rightSensorConnected) bloodFlow.rightMask = mask;
                        flashDefaultCameras()
                    }
                })
            }
        }

        function onSignalDisconnected(descriptor, port) {
            console.log(descriptor + " disconnected from " + port)
        }

        function onConnectionStatusChanged() {
            // Reset camera ready state if sensors disconnect
            if (!MOTIONInterface.leftSensorConnected && !MOTIONInterface.rightSensorConnected) {
                bloodFlow.camerasReady = false
            }
        }

        function onConfigFinished(ok, err) {
            bloodFlow.configuring = false
            bloodFlow.camerasReady = true  // always unblock; allConnected is the real gate
            if (ok) {
                console.log("Camera configuration complete")
            } else {
                console.log("Camera configuration failed: " + err)
            }
        }

        function onLaserStateChanged() {}
        function onSafetyFailureStateChanged() {}
    }

    Component.onCompleted: {
        applyDefaultCameras()
    }

    Component.onDestruction: {
        console.log("Closing UI, clearing MOTIONInterface...")
    }
}
