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
    color: theme.bgBase
    radius: 0

    AppTheme { id: theme }

    property bool scanning: false
    property bool camerasReady: true  // starts true, goes false when camera selection changes
    property bool configuring: false  // true during camera flash
    property bool _pendingContactCheck: false

    // FDA mode (read from app config). Forces Middle camera pattern + free run,
    // hides scan-settings button, and swaps in the FDA plot view.
    property bool reducedMode: MOTIONInterface.appConfig.reducedMode === true

    // Camera masks (updated by camera selection modal)
    property int leftMask: 0x99   // default "Outer"
    property int rightMask: 0x00

    property string sessionId: MOTIONInterface.userLabel || ""

    // Duration from scan time modal
    property bool freeRun: reducedMode
    property int durationSec: reducedMode ? 43200 : 3600  // 12h in FDA mode, 1h default

    onReducedModeChanged: {
        if (reducedMode) {
            freeRun = true
            durationSec = 43200
            leftMask = 0x66
            rightMask = 0x66
        }
    }
    property int elapsedSec: 0

    // The elapsed-time ticker runs exactly while the MCU trigger is firing.
    // Using a declarative `running:` binding means the timer auto-stops the
    // instant triggerState flips to "OFF" (top of the SDK teardown, right
    // after stop_trigger) rather than when captureFinished arrives 2-4s later
    // after all the camera-disable / USB-drain / writer-join work is done.
    Timer {
        id: scanTimer
        interval: 1000
        repeat: true
        running: bloodFlow.scanning && MOTIONInterface.triggerState === "ON"
        onTriggered: bloodFlow.elapsedSec += 1
    }

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
        var cfg      = MOTIONInterface.appConfig;
        var defLeft  = reducedMode ? 0x66 : (cfg.leftMask  !== undefined ? cfg.leftMask  : 0x99);
        var defRight = reducedMode ? 0x66 : (cfg.rightMask !== undefined ? cfg.rightMask : 0x99);
        if (MOTIONInterface.leftSensorConnected)  leftMask  = defLeft;
        if (MOTIONInterface.rightSensorConnected) rightMask = defRight;
        if (cfg.autoConfigureOnStartup !== false &&
                (MOTIONInterface.leftSensorConnected || MOTIONInterface.rightSensorConnected)) {
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

    // ButtonPanel — sits above modal backdrops so it's always clickable
    ButtonPanel {
        id: buttonPanel
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.margins: 8
        width: 80
        z: 10000

        scanning: bloodFlow.scanning
        waiting: bloodFlow.configuring
        camerasReady: bloodFlow.camerasReady && !bloodFlow.configuring
        reducedMode: bloodFlow.reducedMode

        onStartStopClicked: {
            if (bloodFlow.scanning) {
                scanRunner.cancel()
                scanDialog.close()
                if (bloodFlow.reducedMode) reducedPlot.stopScan()
                else                   embeddedPlot.stopScan()
                notesModal.open()
            } else {
                bloodFlow.scanning = true
                scanDialog.message = "Scanning..."
                scanDialog.stageText = "Preparing..."
                scanDialog.progress = 1
                if (bloodFlow.reducedMode) reducedPlot.startScan()
                else                   embeddedPlot.startScan(bloodFlow.leftMask, bloodFlow.rightMask)
                scanRunner.start()
            }
        }

        onScanSettingsClicked: {
            var wasOpen = scanSettingsModal.visible
            closeAllModals()
            if (!wasOpen) {
                scanSettingsModal.setInitialSelection(
                    maskToArray(leftMask),
                    maskToArray(rightMask)
                )
                scanSettingsModal.open()
            }
        }
        onNotesClicked:    { var o = notesModal.visible;    closeAllModals(); if (!o) notesModal.open() }
        onCheckClicked:    {
            contactQualityModal.reset(false, 0)
            bloodFlow._pendingContactCheck = true
            bloodFlow.configuring = true
            var cqLeft  = MOTIONInterface.leftSensorConnected  ? 0xFF : 0x00
            var cqRight = MOTIONInterface.rightSensorConnected ? 0xFF : 0x00
            MOTIONInterface.startConfigureCameraSensors(cqLeft, cqRight)
        }
        onHistoryClicked:  { var o = historyModal.visible;  closeAllModals(); if (!o) historyModal.open() }
        onLogClicked:      { var o = scanDialog.visible;    closeAllModals(); if (!o) scanDialog.open() }
        onSettingsClicked: { var o = settingsModal.visible; closeAllModals(); if (!o) settingsModal.open() }
    }

    // Data viewer — fills remaining space to the right of ButtonPanel
    EmbeddedRealtimePlot {
        id: embeddedPlot
        visible: !bloodFlow.reducedMode
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.left: buttonPanel.right
        anchors.right: parent.right
        anchors.margins: 8
        anchors.leftMargin: 16

        showBfiBvi:  settingsModal.showBfiBvi
        windowSeconds: settingsModal.plotWindowSec
        bfiColor: settingsModal.bfiColor
        bviColor: settingsModal.bviColor
        bviLowPassEnabled:  settingsModal.bviLowPassEnabled
        bviLowPassCutoffHz: settingsModal.bviLowPassCutoffHz
        bfiClampLow:  MOTIONInterface.appConfig.bfiClampLow  !== undefined ? MOTIONInterface.appConfig.bfiClampLow  : 0.0
        bfiClampHigh: MOTIONInterface.appConfig.bfiClampHigh !== undefined ? MOTIONInterface.appConfig.bfiClampHigh : 10.0
        bviClampLow:  MOTIONInterface.appConfig.bviClampLow  !== undefined ? MOTIONInterface.appConfig.bviClampLow  : 0.0
        bviClampHigh: MOTIONInterface.appConfig.bviClampHigh !== undefined ? MOTIONInterface.appConfig.bviClampHigh : 10.0
        autoScale:        settingsModal.autoScale
        autoScalePerPlot: settingsModal.autoScalePerPlot
        bfiMin:      settingsModal.bfiMin
        bfiMax:      settingsModal.bfiMax
        bviMin:      settingsModal.bviMin
        bviMax:      settingsModal.bviMax
        meanMin:     settingsModal.meanMin
        meanMax:     settingsModal.meanMax
        contrastMin: settingsModal.contrastMin
        contrastMax: settingsModal.contrastMax
        previewLeftMask:  bloodFlow.leftMask
        previewRightMask: bloodFlow.rightMask
    }

    // FDA-mode data viewer — two big aggregated plots
    ReducedPlotView {
        id: reducedPlot
        visible: bloodFlow.reducedMode
        windowSeconds: settingsModal.plotWindowSec
        bfiColor: settingsModal.bfiColor
        bviColor: settingsModal.bviColor
        bviLowPassEnabled:  settingsModal.bviLowPassEnabled
        bviLowPassCutoffHz: settingsModal.bviLowPassCutoffHz
        bfiClampLow:  MOTIONInterface.appConfig.bfiClampLow  !== undefined ? MOTIONInterface.appConfig.bfiClampLow  : 0.0
        bfiClampHigh: MOTIONInterface.appConfig.bfiClampHigh !== undefined ? MOTIONInterface.appConfig.bfiClampHigh : 10.0
        bviClampLow:  MOTIONInterface.appConfig.bviClampLow  !== undefined ? MOTIONInterface.appConfig.bviClampLow  : 0.0
        bviClampHigh: MOTIONInterface.appConfig.bviClampHigh !== undefined ? MOTIONInterface.appConfig.bviClampHigh : 10.0
        autoScale: settingsModal.autoScale
        bfiMin: settingsModal.bfiMin
        bfiMax: settingsModal.bfiMax
        bviMin: settingsModal.bviMin
        bviMax: settingsModal.bviMax
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.left: buttonPanel.right
        anchors.right: parent.right
        anchors.margins: 8
        anchors.leftMargin: 16
    }

    function closeAllModals() {
        if (scanSettingsModal.visible) scanSettingsModal.close()
        if (notesModal.visible)        notesModal.close()
        if (historyModal.visible)      historyModal.close()
        if (settingsModal.visible)     settingsModal.close()
        if (scanDialog.visible)        scanDialog.close()
    }

    // ===== MODALS =====
    ScanSettingsModal {
        id: scanSettingsModal
        onSelectionChanged: function(newLeftMask, newRightMask) {
            bloodFlow.freeRun = scanSettingsModal.freeRun
            bloodFlow.durationSec = scanSettingsModal.freeRun ? 43200 : scanSettingsModal.durationSec
            bloodFlow.leftMask = newLeftMask
            bloodFlow.rightMask = newRightMask
        }
    }

    NotesModal {
        id: notesModal
    }

    HistoryModal {
        id: historyModal
    }

    SettingsModal {
        id: settingsModal
    }

    ContactQualityModal {
        id: contactQualityModal
        anchors.fill: parent
        onStopScanRequested: MOTIONInterface.stopCapture()
        onContinueRequested: { /* no-op: leave scan running */ }
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
        subjectId: MOTIONInterface.userLabel
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
            if (scanRunner._stage === "capture") {
                bloodFlow.elapsedSec = 0
                // scanTimer is started declaratively by its `running:` binding
                // (bloodFlow.scanning && triggerState === "ON") — no imperative
                // start() needed here.
            }
        }
        onProgressUpdate: function(pct) {
            scanDialog.progress = pct
        }
        onMessageOut: function(line) {
            scanDialog.appendLog(line)
            console.log(line)
        }
        onScanFinished: function(ok, err, left, right) {
            // scanTimer stops automatically via its `running:` binding once
            // bloodFlow.scanning flips false or triggerState goes "OFF".
            bloodFlow.scanning = false

            if (err === "Canceled") {
                scanDialog.close()
                if (bloodFlow.reducedMode) reducedPlot.stopScan(); else embeddedPlot.stopScan()
                notesModal.open()
                return
            }

            if (!ok) {
                scanDialog.appendLog("ERROR: " + err)
                scanDialog.stageText = "Error during capture"
                scanDialog.done = true
                if (bloodFlow.reducedMode) reducedPlot.stopScan(); else embeddedPlot.stopScan()
                return
            }

            scanDialog.stageText = "Capture complete"
            scanDialog.progress = 100
            scanDialog.done = true
            if (bloodFlow.reducedMode) reducedPlot.stopScan(); else embeddedPlot.stopScan()
            notesModal.open()
        }
    }

    // ===== CONNECTIONS =====
    Connections {
        target: MOTIONInterface

        function onSignalConnected(descriptor, port) {
            console.log(descriptor + " connected on " + port)
            // Auto-flash default cameras when sensors connect
            if ((descriptor || "").toUpperCase().indexOf("SENSOR") >= 0) {
                Qt.callLater(function() {
                    if (!bloodFlow.scanning && !bloodFlow.configuring) {
                        var cfg      = MOTIONInterface.appConfig;
                        var defLeft  = bloodFlow.reducedMode ? 0x66 : (cfg.leftMask  !== undefined ? cfg.leftMask  : 0x99);
                        var defRight = bloodFlow.reducedMode ? 0x66 : (cfg.rightMask !== undefined ? cfg.rightMask : 0x99);
                        if (MOTIONInterface.leftSensorConnected)  bloodFlow.leftMask  = defLeft;
                        if (MOTIONInterface.rightSensorConnected) bloodFlow.rightMask = defRight;
                        if (cfg.autoConfigureOnStartup !== false)
                            flashDefaultCameras()
                    }
                })
            }
        }

        function onSignalDisconnected(descriptor, port) {
            console.log(descriptor + " disconnected from " + port)
        }

        function onConnectionStatusChanged() {
            if (!MOTIONInterface.leftSensorConnected && !MOTIONInterface.rightSensorConnected) {
                bloodFlow.camerasReady = false
            } else if (MOTIONInterface.leftSensorConnected || MOTIONInterface.rightSensorConnected) {
                bloodFlow.camerasReady = true
            }
        }

        function onConfigFinished(ok, err) {
            bloodFlow.configuring = false
            bloodFlow.camerasReady = true  // always unblock; allConnected is the real gate
            if (bloodFlow._pendingContactCheck) {
                bloodFlow._pendingContactCheck = false
                if (ok) {
                    MOTIONInterface.runContactQualityCheck()
                } else {
                    contactQualityModal.showError("Camera configuration failed: " + err)
                }
                return
            }
            if (ok) {
                console.log("Camera configuration complete")
            } else {
                console.log("Camera configuration failed: " + err)
            }
        }

        function onLaserStateChanged() {}
        function onSafetyFailureStateChanged() {}

        // Contact-quality quick-check lifecycle
        function onContactQualityCheckStarted(seconds) {
            contactQualityModal.reset(false, seconds)
        }
        function onContactQualityCheckFinished(ok, error, warnings) {
            if (!ok) {
                var msg = (error && error.length > 0) ? error : "Quick check failed"
                contactQualityModal.showError(msg)
                return
            }
            if (warnings.length === 0) { contactQualityModal.showOk(); return }
            for (var i = 0; i < warnings.length; ++i) {
                var w = warnings[i]
                contactQualityModal.addWarning(w.camera, w.typeText, w.value)
            }
        }
        // Live-scan warnings (ContactQualityMonitor via SciencePipeline)
        function onContactQualityWarning(camera, typeKey, typeText, value) {
            if (contactQualityModal.state_ === "checking" || !contactQualityModal.visible) {
                contactQualityModal.reset(true)
            } else {
                contactQualityModal.liveScan = true
            }
            contactQualityModal.addWarning(camera, typeText, value)
        }
    }

    Component.onCompleted: {
        if (reducedMode) {
            freeRun = true
            durationSec = 43200
            leftMask = 0x66
            rightMask = 0x66
        }
        applyDefaultCameras()
    }

    Component.onDestruction: {
        console.log("Closing UI, clearing MOTIONInterface...")
    }
}
