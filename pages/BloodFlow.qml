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
    color: AppTheme.bgBase
    radius: 0


    property bool scanning: false
    property bool camerasReady: true  // gates Start/Check; false only while no sensor is connected

    // Aggregate live state of the contact-quality runners so main.qml
    // can detect 'check in progress' for the close-while-busy warning
    // (issue #75). Either runner being non-idle counts as busy.
    // Includes a gate-pending check (armed but waiting for the pipeline
    // to go idle) so main.qml's close-while-busy warning still covers it.
    readonly property bool checkRunning: qualityCheckRunner.running ||
                                         (startGate.running && startGate.action === "check")

    // Hand main.qml the same ModalManager BloodFlow's icon-bar uses,
    // so the close-while-busy handler can dismiss any open modal
    // (saving its state via the modal's close() function) before the
    // app tears down.
    readonly property alias modalManager: modalManager

    // Clinical mode (read from app config). Forces Far camera pattern +
    // free run, hides scan-settings button, and swaps in the clinical
    // plot view.
    property bool clinicalMode: MotionInterface.appConfig.clinicalMode === true
    // In clinical mode, Start first runs a contact-quality preflight check.
    property bool clinicalStartPending: false
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

    property string sessionId: MotionInterface.userLabel || ""

    // Duration from scan time modal
    property bool freeRun: clinicalMode
    property int durationSec: clinicalMode ? 43200 : 3600  // 12h in clinical mode, 1h default

    onClinicalModeChanged: {
        if (clinicalMode) {
            freeRun = true
            durationSec = 43200
            var _cfg = MotionInterface.appConfig;
            leftMask  = _cfg.clinicalModeLeftMask  !== undefined ? _cfg.clinicalModeLeftMask  : 0xC3
            rightMask = _cfg.clinicalModeRightMask !== undefined ? _cfg.clinicalModeRightMask : 0xC3
        }
    }
    property int elapsedSec: 0

    // The elapsed-time ticker runs exactly while the MCU trigger is firing.
    // Using a declarative `running:` binding means the timer auto-stops the
    // instant triggerState flips to "OFF" (top of the SDK teardown, right
    // after stop_trigger) rather than when captureFinished arrives 2-4s later
    // after all the camera-disable / USB-drain / writer-join work is done.
    //
    // Each tick PULLS the connector's trigger-ON clock rather than counting
    // ticks (issue #201): a QML Timer fires late under GUI load and never
    // catches up, so a += 1 counter drifts behind real time and disagrees
    // with the notes duration line written from the connector's clock.
    Timer {
        id: scanTimer
        interval: 1000
        repeat: true
        running: bloodFlow.scanning && MotionInterface.triggerState === "ON"
        onTriggered: bloodFlow.elapsedSec = MotionInterface.scanElapsedSec()
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
        var cfg      = MotionInterface.appConfig;
        var defLeft  = clinicalMode ? (cfg.clinicalModeLeftMask  !== undefined ? cfg.clinicalModeLeftMask  : 0xC3) : (cfg.leftMask  !== undefined ? cfg.leftMask  : 0x99);
        var defRight = clinicalMode ? (cfg.clinicalModeRightMask !== undefined ? cfg.clinicalModeRightMask : 0xC3) : (cfg.rightMask !== undefined ? cfg.rightMask : 0x99);
        if (MotionInterface.leftSensorConnected && !_leftMaskInitialApplied) {
            leftMask = defLeft;
            _leftMaskInitialApplied = true;
        }
        if (MotionInterface.rightSensorConnected && !_rightMaskInitialApplied) {
            rightMask = defRight;
            _rightMaskInitialApplied = true;
        }
        // No FPGA flash here (issue #154): ScanRunner runs
        // FlashSensorsTask unconditionally on every Start/Check, so a
        // startup flash would only block the Start button for ~2 min
        // doing redundant work.
    }

    function beginScanNow() {
        bloodFlow.scanning = true
        bloodFlow.suppressLiveCqModal = false
        clinicalStartPending = false
        // Drop any stale CQ warning entries from a previous scan/check.
        // The connector creates a fresh _ContactQualityState per scan, so
        // it never re-emits "cleared" for a camera that wasn't latched in
        // the new scan — without this reset, the modal would keep showing
        // an orange dot from the prior scan.
        contactQualityModal.entries = []
        scanRunner.start()
    }

    // Start gate: the pre-scan CQ check's SDK worker keeps unwinding
    // for ~2 s after its results are displayed, and a start issued in that
    // window is refused synchronously by the connector's _ensure_idle gate
    // (the failure used to be swallowed — modal closed, nothing happened).
    // Poll isPipelineIdle() and begin the moment the connector is actually
    // free; give up loudly after 8 s — unless a camera configuration (FPGA
    // flash, ~50 s) is what holds the pipeline (issue #283), in which case
    // wait it out on the flash watchdog's timescale instead of erroring.
    property bool scanStartPending: startGate.running && startGate.action === "scan"
    function beginScanWhenReady() {
        if (bloodFlow.scanning || startGate.running) return
        startGate.arm("scan")
    }
    // Same gate for starting a contact-quality check (Check button and the
    // clinical pre-scan check): firing qualityCheckRunner while a
    // configuration was still draining used to surface a raw "Camera
    // configuration already in progress" error in the modal (issue #283).
    function beginCheckWhenReady() {
        if (bloodFlow.scanning || startGate.running || qualityCheckRunner.running)
            return
        startGate.arm("check")
    }
    Timer {
        id: startGate
        interval: 200
        repeat: true
        triggeredOnStart: true   // idle path starts with no delay
        property string action: "scan"   // "scan" | "check"
        property int elapsedMs: 0
        function arm(what) {
            action = what
            elapsedMs = 0
            start()
        }
        onTriggered: {
            if (MotionInterface.isPipelineIdle()) {
                stop()
                if (action === "scan")
                    beginScanNow()
                else
                    qualityCheckRunner.start()
                return
            }
            elapsedMs += interval
            // A config in flight legitimately blocks for ~50 s — match
            // ScanRunner's flash watchdog bound. The post-connect sensor
            // init (issue #303) legitimately holds the pipeline for a few
            // seconds after a (re)connect — wait that out on its own bound
            // so a start armed just as a sensor replugs defers instead of
            // erroring. Anything else holding the pipeline past 8 s is
            // stuck; give up loudly.
            var deadline = MotionInterface.isConfigInFlight() ? 250000
                         : MotionInterface.sensorInitBusy ? 30000
                         : 8000
            if (elapsedMs >= deadline) {
                stop()
                if (action === "scan") {
                    MotionInterface.notify(
                        "Could not start scan — the previous step is still " +
                        "finishing. Please press Start again.", "error")
                } else {
                    contactQualityModal.showError(
                        "Could not start check — the previous step is still " +
                        "finishing. Please try again.")
                }
            }
        }
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
        waiting: bloodFlow.scanStartPending
        // Issue #303: hold Start/Check disabled while the connector's async
        // post-connect sensor init (debug flags, camera power masks, info
        // reads) is still in flight — a start in that window collides with
        // the init sequence ("Failed to program FPGA" on both sensors) and
        // can wedge a camera until DUT power-cycle. sensorInitBusy re-arms
        // on every sensor (re)connect, so a mid-session replug re-gates too.
        camerasReady: bloodFlow.camerasReady && !MotionInterface.sensorInitBusy
        clinicalMode: bloodFlow.clinicalMode

        // Action buttons — close any open modal first (which by
        // convention saves), then perform the action. If the open
        // modal is non-dismissable (e.g. ContactQualityModal during
        // an in-flight check), modalManager.closeCurrent() is a
        // no-op and the action below still runs.
        onStartStopClicked: {
            // A start is already armed and waiting on the connector to go
            // idle — ignore further clicks until it fires or times out.
            if (bloodFlow.scanStartPending) return
            modalManager.closeCurrent()
            if (bloodFlow.scanning) {
                scanRunner.cancel()
                // Notes modal opens via MotionInterface.scanNotesReady
                // after the SDK actually unwinds and the duration line
                // has been appended to scanNotes. Opening it here would
                // race the append and pop an empty modal.
            } else {
                if (bloodFlow.clinicalMode) {
                    clinicalStartPending = true
                    contactQualityModal.preScanMode = true
                    contactQualityModal.reset(true)
                    beginCheckWhenReady()
                } else {
                    beginScanWhenReady()
                }
            }
        }
        onCheckClicked: {
            modalManager.closeCurrent()
            contactQualityModal.reset(false)
            beginCheckWhenReady()
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
        onSettingsClicked: modalManager.toggle(settingsModal)
        // App-log viewer is a separate Window, not a ModalManager modal —
        // clicking Logs just shows/raises it (close via its title bar).
        onLogsClicked:     logViewerWindow.open()
    }

    // Allow external callers (firmware banner) to open the Settings overlay.
    function openSettings() { modalManager.toggle(settingsModal) }

    // Single source of truth for which modal is on screen. See
    // ModalManager.qml for semantics. The list must include every
    // modal that should participate in click-outside / icon-bar
    // close behavior; ContactQualityModal opts out of dismissal
    // dynamically via its `dismissable` property.
    ModalManager {
        id: modalManager
        modals: [scanSettingsModal, notesModal, historyModal,
                 settingsModal, contactQualityModal, logsModal,
                 sampleScanOfferModal]
    }

    // Data viewer — fills remaining space to the right of ButtonPanel.
    PlotViewer {
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.left: buttonPanel.right
        anchors.right: parent.right
        anchors.margins: 8
        anchors.leftMargin: 16
        clinicalMode: bloodFlow.clinicalMode
        // Clinical mode never autoscales: the toggle is hidden in both the
        // Settings modal and the viewer's three-dot popup, so a stale
        // autoScale=true in config must not leave it stuck on.
        autoScale: bloodFlow.clinicalMode ? false : settingsModal.autoScale
        displayMode: settingsModal.showBfiBvi ? "bfi_bvi" : "mean_contrast"
        leftMask:  bloodFlow.leftMask
        rightMask: bloodFlow.rightMask
        // Trace colors — settingsModal loads bfiColor/bviColor from app
        // config and persists edits from the color pickers.
        bfiColor: settingsModal.bfiColor
        bviColor: settingsModal.bviColor
        // Manual y-axis bounds — applied when autoScale is off.
        settingBfiMin:      settingsModal.bfiMin
        settingBfiMax:      settingsModal.bfiMax
        settingBviMin:      settingsModal.bviMin
        settingBviMax:      settingsModal.bviMax
        settingMeanMin:     settingsModal.meanMin
        settingMeanMax:     settingsModal.meanMax
        settingContrastMin: settingsModal.contrastMin
        settingContrastMax: settingsModal.contrastMax
        // Bottom-right settings popup writes back through these
        // signals → settingsModal owns the persisted state and
        // the Settings modal stays in sync with the viewer's quick
        // toggles.
        onAutoScaleToggleRequested: function(enabled) {
            settingsModal.autoScale = enabled
            settingsModal.autoScalePerPlot = enabled
        }
        onDisplayModeToggleRequested: function(bfiBviMode) {
            settingsModal.showBfiBvi = bfiBviMode
        }
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

    // No-device offer to open the bundled sample scan. Raised by the
    // connector's startup watchdog; research builds only.
    SampleScanOfferModal {
        id: sampleScanOfferModal
    }

    // Spacebar during an active scan pops the Notes modal with a fresh
    // newline + [elapsed / wall-clock] timestamp, cursor ready to type.
    // Gated so it only fires mid-scan and never over another modal; once
    // NotesModal opens, modalManager.current is non-null so the shortcut
    // disables itself and Space types a literal space in the textarea.
    Shortcut {
        sequence: "Space"
        enabled: bloodFlow.scanning
                 && MotionInterface.triggerState === "ON"
                 && modalManager.current === null
        onActivated: {
            var elapsed = MotionInterface.scanElapsedStr()
            var wall = Qt.formatTime(new Date(), "HH:mm:ss")
            notesModal.openWithTimestamp(elapsed + " / " + wall)
        }
    }

    // Open the notes modal exactly when the connector signals that
    // scanNotes has been finalized for the just-completed scan (duration
    // line appended, notes.txt written). This is the only path that
    // guarantees notesArea.text snapshots the post-scan content; opening
    // the modal from onStartStopClicked or scanFinished races the
    // append because scanRunner.scanFinished fires synchronously from
    // cancel(), before the SDK has unwound and _on_complete has run.
    Connections {
        target: MotionInterface
        function onScanNotesReady() { notesModal.open() }
        // Startup watchdog found no device (research builds only). Don't
        // stomp a modal the user opened during the 12 s window. The
        // watchdog is one-shot, so an offer dropped here is not retried
        // this launch (relaunch to be offered again).
        function onSampleScanOfferRequested() {
            if (!modalManager.current) sampleScanOfferModal.open()
        }
        // Snap the header counter to the authoritative value on every
        // trigger edge. The OFF edge matters most: it carries the SDK
        // timestamp correction, so the final displayed value lands on
        // exactly the duration the notes line will report — without this,
        // the counter keeps the value of its last 1 s tick, which can sit
        // a second high while the queued OFF event was in flight.
        function onTriggerStateChanged() {
            if (bloodFlow.scanning)
                bloodFlow.elapsedSec = MotionInterface.scanElapsedSec()
        }
    }

    HistoryModal {
        id: historyModal
    }

    SettingsModal {
        id: settingsModal
        // Audit Log: the password gate lives in SettingsModal; on success
        // close Settings first so toggle() sees current === null and goes
        // straight to logsModal.open() (no redundant close), then open the
        // ModalManager-governed LogsModal.
        onLogsRequested: {
            settingsModal.close()
            modalManager.toggle(logsModal)
        }
    }

    LogsModal {
        id: logsModal
    }

    // Engineering live app-log viewer (icon-bar Logs button) — a separate
    // non-modal Window, distinct from the audit-log LogsModal above.
    LogViewerWindow {
        id: logViewerWindow
    }

    ContactQualityModal {
        id: contactQualityModal
        anchors.fill: parent
        // Pre-scan check always evaluates all physically-present cameras,
        // regardless of the active scan mask.
        leftMask: bloodFlow.clinicalStartPending
                  ? (MotionInterface.leftSensorConnected ? 0xFF : 0x00)
                  : bloodFlow.leftMask
        rightMask: bloodFlow.clinicalStartPending
                   ? (MotionInterface.rightSensorConnected ? 0xFF : 0x00)
                   : bloodFlow.rightMask
        onStopScanRequested: {
            bloodFlow.suppressLiveCqModal = true
            contactQualityModal.close()
            if (bloodFlow.scanning) {
                // Route through ScanRunner so the normal "Canceled" flow
                // runs and Notes opens consistently.
                scanRunner.cancel()
            } else {
                MotionInterface.stopCapture()
            }
            clinicalStartPending = false
        }
        onContinueRequested: {
            if (bloodFlow.clinicalStartPending) {
                // Defense in depth for issue #268: never arm the scan while
                // the pre-scan check is still in flight. The modal already
                // disables Start Scan during "checking"; this catches any
                // other path that fires continueRequested early. By the time
                // results are on screen the runner is idle, so the normal
                // click-through is unaffected.
                if (bloodFlow.checkRunning) return
                contactQualityModal.close()
                // Gated, not direct: the CQ check's worker is often still
                // unwinding when the user clicks Start Scan, and an
                // immediate start would be refused (silently, pre-gate).
                beginScanWhenReady()
            }
            // Otherwise (live-scan warning modal), Continue just dismisses.
        }
        onRetestRequested: {
            contactQualityModal.preScanMode = bloodFlow.clinicalStartPending
            contactQualityModal.reset(bloodFlow.clinicalStartPending)
            beginCheckWhenReady()
        }
        onDismissed: {
            if (!bloodFlow.scanning)
                clinicalStartPending = false
            if (!bloodFlow.clinicalStartPending)
                contactQualityModal.preScanMode = false
        }
    }

    // ===== SCAN RUNNER (capture mode) =====
    ScanRunner {
        id: scanRunner
        mode: "capture"
        connector: MotionInterface
        leftMask: bloodFlow.leftMask
        rightMask: bloodFlow.rightMask
        durationSec: bloodFlow.durationSec
        subjectId: MotionInterface.userLabel
        disableLaser: false
        laserOn: true
        laserPower: 50
        // triggerConfig left at the SetTriggerTask default ({}) so
        // the connector's setTrigger merges only TriggerStatus over the
        // SDK-resolved DEFAULT_TRIGGER_CONFIG.
        triggerConfig: ({})

        onStageUpdate: function(txt) {
            if (scanRunner._stage === "capture") {
                bloodFlow.elapsedSec = 0
                // scanTimer is started declaratively by its `running:` binding
                // (bloodFlow.scanning && triggerState === "ON") — no imperative
                // start() needed here.
            }
        }
        onMessageOut: function(line) {
            console.log(line)
        }
        onScanFinished: function(ok, err, left, right) {
            // scanTimer stops automatically via its `running:` binding once
            // bloodFlow.scanning flips false or triggerState goes "OFF".
            bloodFlow.scanning = false
            bloodFlow.suppressLiveCqModal = false

            if (!ok && err !== "Canceled") {
                console.log("ERROR: " + err)
                // The scan progress dialog that used to surface these is
                // gone — without a toast, a failed start is invisible.
                MotionInterface.notify("Scan failed: " + err, "error")
            }
            // Notes modal opens via MotionInterface.scanNotesReady.
        }
    }

    // ===== SCAN RUNNER (check mode) =====
    // Shares flash + trigger/laser plumbing with scanRunner; final stage is
    // the contact-quality check instead of capture. Always flashes 0xFF so
    // every physically-present camera participates — absent cameras are
    // skipped by the configure workflow. Every physically-present camera is
    // also EVALUATED and gates pass/fail (issue #420, reversing #277's
    // scan-mask narrowing): the preflight is a whole-module seating check,
    // so an unmeasured camera must never render as good contact. Known
    // trade-off, accepted: a dead camera fails every preflight even when
    // it's excluded from the scan mask.
    ScanRunner {
        id: qualityCheckRunner
        mode: "check"
        connector: MotionInterface
        leftMask: MotionInterface.leftSensorConnected  ? 0xFF : 0x00
        rightMask: MotionInterface.rightSensorConnected ? 0xFF : 0x00
        laserOn: true
        laserPower: 50
        // See note on the scanRunner triggerConfig above — same here.
        triggerConfig: ({})

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
        target: MotionInterface

        function onSignalConnected(descriptor, port) {
            // Adopt the config default camera masks when sensors connect.
            // Descriptor is the handle name from the SDK ("console" / "left" / "right").
            // The SDK already logs the state transition at INFO; no need
            // to duplicate it from QML.
            if (descriptor === "left" || descriptor === "right") {
                Qt.callLater(function() {
                    if (!bloodFlow.scanning) {
                        var cfg      = MotionInterface.appConfig;
                        var defLeft  = bloodFlow.clinicalMode ? (cfg.clinicalModeLeftMask  !== undefined ? cfg.clinicalModeLeftMask  : 0xC3) : (cfg.leftMask  !== undefined ? cfg.leftMask  : 0x99);
                        var defRight = bloodFlow.clinicalMode ? (cfg.clinicalModeRightMask !== undefined ? cfg.clinicalModeRightMask : 0xC3) : (cfg.rightMask !== undefined ? cfg.rightMask : 0x99);
                        // First connect this session per side: apply cfg
                        // default. Subsequent reconnects (e.g. console power
                        // cycle) preserve whatever's already in *Mask —
                        // either the cfg default already adopted earlier or
                        // the user's Scan Settings choice (issue #127).
                        if (MotionInterface.leftSensorConnected && !bloodFlow._leftMaskInitialApplied) {
                            bloodFlow.leftMask  = defLeft;
                            bloodFlow._leftMaskInitialApplied = true;
                        }
                        if (MotionInterface.rightSensorConnected && !bloodFlow._rightMaskInitialApplied) {
                            bloodFlow.rightMask = defRight;
                            bloodFlow._rightMaskInitialApplied = true;
                        }
                        // No FPGA flash on connect (issue #154): ScanRunner
                        // runs FlashSensorsTask on every Start/Check, so the
                        // post-power-cycle FPGA state is restored there.
                    }
                })
            }
        }

        function onSignalDisconnected(descriptor, port) {
            // SDK already logs the state transition; no QML log needed.
        }

        function onConnectionStatusChanged() {
            if (!MotionInterface.leftSensorConnected && !MotionInterface.rightSensorConnected) {
                bloodFlow.camerasReady = false
            } else if (MotionInterface.leftSensorConnected || MotionInterface.rightSensorConnected) {
                bloodFlow.camerasReady = true
            }
        }

        function onConfigFinished(ok, err) {
            bloodFlow.camerasReady = true  // always unblock; allConnected is the real gate
            if (ok) {
                console.log("Camera configuration complete")
            } else {
                console.log("Camera configuration failed: " + err)
            }
        }

        // Contact-quality quick-check lifecycle
        function onContactQualityCheckStarted(seconds) {
            // ``seconds`` is no longer shown as a countdown — the modal is
            // now just an indeterminate spinner during the check.
            contactQualityModal.preScanMode = bloodFlow.clinicalStartPending
            contactQualityModal.reset(bloodFlow.clinicalStartPending)
        }
        function onContactQualityCheckFinished(ok, error, warnings) {
            if (bloodFlow.clinicalStartPending) {
                // Clinical-mode preflight: always land in live-style footer so
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
            if (bloodFlow.clinicalStartPending)
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
        if (clinicalMode) {
            freeRun = true
            durationSec = 43200
            var _cfg = MotionInterface.appConfig;
            leftMask  = _cfg.clinicalModeLeftMask  !== undefined ? _cfg.clinicalModeLeftMask  : 0xC3
            rightMask = _cfg.clinicalModeRightMask !== undefined ? _cfg.clinicalModeRightMask : 0xC3
        }
        applyDefaultCameras()
    }

    Component.onDestruction: {
        console.log("Closing UI, clearing MotionInterface...")
    }
}
