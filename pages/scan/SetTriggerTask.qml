// qml/scan/SetTriggerTask.qml
import QtQuick 6.5

QtObject {
    id: task
    property var connector
    property bool laserOn: true
    property var triggerConfig: ({})     // extra fields merged into payload

    signal started()
    signal progress(int pct)
    signal log(string line)
    signal finished(bool ok, string error)

    function run() {
        started()
        progress(20)
        log("Setting trigger…")

        if (!connector || !connector.setTrigger) {
            finished(false, "Connector missing setTrigger")
            return
        }

        // Build payload for backend: 2=ON, 1=OFF
        var payload = { "TriggerStatus": laserOn ? 2 : 1 }
        for (var k in triggerConfig) payload[k] = triggerConfig[k]

        // Set trigger. Laser power is applied once per console connect
        // by the connector, not per scan.
        try {
            var res = connector.setTrigger(JSON.stringify(payload))
            var ok = (typeof res === "boolean") ? res : true
            if (!ok) {
                log("setTrigger returned false")
                finished(false, "setTrigger returned false")
                return
            }
            log("Trigger set.")
        } catch (e) {
            log("setTrigger exception: " + e)
            finished(false, "setTrigger exception: " + e)
            return
        }

        progress(25)
        finished(true, "")
    }
}
