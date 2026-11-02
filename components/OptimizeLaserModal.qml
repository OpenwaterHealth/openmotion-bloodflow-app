import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Optimize Laser progress and result (docs/auto_exposure). Presentation
// only: BloodFlow's optimize ScanRunner drives the run, the connector owns
// every gate, and this modal mirrors optimizeLaserStarted / Progress /
// Finished. Not dismissable while a run is in flight (Cancel stops it; the
// run restores the defaults before it reports).
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    // Modal interface — see HistoryModal.qml for rationale.
    readonly property string label: "Optimize Laser"
    readonly property bool dismissable: !running
    readonly property int iconBarInset: 104

    property bool running: false
    property bool canceling: false
    property bool finished: false
    property bool ok: false
    property string stage: ""
    property string message: ""
    property var alerts: []
    property var cameras: []          // latest iteration's camera rows
    property int iteration: 0
    property var pulseWidthUs: null
    property var emittedPulseWidthUs: null
    property var currentMa: null
    property string csvName: ""
    // The run hit its iteration limit and waits for the operator.
    property bool deciding: false
    property string question: ""
    // Start to finish, button press included (the camera setup counts).
    property double startedAt: 0
    property string timeText: ""

    signal cancelRequested()

    function reset() {
        running = true; canceling = false; finished = false; ok = false
        stage = "Preparing…"; message = ""; alerts = []; cameras = []
        iteration = 0; pulseWidthUs = null; emittedPulseWidthUs = null
        currentMa = null; csvName = ""; deciding = false; question = ""
        startedAt = Date.now(); timeText = ""
    }
    function open() { root.visible = true; card.forceActiveFocus() }
    function close() { if (root.dismissable) root.visible = false }
    // Flash / start failures that never reach the connector's run.
    function showError(text) {
        running = false; canceling = false; finished = true; ok = false
        deciding = false
        message = text
        timeText = _elapsedText(null)
    }
    function answer(choice) {
        deciding = false
        stage = choice === "continue" ? "Continuing…"
              : choice === "accept" ? "Keeping the current settings…"
              : "Stopping, restoring the defaults…"
        MotionInterface.answerOptimizeLaser(choice)
    }

    function _duration(s) {
        s = Math.round(s)
        return s >= 60 ? (Math.floor(s / 60) + " min " + (s % 60) + " s") : (s + " s")
    }
    function _elapsedText(runSeconds) {
        if (!startedAt) return ""
        var total = (Date.now() - startedAt) / 1000
        var t = "Total time: " + _duration(total)
        if (runSeconds !== null && runSeconds !== undefined)
            t += " (camera setup " + _duration(Math.max(0, total - runSeconds))
                 + ", optimizing " + _duration(runSeconds) + ")"
        return t
    }

    function _fmt(v, unit) { return (v === null || v === undefined) ? "—" : (Math.round(v) + " " + unit) }
    function _statusColor(s) {
        if (s === "on_target") return AppTheme.statusGreen
        if (s === "too_dim" || s === "too_bright") return AppTheme.accentYellow
        if (s === "no_data") return AppTheme.accentRed
        return AppTheme.textSecondary
    }
    // Short, so both sensors' tables fit side by side; the alert lines
    // below the table spell out "at maximum / minimum gain".
    function _statusText(s) {
        return ({ "on_target": "on target", "too_dim": "too dim",
                  "too_bright": "too bright", "adjusting": "adjusting",
                  "no_data": "no data" })[s] || s
    }
    function _side(side) {
        return cameras.filter(function(c) { return c.side === side })
    }

    Connections {
        target: MotionInterface
        function onOptimizeLaserStarted() {
            root.stage = "Optimizing laser and camera settings…"
        }
        function onOptimizeLaserProgress(row) {
            root.iteration = row.iteration
            root.pulseWidthUs = row.pulseWidthUs
            root.emittedPulseWidthUs = row.emittedPulseWidthUs
            root.currentMa = row.currentMa
            root.cameras = row.cameras
            root.stage = row.done ? "Finishing…" : ("Iteration " + row.iteration + " done, next: "
                         + root._fmt(row.next.pulseWidthUs, "µs") + ", "
                         + root._fmt(row.next.currentMa, "mA"))
        }
        function onOptimizeLaserDecisionNeeded(q) {
            root.iteration = q.iteration
            root.pulseWidthUs = q.pulseWidthUs
            root.emittedPulseWidthUs = q.emittedPulseWidthUs
            root.currentMa = q.currentMa
            root.cameras = q.cameras
            root.question = q.question
            root.deciding = true
        }
        function onOptimizeLaserFinished(ok, message, summary) {
            root.running = false
            root.canceling = false
            root.deciding = false
            root.finished = true
            root.ok = ok
            root.message = message
            root.timeText = root._elapsedText(summary.runSeconds)
            root.alerts = summary.alerts || []
            root.csvName = summary.csvName || ""
            if (summary.cameras && summary.cameras.length) root.cameras = summary.cameras
            if (summary.pulseWidthUs !== undefined) {
                root.pulseWidthUs = summary.pulseWidthUs
                root.emittedPulseWidthUs = summary.emittedPulseWidthUs
                root.currentMa = summary.currentMa
                root.iteration = summary.iterations || root.iteration
            }
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

    Rectangle {
        id: card
        objectName: "optimizeLaserCard"
        width: Math.min(parent.width - root.iconBarInset - 40, 660)
        height: Math.min(parent.height - 40, contentCol.implicitHeight + 48)
        radius: 12
        color: AppTheme.sheetBg
        border.color: AppTheme.borderSubtle
        border.width: 2
        anchors.verticalCenter: parent.verticalCenter
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: root.iconBarInset / 2

        // Absorb empty-space clicks so they don't reach the backdrop.
        MouseArea { anchors.fill: parent }

        ColumnLayout {
            id: contentCol
            anchors.fill: parent
            anchors.margins: 24
            spacing: 14

            RowLayout {
                Layout.fillWidth: true
                Text {
                    text: "Optimize Laser"
                    color: AppTheme.textPrimary
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                }
                Item { Layout.fillWidth: true }
                BusyIndicator {
                    running: root.running
                    visible: root.running
                    Layout.preferredWidth: 26; Layout.preferredHeight: 26
                }
            }

            Text {
                objectName: "optimizeLaserQuestion"
                visible: root.deciding
                text: root.question
                color: AppTheme.accentYellow
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            Text {
                visible: !root.deciding
                text: root.finished ? root.message : (root.canceling ? "Canceling, restoring the defaults…" : root.stage)
                color: root.finished ? (root.ok ? AppTheme.textPrimary : AppTheme.accentYellow) : AppTheme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // Shared laser settings of the latest iteration (or the result).
            Text {
                visible: root.pulseWidthUs !== null
                text: (root.finished && root.ok ? "Settings: " : "Iteration " + root.iteration + ": ")
                      + "pulse " + root._fmt(root.pulseWidthUs, "µs")
                      + " (~" + root._fmt(root.emittedPulseWidthUs, "µs") + " emitted), current "
                      + root._fmt(root.currentMa, "mA") + ", exposure 2196 µs"
                color: AppTheme.textSecondary
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // Per-camera table, one column block per sensor module. Fixed
            // column widths (status elided), so it never runs past the card.
            RowLayout {
                objectName: "optimizeLaserTable"
                visible: root.cameras.length > 0
                Layout.fillWidth: true
                spacing: 24
                Repeater {
                    model: ["left", "right"]
                    delegate: ColumnLayout {
                        id: sideCol
                        readonly property var rows: root._side(modelData)
                        visible: rows.length > 0
                        Layout.alignment: Qt.AlignTop
                        spacing: 4
                        Text {
                            text: modelData === "left" ? "Left sensor" : "Right sensor"
                            color: AppTheme.textPrimary
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Repeater {
                            model: sideCol.rows
                            delegate: RowLayout {
                                spacing: 8
                                Text { text: modelData.camera; color: AppTheme.textSecondary; font.pixelSize: 12; Layout.preferredWidth: 24 }
                                Text {
                                    text: (Math.round(modelData.gain * 10000) / 10000) + "×"
                                    color: AppTheme.textSecondary; font.pixelSize: 12
                                    Layout.preferredWidth: 58; horizontalAlignment: Text.AlignRight
                                }
                                Text {
                                    // Dark-subtracted signal; a saturated frame has none.
                                    text: modelData.signal !== null && modelData.signal !== undefined
                                          ? (Math.round(modelData.signal) + " DN") : "saturated"
                                    color: AppTheme.textPrimary; font.pixelSize: 12
                                    Layout.preferredWidth: 72; horizontalAlignment: Text.AlignRight
                                }
                                Text {
                                    text: root._statusText(modelData.status)
                                    color: root._statusColor(modelData.status); font.pixelSize: 12
                                    Layout.preferredWidth: 76
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }
                }
            }

            Repeater {
                model: root.alerts
                delegate: Text {
                    text: "• " + modelData
                    color: AppTheme.accentYellow
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
            }

            Text {
                objectName: "optimizeLaserTime"
                visible: root.finished && root.timeText !== ""
                text: root.timeText
                color: AppTheme.textSecondary
                font.pixelSize: 12
                Layout.fillWidth: true
            }

            Text {
                visible: root.finished && root.csvName !== ""
                text: "Log: data/optimize-laser/" + root.csvName
                color: AppTheme.textTertiary
                font.pixelSize: 11
                Layout.fillWidth: true
                elide: Text.ElideMiddle
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Item { Layout.fillWidth: true }

                // The iteration-limit question: keep going, keep what the
                // device holds now, or stop (which restores the defaults).
                Repeater {
                    model: root.deciding ? [
                        { choice: "stop", text: "Stop and restore defaults" },
                        { choice: "accept", text: "Accept current settings" },
                        { choice: "continue", text: "Keep iterating" }
                    ] : []
                    delegate: Button {
                        objectName: "optimizeLaserAnswer_" + modelData.choice
                        text: modelData.text
                        Layout.preferredHeight: 32
                        onClicked: root.answer(modelData.choice)
                        contentItem: Text {
                            text: parent.text; font.pixelSize: 13
                            color: modelData.choice === "continue" ? "#FFFFFF" : AppTheme.textSecondary
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            color: modelData.choice === "continue"
                                   ? (parent.hovered ? Qt.lighter(AppTheme.accentInteractive, 1.1)
                                                     : AppTheme.accentInteractive)
                                   : (parent.hovered ? AppTheme.bgHover : AppTheme.bgInput)
                            radius: 4
                            border.color: AppTheme.borderSoft
                            border.width: modelData.choice === "continue" ? 0 : 1
                        }
                    }
                }

                Button {
                    objectName: "optimizeLaserCancelButton"
                    visible: root.running && !root.deciding
                    enabled: !root.canceling
                    text: "Cancel"
                    Layout.preferredHeight: 32
                    onClicked: { root.canceling = true; root.cancelRequested() }
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: parent.enabled ? AppTheme.textSecondary : AppTheme.textDisabled
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? AppTheme.bgHover : AppTheme.bgInput
                        radius: 4
                        border.color: AppTheme.borderSoft; border.width: 1
                    }
                }

                Button {
                    visible: !root.running
                    text: "Close"
                    Layout.preferredHeight: 32
                    onClicked: root.close()
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: "#FFFFFF"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? Qt.lighter(AppTheme.accentInteractive, 1.1) : AppTheme.accentInteractive
                        radius: 4
                    }
                }
            }
        }

        Keys.onReleased: function(event) {
            if (event.key === Qt.Key_Escape) { root.close(); event.accepted = true }
        }
    }
}
