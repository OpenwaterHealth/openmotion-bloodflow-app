// qml/scan/OptimizeLaserTask.qml
//
// Final task of ScanRunner mode "optimize": runs Optimize Laser on the
// cameras the flash step just configured. Completes when the connector
// emits optimizeLaserFinished (also emitted, synchronously, for a refused
// start). OptimizeLaserModal observes the same signals for its display;
// this task uses them purely for flow control.
import QtQuick 6.5

QtObject {
    id: task
    property var connector
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
            try { connector.optimizeLaserFinished.disconnect(_onDone) } catch(e) {}
            _onDone = null
        }
    }

    function run() {
        if (!connector || !connector.optimizeLaser) {
            finished(false, "Connector missing optimizeLaser()")
            return
        }
        _disconnectHandlers()
        started()
        progress(30)
        log("Running Optimize Laser…")

        _onDone = function(ok, message, summary) {
            _disconnectHandlers()
            finished(!!ok, ok ? "" : (message || "Optimize Laser failed"))
        }
        connector.optimizeLaserFinished.connect(_onDone)

        try {
            connector.optimizeLaser(task.leftCameraMask, task.rightCameraMask)
        } catch (e) {
            if (_onDone) {
                _disconnectHandlers()
                finished(false, "optimizeLaser exception: " + e)
            }
        }
    }

    function cancel() {
        // The run stops between (or during) its short scans and restores the
        // defaults before optimizeLaserFinished arrives.
        if (connector && connector.cancelOptimizeLaser)
            try { connector.cancelOptimizeLaser() } catch(e) {}
    }
}
