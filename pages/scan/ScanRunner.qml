// qml/scan/ScanRunner.qml
//
// Drives the shared scan pipeline:
//   FlashSensorsTask -> SetTriggerTask -> <final task>
//
// ``mode`` selects the final task:
//   * "capture" — CaptureDataTask  (normal user scan)
//   * "check"   — ContactQualityCheckTask  (contact-quality quick check)
//
// Properties irrelevant to a given mode are ignored (e.g. ``durationSec``
// / ``dataDir`` for "check"; ``leftPath``/``rightPath`` in ``scanFinished``
// are empty strings in "check" mode).
import QtQuick 6.5
import "."

QtObject {
    id: runner
    property var connector

    // "capture" | "check"
    property string mode: "capture"

    property int leftMask: 0x5A
    property int rightMask: 0x5A

    property int durationSec: 60
    property string subjectId: ""
    property string dataDir: ""
    property bool disableLaser: false
    property bool laserOn: true
    property int laserPower: 50
    property var triggerConfig: ({})

    signal stageUpdate(string stage)
    signal progressUpdate(int pct)
    signal messageOut(string text)
    signal scanFinished(bool ok, string error, string leftPath, string rightPath)

    // internal
    property string _stage: "idle"
    property bool _done: false

    // True from the moment a runner starts until it reports
    // finished/cancelled. Used by main.qml's close-while-busy warning
    // (issue #75) to know whether a check / scan is in flight.
    readonly property bool running: _stage !== "idle" && !_done
    function _finish(ok, err, l, r) {
        if (_done) return
        _done = true
        // Stop any active watchdog so a late trigger doesn't emit spurious
        // "timed out" messages after completion / cancellation.
        flashWatchdog.stop()
        setTriggerWatchdog.stop()
        checkWatchdog.stop()
        scanFinished(ok, err || "", l || "", r || "")
        _stage = "idle"
    }

    // --- Watchdogs (declarative; stopped in `_finish`) ----------------------

    property Timer flashWatchdog: Timer {
        interval: 250000   // ~4 min — flash step observed at ~50s
        repeat: false
        onTriggered: {
            runner.messageOut("Flash step timed out.")
            runner._finish(false, "Flash step timed out", "", "")
        }
    }

    property Timer setTriggerWatchdog: Timer {
        interval: 5000     // trigger + laser are quick sync calls
        repeat: false
        onTriggered: {
            runner.messageOut("SetTrigger step timed out.")
            runner._finish(false, "SetTrigger step timed out", "", "")
        }
    }

    property Timer checkWatchdog: Timer {
        interval: 30000    // contact-quality check is 1-4s; generous slack
        repeat: false
        onTriggered: {
            runner.messageOut("Contact-quality check timed out.")
            runner._finish(false, "Contact-quality check timed out", "", "")
        }
    }

    // --- Flash --------------------------------------------------------------

    property FlashSensorsTask flashTask: FlashSensorsTask {
        connector: runner.connector
        leftCameraMask: runner.leftMask
        rightCameraMask: runner.rightMask

        onStarted: {
            runner._stage = "flash"
            runner.stageUpdate("Configuring sensors/FPGA…")
            runner.flashWatchdog.restart()
        }
        onProgress: function(pct) { runner.progressUpdate(pct) }
        onLog: function(line) { runner.messageOut(line) }
        // Guard against late-fire (issue #124): a connector signal that was
        // pending when an earlier cycle's ``_finish`` ran can still arrive
        // and propagate ``finished`` here. Without the guard, the handler
        // mutates ``_stage`` and re-enters ``_finish``, which then bails on
        // ``_done`` — leaving the runner wedged in a non-idle stage so the
        // next ``start()`` is silently rejected.
        onFinished: function(ok, err) {
            if (runner._done) return
            runner.flashWatchdog.stop()
            if (!ok) { runner._finish(false, err, "", ""); return }
            runner.setTriggerTask.run()
        }
    }

    // --- Set trigger ---------------------------------------------------------

    property SetTriggerTask setTriggerTask: SetTriggerTask {
        connector: runner.connector
        laserOn: runner.laserOn
        triggerConfig: runner.triggerConfig

        onStarted: {
            runner._stage = "set"
            runner.stageUpdate("Setting trigger…")
            runner.setTriggerWatchdog.restart()
        }
        onProgress: function(pct) { runner.progressUpdate(pct) }
        onLog: function(line) { runner.messageOut(line) }
        onFinished: function(ok, err) {
            if (runner._done) return
            runner.setTriggerWatchdog.stop()
            if (!ok) { runner._finish(false, err, "", ""); return }
            if (runner.mode === "check") {
                runner.checkTask.run()
            } else {
                runner.captureTask.run()
            }
        }
    }

    // --- Capture (mode: "capture") -----------------------------------------

    property CaptureDataTask captureTask: CaptureDataTask {
        connector: runner.connector
        leftCameraMask: runner.leftMask
        rightCameraMask: runner.rightMask
        durationSec: runner.durationSec
        subjectId: runner.subjectId
        dataDir: runner.dataDir
        disableLaser: runner.disableLaser

        onStarted: {
            runner._stage = "capture"
            runner.stageUpdate("Capturing…")
        }
        onProgress: function(pct) { runner.progressUpdate(pct) }
        onLog: function(line) { runner.messageOut(line) }
        onFinished: function(ok, err) {
            if (runner._done) return
            if (!ok) { runner._finish(false, err, "", ""); return }
            // The new SDK pipeline writes CSVs in real-time via CsvSink,
            // so there's no post-process .raw→.csv conversion to do here —
            // this is a finalization gesture: notes get written and the
            // notes modal opens on scanNotesReady.
            runner._stage = "finish"
            runner.stageUpdate("Scan complete")
            runner._finish(true, "", "", "")
        }
    }

    // --- Contact-quality check (mode: "check") -----------------------------

    property ContactQualityCheckTask checkTask: ContactQualityCheckTask {
        connector: runner.connector

        onStarted: {
            runner._stage = "check"
            runner.stageUpdate("Running contact-quality check…")
            runner.checkWatchdog.restart()
        }
        onProgress: function(pct) { runner.progressUpdate(pct) }
        onLog: function(line) { runner.messageOut(line) }
        onFinished: function(ok, err) {
            if (runner._done) return
            runner.checkWatchdog.stop()
            runner._finish(ok, err, "", "")
        }
    }

    // --- Controls -----------------------------------------------------------

    function start() {
        if (runner._stage !== "idle") {
            messageOut("Scan already running, ignoring start()")
            return
        }
        _done = false
        progressUpdate(1)
        stageUpdate("Preparing…")
        messageOut("ScanRunner: start(mode=" + runner.mode + ")")
        flashTask.run()
    }

    function cancel() {
        switch (runner._stage) {
        case "flash":
            if (connector && connector.cancelConfigureCameraSensors)
                try { connector.cancelConfigureCameraSensors() } catch(e) {}
            break
        case "capture":
        case "check":
            // Both capture and the contact-quality check run through
            // ``start_scan`` at the SDK layer, so stopCapture applies to
            // either. Falls through to stopTrigger if unavailable.
            if (connector && connector.stopCapture)
                try { connector.stopCapture() } catch(e) {}
            else if (connector && connector.stopTrigger)
                try { connector.stopTrigger() } catch(e) {}
            break
        case "finish":
            // No long-running work — scan-notes write is synchronous,
            // and CSVs are already on disk. Nothing to cancel.
            break
        }
        runner._finish(false, "Canceled", "", "")
    }
}
