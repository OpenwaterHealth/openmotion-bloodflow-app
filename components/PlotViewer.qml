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
    // Dev mode (default) — one cell per active camera (bits set in
    // leftMask / rightMask). 4 columns max; left-side cams fill the
    // top rows, right-side cams fill the rows below.
    readonly property var _devCellModel: {
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

    // Reduced mode — 2 cells, one per side, each rendering the
    // side-averaged stream (cam_id=-1, fed by SDK's SideAveragingStage
    // via _LivePlotSink.consume). Stacked vertically in a single column.
    readonly property var _reducedCellModel: [
        { side: "left",  camId: -1, row: 0, col: 0 },
        { side: "right", camId: -1, row: 1, col: 0 }
    ]

    readonly property var _activeCellModel: viewer.reducedMode
        ? _reducedCellModel
        : _devCellModel

    // ── Autoscale recompute (shared by Timer + displayMode change) ────
    function _recomputeAutoscale() {
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
        // Cap startT so the window can't extend past the live edge —
        // otherwise the scrubber inset slides off into empty future
        // space when the user keeps dragging right. Lower bound 0
        // (scan start); upper bound puts the rightmost data at the
        // right edge of the visible plot.
        var maxStart = Math.max(0, viewer.liveEdgeSnapshot - viewer.windowSeconds)
        viewer.windowStartT = Math.max(0, Math.min(maxStart, startT))
        viewer._dirty = true
    }

    function backToLive() {
        viewer.followLive = true
        viewer._dirty = true
    }

    // ── Hover crosshair ────────────────────────────────────────────────
    // Cells broadcast cursor time here via cursorAt(); cells read
    // viewer.cursorT to draw the synced vertical line. NaN means hide.
    property real cursorT: NaN

    function cursorAt(t) {
        viewer.cursorT = t
        // Mark dirty so the next paintThrottle tick (≤ 33 ms) repaints
        // every cell with the new crosshair x — no per-mousemove paints.
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
        // 33 ms = 30 Hz. Each tick moves the trace ~1 sample at the
        // data rate (40 Hz), so the scroll feels continuous instead
        // of stepwise.
        interval: 33
        running: viewer.scanSource !== null
        repeat: true
        onTriggered: {
            if (viewer._dirty) {
                viewer._dirty = false
                viewer.liveEdgeSnapshot = viewer.scanSource ? viewer.scanSource.liveEdge : 0
                viewer.paintTick++
            }
        }
    }

    // Source change forces an immediate paint without waiting for the
    // first samplesAppended; also resets followLive so each new source
    // (live or past) opens at its own latest window.
    onScanSourceChanged: {
        viewer.liveEdgeSnapshot = viewer.scanSource ? viewer.scanSource.liveEdge : 0
        viewer.followLive = true
        viewer._dirty = true
        // Re-fit y-axis to the new source's data immediately, otherwise
        // a past scan loaded with very different value ranges would draw
        // off-axis until the next autoscale tick.
        viewer._recomputeAutoscale()
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
        onTriggered: viewer._recomputeAutoscale()
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
            liveSourceAvailable: MOTIONInterface.liveSourceAvailable

            onDisplayModeRequested: function(mode) {
                viewer.displayMode = mode
                console.info("[Plot] displayMode → " + mode)
                // Force an immediate autoscale recompute so the new pair
                // of metrics renders into a sensible y-axis right away
                // — without this the old (BFI/BVI-fit) bounds make
                // mean (~100) and contrast (~0.3) draw entirely
                // off-screen until the next autoscale tick.
                viewer._recomputeAutoscale()
            }
            onWindowSecondsRequested: function(s) {
                // Route through setWindow so the off-edge cap re-applies
                // — expanding from 5 s to 60 s while paused near liveEdge
                // would otherwise push the right edge past liveEdge.
                // When followLive, setWindow's _ensureFrozen snapshots
                // the current view first, preserving the visual position.
                if (viewer.followLive) {
                    viewer.windowSeconds = s
                } else {
                    viewer.setWindow(viewer.windowStartT, s)
                }
                viewer._dirty = true
                console.info("[Plot] windowSeconds → " + s + " s")
            }
            onAutoScaleToggled: function(enabled) {
                viewer.autoScale = enabled
                console.info("[Plot] autoScale → " + enabled)
            }
            onBackToLiveRequested: {
                // When the viewer is on a past source, "Back to live"
                // means switch back to the held LiveScanSource. When
                // already live (just paused), it means resume follow.
                if (viewer.scanSource && viewer.scanSource.live === false
                        && MOTIONInterface.liveSourceAvailable) {
                    MOTIONInterface.showLiveSource()
                    console.info("[Plot] back-to-live (past → live source)")
                } else {
                    viewer.backToLive()
                    console.info("[Plot] back-to-live (followLive → true)")
                }
            }
        }

        GridLayout {
            id: grid
            Layout.fillWidth: true
            Layout.fillHeight: true
            // Reduced mode: single column, 2 stacked cells. Dev mode:
            // 4 columns, rows determined by active-cam count.
            columns: viewer.reducedMode ? 1 : 4
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
                    cursorT: viewer.cursorT
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

        PlotScrubber {
            Layout.fillWidth: true
            Layout.preferredHeight: 28
            visible: viewer.scanSource !== null
            fullScanDuration: viewer.liveEdgeSnapshot
            // When followLive, project the visible window onto the live
            // edge so the inset stays glued to the right; when paused
            // (panned/zoomed), reflect the user-set windowStartT.
            windowStartT: viewer.followLive
                ? Math.max(0, viewer.liveEdgeSnapshot - viewer.windowSeconds)
                : viewer.windowStartT
            windowSeconds: viewer.windowSeconds
            followLive: viewer.followLive

            onPanRequested: function(startT) {
                viewer.setWindow(startT, viewer.windowSeconds)
                console.info("[Plot] scrubber pan → " + startT.toFixed(2) + " s")
            }
        }
    }

    // ── Hover tooltip ──────────────────────────────────────────────────
    // Top-right floating panel: appears when cursorT is finite (hover
    // active over any cell), lists the time + per-cell primary/secondary
    // values at that time. Per-cell values are queried from the source
    // via value_at() each tick — bound to paintTick so the tooltip
    // refreshes in lockstep with the cells.
    Rectangle {
        id: hoverTooltip
        visible: isFinite(viewer.cursorT) && viewer.scanSource !== null
                  && viewer._activeCellModel.length > 0
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 56  // clear the toolbar
        anchors.rightMargin: 16
        color: theme.bgElevated
        border.color: theme.borderSubtle
        border.width: 1
        radius: 4
        width: tooltipColumn.implicitWidth + 16
        height: tooltipColumn.implicitHeight + 12

        // Force a recompute of the rows when paintTick advances. We bind
        // to paintTick rather than cursorT so the tooltip only refreshes
        // at the throttled tick rate, not on every mousemove.
        property var _rows: {
            void viewer.paintTick  // dependency
            if (!isFinite(viewer.cursorT) || !viewer.scanSource) return []
            var t = viewer.cursorT
            var primMetric = viewer._displayPair.primary
            var secMetric = viewer._displayPair.secondary
            var primColor = viewer._traceColorForMetric(primMetric)
            var secColor = viewer._traceColorForMetric(secMetric)
            var rows = []
            for (var i = 0; i < viewer._activeCellModel.length; i++) {
                var c = viewer._activeCellModel[i]
                var pv = viewer.scanSource.value_at(c.side, c.camId, primMetric, t)
                var sv = viewer.scanSource.value_at(c.side, c.camId, secMetric, t)
                rows.push({
                    label: c.side.charAt(0).toUpperCase() + (c.camId + 1),
                    pVal: pv, pColor: primColor,
                    sVal: sv, sColor: secColor,
                })
            }
            return rows
        }

        Column {
            id: tooltipColumn
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.margins: 8
            spacing: 1

            Text {
                text: {
                    var t = viewer.cursorT
                    if (!isFinite(t)) return ""
                    var mins = Math.floor(t / 60)
                    var secs = (t - mins * 60).toFixed(3)
                    return "t = " + mins + ":" + (secs < 10 ? "0" + secs : secs) + " s"
                }
                color: theme.textSecondary
                font.pixelSize: 11
                font.family: "Roboto Mono"
                font.bold: true
            }
            Repeater {
                model: hoverTooltip._rows
                delegate: Row {
                    spacing: 6
                    Text {
                        text: modelData.label
                        width: 30
                        color: theme.textSecondary
                        font.pixelSize: 10
                        font.family: "Roboto Mono"
                    }
                    Text {
                        text: isFinite(modelData.pVal) ? modelData.pVal.toFixed(2) : "—"
                        width: 50
                        horizontalAlignment: Text.AlignRight
                        color: modelData.pColor
                        font.pixelSize: 10
                        font.family: "Roboto Mono"
                    }
                    Text {
                        text: isFinite(modelData.sVal) ? modelData.sVal.toFixed(2) : "—"
                        width: 50
                        horizontalAlignment: Text.AlignRight
                        color: modelData.sColor
                        font.pixelSize: 10
                        font.family: "Roboto Mono"
                    }
                }
            }
        }
    }
}
