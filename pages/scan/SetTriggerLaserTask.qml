// qml/scan/SetTriggerLaserTask.qml
//
// Step 2 of the scan pipeline: push the default trigger config to the
// console and apply the laser-power I2C config.  The trigger payload is
// sourced from ``app_config.json -> triggerConfig`` by the Python
// ``applyDefaultTrigger`` slot — keeping payload construction on the
// Python side avoids nested-QVariantMap iteration pitfalls in QML.
import QtQuick 6.5

QtObject {
    id: task
    property var connector
    property bool laserOn: true
    property bool applyLaserPowerFromConfig: true  // toggle for testing

    signal started()
    signal progress(int pct)
    signal log(string line)
    signal finished(bool ok, string error)

    function run() {
        started()
        progress(20)
        log("Setting trigger & laser…")

        if (!connector || !connector.applyDefaultTrigger
                || !connector.setLaserPowerFromConfig) {
            finished(false, "Connector missing applyDefaultTrigger/setLaserPowerFromConfig")
            return
        }

        // Set trigger
        try {
            var res = connector.applyDefaultTrigger(laserOn)
            if (!res) {
                log("applyDefaultTrigger returned false")
                finished(false, "applyDefaultTrigger returned false")
                return
            }
            log("Trigger set.")
        } catch (e) {
            log("applyDefaultTrigger exception: " + e)
            finished(false, "applyDefaultTrigger exception: " + e)
            return
        }

        // Optionally apply laser power from config
        if (applyLaserPowerFromConfig) {
            progress(23)
            try {
                var res2 = connector.setLaserPowerFromConfig()
                var ok2 = (typeof res2 === "boolean") ? res2 : true
                if (!ok2) {
                    log("setLaserPowerFromConfig returned false")
                    finished(false, "setLaserPowerFromConfig returned false")
                    return
                }
                log("Laser power applied from config.")
            } catch (e2) {
                log("setLaserPowerFromConfig exception: " + e2)
                finished(false, "setLaserPowerFromConfig exception: " + e2)
                return
            }
        }

        progress(25)
        finished(true, "")
    }
}
