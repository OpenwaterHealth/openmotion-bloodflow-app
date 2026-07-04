import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  PulseViewModal — full-screen cardiac pulse-waveform view.
 *
 *  Two windows (left / right), each showing the ensemble-average template
 *  pulse over a min/max envelope with the live beat sweeping across, plus a
 *  left-vs-right pulse-shape statistics panel.
 *
 *  Data arrives on MotionInterface.pulseSnapshot(side, map) — from the SDK
 *  "pulse" pipeline channel during a live clinical scan, or from the
 *  synthetic PulseDemoController when opened idle (the app has no hardware
 *  mock mode, so the demo makes the view useful and verifiable offline).
 *
 *  ModalManager contract: visible / open() / close() / label.
 */
Item {
    id: root
    anchors.fill: parent
    visible: false
    z: 9998

    // Bound from BloodFlow: a live scan feeds real pulse data, so the demo
    // must not run on top of it.
    property bool scanning: false

    readonly property string label: "Pulse Waveform"
    // Keep the card clear of the left icon bar on narrow windows
    // (ButtonPanel z:10000 > this z:9998) — see the modal-icon-bar-bleed note.
    readonly property int iconBarInset: 104

    property var leftSnap: null
    property var rightSnap: null
    property real yMin: 0.0
    property real yMax: 10.0
    property string demoPreset: "normal"
    property real demoBpm: 72.0

    function open() {
        root.leftSnap = null
        root.rightSnap = null
        root.visible = true
        if (!root.scanning && MotionInterface.appConfig.pulseView !== false)
            MotionInterface.startPulseDemo(root.demoPreset, root.demoBpm)
    }
    function close() {
        MotionInterface.stopPulseDemo()
        root.visible = false
    }

    onScanningChanged: {
        if (scanning && MotionInterface.pulseDemoActive)
            MotionInterface.stopPulseDemo()
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

    // ── backdrop ─────────────────────────────────────────────────────
    Rectangle {
        anchors.fill: parent
        color: "#000000"
        opacity: 0.55
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true                       // absorb hover from the plot behind
            onWheel: function(wheel) { wheel.accepted = true }
            onClicked: root.close()
        }
    }

    // ── card ─────────────────────────────────────────────────────────
    Rectangle {
        id: card
        anchors.centerIn: parent
        anchors.horizontalCenterOffset: root.iconBarInset / 2
        width: Math.min(parent.width - root.iconBarInset - 40, 1500)
        height: Math.min(parent.height - 56, 950)
        color: AppTheme.bgPanel
        radius: 14
        border.color: AppTheme.borderStrong
        border.width: 1

        // Swallow clicks/scroll so they don't fall through to the backdrop.
        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            onWheel: function(wheel) { wheel.accepted = true }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 16
            spacing: 12

            // ── header ───────────────────────────────────────────────
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
                        text: MotionInterface.pulseDemoMode === "replay"
                              ? "SAMPLE DATA" : "SYNTHETIC DEMO"
                        color: AppTheme.accentBlue
                        font.pixelSize: 11; font.bold: true
                    }
                }

                Item { Layout.fillWidth: true }

                // Replay the bundled recorded sample scan (real pulse morphology).
                Button {
                    visible: MotionInterface.pulseDemoActive
                    text: "Sample scan"
                    flat: true
                    highlighted: MotionInterface.pulseDemoMode === "replay"
                    onClicked: MotionInterface.startPulseSample()
                }

                // Synthetic shape presets (only meaningful while the demo drives it).
                Repeater {
                    model: [["Normal", "normal"], ["High PI", "high_pi"],
                            ["Low PI", "low_pi"], ["Damped", "damped"],
                            ["Noisy", "noisy"]]
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

                Rectangle {
                    Layout.preferredWidth: 32; Layout.preferredHeight: 32
                    radius: 8; color: closeArea.containsMouse ? AppTheme.bgHover : "transparent"
                    Text { anchors.centerIn: parent; text: "✕"; color: AppTheme.textSecondary; font.pixelSize: 18 }
                    MouseArea {
                        id: closeArea
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.close()
                    }
                }
            }

            // ── the two pulse windows ────────────────────────────────
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

            // ── comparison stats ─────────────────────────────────────
            PulseStatsPanel {
                Layout.fillWidth: true
                Layout.preferredHeight: 330
                leftSnap: root.leftSnap
                rightSnap: root.rightSnap
            }
        }
    }
}
