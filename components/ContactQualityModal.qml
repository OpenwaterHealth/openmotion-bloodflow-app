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
 *      reset(forLiveScan)        -> enter "checking" state
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
 *      forceDismissed()     — engineering Force Dismiss clicked; fires
 *                             alongside dismissed(). BloodFlow uses it to
 *                             keep the live-scan modal suppressed for the
 *                             rest of the scan (#492).
 */
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9999


    // ── state ────────────────────────────────────────────────────────────
    // One of: "checking" | "ok" | "warnings" | "error"
    property string state_: "checking"
    // Whether the modal was opened during a live scan (controls footer).
    property bool liveScan: false
    // True when this modal is being used as a clinical-mode pre-scan gate.
    property bool preScanMode: false
    // Live-scan modal is only dismissable when no CQ issues remain active.
    property bool liveScanDismissable: false
    // ModalManager opt-out: don't allow click-outside / icon-bar clicks to
    // close this modal while a check is in flight, while it is gating a
    // pre-scan Start, or while a live scan is running. The user must use
    // the in-modal buttons (Start Scan / Dismiss / Stop scan) so the
    // associated scan / runner state is unwound correctly.
    readonly property bool dismissable: state_ !== "checking"
                                        && !preScanMode
                                        && !liveScan

    // Modal interface — see HistoryModal.qml for rationale. Derived
    // from state_ since this modal's title shifts with the check
    // outcome; the title Text below binds to this.
    readonly property string label: {
        if (state_ === "checking") return "Checking contact quality…"
        if (state_ === "ok")       return "Good signal quality"
        if (state_ === "error")    return "Contact check failed"
        return "Contact Quality Notification"
    }
    // Require an all-clear holdoff before enabling Continue.
    property int clearHoldoffMs: 2000
    // Active camera masks for current scan selection (used in live-scan mode).
    property int leftMask: 0xFF
    property int rightMask: 0xFF
    property string errorText: ""

    // Each entry: { camera, typeKey, typeText, value }
    property var entries: []

    readonly property bool engineeringMode: !!(MotionInterface.appConfig && MotionInterface.appConfig.engineeringMode)

    signal stopScanRequested()
    signal continueRequested()
    signal retestRequested()
    signal dismissed()
    signal forceDismissed()

    // ── public API ───────────────────────────────────────────────────────
    function open()  { root.visible = true; panel.forceActiveFocus() }
    function close() { root.visible = false }

    function reset(forLiveScan) {
        liveScan = !!forLiveScan
        liveScanDismissable = !liveScan
        clearHoldoffTimer.stop()
        entries = []
        errorText = ""
        state_ = "checking"
        if (!visible) open()
    }

    function showOk() {
        state_ = "ok"
        if (!visible) open()
    }

    function showError(msg) {
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

    // Returns the deduped typeKey array for a camera's active warnings.
    // Empty when the camera has no warnings. Used by CameraDot (#128)
    // to decide between single-color and split rendering in dev mode.
    function cameraWarningTypes(side, camIndex1) {
        var prefix = (side === "left") ? "L" : "R"
        var label = prefix + camIndex1
        var types = []
        for (var i = 0; i < entries.length; ++i) {
            if (entries[i].camera === label
                    && types.indexOf(entries[i].typeKey) === -1)
                types.push(entries[i].typeKey)
        }
        return types
    }

    function cameraTooltip(side, camIndex1) {
        var prefix = (side === "left") ? "L" : "R"
        var label = prefix + camIndex1
        var lines = [label]
        var showDn = !!(MotionInterface.appConfig && MotionInterface.appConfig.engineeringMode)
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

    function cameraEnabled(side, camIndex1) {
        // Camera N maps to bit N-1, matching every data-side consumer:
        // the SDK's `mask & (1 << cam_id)` and the connector's
        // `cam_id + 1` labels. Only the non-palindromic masks (the
        // Left/Right presets) can distinguish this from the reversed
        // mapping — see #384.
        var bit = camIndex1 - 1
        var mask = (side === "left") ? root.leftMask : root.rightMask
        return ((mask >> bit) & 1) === 1
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
        // Click-outside dismisses, but only when the modal opted in via
        // the `dismissable` property (false during checking / pre-scan
        // gating / live-scan warnings — see `dismissable` binding above).
        // hoverEnabled + onWheel capture ALL pointer input so scroll/hover
        // can't fall through to the plot viewer behind the modal (#214).
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onClicked: { if (root.dismissable) root.close() }
            onWheel: function(wheel) { wheel.accepted = true }
        }
    }

    // ── dialog panel ─────────────────────────────────────────────────────
    Rectangle {
        id: panel
        width: 520
        height: 480
        radius: 10
        color: AppTheme.sheetBg
        border.width: 2
        border.color: root.state_ === "ok" ? AppTheme.accentGreen
                    : (root.state_ === "warnings"
                       ? ((root.liveScan && root.liveScanDismissable) ? AppTheme.accentGreen : AppTheme.accentOrange)
                       : (root.state_ === "error" ? AppTheme.accentRed : AppTheme.borderSubtle))
        anchors.centerIn: parent
        focus: true

        // Absorb empty-space clicks inside the modal so they don't
        // propagate to the backdrop and close the modal (issue #106).
        MouseArea { anchors.fill: parent }

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
                color: AppTheme.textPrimary
                wrapMode: Text.WordWrap
                text: root.label
            }

            // Spinner for "checking" state. No countdown / status text:
            // camera programming now happens once at startup, so both the
            // old "Configuring sensor modules…" and "x / Ns" phases collapse
            // into a single indeterminate spinner.
            BusyIndicator {
                visible: root.state_ === "checking"
                running: visible
                Layout.alignment: Qt.AlignHCenter
            }

            // OK message
            Text {
                visible: root.state_ === "ok"
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: AppTheme.textSecondary
                font.pixelSize: 14
                text: "All cameras are reporting acceptable ambient light and contact levels."
            }

            // Warnings subtitle
            Text {
                visible: root.state_ === "warnings"
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: AppTheme.textSecondary
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
                color: AppTheme.accentRed
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
                    visible: MotionInterface.leftSensorConnected
                    width: 180; height: 210; radius: 22
                    color: AppTheme.bgCard
                    border.color: AppTheme.borderSubtle; border.width: 2

                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 8; spacing: 6

                        Text {
                            text: "Left Sensor"
                            font.pixelSize: 14; color: AppTheme.textSecondary
                            horizontalAlignment: Text.AlignHCenter
                            Layout.alignment: Qt.AlignHCenter
                        }

                        GridLayout {
                            columns: 3; columnSpacing: 16; rowSpacing: 8
                            Layout.alignment: Qt.AlignHCenter
                            property int cs: 18

                            CameraDot { side: "left"; camIndex1: 1; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "left"; camIndex1: 8; modal: root; size: parent.cs }

                            CameraDot { side: "left"; camIndex1: 2; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "left"; camIndex1: 7; modal: root; size: parent.cs }

                            CameraDot { side: "left"; camIndex1: 3; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "left"; camIndex1: 6; modal: root; size: parent.cs }

                            CameraDot { side: "left"; camIndex1: 4; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "left"; camIndex1: 5; modal: root; size: parent.cs }

                            Item {}
                            LaserDot { size: parent.cs }
                            Item {}
                        }
                    }
                }

                // Right sensor
                Rectangle {
                    visible: MotionInterface.rightSensorConnected
                    width: 180; height: 210; radius: 22
                    color: AppTheme.bgCard
                    border.color: AppTheme.borderSubtle; border.width: 2

                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 8; spacing: 6

                        Text {
                            text: "Right Sensor"
                            font.pixelSize: 14; color: AppTheme.textSecondary
                            horizontalAlignment: Text.AlignHCenter
                            Layout.alignment: Qt.AlignHCenter
                        }

                        GridLayout {
                            columns: 3; columnSpacing: 16; rowSpacing: 8
                            Layout.alignment: Qt.AlignHCenter
                            property int cs: 18

                            CameraDot { side: "right"; camIndex1: 1; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "right"; camIndex1: 8; modal: root; size: parent.cs }

                            CameraDot { side: "right"; camIndex1: 2; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "right"; camIndex1: 7; modal: root; size: parent.cs }

                            CameraDot { side: "right"; camIndex1: 3; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "right"; camIndex1: 6; modal: root; size: parent.cs }

                            CameraDot { side: "right"; camIndex1: 4; modal: root; size: parent.cs }
                            Item {}
                            CameraDot { side: "right"; camIndex1: 5; modal: root; size: parent.cs }

                            Item {}
                            LaserDot { size: parent.cs }
                            Item {}
                        }
                    }
                }
            }

            // Per-camera dot color legend (#128). Engineering mode only —
            // Research operators just see a single orange and don't need this.
            RowLayout {
                visible: root.engineeringMode
                         && (root.state_ === "ok" || root.state_ === "warnings")
                Layout.alignment: Qt.AlignHCenter
                spacing: 18

                RowLayout {
                    spacing: 6
                    Rectangle { width: 10; height: 10; radius: 5
                        color: AppTheme.accentOrangeAmbient
                        border.color: "black"; border.width: 1 }
                    Text { text: "ambient"; color: AppTheme.textSecondary; font.pixelSize: 11 }
                }
                RowLayout {
                    spacing: 6
                    Rectangle { width: 10; height: 10; radius: 5
                        color: AppTheme.accentOrangeContact
                        border.color: "black"; border.width: 1 }
                    Text { text: "contact"; color: AppTheme.textSecondary; font.pixelSize: 11 }
                }
                RowLayout {
                    spacing: 6
                    Rectangle {
                        width: 10; height: 10; radius: 5
                        border.color: "black"; border.width: 1
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0.0;    color: AppTheme.accentOrangeAmbient }
                            GradientStop { position: 0.4999; color: AppTheme.accentOrangeAmbient }
                            GradientStop { position: 0.5001; color: AppTheme.accentOrangeContact }
                            GradientStop { position: 1.0;    color: AppTheme.accentOrangeContact }
                        }
                    }
                    Text { text: "both"; color: AppTheme.textSecondary; font.pixelSize: 11 }
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
                visible: root.state_ !== "checking" || root.engineeringMode

                // Clinical-mode pre-scan footer
                Button {
                    visible: root.preScanMode
                    text: "Dismiss"
                    hoverEnabled: true
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: parent.hovered ? "#FFFFFF" : AppTheme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.accentInteractive : AppTheme.bgInput
                        radius: 4; border.color: parent.hovered ? AppTheme.accentInteractive : AppTheme.borderSoft; border.width: 1
                    }
                    onClicked: { root.close(); root.dismissed() }
                }
                Button {
                    visible: root.preScanMode
                    text: "Retest"
                    hoverEnabled: true
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: parent.hovered ? "#FFFFFF" : AppTheme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.accentInteractive : AppTheme.bgInput
                        radius: 4; border.color: parent.hovered ? AppTheme.accentInteractive : AppTheme.borderSoft; border.width: 1
                    }
                    onClicked: { root.close(); root.retestRequested() }
                }
                Button {
                    visible: root.preScanMode
                    // Never startable mid-check (issue #268): engineering
                    // mode makes this footer visible during "checking" (for
                    // Force Dismiss), which used to let Start Scan arm the
                    // scan before the check reported — the scan then began
                    // regardless of the contact-quality outcome.
                    enabled: root.state_ !== "checking"
                    text: "Start Scan"
                    hoverEnabled: enabled
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: !parent.enabled ? AppTheme.textDisabled
                              : (parent.hovered ? "#FFFFFF" : AppTheme.textSecondary)
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: !parent.enabled ? AppTheme.bgCard
                              : (parent.hovered ? AppTheme.accentGreen : AppTheme.bgInput)
                        radius: 4
                        border.color: !parent.enabled ? AppTheme.borderSubtle
                                    : (parent.hovered ? AppTheme.accentGreen : AppTheme.borderSoft)
                        border.width: 1
                    }
                    onClicked: { root.continueRequested(); root.close(); root.dismissed() }
                }

                // Live-scan footer (warnings state only)
                Button {
                    visible: !root.preScanMode && root.liveScan && root.state_ === "warnings"
                    text: "Stop scan"
                    hoverEnabled: true
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: parent.hovered ? "#FFFFFF" : AppTheme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.accentRed : AppTheme.bgInput
                        radius: 4; border.color: parent.hovered ? AppTheme.accentRed : AppTheme.borderSoft; border.width: 1
                    }
                    onClicked: { root.stopScanRequested(); root.close(); root.dismissed() }
                }
                Button {
                    visible: !root.preScanMode && root.liveScan && (root.state_ === "warnings" || root.state_ === "ok")
                    enabled: root.liveScanDismissable
                    text: "Continue"
                    hoverEnabled: enabled
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: !parent.enabled ? AppTheme.textDisabled
                              : (parent.hovered ? "#FFFFFF" : AppTheme.textSecondary)
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: !parent.enabled ? AppTheme.bgCard
                              : (parent.hovered ? AppTheme.accentInteractive : AppTheme.bgInput)
                        radius: 4
                        border.color: !parent.enabled ? AppTheme.borderSubtle
                                    : (parent.hovered ? AppTheme.accentInteractive : AppTheme.borderSoft)
                        border.width: 1
                    }
                    onClicked: { root.continueRequested(); root.close(); root.dismissed() }
                }

                // Quick-check / OK / error footer
                Button {
                    visible: !root.preScanMode && !(root.liveScan && root.state_ === "warnings")
                    text: "Dismiss"
                    hoverEnabled: true
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: parent.hovered ? "#FFFFFF" : AppTheme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.accentInteractive : AppTheme.bgInput
                        radius: 4; border.color: parent.hovered ? AppTheme.accentInteractive : AppTheme.borderSoft; border.width: 1
                    }
                    onClicked: { root.close(); root.dismissed() }
                }
                Button {
                    visible: !root.preScanMode && !(root.liveScan && root.state_ === "warnings")
                    text: "Retest"
                    hoverEnabled: true
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: parent.hovered ? "#FFFFFF" : AppTheme.textSecondary
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.accentInteractive : AppTheme.bgInput
                        radius: 4; border.color: parent.hovered ? AppTheme.accentInteractive : AppTheme.borderSoft; border.width: 1
                    }
                    onClicked: { root.close(); root.retestRequested() }
                }

                // Engineering-mode escape hatch: bypass all contact-quality
                // gates. Sticky during a live scan (#492) — the extra
                // forceDismissed() signal lets BloodFlow suppress live
                // re-opens until the scan ends. ESC (also engineering-gated)
                // stays a one-shot dismiss for when you still want the next
                // warning.
                Button {
                    objectName: "cqForceDismissBtn"
                    visible: root.engineeringMode
                    text: "Force Dismiss"
                    hoverEnabled: true
                    Layout.preferredHeight: 45
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 12
                        color: parent.hovered ? "#FFFFFF" : "#E8A020"
                        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? "#B8740F" : AppTheme.bgInput
                        radius: 4
                        border.color: parent.hovered ? "#E8A020" : "#E8A020"
                        border.width: 1
                    }
                    onClicked: { root.close(); root.forceDismissed(); root.dismissed() }
                }
            }
        }

        // ESC closes (unless we're mid-check, or awaiting Stop/Continue
        // decision during a live scan). Engineering mode bypasses all gates.
        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape
                    && (root.engineeringMode
                        || (root.state_ !== "checking"
                            && !(root.liveScan && root.state_ === "warnings" && !root.liveScanDismissable && !root.preScanMode)))) {
                root.close()
                root.dismissed()
                event.accepted = true
            }
        }
    }
}
