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
    // Only renders cells for active cameras (bits set in leftMask /
    // rightMask). Layout: 4 columns max, left-side cams fill the top
    // rows, right-side cams fill the rows below. No empty trailing
    // cells unless a side's active count isn't a multiple of 4.
    readonly property var _activeCellModel: {
        var lm = (MOTIONInterface.appConfig.leftMask  || 0) & 0xFF
        var rm = (MOTIONInterface.appConfig.rightMask || 0) & 0xFF
        var leftCams = []
        var rightCams = []
        for (var i = 0; i < 8; i++) {
            if (lm & (1 << i)) leftCams.push(i)
            if (rm & (1 << i)) rightCams.push(i)
        }
        var entries = []
        var leftRows = Math.ceil(leftCams.length / 4)
        for (var li = 0; li < leftCams.length; li++) {
            entries.push({
                side: "left",
                camId: leftCams[li],
                row: Math.floor(li / 4),
                col: li % 4
            })
        }
        for (var ri = 0; ri < rightCams.length; ri++) {
            entries.push({
                side: "right",
                camId: rightCams[ri],
                row: leftRows + Math.floor(ri / 4),
                col: ri % 4
            })
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
                model: viewer._activeCellModel
                delegate: PlotCell {
                    Layout.row: modelData.row
                    Layout.column: modelData.col
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

        // Placeholder when no cameras are active (both masks 0). Lets the
        // viewer still show its toolbar + scan-source state without an
        // empty grid below it.
        Item {
            visible: viewer._activeCellModel.length === 0
            Layout.fillWidth: true
            Layout.fillHeight: true
            Text {
                anchors.centerIn: parent
                text: "No active cameras selected"
                color: theme.textTertiary
                font.pixelSize: 14
                font.family: "Roboto Mono"
            }
        }
    }
}
