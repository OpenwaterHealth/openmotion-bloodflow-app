import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Laser optimizer progress (#746, epic #745). Presentation only: the SDK's
// LaserOptimizerWorkflow probes, decides and scans; the connector forwards
// each probe (laserOptimizerProbe → addProbe) and the result
// (laserOptimizerFinished → finished). BloodFlow closes this when the final
// scan starts so the live plots are visible, and reopens it for any outcome
// that needs the operator (not enough light → reposition and Retry).
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    // Modal interface — see HistoryModal.qml for rationale.
    readonly property string label: "Laser Optimizer"
    readonly property int iconBarInset: 104

    // "probing" | "scanning" | "done"
    property string phase: "probing"
    property string outcome: ""
    property string statusText: ""
    readonly property bool canRetry: phase === "done"
        && ["insufficient_signal", "too_bright", "no_convergence", "error"]
               .indexOf(outcome) >= 0
    readonly property bool failed: phase === "done"
        && ["completed", "scan_stopped", "canceled"].indexOf(outcome) < 0

    signal retryRequested()
    signal stopRequested()

    ListModel { id: probeModel }

    // One fixed-width cell of the probe table.
    component Cell: Text {
        property real w: 80
        Layout.preferredWidth: w
        font.pixelSize: 12
        font.family: "Consolas, monospace"
        color: AppTheme.textSecondary
        elide: Text.ElideRight
    }
    readonly property int probeCount: probeModel.count
    // The operator settings this run uses (Settings → Laser Optimizer).
    readonly property var opt: MotionInterface.laserOptimizerSettings || ({})

    function reset() {
        probeModel.clear()
        phase = "probing"
        outcome = ""
        statusText = "Starting at " + opt.startGain + "x gain and a "
                     + opt.startPulseUs + " µs pulse…"
    }
    function addProbe(row) {
        probeModel.append(row)
        statusText = row.action === "probe" ? "Next: " + row.next + " — " + row.reason
                   : row.action === "accept" ? "Accepted " + row.setting + " — " + row.reason
                   : row.reason
    }
    function scanStarted(setting) {
        phase = "scanning"
        statusText = "Scanning at " + setting
    }
    function finished(outcomeValue, text) {
        phase = "done"
        outcome = outcomeValue
        statusText = text
    }
    function open() {
        root.visible = true
        card.forceActiveFocus()
    }
    function close() { root.visible = false }

    // Dimmed backdrop. Closing only hides the progress view; the run keeps
    // going (Stop in the panel or here ends it).
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
        width: Math.min(parent.width - root.iconBarInset - 40, 640)
        height: contentCol.implicitHeight + 48
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
                    text: "Laser optimizer"
                    color: AppTheme.textPrimary
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                }
                Item { Layout.fillWidth: true }
                BusyIndicator {
                    running: root.phase === "probing"
                    visible: running
                    Layout.preferredWidth: 24
                    Layout.preferredHeight: 24
                }
            }

            Text {
                text: "Tunes the laser pulse width and camera gain until the near "
                      + "cameras (4 and 5) read " + root.opt.targetMeanDn + " DN (± "
                      + root.opt.tolerancePct + " %), then scans for "
                      + root.opt.scanDurationSec + " s. Engineering experiments "
                      + "only: the laser-safety limits are widened for the run and "
                      + "restored afterwards."
                color: AppTheme.textTertiary
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // ── Probe table ──
            ColumnLayout {
                objectName: "laserOptimizerProbeTable"
                Layout.fillWidth: true
                spacing: 2

                RowLayout {
                    spacing: 8
                    Cell { w: 24;  text: "#";          color: AppTheme.textTertiary }
                    Cell { w: 120; text: "Gain / pulse"; color: AppTheme.textTertiary }
                    Cell { w: 76;  text: "Near mean";  color: AppTheme.textTertiary }
                    Cell { w: 64;  text: "Clipped";    color: AppTheme.textTertiary }
                    Cell { w: 70;  text: "Camera";     color: AppTheme.textTertiary }
                    Cell { w: 60;  text: "Dark Δ";     color: AppTheme.textTertiary }
                    Cell { w: 130; text: "Next";       color: AppTheme.textTertiary }
                }
                Rectangle { Layout.fillWidth: true; height: 1; color: AppTheme.borderSoft }

                Repeater {
                    model: probeModel
                    delegate: RowLayout {
                        spacing: 8
                        Cell { w: 24;  text: model.probe }
                        Cell { w: 120; text: model.setting; color: AppTheme.textPrimary }
                        Cell {
                            w: 76
                            text: model.meanDn >= 0 ? model.meanDn.toFixed(1) + " DN" : "—"
                            color: model.action === "accept" ? AppTheme.accentGreen
                                                              : AppTheme.textSecondary
                        }
                        Cell {
                            w: 64
                            text: model.clippedPct >= 0 ? model.clippedPct.toFixed(1) + " %" : "—"
                            color: model.clipped ? AppTheme.accentYellow : AppTheme.textSecondary
                        }
                        Cell { w: 70;  text: model.camera }
                        // Scheduled dark minus the probe's laser-off frame.
                        Cell { w: 60;  text: model.darkLeak }
                        Cell {
                            w: 130
                            text: model.action === "probe" ? model.next
                                : model.action === "accept" ? "scan"
                                : model.action.replace("_", " ")
                        }
                    }
                }

                Text {
                    visible: probeModel.count === 0
                    text: "No probes yet."
                    color: AppTheme.textTertiary
                    font.pixelSize: 12
                }

                Text {
                    visible: probeModel.count > 0
                    Layout.topMargin: 4
                    text: "Each probe averages " + root.opt.probeFrames + " light frames. "
                          + "Dark Δ: scheduled dark frame minus the laser-off frame, "
                          + "largest over the cameras (≈ 0 DN when the dark is clean; "
                          + "above 5 DN stops the run)."
                    color: AppTheme.textTertiary
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
            }

            Text {
                objectName: "laserOptimizerStatus"
                text: root.statusText
                color: root.failed ? AppTheme.accentOrange : AppTheme.textSecondary
                font.pixelSize: 13
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Item { Layout.fillWidth: true }

                Button {
                    text: root.phase === "done" ? "Close" : "Hide"
                    Layout.preferredHeight: 32
                    onClicked: root.close()
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: AppTheme.textSecondary
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
                    visible: root.phase !== "done"
                    text: "Stop"
                    Layout.preferredHeight: 32
                    onClicked: root.stopRequested()
                    contentItem: Text {
                        text: parent.text; font.pixelSize: 13
                        color: "#FFFFFF"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                    background: Rectangle {
                        color: parent.hovered ? Qt.lighter(AppTheme.accentRed, 1.1) : AppTheme.accentRed
                        radius: 4
                    }
                }

                Button {
                    objectName: "laserOptimizerRetry"
                    visible: root.canRetry
                    text: "Retry"
                    Layout.preferredHeight: 32
                    onClicked: root.retryRequested()
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
