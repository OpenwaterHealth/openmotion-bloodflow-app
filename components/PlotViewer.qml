import QtQuick 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Phase 2b-i — multi-cell viewer. Always renders 16 cells (2 sides × 8 cams).
// Inactive cameras just render an empty Canvas + label; no per-mask branching.
// Reduced-mode 2-cell layout is Phase 2b-ii. Toolbar + autoscale arrive in
// Task 5; the current header text is the Phase 2a placeholder.
Rectangle {
    id: viewer
    anchors.fill: parent
    color: theme.bgPlot
    radius: 8
    border.color: theme.borderSoft
    border.width: 1

    AppTheme { id: theme }

    // ── Inputs ─────────────────────────────────────────────────────────
    property bool reducedMode: false   // honored in Phase 2b-ii

    // ── State (owned here; pushed to every cell) ───────────────────────
    property string metric: "bvi"
    property real windowSeconds: 15
    property real yMin: 0.0
    property real yMax: 10.0

    // ── Source subscription ────────────────────────────────────────────
    readonly property var scanSource: MOTIONInterface.currentScanSource

    // ── Grid model ─────────────────────────────────────────────────────
    // 16 entries in row-major order matching the layout grid:
    //   row 0: L1 L2 L3 L4
    //   row 1: L5 L6 L7 L8
    //   row 2: R1 R2 R3 R4
    //   row 3: R5 R6 R7 R8
    readonly property var _cellModel: {
        var entries = []
        var sides = ["left", "right"]
        for (var s = 0; s < sides.length; s++) {
            for (var c = 0; c < 8; c++) {
                entries.push({ side: sides[s], camId: c })
            }
        }
        return entries
    }

    // ── Trace color per metric ─────────────────────────────────────────
    function _traceColorForMetric(m) {
        if (m === "bfi")      return theme.accentRed
        if (m === "bvi")      return theme.statusBlue
        if (m === "mean")     return theme.accentOrange
        if (m === "contrast") return theme.accentYellow
        return theme.statusBlue
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        // Header — Phase 2a placeholder. Replaced by PlotToolbar in Task 5.
        Text {
            Layout.fillWidth: true
            text: viewer.scanSource
                ? "● Live · " + (viewer.scanSource.live ? "LiveScanSource" : "PastScanSource")
                  + "   ·   metric=" + viewer.metric.toUpperCase()
                  + "   window=" + viewer.windowSeconds + "s"
                : "○ No active scan source"
            color: theme.textSecondary
            font.pixelSize: 12
            font.family: "Roboto Mono"
        }

        GridLayout {
            id: grid
            Layout.fillWidth: true
            Layout.fillHeight: true
            columns: 4
            rowSpacing: 6
            columnSpacing: 6

            Repeater {
                model: viewer._cellModel
                delegate: PlotCell {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    source: viewer.scanSource
                    side: modelData.side
                    camId: modelData.camId
                    metric: viewer.metric
                    windowSeconds: viewer.windowSeconds
                    yMin: viewer.yMin
                    yMax: viewer.yMax
                    traceColor: viewer._traceColorForMetric(viewer.metric)
                }
            }
        }
    }
}
