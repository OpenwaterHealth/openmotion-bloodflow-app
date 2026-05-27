import QtQuick 6.0
import QtQuick.Controls 6.0
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
    property bool autoScale: true

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

    // Autoscale tick — 1 Hz. Calls into the source's compute_bounds_for_metric
    // (Phase 2b-i Task 1) and pushes the result into the viewer's yMin/yMax,
    // which propagates to every cell via property binding.
    Timer {
        interval: 1000
        running: viewer.autoScale && viewer.scanSource !== null
        repeat: true
        triggeredOnStart: true
        onTriggered: {
            if (!viewer.scanSource) return
            var b = viewer.scanSource.compute_bounds_for_metric(viewer.metric)
            if (b && typeof b.yMin === "number" && typeof b.yMax === "number") {
                viewer.yMin = b.yMin
                viewer.yMax = b.yMax
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        PlotToolbar {
            Layout.fillWidth: true
            scanSource: viewer.scanSource
            metric: viewer.metric
            windowSeconds: viewer.windowSeconds
            autoScale: viewer.autoScale

            onMetricRequested: function(m) { viewer.metric = m }
            onWindowSecondsRequested: function(s) { viewer.windowSeconds = s }
            onAutoScaleToggled: function(enabled) { viewer.autoScale = enabled }
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
