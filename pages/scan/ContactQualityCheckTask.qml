// qml/scan/ContactQualityCheckTask.qml
//
// Mirrors CaptureDataTask but invokes runContactQualityCheck() instead of
// startCapture(). Completes when the connector emits
// contactQualityCheckFinished.  The modal's own Connections block on
// BloodFlow.qml continues to observe that signal for UI state; this task
// uses it purely for flow control.
import QtQuick 6.5

QtObject {
    id: task
    property var connector
    // Configured scan mask (cameras the user actually selected — a camera
    // set to "None" is cleared here) so the CQ pass/fail verdict only
    // requires contact on cameras that will actually be used.
    property int leftCameraMask: 0
    property int rightCameraMask: 0

    signal started()
    signal progress(int pct)
    signal log(string line)
    signal finished(bool ok, string error)

    property var _onDone: null

    function _disconnectHandlers() {
        // See CaptureDataTask._disconnectHandlers — same rationale (issue #124).
        if (_onDone) {
            try { connector.contactQualityCheckFinished.disconnect(_onDone) } catch(e) {}
            _onDone = null
        }
    }

    function run() {
        if (!connector || !connector.runContactQualityCheck) {
            finished(false, "Connector missing runContactQualityCheck()")
            return
        }
        _disconnectHandlers()
        started()
        progress(50)
        log("Running contact quality check…")

        _onDone = function(ok, err, entries) {
            try { connector.contactQualityCheckFinished.disconnect(_onDone) } catch(e) {}
            finished(!!ok, err || "")
        }
        connector.contactQualityCheckFinished.connect(_onDone)

        try {
            connector.runContactQualityCheck(task.leftCameraMask, task.rightCameraMask)
        } catch (e) {
            try { connector.contactQualityCheckFinished.disconnect(_onDone) } catch(e2) {}
            finished(false, "runContactQualityCheck exception: " + e)
        }
    }

    function cancel() {
        // Contact-quality check is a short scan; let it complete naturally.
    }
}
