// qml/scan/ScanRunner.qml
//
// Drives the shared scan pipeline:
//   FlashSensorsTask -> SetTriggerLaserTask -> <final task>
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

    signal stageUpdate(string stage)
    signal progressUpdate(int pct)
    signal messageOut(string text)
    signal scanFinished(bool ok, string error, string leftPath, string rightPath)

    // internal
    property string _stage: "idle"
    property bool _done: false
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
            runner.messageOut("SetTrigger/Laser step timed out.")
            runner._finish(false, "SetTrigger/Laser step timed out", "", "")
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
        onFinished: function(ok, err) {
            runner.flashWatchdog.stop()
            if (!ok) { runner._finish(false, err, "", ""); return }
            runner.setTriggerLaserTask.run()
        }
    }

    // --- Set trigger/laser --------------------------------------------------

    property SetTriggerLaserTask setTriggerLaserTask: SetTriggerLaserTask {
        connector: runner.connector
        laserOn: runner.laserOn

        onStarted: {
            runner._stage = "set"
            runner.stageUpdate("Setting trigger & laser…")
            runner.setTriggerWatchdog.restart()
        }
        onProgress: function(pct) { runner.progressUpdate(pct) }
        onLog: function(line) { runner.messageOut(line) }
        onFinished: function(ok, err) {
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
            if (!ok) { runner._finish(false, err, "", ""); return }
            runner._stage = "post"
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
        case "post":
            if (connector && connector.cancelPostProcess)
                try { connector.cancelPostProcess() } catch(e) {}
            break
        }
        runner._finish(false, "Canceled", "", "")
    }
}
