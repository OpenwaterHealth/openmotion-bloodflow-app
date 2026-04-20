import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import QtQuick.Controls as Controls
import OpenMotion 1.0

/*  ContactQualityModal — four-state notification for contact-quality checks.
 *
 *  States: "checking" | "ok" | "warnings" | "error".
 *  Opened as a quick-check modal (liveScan=false) or as a live-scan warning
 *  modal (liveScan=true); the footer switches between Dismiss and
 *  Stop scan / Continue accordingly.
 *
 *  API:
 *      open() / close()
 *      reset(forLiveScan, durationEstimate)
 *                                -> enter "checking" state; durationEstimate
 *                                   (seconds, optional) drives the elapsed
 *                                   counter shown under the spinner
 *      showOk()                  -> enter "ok" state
 *      showError(msg)            -> enter "error" state with message
 *      addWarning(cameraLabel, typeKey, typeText, value)
 *                                -> append a dedup'd warning row,
 *                                   auto-transitioning to "warnings" state
 *      clearWarning(cameraLabel, typeKey)
 *                                -> clear one active warning; if none remain
 *                                   during live scan, modal becomes dismissable
 *
 *  Signals:
 *      stopScanRequested()  — user clicked "Stop scan" (live-scan footer)
 *      continueRequested()  — user clicked "Continue"  (live-scan footer)
 *      dismissed()          — modal closed by any button
 */
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9999

    AppTheme { id: theme }

    // ── state ────────────────────────────────────────────────────────────
    // One of: "checking" | "ok" | "warnings" | "error"
    property string state_: "checking"
    // Whether the modal was opened during a live scan (controls footer).
    property bool liveScan: false
    // Live-scan modal is only dismissable when no CQ issues remain active.
    property bool liveScanDismissable: false
    // Require an all-clear holdoff before enabling Continue.
    property int clearHoldoffMs: 2000
    // Active camera masks for current scan selection (used in live-scan mode).
    property int leftMask: 0xFF
    property int rightMask: 0xFF
    property string errorText: ""

    // Elapsed-time tracking for the "checking" state. ``durationEstimate``
    // is purely cosmetic (shown as "~Ns"); the real completion signal comes
    // from contactQualityCheckFinished.
    property int durationEstimate: 0
    property int elapsedMs: 0

    // Each entry: { camera, typeKey, typeText, value }
    property var entries: []

    signal stopScanRequested()
    signal continueRequested()
    signal retestRequested()
    signal dismissed()

    // ── public API ───────────────────────────────────────────────────────
    function open()  { root.visible = true; panel.forceActiveFocus() }
    function close() { root.visible = false; elapsedTimer.stop() }

    function reset(forLiveScan, durationEstimateArg) {
        liveScan = !!forLiveScan
        liveScanDismissable = !liveScan
        clearHoldoffTimer.stop()
        entries = []
        errorText = ""
        state_ = "checking"
        durationEstimate = (durationEstimateArg !== undefined && durationEstimateArg !== null)
                           ? Math.max(0, durationEstimateArg | 0) : 0
        elapsedMs = 0
        elapsedTimer.restart()
        if (!visible) open()
    }

    function showOk() {
        elapsedTimer.stop()
        state_ = "ok"
        if (!visible) open()
    }

    function showError(msg) {
        elapsedTimer.stop()
        errorText = msg || "Hardware error"
        state_ = "error"
        if (!visible) open()
    }

    // Upsert a warning row. Key = camera + typeKey.
    function addWarning(cameraLabel, typeKey, typeText, value) {
        for (var i = 0; i < entries.length; ++i) {
            if (entries[i].camera === cameraLabel && entries[i].typeKey === typeKey) {
                var upd = entries.slice()
                upd[i] = {
                    camera: cameraLabel,
                    typeKey: typeKey,
                    typeText: typeText,
                    value: value
                }
                entries = upd
                elapsedTimer.stop()
                state_ = "warnings"
                liveScanDismissable = false
                clearHoldoffTimer.stop()
                if (!visible) open()
                return
            }
        }
        var copy = entries.slice()
        copy.push({
            camera: cameraLabel,
            typeKey: typeKey,
            typeText: typeText,
            value: value
        })
        entries = copy
        elapsedTimer.stop()
        state_ = "warnings"
        liveScanDismissable = false
        clearHoldoffTimer.stop()
        if (!visible) open()
    }

    function clearWarning(cameraLabel, typeKey) {
        var copy = []
        var removed = false
        for (var i = 0; i < entries.length; ++i) {
            var e = entries[i]
            if (e.camera === cameraLabel && e.typeKey === typeKey) {
                removed = true
                continue
            }
            copy.push(e)
        }
        if (!removed)
            return
        entries = copy
        if (liveScan && state_ === "warnings" && entries.length === 0) {
            liveScanDismissable = false
            clearHoldoffTimer.restart()
            if (!visible) open()
        }
    }

    // Build per-camera quality status from entries.
    // Returns "good" (no warnings), "bad" (has warning), or "inactive".
    function cameraStatus(side, camIndex1) {
        if (root.liveScan && !cameraEnabled(side, camIndex1))
            return "inactive"
        var prefix = (side === "left") ? "L" : "R"
        var label = prefix + camIndex1
        if (root.state_ === "checking") return "checking"
        if (root.state_ === "error") return "inactive"
        for (var i = 0; i < entries.length; ++i) {
            if (entries[i].camera === label) return "bad"
        }
        return "good"
    }

    function cameraTooltip(side, camIndex1) {
        var prefix = (side === "left") ? "L" : "R"
        var label = prefix + camIndex1
        var lines = [label]
        var showDn = !!(MOTIONInterface.appConfig && MOTIONInterface.appConfig.developerMode)
        if (root.liveScan && !cameraEnabled(side, camIndex1)) {
            lines.push("Inactive for current scan mask")
            return lines.join("\n")
        }
        for (var i = 0; i < entries.length; ++i) {
            if (entries[i].camera === label)
                lines.push(showDn
                           ? (entries[i].typeText + " (" + entries[i].value.toFixed(1) + " DN)")
                           : entries[i].typeText)
        }
        return lines.join("\n")
    }

    function cameraColor(side, camIndex1) {
        var st = cameraStatus(side, camIndex1)
        if (st === "good")     return "#A3E4A1"  // pale green
        if (st === "bad")      return "#E67E22"  // strong orange
        if (st === "checking") return "#666666"
        return "#666666"
    }

    function cameraEnabled(side, camIndex1) {
        // Camera 1 maps to bit 7 ... camera 8 maps to bit 0.
        var bit = 8 - camIndex1
        var mask = (side === "left") ? root.leftMask : root.rightMask
        return ((mask >> bit) & 1) === 1
    }

    // Ticks every 500 ms while in "checking" state to update the elapsed
    // label. Stopped on every exit path (close / showOk / showError /
    // addWarning) so it never leaks past the check.
    Timer {
        id: elapsedTimer
        interval: 500
        repeat: true
        onTriggered: {
            if (root.state_ !== "checking") { stop(); return }
            root.elapsedMs += interval
        }
    }

    // Continue becomes available only after contact quality stays clear
    // for clearHoldoffMs continuously during a live warning state.
    Timer {
        id: clearHoldoffTimer
        interval: root.clearHoldoffMs
        repeat: false
        onTriggered: {
            if (root.liveScan && root.state_ === "warnings" && root.entries.length === 0) {
                root.liveScanDismissable = true
            }
        }
    }

    // ── dimmed backdrop (blocks clicks to page below) ────────────────────
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        MouseArea { anchors.fill: parent; onClicked: {} }
    }

    // ── dialog panel ─────────────────────────────────────────────────────
    Rectangle {
        id: panel
        width: 520
        height: 480
        radius: 10
        color: theme.bgContainer
        border.width: 2
        border.color: root.state_ === "ok" ? theme.accentGreen
                    : (root.state_ === "warnings"
                       ? ((root.liveScan && root.liveScanDismissable) ? theme.accentGreen : theme.accentOrange)
                       : (root.state_ === "error" ? theme.accentRed : theme.borderSubtle))
        anchors.centerIn: parent
        focus: true

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            // Title
            Text {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignHCenter
                horizontalAlignment: Text.AlignHCenter
                font.pixelSize: 20
                font.bold: true
                color: theme.textPrimary
                wrapMode: Text.WordWrap
                text: {
                    if (root.state_ === "checking") return "Checking contact quality…"
                    if (root.state_ === "ok")       return "Good signal quality"
                    if (root.state_ === "error")    return "Contact check failed"
                    return "Contact Quality Notification"
                }
            }

            // Spinner for "checking" state
            BusyIndicator {
                visible: root.state_ === "checking"
                running: visible
                Layout.alignment: Qt.AlignHCenter
            }

            // Elapsed / expected-duration counter under the spinner. Only
            // shown while actively checking; hidden in ok/warnings/error.
            Text {
                visible: root.state_ === "checking"
                Layout.alignment: Qt.AlignHCenter
                color: theme.textSecondary
                font.pixelSize: 13
                text: {
                    var secs = Math.floor(root.elapsedMs / 1000)
                    if (root.durationEstimate > 0)
                        return "Checking contact quality… (" + secs + "s / ~" + root.durationEstimate + "s)"
                    return "Checking contact quality… (" + secs + "s)"
                }
            }

            // OK message
            Text {
                visible: root.state_ === "ok"
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: theme.textSecondary
                font.pixelSize: 14
                text: "All cameras are reporting acceptable ambient light and contact levels."
            }

            // Warnings subtitle
            Text {
                visible: root.state_ === "warnings"
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: theme.textSecondary
                font.pixelSize: 14
                text: (root.entries.length > 0)
                      ? "Hover over orange cameras for details."
                      : "All contact quality issues are currently inactive. You may dismiss."
            }

            // Error message
            Text {
                visible: root.state_ === "error"
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: theme.accentRed
                font.pixelSize: 14
                text: root.errorText
            }

            // ── Sensor diagrams ──────────────────────────────────────
            RowLayout {
                visible: root.state_ === "ok" || root.state_ === "warnings"
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignHCenter
                spacing: 20

                // Left sensor
                Rectangle {
                    visible: MOTIONInterface.leftSensorConnected
                    width: 180; height: 210; radius: 22
                    color: theme.bgCard
                    border.color: theme.borderSubtle; border.width: 2

                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 8; spacing: 6

                        Text {
                            text: "Left Sensor"
                            font.pixelSize: 14; color: theme.textSecondary
                            horizontalAlignment: Text.AlignHCenter
                            Layout.alignment: Qt.AlignHCenter
                        }

                        GridLayout {
                            columns: 3; columnSpacing: 16; rowSpacing: 8
                            Layout.alignment: Qt.AlignHCenter
                            property int cs: 18

                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("left", 1); border.color: "black"; border.width: 1
                                MouseArea { id: lh1; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: lh1.containsMouse; Controls.ToolTip.text: cameraTooltip("left", 1) }
                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("left", 8); border.color: "black"; border.width: 1
                                MouseArea { id: lh2; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: lh2.containsMouse; Controls.ToolTip.text: cameraTooltip("left", 8) }

                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("left", 2); border.color: "black"; border.width: 1
                                MouseArea { id: lh3; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: lh3.containsMouse; Controls.ToolTip.text: cameraTooltip("left", 2) }
                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("left", 7); border.color: "black"; border.width: 1
                                MouseArea { id: lh4; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: lh4.containsMouse; Controls.ToolTip.text: cameraTooltip("left", 7) }

                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("left", 3); border.color: "black"; border.width: 1
                                MouseArea { id: lh5; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: lh5.containsMouse; Controls.ToolTip.text: cameraTooltip("left", 3) }
                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("left", 6); border.color: "black"; border.width: 1
                                MouseArea { id: lh6; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: lh6.containsMouse; Controls.ToolTip.text: cameraTooltip("left", 6) }

                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("left", 4); border.color: "black"; border.width: 1
                                MouseArea { id: lh7; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: lh7.containsMouse; Controls.ToolTip.text: cameraTooltip("left", 4) }
                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("left", 5); border.color: "black"; border.width: 1
                                MouseArea { id: lh8; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: lh8.containsMouse; Controls.ToolTip.text: cameraTooltip("left", 5) }

                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: "#FFD700"; border.color: "black"; border.width: 1 }
                            Item {}
                        }
                    }
                }

                // Right sensor
                Rectangle {
                    visible: MOTIONInterface.rightSensorConnected
                    width: 180; height: 210; radius: 22
                    color: theme.bgCard
                    border.color: theme.borderSubtle; border.width: 2

                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 8; spacing: 6

                        Text {
                            text: "Right Sensor"
                            font.pixelSize: 14; color: theme.textSecondary
                            horizontalAlignment: Text.AlignHCenter
                            Layout.alignment: Qt.AlignHCenter
                        }

                        GridLayout {
                            columns: 3; columnSpacing: 16; rowSpacing: 8
                            Layout.alignment: Qt.AlignHCenter
                            property int cs: 18

                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("right", 1); border.color: "black"; border.width: 1
                                MouseArea { id: rh1; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: rh1.containsMouse; Controls.ToolTip.text: cameraTooltip("right", 1) }
                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("right", 8); border.color: "black"; border.width: 1
                                MouseArea { id: rh2; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: rh2.containsMouse; Controls.ToolTip.text: cameraTooltip("right", 8) }

                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("right", 2); border.color: "black"; border.width: 1
                                MouseArea { id: rh3; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: rh3.containsMouse; Controls.ToolTip.text: cameraTooltip("right", 2) }
                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("right", 7); border.color: "black"; border.width: 1
                                MouseArea { id: rh4; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: rh4.containsMouse; Controls.ToolTip.text: cameraTooltip("right", 7) }

                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("right", 3); border.color: "black"; border.width: 1
                                MouseArea { id: rh5; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: rh5.containsMouse; Controls.ToolTip.text: cameraTooltip("right", 3) }
                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("right", 6); border.color: "black"; border.width: 1
                                MouseArea { id: rh6; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: rh6.containsMouse; Controls.ToolTip.text: cameraTooltip("right", 6) }

                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("right", 4); border.color: "black"; border.width: 1
                                MouseArea { id: rh7; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: rh7.containsMouse; Controls.ToolTip.text: cameraTooltip("right", 4) }
                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: cameraColor("right", 5); border.color: "black"; border.width: 1
                                MouseArea { id: rh8; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                                Controls.ToolTip.visible: rh8.containsMouse; Controls.ToolTip.text: cameraTooltip("right", 5) }

                            Item {}
                            Rectangle { width: parent.cs; height: parent.cs; radius: parent.cs/2
                                color: "#FFD700"; border.color: "black"; border.width: 1 }
                            Item {}
                        }
                    }
                }
            }

            Item {
                Layout.fillHeight: true
                Layout.fillWidth: true
            }

            // Footer buttons
            RowLayout {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignHCenter
                spacing: 12
                visible: root.state_ !== "checking"

                // Live-scan footer (warnings state only)
                Button {
                    visible: root.liveScan && root.state_ === "warnings"
                    text: "Stop scan"
                    hoverEnabled: true
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: parent.hovered ? "#FFFFFF" : theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.accentRed : theme.bgInput
                        radius: 4; border.color: parent.hovered ? theme.accentRed : theme.borderSoft; border.width: 1
                    }
                    onClicked: { root.stopScanRequested(); root.close(); root.dismissed() }
                }
                Button {
                    visible: root.liveScan && root.state_ === "warnings"
                    enabled: root.liveScanDismissable
                    text: "Continue"
                    hoverEnabled: enabled
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: !parent.enabled ? theme.textDisabled
                              : (parent.hovered ? "#FFFFFF" : theme.textSecondary)
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: !parent.enabled ? theme.bgCard
                              : (parent.hovered ? theme.accentBlue : theme.bgInput)
                        radius: 4
                        border.color: !parent.enabled ? theme.borderSubtle
                                    : (parent.hovered ? theme.accentBlue : theme.borderSoft)
                        border.width: 1
                    }
                    onClicked: { root.continueRequested(); root.close(); root.dismissed() }
                }

                // Quick-check / OK / error footer
                Button {
                    visible: !(root.liveScan && root.state_ === "warnings")
                    text: "Dismiss"
                    hoverEnabled: true
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: parent.hovered ? "#FFFFFF" : theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.accentBlue : theme.bgInput
                        radius: 4; border.color: parent.hovered ? theme.accentBlue : theme.borderSoft; border.width: 1
                    }
                    onClicked: { root.close(); root.dismissed() }
                }
                Button {
                    visible: !(root.liveScan && root.state_ === "warnings")
                    text: "Retest"
                    hoverEnabled: true
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: parent.hovered ? "#FFFFFF" : theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? theme.accentBlue : theme.bgInput
                        radius: 4; border.color: parent.hovered ? theme.accentBlue : theme.borderSoft; border.width: 1
                    }
                    onClicked: { root.close(); root.retestRequested() }
                }
            }
        }

        // ESC closes (unless we're mid-check, or awaiting Stop/Continue
        // decision during a live scan).
        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape
                    && root.state_ !== "checking"
                    && !(root.liveScan && root.state_ === "warnings" && !root.liveScanDismissable)) {
                root.close()
                root.dismissed()
                event.accepted = true
            }
        }
    }
}
