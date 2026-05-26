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

    // Aggregate live state of the contact-quality runners so main.qml
    // can detect 'check in progress' for the close-while-busy warning
    // (issue #75). Either runner being non-idle counts as busy.
    readonly property bool checkRunning: qualityCheckRunner.running

    // Hand main.qml the same ModalManager BloodFlow's icon-bar uses,
    // so the close-while-busy handler can dismiss any open modal
    // (saving its state via the modal's close() function) before the
    // app tears down.
    readonly property alias modalManager: modalManager

    // FDA mode (read from app config). Forces Far camera pattern + free run,
    // hides scan-settings button, and swaps in the FDA plot view.
    property bool reducedMode: MOTIONInterface.appConfig.reducedMode === true
    // In reduced mode, Start first runs a contact-quality preflight check.
    property bool reducedStartPending: false
    // Prevent late CQ callbacks from re-opening the modal while a stop/cancel
    // is in flight.
    property bool suppressLiveCqModal: false

    // Camera masks (updated by camera selection modal)
    property int leftMask: 0x99   // default "Outer"
    property int rightMask: 0x00

    // Track whether the cfg-default mask has been applied this session, per
    // side. Once true, sensor reconnects (e.g. console power cycle) leave
    // the current mask alone instead of clobbering the user's Scan Settings
    // choice with cfg.leftMask / cfg.rightMask (issue #127). Set true when
    // defaults are applied OR the user picks a mask in ScanSettingsModal.
    property bool _leftMaskInitialApplied: false
    property bool _rightMaskInitialApplied: false

    property string sessionId: MOTIONInterface.userLabel || ""

    // Duration from scan time modal
    property bool freeRun: reducedMode
    property int durationSec: reducedMode ? 43200 : 3600  // 12h in FDA mode, 1h default

    onReducedModeChanged: {
        if (reducedMode) {
            freeRun = true
            durationSec = 43200
            var _cfg = MOTIONInterface.appConfig;
            leftMask  = _cfg.reducedModeLeftMask  !== undefined ? _cfg.reducedModeLeftMask  : 0xC3
            rightMask = _cfg.reducedModeRightMask !== undefined ? _cfg.reducedModeRightMask : 0xC3
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
        var defLeft  = reducedMode ? (cfg.reducedModeLeftMask  !== undefined ? cfg.reducedModeLeftMask  : 0xC3) : (cfg.leftMask  !== undefined ? cfg.leftMask  : 0x99);
        var defRight = reducedMode ? (cfg.reducedModeRightMask !== undefined ? cfg.reducedModeRightMask : 0xC3) : (cfg.rightMask !== undefined ? cfg.rightMask : 0x99);
        if (MOTIONInterface.leftSensorConnected && !_leftMaskInitialApplied) {
            leftMask = defLeft;
            _leftMaskInitialApplied = true;
        }
        if (MOTIONInterface.rightSensorConnected && !_rightMaskInitialApplied) {
            rightMask = defRight;
            _rightMaskInitialApplied = true;
        }
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

    function beginScanNow() {
        bloodFlow.scanning = true
        bloodFlow.suppressLiveCqModal = false
        reducedStartPending = false
        // Drop any stale CQ warning entries from a previous scan/check.
        // The connector creates a fresh _ContactQualityState per scan, so
        // it never re-emits "cleared" for a camera that wasn't latched in
        // the new scan — without this reset, the modal would keep showing
        // an orange dot from the prior scan.
        contactQualityModal.entries = []
        scanDialog.message = "Scanning..."
        scanDialog.stageText = "Preparing..."
        scanDialog.progress = 1
        if (bloodFlow.reducedMode) reducedPlot.startScan()
        else                        embeddedPlot.startScan(bloodFlow.leftMask, bloodFlow.rightMask)
        scanRunner.start()
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

        // Action buttons — close any open modal first (which by
        // convention saves), then perform the action. If the open
        // modal is non-dismissable (e.g. ContactQualityModal during
        // an in-flight check), modalManager.closeCurrent() is a
        // no-op and the action below still runs.
        onStartStopClicked: {
            modalManager.closeCurrent()
            if (bloodFlow.scanning) {
                scanRunner.cancel()
                scanDialog.close()
                if (bloodFlow.reducedMode) reducedPlot.stopScan()
                else                   embeddedPlot.stopScan()
                // Notes modal opens via MOTIONInterface.scanNotesReady
                // after the SDK actually unwinds and the duration line
                // has been appended to scanNotes. Opening it here would
                // race the append and pop an empty modal.
            } else {
                if (bloodFlow.reducedMode) {
                    reducedStartPending = true
                    contactQualityModal.preScanMode = true
                    contactQualityModal.reset(true, 0)
                    qualityCheckRunner.start()
                } else {
                    beginScanNow()
                }
            }
        }
        onCheckClicked: {
            modalManager.closeCurrent()
            contactQualityModal.reset(false, 0)
            qualityCheckRunner.start()
        }

        // Toggle buttons — open the named modal, or close it if it's
        // already open. modalManager.toggle() handles closing whatever
        // else might be on screen first.
        onScanSettingsClicked: {
            if (!scanSettingsModal.visible) {
                scanSettingsModal.setInitialSelection(
                    maskToArray(leftMask),
                    maskToArray(rightMask)
                )
            }
            modalManager.toggle(scanSettingsModal)
        }
        onNotesClicked:    modalManager.toggle(notesModal)
        onHistoryClicked:  modalManager.toggle(historyModal)
        onLogClicked:      modalManager.toggle(scanDialog)
        onSettingsClicked: modalManager.toggle(settingsModal)
    }

    // Single source of truth for which modal is on screen. See
    // ModalManager.qml for semantics. The list must include every
    // modal that should participate in click-outside / icon-bar
    // close behavior; ContactQualityModal opts out of dismissal
    // dynamically via its `dismissable` property.
    ModalManager {
        id: modalManager
        modals: [scanSettingsModal, notesModal, historyModal,
                 settingsModal, contactQualityModal, scanDialog]
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

    // ===== MODALS =====
    ScanSettingsModal {
        id: scanSettingsModal
        onSelectionChanged: function(newLeftMask, newRightMask) {
            bloodFlow.freeRun = scanSettingsModal.freeRun
            var dur = scanSettingsModal.freeRun ? 43200 : scanSettingsModal.durationSec
            if (dur <= 0) dur = 3600
            bloodFlow.durationSec = dur
            bloodFlow.leftMask = newLeftMask
            bloodFlow.rightMask = newRightMask
            // A user-driven selection counts as "initial applied" so that
            // a later sensor reconnect cannot overwrite it with the cfg
            // default (issue #127). Covers the edge case where the user
            // edits Scan Settings before sensors first enumerate.
            bloodFlow._leftMaskInitialApplied = true
            bloodFlow._rightMaskInitialApplied = true
        }
    }

    NotesModal {
        id: notesModal
    }

    // Open the notes modal exactly when the connector signals that
    // scanNotes has been finalized for the just-completed scan (duration
    // line appended, notes.txt written). This is the only path that
    // guarantees notesArea.text snapshots the post-scan content; opening
    // the modal from onStartStopClicked or scanFinished races the
    // append because scanRunner.scanFinished fires synchronously from
    // cancel(), before the SDK has unwound and _on_complete has run.
    Connections {
        target: MOTIONInterface
        function onScanNotesReady() { notesModal.open() }
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
        // Pre-scan check always evaluates all physically-present cameras,
        // regardless of the active scan mask.
        leftMask: bloodFlow.reducedStartPending
                  ? (MOTIONInterface.leftSensorConnected ? 0xFF : 0x00)
                  : bloodFlow.leftMask
        rightMask: bloodFlow.reducedStartPending
                   ? (MOTIONInterface.rightSensorConnected ? 0xFF : 0x00)
                   : bloodFlow.rightMask
        onStopScanRequested: {
            bloodFlow.suppressLiveCqModal = true
            contactQualityModal.close()
            if (bloodFlow.scanning) {
                // Route through ScanRunner so the normal "Canceled" flow
                // runs and Notes opens consistently.
                scanRunner.cancel()
            } else {
                MOTIONInterface.stopCapture()
            }
            reducedStartPending = false
        }
        onContinueRequested: {
            if (bloodFlow.reducedStartPending) {
                contactQualityModal.close()
                beginScanNow()
            }
            // Otherwise (live-scan warning modal), Continue just dismisses.
        }
        onRetestRequested: {
            contactQualityModal.preScanMode = bloodFlow.reducedStartPending
            contactQualityModal.reset(bloodFlow.reducedStartPending, 0)
            qualityCheckRunner.start()
        }
        onDismissed: {
            if (!bloodFlow.scanning)
                reducedStartPending = false
            if (!bloodFlow.reducedStartPending)
                contactQualityModal.preScanMode = false
        }
    }

    ScanProgressDialog {
        id: scanDialog
    }

    // ===== SCAN RUNNER (capture mode) =====
    ScanRunner {
        id: scanRunner
        mode: "capture"
        connector: MOTIONInterface
        leftMask: bloodFlow.leftMask
        rightMask: bloodFlow.rightMask
        durationSec: bloodFlow.durationSec
        subjectId: MOTIONInterface.userLabel
        dataDir: MOTIONInterface.directory
        disableLaser: false
        laserOn: true
        laserPower: 50
        // triggerConfig left at the SetTriggerLaserTask default ({}) so
        // the connector's setTrigger merges only TriggerStatus over the
        // SDK-resolved default trigger config. Local overrides go in
        // app_config.json's triggerConfig key (passed through to
        // MotionInterface(default_trigger_config=...) at startup).
        triggerConfig: (typeof appTriggerConfig !== "undefined") ? appTriggerConfig : ({})

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
            bloodFlow.suppressLiveCqModal = false

            if (err === "Canceled") {
                scanDialog.close()
                if (bloodFlow.reducedMode) reducedPlot.stopScan(); else embeddedPlot.stopScan()
                // Notes modal opens via MOTIONInterface.scanNotesReady.
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
            // Notes modal opens via MOTIONInterface.scanNotesReady.
        }
    }

    // ===== SCAN RUNNER (check mode) =====
    // Shares flash + trigger/laser plumbing with scanRunner; final stage is
    // the contact-quality check instead of capture. Always flashes 0xFF so
    // every physically-present camera participates — absent cameras are
    // skipped by the configure workflow.
    ScanRunner {
        id: qualityCheckRunner
        mode: "check"
        connector: MOTIONInterface
        leftMask: MOTIONInterface.leftSensorConnected  ? 0xFF : 0x00
        rightMask: MOTIONInterface.rightSensorConnected ? 0xFF : 0x00
        laserOn: true
        laserPower: 50
        // See note on the scanRunner triggerConfig above — same here.
        triggerConfig: (typeof appTriggerConfig !== "undefined") ? appTriggerConfig : ({})

        onStageUpdate: function(txt) {
            console.log("ContactQuality: " + txt)
        }
        onMessageOut: function(line) { console.log("ContactQuality: " + line) }
        onScanFinished: function(ok, err, left, right) {
            // Flash/trigger stage failures surface here; the final "check"
            // stage forwards its own result via contactQualityCheckFinished
            // (consumed by the modal's Connections block), so skip here to
            // avoid double-reporting.
            if (!ok && qualityCheckRunner._stage !== "check") {
                contactQualityModal.showError(err || "Check pipeline failed")
            }
        }
    }

    // ===== CONNECTIONS =====
    Connections {
        target: MOTIONInterface

        function onSignalConnected(descriptor, port) {
            // Auto-flash default cameras when sensors connect.
            // Descriptor is the handle name from the SDK ("console" / "left" / "right").
            // The SDK already logs the state transition at INFO; no need
            // to duplicate it from QML.
            if (descriptor === "left" || descriptor === "right") {
                Qt.callLater(function() {
                    if (!bloodFlow.scanning && !bloodFlow.configuring) {
                        var cfg      = MOTIONInterface.appConfig;
                        var defLeft  = bloodFlow.reducedMode ? (cfg.reducedModeLeftMask  !== undefined ? cfg.reducedModeLeftMask  : 0xC3) : (cfg.leftMask  !== undefined ? cfg.leftMask  : 0x99);
                        var defRight = bloodFlow.reducedMode ? (cfg.reducedModeRightMask !== undefined ? cfg.reducedModeRightMask : 0xC3) : (cfg.rightMask !== undefined ? cfg.rightMask : 0x99);
                        // First connect this session per side: apply cfg
                        // default. Subsequent reconnects (e.g. console power
                        // cycle) preserve whatever's already in *Mask —
                        // either the cfg default already adopted earlier or
                        // the user's Scan Settings choice (issue #127). The
                        // re-flash below still runs because the FPGA loses
                        // its camera-enable state on power cycle.
                        if (MOTIONInterface.leftSensorConnected && !bloodFlow._leftMaskInitialApplied) {
                            bloodFlow.leftMask  = defLeft;
                            bloodFlow._leftMaskInitialApplied = true;
                        }
                        if (MOTIONInterface.rightSensorConnected && !bloodFlow._rightMaskInitialApplied) {
                            bloodFlow.rightMask = defRight;
                            bloodFlow._rightMaskInitialApplied = true;
                        }
                        if (cfg.autoConfigureOnStartup !== false)
                            flashDefaultCameras()
                    }
                })
            }
        }

        function onSignalDisconnected(descriptor, port) {
            // SDK already logs the state transition; no QML log needed.
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
            contactQualityModal.preScanMode = bloodFlow.reducedStartPending
            contactQualityModal.reset(bloodFlow.reducedStartPending, seconds)
        }
        function onContactQualityCheckFinished(ok, error, warnings) {
            if (bloodFlow.reducedStartPending) {
                // Reduced-mode preflight: always land in live-style footer so
                // user can explicitly Continue into the main scan.
                contactQualityModal.liveScan = true
            }
            if (warnings.length > 0) {
                for (var i = 0; i < warnings.length; ++i) {
                    var w = warnings[i]
                    contactQualityModal.addWarning(w.camera, w.typeKey, w.typeText, w.value)
                }
                return
            }
            if (!ok) {
                var msg = (error && error.length > 0) ? error : "Quick check failed"
                contactQualityModal.showError(msg)
                return
            }
            contactQualityModal.showOk()
            if (bloodFlow.reducedStartPending)
                contactQualityModal.liveScanDismissable = true
        }
        // Live-scan warnings (ContactQualityMonitor via SciencePipeline)
        function onContactQualityWarning(camera, typeKey, typeText, value) {
            if (bloodFlow.suppressLiveCqModal || !bloodFlow.scanning)
                return
            if (contactQualityModal.state_ === "checking" || !contactQualityModal.visible) {
                contactQualityModal.reset(true)
            } else {
                contactQualityModal.liveScan = true
            }
            contactQualityModal.addWarning(camera, typeKey, typeText, value)
        }

        function onContactQualityIssueStateChanged(camera, typeKey, typeText, value, active) {
            if (bloodFlow.suppressLiveCqModal || !bloodFlow.scanning)
                return
            if (!active)
                contactQualityModal.clearWarning(camera, typeKey)
        }
    }

    Component.onCompleted: {
        if (reducedMode) {
            freeRun = true
            durationSec = 43200
            var _cfg = MOTIONInterface.appConfig;
            leftMask  = _cfg.reducedModeLeftMask  !== undefined ? _cfg.reducedModeLeftMask  : 0xC3
            rightMask = _cfg.reducedModeRightMask !== undefined ? _cfg.reducedModeRightMask : 0xC3
        }
        applyDefaultCameras()
    }

    Component.onDestruction: {
        console.log("Closing UI, clearing MOTIONInterface...")
    }
}
