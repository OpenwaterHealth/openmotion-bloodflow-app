import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  PulseView — inline cardiac pulse-waveform viewer (Research "Pulse" viewer
 *  mode; selected in Scan Settings). Fills the main content area in place of
 *  the PlotViewer: a left and a right window (template pulse over a min/max
 *  envelope with the live beat) plus a left-vs-right pulse-shape stats panel.
 *
 *  Purely scan-driven: data arrives on MotionInterface.pulseSnapshot(side, map)
 *  from the SDK "pulse" pipeline channel while a scan runs — real sensor data
 *  when demo mode is off, or the replayed recording when demo mode is on. The
 *  Start/Stop control owns the lifecycle; the viewer never generates its own
 *  data. Idle it shows a placeholder; on stop it freezes the final pulse.
 */
Item {
    id: root

    // True when this is the selected viewer (BloodFlow.pulseViewerActive).
    property bool active: false
    // True while a scan is running (real sensors or demo-mode replay).
    property bool scanning: false

    property var leftSnap: null
    property var rightSnap: null
    property real yMin: 0.0
    property real yMax: 10.0

    readonly property bool hasData: leftSnap !== null || rightSnap !== null

    // A fresh scan (or (re)selecting the viewer) clears the previous capture.
    // On stop we deliberately keep the last snapshot so the final pulse stays
    // on screen for review, mirroring how the plots freeze.
    onActiveChanged: if (active) { root.leftSnap = null; root.rightSnap = null }
    onScanningChanged: if (scanning) { root.leftSnap = null; root.rightSnap = null }

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
            // Status pill: DEMO REPLAY when engineering demo mode drives the
            // scan, LIVE for real sensor data. Only while a scan is running.
            Rectangle {
                visible: root.scanning
                Layout.preferredHeight: 22
                Layout.preferredWidth: liveLbl.width + 20
                radius: 11
                color: MotionInterface.appConfig.demoMode === true
                       ? Qt.rgba(0.29, 0.56, 0.89, 0.20)
                       : Qt.rgba(0.18, 0.80, 0.44, 0.20)
                Text {
                    id: liveLbl
                    anchors.centerIn: parent
                    text: MotionInterface.appConfig.demoMode === true ? "DEMO REPLAY" : "LIVE"
                    color: MotionInterface.appConfig.demoMode === true
                           ? AppTheme.accentBlue : AppTheme.accentGreen
                    font.pixelSize: 11; font.bold: true
                }
            }

            Item { Layout.fillWidth: true }
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

    // ── idle / acquiring placeholder ─────────────────────────────────────
    // Shown over the empty canvases until pulse data arrives, so the viewer
    // never displays anything the current scan didn't produce.
    Rectangle {
        anchors.fill: parent
        visible: !root.hasData
        color: Qt.rgba(0, 0, 0, 0.35)

        Text {
            anchors.centerIn: parent
            horizontalAlignment: Text.AlignHCenter
            color: AppTheme.textSecondary
            font.pixelSize: 16
            text: root.scanning
                  ? "Acquiring pulse — a few beats needed…"
                  : (MotionInterface.appConfig.demoMode === true
                     ? "Demo mode on — press Start to replay the recording."
                     : "Press Start to begin a scan.")
        }
    }
}
