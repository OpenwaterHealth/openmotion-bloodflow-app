import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
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
 *      addWarning(cameraLabel, typeText, value)
 *                                -> append a dedup'd warning row,
 *                                   auto-transitioning to "warnings" state
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
    property string errorText: ""

    // Each entry: { camera: "L4", typeText: "Poor sensor contact", value: 72.5 }
    property var entries: []

    signal stopScanRequested()
    signal continueRequested()
    signal dismissed()

    // ── public API ───────────────────────────────────────────────────────
    function open()  { root.visible = true }
    function close() { root.visible = false }

    function reset(forLiveScan) {
        liveScan = !!forLiveScan
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

    // Append a warning row. Duplicates (same camera+type) are ignored.
    function addWarning(cameraLabel, typeText, value) {
        for (var i = 0; i < entries.length; ++i) {
            if (entries[i].camera === cameraLabel && entries[i].typeText === typeText)
                return
        }
        var copy = entries.slice()
        copy.push({ camera: cameraLabel, typeText: typeText, value: value })
        entries = copy
        state_ = "warnings"
        if (!visible) open()
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
        height: 420
        radius: 10
        color: theme.bgContainer
        border.width: 2
        border.color: root.state_ === "ok"       ? theme.accentGreen
                    : root.state_ === "warnings" ? theme.accentOrange
                    : root.state_ === "error"    ? theme.accentRed
                    :                              theme.borderSubtle
        anchors.centerIn: parent
        focus: true

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            // Title
            Text {
                Layout.fillWidth: true
                font.pixelSize: 20
                font.bold: true
                color: theme.textPrimary
                wrapMode: Text.WordWrap
                text: {
                    if (root.state_ === "checking") return "Checking contact quality…"
                    if (root.state_ === "ok")       return "Good signal quality"
                    if (root.state_ === "error")    return "Contact check failed"
                    return "Contact quality warnings"
                }
            }

            // Spinner for "checking" state
            BusyIndicator {
                visible: root.state_ === "checking"
                running: visible
                Layout.alignment: Qt.AlignHCenter
            }

            // OK message
            Text {
                visible: root.state_ === "ok"
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                color: theme.textSecondary
                font.pixelSize: 14
                text: "All cameras are reporting acceptable ambient light and contact levels."
            }

            // Error message
            Text {
                visible: root.state_ === "error"
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                color: theme.accentRed
                font.pixelSize: 14
                text: root.errorText
            }

            // Warning list
            ListView {
                visible: root.state_ === "warnings"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: root.entries
                spacing: 6
                delegate: Rectangle {
                    width: ListView.view ? ListView.view.width : 0
                    height: 36
                    color: theme.bgCard
                    radius: 4
                    border.color: theme.borderSoft
                    border.width: 1
                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 12
                        Text {
                            text: modelData.camera
                            color: theme.textPrimary
                            font.bold: true
                            font.pixelSize: 14
                            Layout.preferredWidth: 50
                        }
                        Text {
                            text: modelData.typeText
                            color: theme.textPrimary
                            font.pixelSize: 14
                            Layout.fillWidth: true
                        }
                        Text {
                            text: modelData.value.toFixed(1) + " DN"
                            color: theme.textSecondary
                            font.pixelSize: 12
                        }
                    }
                }
            }

            // Spacer so the footer sticks to the bottom when state has no
            // fillHeight content (checking / ok / error).
            Item {
                visible: root.state_ !== "warnings"
                Layout.fillHeight: true
                Layout.fillWidth: true
            }

            // Footer buttons
            RowLayout {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignRight
                spacing: 12
                visible: root.state_ !== "checking"

                // Live-scan footer (warnings state only)
                Button {
                    visible: root.liveScan && root.state_ === "warnings"
                    text: "Stop scan"
                    onClicked: {
                        root.stopScanRequested()
                        root.close()
                        root.dismissed()
                    }
                }
                Button {
                    visible: root.liveScan && root.state_ === "warnings"
                    text: "Continue"
                    onClicked: {
                        root.continueRequested()
                        root.close()
                        root.dismissed()
                    }
                }

                // Quick-check / OK / error footer
                Button {
                    visible: !(root.liveScan && root.state_ === "warnings")
                    text: "Dismiss"
                    onClicked: {
                        root.close()
                        root.dismissed()
                    }
                }
            }
        }

        // ESC closes (unless we're mid-check).
        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape && root.state_ !== "checking") {
                root.close()
                root.dismissed()
                event.accepted = true
            }
        }
    }
}
