import QtQuick 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Phase 2a — single-cell shell that binds to MOTIONInterface.currentScanSource
// and shows ONE PlotCell for left/0/bfi. Phase 2b grows this into a full
// GridLayout with per-camera cells + toolbar + scrubber.
Rectangle {
    id: viewer
    color: "#0E0E0E"
    radius: 8
    border.color: "#2A2A2A"
    border.width: 1

    // ── Inputs ─────────────────────────────────────────────────────────
    // Reduced mode is consumed by Phase 2b for layout selection. Stored
    // here now so the BloodFlow.qml wiring matches Phase 2b's contract.
    property bool reducedMode: false

    // ── Source subscription ────────────────────────────────────────────
    // currentScanSource is a notify-bound pyqtProperty on MOTIONConnector.
    // It updates at every scan start (new LiveScanSource) and will update
    // again when Phase 2b's loadPastScan installs a PastScanSource.
    readonly property var scanSource: MOTIONInterface.currentScanSource

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        // Header — temporary; replaced by PlotToolbar.qml in Phase 2b.
        Text {
            Layout.fillWidth: true
            text: viewer.scanSource
                ? "● Live · " + (viewer.scanSource.live ? "LiveScanSource" : "PastScanSource")
                : "○ No active scan source"
            color: "#CCCCCC"
            font.pixelSize: 12
            font.family: "Roboto Mono"
        }

        PlotCell {
            id: cell
            Layout.fillWidth: true
            Layout.fillHeight: true
            source: viewer.scanSource
            side: "left"
            camId: 0
            metric: "bfi"
            windowSeconds: 15
            yMin: 0.0
            yMax: 10.0
            traceColor: "#E74C3C"
        }
    }
}
