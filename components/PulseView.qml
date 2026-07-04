import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  PulseView — inline cardiac pulse-waveform viewer (Research "Pulse" viewer
 *  mode; selected in Scan Settings). Fills the main content area in place of
 *  the PlotViewer: a left and a right window (template pulse over a min/max
 *  envelope with the live beat) plus a left-vs-right pulse-shape stats panel.
 *
 *  Data arrives on MotionInterface.pulseSnapshot(side, map): from the SDK
 *  "pulse" pipeline channel during a live scan, or — when idle — from the
 *  synthetic PulseDemoController so the viewer previews without hardware.
 */
Item {
    id: root

    // True when this is the selected viewer (BloodFlow.pulseViewerActive).
    property bool active: false
    // True while a scan is running — live pipeline data takes over from the demo.
    property bool scanning: false

    property var leftSnap: null
    property var rightSnap: null
    property real yMin: 0.0
    property real yMax: 10.0
    property string demoPreset: "normal"
    property real demoBpm: 72.0

    function _startDemoIfIdle() {
        if (active && !scanning && MotionInterface.appConfig.pulseView !== false)
            MotionInterface.startPulseDemo(root.demoPreset, root.demoBpm)
    }
    onActiveChanged: {
        if (active) {
            root.leftSnap = null
            root.rightSnap = null
            _startDemoIfIdle()
        } else {
            MotionInterface.stopPulseDemo()
        }
    }
    onScanningChanged: {
        if (scanning) MotionInterface.stopPulseDemo()
        else _startDemoIfIdle()
    }

    function _extent(snap, ext) {
        if (!snap || snap.beatCount <= 0) return ext
        var arrs = [snap.envMin, snap.envMax, snap.liveValue]
        for (var a = 0; a < arrs.length; a++) {
            var arr = arrs[a]
            if (!arr) continue
            for (var i = 0; i < arr.length; i++) {
                if (isFinite(arr[i])) {
                    if (arr[i] < ext[0]) ext[0] = arr[i]
                    if (arr[i] > ext[1]) ext[1] = arr[i]
                }
            }
        }
        return ext
    }
    function _recomputeScale() {
        var ext = _extent(rightSnap, _extent(leftSnap, [Infinity, -Infinity]))
        if (ext[0] === Infinity) { yMin = 0.0; yMax = 10.0; return }
        var pad = (ext[1] - ext[0]) * 0.12
        if (pad <= 0) pad = 0.5
        yMin = ext[0] - pad
        yMax = ext[1] + pad
    }

    Connections {
        target: MotionInterface
        function onPulseSnapshot(side, payload) {
            if (side === "left") root.leftSnap = payload
            else if (side === "right") root.rightSnap = payload
            root._recomputeScale()
        }
    }

    Rectangle {
        anchors.fill: parent
        color: AppTheme.bgBase
        radius: 0
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 12

        // ── header ───────────────────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            Text {
                text: "Pulse Waveform"
                color: AppTheme.textPrimary
                font.pixelSize: 22; font.bold: true
            }
            Rectangle {
                visible: MotionInterface.pulseDemoActive
                Layout.preferredHeight: 22
                Layout.preferredWidth: demoLbl.width + 20
                radius: 11
                color: Qt.rgba(0.29, 0.56, 0.89, 0.20)
                Text {
                    id: demoLbl
                    anchors.centerIn: parent
                    text: MotionInterface.pulseDemoMode === "replay" ? "SAMPLE DATA" : "SYNTHETIC DEMO"
                    color: AppTheme.accentBlue
                    font.pixelSize: 11; font.bold: true
                }
            }
            Rectangle {
                visible: root.scanning
                Layout.preferredHeight: 22
                Layout.preferredWidth: liveLbl.width + 20
                radius: 11
                color: Qt.rgba(0.18, 0.80, 0.44, 0.20)
                Text {
                    id: liveLbl
                    anchors.centerIn: parent
                    text: "LIVE"
                    color: AppTheme.accentGreen
                    font.pixelSize: 11; font.bold: true
                }
            }

            Item { Layout.fillWidth: true }

            // Demo controls — only when previewing (no live scan).
            Button {
                visible: MotionInterface.pulseDemoActive
                text: "Sample scan"
                flat: true
                highlighted: MotionInterface.pulseDemoMode === "replay"
                onClicked: MotionInterface.startPulseSample()
            }
            Repeater {
                model: [["Normal", "normal"], ["High PI", "high_pi"],
                        ["Low PI", "low_pi"], ["Damped", "damped"], ["Noisy", "noisy"]]
                delegate: Button {
                    required property var modelData
                    visible: MotionInterface.pulseDemoActive
                    text: modelData[0]
                    flat: true
                    highlighted: MotionInterface.pulseDemoMode !== "replay"
                                 && root.demoPreset === modelData[1]
                    onClicked: {
                        root.demoPreset = modelData[1]
                        MotionInterface.startPulseDemo(modelData[1], root.demoBpm)
                    }
                }
            }
        }

        // ── the two pulse windows ────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 12

            PulseCanvas {
                Layout.fillWidth: true
                Layout.fillHeight: true
                title: "LEFT"
                accent: AppTheme.accentGreen
                snap: root.leftSnap
                yMin: root.yMin
                yMax: root.yMax
            }
            PulseCanvas {
                Layout.fillWidth: true
                Layout.fillHeight: true
                title: "RIGHT"
                accent: AppTheme.accentBlue
                snap: root.rightSnap
                yMin: root.yMin
                yMax: root.yMax
            }
        }

        // ── comparison stats ─────────────────────────────────────────────
        PulseStatsPanel {
            Layout.fillWidth: true
            Layout.preferredHeight: 330
            leftSnap: root.leftSnap
            rightSnap: root.rightSnap
        }
    }
}
