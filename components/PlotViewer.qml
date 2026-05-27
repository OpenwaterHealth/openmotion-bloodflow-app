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
    property real windowSeconds: 15
    property bool autoScale: true

    // ── Time-axis state ────────────────────────────────────────────────
    // followLive = true: cells render the last `windowSeconds` of data
    // up to source.liveEdge (DVR "live" mode). False: the visible window
    // is pinned at [windowStartT, windowStartT + windowSeconds] and any
    // new samples scroll on without moving the view (DVR "paused" mode).
    // Any pan/wheel-zoom interaction sets followLive=false; only the
    // "Back to live" button restores it.
    property bool followLive: true
    property real windowStartT: 0.0

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

    // ── DVR controls (called from PlotCell MouseArea) ─────────────────
    // Window-bounds: hard floor at 0.5 s so wheel-zoom can't collapse
    // the visible window to nothing; ceiling at 600 s to keep the
    // decimation point count sane for very-long scans.
    readonly property real _minWindowSeconds: 0.5
    readonly property real _maxWindowSeconds: 600.0

    function _ensureFrozen() {
        // Capture the currently-visible window start so pan/zoom from
        // followLive transitions smoothly (no visual jump). After this
        // the caller mutates windowStartT/windowSeconds freely.
        if (viewer.followLive) {
            // Use the snapshot (matches what's actually drawn) rather
            // than re-reading source.liveEdge — avoids a one-frame jump
            // at the moment of pan/zoom on a fast source.
            viewer.windowStartT = Math.max(0, viewer.liveEdgeSnapshot - viewer.windowSeconds)
        }
        viewer.followLive = false
    }

    function setWindow(startT, seconds) {
        _ensureFrozen()
        viewer.windowSeconds = Math.max(_minWindowSeconds, Math.min(_maxWindowSeconds, seconds))
        viewer.windowStartT = Math.max(0, startT)
        viewer._dirty = true
    }

    function backToLive() {
        viewer.followLive = true
        viewer._dirty = true
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

    // Snapshot of source.liveEdge captured once per paintTick. All cells
    // read this (not source.liveEdge directly) so their windows stay
    // perfectly synced even when individual cell paints land at slightly
    // different wall-clock times under load.
    property real liveEdgeSnapshot: 0.0

    Connections {
        target: viewer.scanSource
        ignoreUnknownSignals: true
        function onSamplesAppended(s, c, m, n) {
            viewer._dirty = true
        }
    }

    Timer {
        id: paintThrottle
        // 33 ms = 30 Hz — restored from the 50 ms throttle once the
        // mean-binning fix dropped per-paint data volume back to width × 1
        // samples. Each tick moves the trace ~1 sample at the data rate
        // (40 Hz), so the scroll feels continuous instead of stepwise.
        interval: 33
        running: viewer.scanSource !== null
        repeat: true
        // [LAG-DIAG] Capture inter-tick gap; if the main event loop is
        // blocked by anything (paint, slot, GC), the next Timer fire
        // arrives late and we'll see a >50 ms gap printed.
        property real _lastTickMs: 0
        onTriggered: {
            var nowMs = Date.now()
            if (paintThrottle._lastTickMs > 0) {
                var gap = nowMs - paintThrottle._lastTickMs
                if (gap > 50) {
                    console.warn("[LAG-DIAG] paintThrottle gap=" + gap +
                                 " ms (expected ~33 ms)")
                }
            }
            paintThrottle._lastTickMs = nowMs
            if (viewer._dirty) {
                viewer._dirty = false
                viewer.liveEdgeSnapshot = viewer.scanSource ? viewer.scanSource.liveEdge : 0
                viewer.paintTick++
            }
        }
    }

    // Source change forces an immediate paint without waiting for the
    // first samplesAppended.
    onScanSourceChanged: {
        viewer.liveEdgeSnapshot = viewer.scanSource ? viewer.scanSource.liveEdge : 0
        viewer._dirty = true
    }

    // Autoscale tick — every 3 s. compute_bounds_for_metric walks every
    // sample across all buffers, so the per-call cost scales with scan
    // duration; 3 s amortizes that work without making the y-axis feel
    // unresponsive (a 3-second delay between bound adjustments is hard
    // to notice during live monitoring).
    Timer {
        interval: 3000
        running: viewer.autoScale && viewer.scanSource !== null
        repeat: true
        triggeredOnStart: true
        onTriggered: {
            if (!viewer.scanSource) return
            // [LAG-DIAG] Time the full autoscale tick (two metrics' worth
            // of compute_bounds_for_metric + property writes).
            var _t0 = Date.now()
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
            var _ms = Date.now() - _t0
            if (_ms > 15) {
                console.warn("[LAG-DIAG] autoscale tick took " + _ms + " ms")
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
            displayMode: viewer.displayMode
            windowSeconds: viewer.windowSeconds
            autoScale: viewer.autoScale
            followLive: viewer.followLive

            onDisplayModeRequested: function(mode) { viewer.displayMode = mode }
            onWindowSecondsRequested: function(s) { viewer.windowSeconds = s; viewer._dirty = true }
            onAutoScaleToggled: function(enabled) { viewer.autoScale = enabled }
            onBackToLiveRequested: viewer.backToLive()
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
                    followLive: viewer.followLive
                    windowStartT: viewer.windowStartT
                    metric: viewer._displayPair.primary
                    yMin: viewer.primaryYMin
                    yMax: viewer.primaryYMax
                    traceColor: viewer._traceColorForMetric(viewer._displayPair.primary)
                    secondaryMetric: viewer._displayPair.secondary
                    secondaryYMin: viewer.secondaryYMin
                    secondaryYMax: viewer.secondaryYMax
                    secondaryColor: viewer._traceColorForMetric(viewer._displayPair.secondary)
                    paintTick: viewer.paintTick
                    liveEdgeSnapshot: viewer.liveEdgeSnapshot
                    panZoomTarget: viewer
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
