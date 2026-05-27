import QtQuick 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Phase 2a — single-cell shell that binds to MOTIONInterface.currentScanSource
// and shows ONE PlotCell for left/0/bfi. Phase 2b grows this into a full
// GridLayout with per-camera cells + toolbar + scrubber.
Rectangle {
    id: viewer
    anchors.fill: parent
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

    // Phase 2a: show the first active camera from the user's mask config
    // so the single cell renders SOMETHING regardless of mask choice.
    // Phase 2b expands to a full per-camera grid driven by both masks.
    readonly property var _firstActive: {
        var lm = (MOTIONInterface.appConfig.leftMask  || 0) & 0xFF
        var rm = (MOTIONInterface.appConfig.rightMask || 0) & 0xFF
        for (var i = 0; i < 8; i++) {
            if (lm & (1 << i)) return { side: "left",  cam: i }
        }
        for (var j = 0; j < 8; j++) {
            if (rm & (1 << j)) return { side: "right", cam: j }
        }
        return { side: "left", cam: 0 }
    }

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
            side: viewer._firstActive.side
            camId: viewer._firstActive.cam
            metric: "bfi"
            windowSeconds: 15
            yMin: 0.0
            yMax: 10.0
            traceColor: "#E74C3C"
        }
    }
}
