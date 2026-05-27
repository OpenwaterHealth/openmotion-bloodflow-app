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
    // displayMode: "bfi_bvi" overlays BFI+BVI on each cell;
    // "mean_contrast" overlays Mean+Contrast. Each metric has its own
    // y-axis mapping (primary*/secondary*) so the two traces don't
    // squash each other when their ranges differ wildly.
    property string displayMode: "bfi_bvi"
    property int windowSeconds: 15
    property bool autoScale: true

    // Independent y-axis bounds per metric — kept here so every cell
    // shares the same scale.
    property real primaryYMin: 0.0
    property real primaryYMax: 10.0
    property real secondaryYMin: 0.0
    property real secondaryYMax: 10.0

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

    // ── Display-mode pair resolution ───────────────────────────────────
    // Maps the displayMode toggle to the primary/secondary metric pair
    // pushed to every cell.
    readonly property var _displayPair: {
        if (viewer.displayMode === "mean_contrast")
            return { primary: "mean", secondary: "contrast" }
        return { primary: "bfi", secondary: "bvi" }
    }

    // ── Viewer-driven paint throttle ───────────────────────────────────
    // Single dirty flag set by ANY samplesAppended emission. A 33 ms
    // Timer ticks paintTick (consumed by every PlotCell) whenever dirty.
    // Caps total cell-paint rate to ~30 Hz regardless of how many
    // samplesAppended signals fire in between, and renders all cells
    // in one pass so they stay visually in lockstep.
    property int paintTick: 0
    property bool _dirty: true   // start true so cells paint at least once

    Connections {
        target: viewer.scanSource
        ignoreUnknownSignals: true
        function onSamplesAppended(s, c, m, n) {
            viewer._dirty = true
        }
    }

    Timer {
        id: paintThrottle
        interval: 33
        running: viewer.scanSource !== null
        repeat: true
        onTriggered: {
            if (viewer._dirty) {
                viewer._dirty = false
                viewer.paintTick++
            }
        }
    }

    // Source change forces an immediate paint without waiting for the
    // first samplesAppended.
    onScanSourceChanged: viewer._dirty = true

    // Autoscale tick — 1 Hz. Computes bounds for both metrics in the
    // current display pair and pushes them into the viewer's per-metric
    // ranges. Setting _dirty afterward ensures cells repaint promptly
    // with the new range.
    Timer {
        interval: 1000
        running: viewer.autoScale && viewer.scanSource !== null
        repeat: true
        triggeredOnStart: true
        onTriggered: {
            if (!viewer.scanSource) return
            var bp = viewer.scanSource.compute_bounds_for_metric(viewer._displayPair.primary)
            if (bp && typeof bp.yMin === "number" && typeof bp.yMax === "number") {
                viewer.primaryYMin = bp.yMin
                viewer.primaryYMax = bp.yMax
            }
            var bs = viewer.scanSource.compute_bounds_for_metric(viewer._displayPair.secondary)
            if (bs && typeof bs.yMin === "number" && typeof bs.yMax === "number") {
                viewer.secondaryYMin = bs.yMin
                viewer.secondaryYMax = bs.yMax
            }
            viewer._dirty = true
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        PlotToolbar {
            Layout.fillWidth: true
            scanSource: viewer.scanSource
            displayMode: viewer.displayMode
            windowSeconds: viewer.windowSeconds
            autoScale: viewer.autoScale

            onDisplayModeRequested: function(mode) { viewer.displayMode = mode }
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
                    windowSeconds: viewer.windowSeconds
                    metric: viewer._displayPair.primary
                    yMin: viewer.primaryYMin
                    yMax: viewer.primaryYMax
                    traceColor: viewer._traceColorForMetric(viewer._displayPair.primary)
                    secondaryMetric: viewer._displayPair.secondary
                    secondaryYMin: viewer.secondaryYMin
                    secondaryYMax: viewer.secondaryYMax
                    secondaryColor: viewer._traceColorForMetric(viewer._displayPair.secondary)
                    paintTick: viewer.paintTick
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
