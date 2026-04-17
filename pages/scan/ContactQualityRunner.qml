// qml/scan/ContactQualityRunner.qml
//
// Mirrors ScanRunner but runs:  FlashSensorsTask -> SetTriggerLaserTask ->
// ContactQualityCheckTask.  Both the Scan button and the Check button now
// share identical flash + trigger/laser plumbing; only the final step
// differs.
import QtQuick 6.5
import "."

QtObject {
    id: runner
    property var connector

    // Camera masks used for the flash step.  Contact quality always checks
    // every physically-present camera (0xFF on both sides); cameras missing
    // from the system are skipped by the configure workflow.
    property int leftMask: 0xFF
    property int rightMask: 0xFF

    property bool laserOn: true
    property var triggerConfig: ({})

    signal stageUpdate(string stage)
    signal progressUpdate(int pct)
    signal messageOut(string text)
    signal runFinished(bool ok, string error)

    property string _stage: "idle"
    property bool _done: false
    function _finish(ok, err) {
        if (_done) return
        _done = true
        runFinished(ok, err || "")
        _stage = "idle"
    }

    // --- Flash ---
    property FlashSensorsTask flashTask: FlashSensorsTask {
        connector: runner.connector
        leftCameraMask: runner.leftMask
        rightCameraMask: runner.rightMask

        property var _wd: null

        onStarted: {
            runner._stage = "flash"
            stageUpdate("Configuring sensors/FPGA…")
            if (_wd) try { _wd.stop() } catch(e) {}
            _wd = Qt.createQmlObject('import QtQuick 6.5; Timer { interval: 250000; repeat: false }', flashTask, "flashWD")
            _wd.triggered.connect(function() {
                messageOut("Flash step timed out.")
                runner._finish(false, "Flash step timed out")
            })
            _wd.start()
        }
        onProgress: function(pct) { progressUpdate(pct) }
        onLog: function(line) { messageOut(line) }
        onFinished: function(ok, err) {
            if (_wd) { try { _wd.stop() } catch(e) {} _wd = null }
            if (!ok) { runner._finish(false, err); return }
            setTask.run()
        }
    }

    // --- Set trigger/laser ---
    property SetTriggerLaserTask setTask: SetTriggerLaserTask {
        connector: runner.connector
        laserOn: runner.laserOn
        triggerConfig: runner.triggerConfig

        property var _wd: null

        onStarted: {
            runner._stage = "set"
            stageUpdate("Setting trigger & laser…")
            if (_wd) try { _wd.stop() } catch(e) {}
            _wd = Qt.createQmlObject('import QtQuick 6.5; Timer { interval: 5000; repeat: false }', setTask, "setWD")
            _wd.triggered.connect(function() {
                messageOut("SetTrigger/Laser step timed out.")
                runner._finish(false, "SetTrigger/Laser step timed out")
            })
            _wd.start()
        }
        onProgress: function(pct) { progressUpdate(pct) }
        onLog: function(line) { messageOut(line) }
        onFinished: function(ok, err) {
            if (_wd) { try { _wd.stop() } catch(e) {} _wd = null }
            if (!ok) { runner._finish(false, err); return }
            checkTask.run()
        }
    }

    // --- Contact-quality check ---
    property ContactQualityCheckTask checkTask: ContactQualityCheckTask {
        connector: runner.connector
        onStarted: {
            runner._stage = "check"
            stageUpdate("Running contact quality check…")
        }
        onProgress: function(pct) { progressUpdate(pct) }
        onLog: function(line) { messageOut(line) }
        onFinished: function(ok, err) { runner._finish(ok, err) }
    }

    function start() {
        if (runner._stage !== "idle") {
            messageOut("Contact quality check already running, ignoring start()")
            return
        }
        _done = false
        progressUpdate(1)
        stageUpdate("Preparing…")
        messageOut("ContactQualityRunner: start()")
        flashTask.run()
    }
}
