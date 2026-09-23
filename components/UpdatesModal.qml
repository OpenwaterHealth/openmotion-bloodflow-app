import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Every pending update in one place (#514): the application and each
// device's firmware, each with its own Update button, plus "Update all".
// Opened from UpdateBanner's "Review", and raised on its own the first
// time a new update is detected (deferred while a scan / check runs).
//
// Presentation only. The connector owns every gate (clinical builds, scan
// running, one update at a time) and the "Update all" order: firmware
// first (sensors, then console), then the app install, which quits and
// relaunches the app (startUpdateAll).
Item {
    id: root
    anchors.fill: parent
    visible: false
    // Above every page modal (9998-10000) and the icon bar, below toasts
    // (99999) and the critical-error modal (100000).
    z: 10500

    readonly property string label: "Software Update"
    readonly property bool dismissable: !MotionInterface.updateBusy

    // Set by the host: true while a scan / check is running. An automatic
    // raise waits for it to clear; the Review button always opens.
    property bool deferAutoOpen: false

    readonly property bool _clinical: MotionInterface.appConfig.clinicalMode === true

    // Per-row progress, keyed "app"/"console"/"left"/"right":
    // { state: "running"|"done"|"failed", text: string, pct: int }.
    // Reassigned (never mutated) so bindings re-evaluate.
    property var rowStatus: ({})
    // "" | "all" | a row key — the action awaiting confirmation.
    property string confirmAction: ""
    property string footerText: ""
    property bool footerOk: true

    // Offers already raised automatically this launch ("key@version").
    property var _offered: ({})
    property bool _autoOpenPending: false

    readonly property var rows: {
        var out = []
        function add(key, name, current, latest, avail) {
            var st = rowStatus[key]
            if (avail || st !== undefined)
                out.push({ key: key, name: name, current: current || "—",
                           latest: latest || "", available: avail })
        }
        // Listed in "Update all" order, so the list reads as its sequence.
        add("left", "Left sensor firmware",
            MotionInterface.leftSensorFirmwareVersion,
            MotionInterface.leftSensorFirmwareLatest,
            MotionInterface.leftSensorFirmwareUpdateAvailable)
        add("right", "Right sensor firmware",
            MotionInterface.rightSensorFirmwareVersion,
            MotionInterface.rightSensorFirmwareLatest,
            MotionInterface.rightSensorFirmwareUpdateAvailable)
        add("console", "Console firmware",
            MotionInterface.consoleFirmwareVersion,
            MotionInterface.consoleFirmwareLatest,
            MotionInterface.consoleFirmwareUpdateAvailable)
        add("app", "Application",
            typeof appVersion !== "undefined" ? appVersion : "",
            MotionInterface.appUpdateLatest,
            MotionInterface.appUpdateAvailable)
        return out
    }
    readonly property int pendingCount: {
        var n = 0
        for (var i = 0; i < rows.length; i++) if (rows[i].available) n++
        return n
    }

    function open() {
        if (root._clinical) return
        root._autoOpenPending = false
        root.visible = true
        card.forceActiveFocus()
    }
    function close() {
        if (!root.dismissable) return
        root.visible = false
        root.confirmAction = ""
    }

    function _labelFor(key) {
        return { app: "The application", console: "The console",
                 left: "The left sensor", right: "The right sensor" }[key] || key
    }
    function _setRow(key, state, text, pct) {
        var s = Object.assign({}, rowStatus)
        s[key] = { state: state, text: text, pct: pct === undefined ? -1 : pct }
        rowStatus = s
    }
    function _offer(key, version) {
        if (root._clinical) return
        var id = key + "@" + version
        if (_offered[id]) return
        var o = Object.assign({}, _offered)
        o[id] = true
        _offered = o
        if (root.visible) return
        if (root.deferAutoOpen) root._autoOpenPending = true
        else root.open()
    }
    onDeferAutoOpenChanged: if (!deferAutoOpen && _autoOpenPending) open()

    function _confirmText(action) {
        var fwNote = " Do not unplug anything until it finishes, then "
                   + "power-cycle the device so it boots the new firmware."
        if (action === "all") {
            var fw = [], hasApp = false
            for (var i = 0; i < rows.length; i++) {
                if (!rows[i].available) continue
                if (rows[i].key === "app") hasApp = true
                else fw.push(rows[i].name.replace(" firmware", ""))
            }
            var t = ""
            if (fw.length)
                t += "Firmware is flashed first, one device at a time: "
                   + fw.join(", ") + ". Each reboots into DFU mode."
            if (hasApp)
                t += (t ? " Then the" : "The") + " application update is "
                   + "downloaded and installed; the app closes and relaunches."
            if (fw.length)
                t += " Do not unplug anything until it finishes. Power-cycle "
                   + "the console and sensors afterwards so they boot the new firmware."
            return t
        }
        if (action === "app")
            return "The application update is downloaded and verified, then "
                 + "the app closes, installs it and relaunches."
        return _labelFor(action) + " will reboot into DFU mode and be re-flashed." + fwNote
    }

    function _run(action) {
        root.confirmAction = ""
        root.footerText = ""
        if (action === "all") {
            MotionInterface.startUpdateAll()
        } else if (action === "app") {
            _setRow("app", "running", "Starting…")
            MotionInterface.applyUpdate(MotionInterface.appUpdateUrl)
        } else {
            _setRow(action, "running", "Starting…")
            MotionInterface.startFirmwareUpdate(action)
        }
    }

    Connections {
        target: MotionInterface
        function onUpdateAvailable(version, url) { root._offer("app", version) }
        function onFirmwareUpdateAvailable(deviceKey, current, latest) {
            root._offer(deviceKey, latest)
        }
        function onFirmwareUpdateProgress(deviceKey, stage, percent, msg) {
            root._setRow(deviceKey, "running", msg, percent)
        }
        function onFirmwareUpdateFinished(deviceKey, ok, msg) {
            root._setRow(deviceKey, ok ? "done" : "failed",
                         ok ? "Written. Power-cycle to finish." : msg)
        }
        function onUpdateProgress(message) { root._setRow("app", "running", message) }
        function onUpdateCheckFailed(msg) {
            // Also fired by a failed background check; only a running
            // install owns the row.
            var st = root.rowStatus["app"]
            if (st && st.state === "running") root._setRow("app", "failed", msg)
        }
        function onBatchUpdateStep(step) {
            root._setRow(step, "running", step === "app" ? "Starting…" : "Waiting for device…")
        }
        function onBatchUpdateFinished(ok, msg) {
            root.footerText = msg
            root.footerOk = ok
        }
    }

    // Dimmed backdrop
    Rectangle {
        anchors.fill: parent
        color: "#000000AA"
        // Capture ALL pointer input so scroll/hover can't fall through to
        // the interactive plot viewer behind the modal (issue #214).
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onClicked: root.close()
            onWheel: function(wheel) { wheel.accepted = true }
        }
    }

    component ModalButton: Button {
        id: btn
        property bool primary: false
        Layout.preferredHeight: 32
        contentItem: Text {
            text: btn.text; font.pixelSize: 13
            color: btn.primary ? "#FFFFFF" : AppTheme.textSecondary
            opacity: btn.enabled ? 1.0 : 0.5
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 4
            opacity: btn.enabled ? 1.0 : 0.5
            color: btn.primary
                ? (btn.hovered ? Qt.lighter(AppTheme.accentInteractive, 1.1) : AppTheme.accentInteractive)
                : (btn.hovered ? AppTheme.bgHover : AppTheme.bgInput)
            border.color: btn.primary ? "transparent" : AppTheme.borderSoft
            border.width: btn.primary ? 0 : 1
        }
    }

    Rectangle {
        id: card
        width: Math.min(parent.width - 40, 560)
        height: contentCol.implicitHeight + 48
        radius: 12
        color: AppTheme.sheetBg
        border.color: AppTheme.borderSubtle
        border.width: 2
        anchors.centerIn: parent

        // Absorb empty-space clicks so they don't reach the backdrop.
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            id: contentCol
            anchors.fill: parent
            anchors.margins: 24
            spacing: 16

            Text {
                text: root.pendingCount > 0 ? "Updates available" : "Software updates"
                color: AppTheme.textPrimary
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }

            Text {
                visible: root.pendingCount > 0
                text: "Update each item on its own, or choose Update all to flash "
                    + "the device firmware first and then install the application update."
                color: AppTheme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Text {
                visible: root.rows.length === 0
                text: "Everything is up to date."
                color: AppTheme.textSecondary
                font.pixelSize: 13
            }

            // ── One row per pending (or just-updated) item ──────────────
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                visible: root.rows.length > 0

                Repeater {
                    model: root.rows
                    delegate: ColumnLayout {
                        id: row
                        required property var modelData
                        required property int index
                        readonly property var st: root.rowStatus[modelData.key]
                        readonly property string phase: st ? st.state : ""
                        Layout.fillWidth: true
                        spacing: 6

                        Rectangle {
                            Layout.fillWidth: true; height: 1
                            color: AppTheme.borderSoft
                            visible: row.index > 0
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            Layout.topMargin: 6
                            Layout.bottomMargin: 6
                            spacing: 12

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2
                                // fillWidth on the Texts too: without it this
                                // column's max width is its implicit width and
                                // the Update button hugs the name.
                                Text {
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    text: row.modelData.name
                                    color: AppTheme.textPrimary
                                    font.pixelSize: 14
                                    font.weight: Font.DemiBold
                                }
                                Text {
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    text: row.modelData.current
                                        + (row.modelData.latest ? "  →  " + row.modelData.latest : "")
                                    color: AppTheme.textSecondary
                                    font.pixelSize: 12
                                    font.family: "Consolas"
                                }
                            }

                            Text {
                                visible: row.phase !== ""
                                text: row.st ? row.st.text + (row.st.pct >= 0 ? "  " + row.st.pct + "%" : "") : ""
                                color: row.phase === "failed" ? AppTheme.accentRed
                                     : row.phase === "done" ? AppTheme.accentGreen
                                     : AppTheme.textSecondary
                                font.pixelSize: 12
                                horizontalAlignment: Text.AlignRight
                                wrapMode: Text.WordWrap
                                Layout.maximumWidth: 220
                            }

                            ModalButton {
                                visible: row.modelData.available && row.phase !== "running"
                                text: row.phase === "failed" ? "Retry" : "Update"
                                enabled: !MotionInterface.updateBusy && root.confirmAction === ""
                                onClicked: root.confirmAction = row.modelData.key
                            }
                        }

                        ProgressBar {
                            Layout.fillWidth: true
                            Layout.bottomMargin: 4
                            visible: row.phase === "running"
                            from: 0; to: 100
                            indeterminate: !row.st || row.st.pct < 0
                            value: row.st && row.st.pct >= 0 ? row.st.pct : 0
                        }
                    }
                }
            }

            Text {
                visible: root.footerText.length > 0
                text: root.footerText
                color: root.footerOk ? AppTheme.accentGreen : AppTheme.accentRed
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // ── Confirmation for the chosen action ─────────────────────
            Rectangle {
                Layout.fillWidth: true
                visible: root.confirmAction !== ""
                implicitHeight: confirmCol.implicitHeight + 24
                radius: 8
                color: AppTheme.bgInput
                border.color: AppTheme.borderSoft
                border.width: 1

                ColumnLayout {
                    id: confirmCol
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 10
                    Text {
                        text: root.confirmAction !== "" ? root._confirmText(root.confirmAction) : ""
                        color: AppTheme.textPrimary
                        font.pixelSize: 13
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12
                        Item { Layout.fillWidth: true }
                        ModalButton {
                            text: "Cancel"
                            onClicked: root.confirmAction = ""
                        }
                        ModalButton {
                            primary: true
                            text: root.confirmAction === "all" ? "Update all" : "Update"
                            onClicked: root._run(root.confirmAction)
                        }
                    }
                }
            }

            // ── Footer ─────────────────────────────────────────────────
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                visible: root.confirmAction === ""
                Item { Layout.fillWidth: true }

                ModalButton {
                    text: root.pendingCount > 0 ? "Later" : "Close"
                    enabled: root.dismissable
                    onClicked: root.close()
                }
                ModalButton {
                    primary: true
                    visible: root.pendingCount > 1
                    text: MotionInterface.batchUpdateRunning ? "Updating…" : "Update all"
                    enabled: !MotionInterface.updateBusy
                    onClicked: root.confirmAction = "all"
                }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
