import QtQuick 6.0

// Phase 2a — single-trace Canvas bound to one (side, cam_id, metric) key
// on a ScanDataSource. No pan/zoom, no crosshair, no axis labels —
// those land in Phase 2b. The cell tracks the live edge of the source
// and renders the last `windowSeconds` of samples, strided to at most
// 2 * width samples per paint.
Item {
    id: cell

    // ── Inputs ─────────────────────────────────────────────────────────
    property var source: null            // ScanDataSource (Python QObject) or null
    property string side: "left"
    property int    camId: 0
    property real   windowSeconds: 15

    // Time-axis state — when followLive=true, the cell tracks the
    // viewer's per-tick liveEdge snapshot (NOT source.liveEdge directly,
    // so every cell sees the same value for a given paintTick). When
    // false, the window is pinned at [windowStartT, windowStartT + windowSeconds].
    property bool followLive: true
    property real windowStartT: 0.0
    property real liveEdgeSnapshot: 0.0

    // Primary trace
    property string metric: "bfi"
    property real yMin: 0.0
    property real yMax: 10.0
    property color traceColor: theme.statusBlue

    // Secondary trace (optional) — set secondaryMetric to a non-empty
    // string to render a second overlaid trace with its own y-mapping.
    // Used for BFI+BVI / Mean+Contrast paired display modes.
    property string secondaryMetric: ""
    property real secondaryYMin: 0.0
    property real secondaryYMax: 10.0
    property color secondaryColor: theme.statusBlue

    // Visual — defaults pulled from AppTheme; can be overridden per-cell.
    property color frameColor: theme.borderSubtle
    property color bgColor: theme.bgPanel

    // DVR target — set to PlotViewer; cell calls .setWindow(startT, sec)
    // on pan/zoom interactions. Cell itself owns no time-axis state;
    // viewer is the single source of truth for windowStartT / followLive /
    // windowSeconds across all cells.
    property var panZoomTarget: null

    AppTheme { id: theme }

    // ── Repaint plumbing ───────────────────────────────────────────────
    // Repaints are throttled by the parent PlotViewer: it owns a 33 ms
    // dirty-flagged Timer that ticks `paintTick` whenever any
    // samplesAppended arrived. Binding paintTick here triggers exactly
    // one repaint per parent tick — caps cell-paint rate to ~30 Hz and
    // keeps all cells visually in lockstep.
    property int paintTick: 0
    onPaintTickChanged: traceCanvas.requestPaint()
    onWidthChanged: traceCanvas.requestPaint()
    onHeightChanged: traceCanvas.requestPaint()
    onSourceChanged: traceCanvas.requestPaint()

    // Render one trace inside the current Canvas context using the given
    // metric/color/yMin/yMax. Defined as a JS function on the cell so
    // both primary and secondary draws share it.
    function _drawTrace(ctx, metricName, color, yMinVal, yMaxVal,
                       tLo, tHi, dt, maxPts, w, h) {
        if (!metricName || metricName.length === 0) return
        var pts = cell.source.points_for_window(
            cell.side, cell.camId, metricName, tLo, tHi, maxPts
        )
        if (pts.length < 2) return
        var dy = yMaxVal - yMinVal
        if (dy <= 0) return
        ctx.beginPath()
        ctx.lineWidth = 1.5
        ctx.strokeStyle = color
        for (var i = 0; i < pts.length; i++) {
            var t = pts[i][0]
            var v = pts[i][1]
            if (!isFinite(v)) continue
            var x = ((t - tLo) / dt) * w
            var y = h - ((v - yMinVal) / dy) * h
            if (i === 0) ctx.moveTo(x, y)
            else ctx.lineTo(x, y)
        }
        ctx.stroke()
    }

    Rectangle {
        anchors.fill: parent
        color: cell.bgColor
        border.color: cell.frameColor
        border.width: 1
        radius: 4
    }

    Canvas {
        id: traceCanvas
        anchors.fill: parent
        anchors.margins: 4

        onPaint: {
            // [LAG-DIAG] Time the per-cell paint. Log when > 15 ms;
            // 8 active cells × >15 ms each would blow the 33 ms tick budget.
            var _paintT0 = Date.now()
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)

            // Midline gridline — always drawn for visual reference, even
            // when no source is bound. Skipped for very narrow cells.
            if (width >= 60) {
                ctx.strokeStyle = cell.frameColor
                ctx.lineWidth = 1
                ctx.beginPath()
                var midY = Math.floor(height / 2) + 0.5  // crisp 1px line
                ctx.moveTo(0, midY)
                ctx.lineTo(width, midY)
                ctx.stroke()
            }

            if (!cell.source) return

            // Window boundaries — followLive locks tHi to the viewer's
            // per-tick snapshot (synced across every cell); otherwise the
            // window stays pinned at the user-set
            // [windowStartT, windowStartT + windowSeconds].
            var tHi = cell.followLive
                ? cell.liveEdgeSnapshot
                : cell.windowStartT + cell.windowSeconds
            var tLo = tHi - cell.windowSeconds
            var dt = tHi - tLo
            if (dt <= 0) return

            // 0.5 samples per pixel. Source-side stride-aligned causal
            // smoothing makes the visual quality robust to lower output
            // counts. Halving maxPts (vs width × 1) lets decimation
            // saturate at ~3 s of scan instead of ~6 s, cutting the
            // ramp-up paint-throttle gap in half and giving us a lower
            // steady-state per-paint marshalling cost.
            var maxPts = Math.max(50, Math.floor(width * 0.5))

            cell._drawTrace(ctx, cell.metric, cell.traceColor,
                            cell.yMin, cell.yMax,
                            tLo, tHi, dt, maxPts, width, height)
            cell._drawTrace(ctx, cell.secondaryMetric, cell.secondaryColor,
                            cell.secondaryYMin, cell.secondaryYMax,
                            tLo, tHi, dt, maxPts, width, height)

            var _paintMs = Date.now() - _paintT0
            if (_paintMs > 15) {
                console.warn("[LAG-DIAG] PlotCell paint took " + _paintMs +
                             " ms (" + cell.side + "/cam" + cell.camId + ")")
            }
        }
    }

    // Cell label — top-left, camera identity plus per-metric range labels
    // colored to match each trace. When secondaryMetric is empty, only
    // the primary metric row is shown.
    Column {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.margins: 8
        spacing: 1

        Text {
            text: cell.side.toUpperCase() + " " + (cell.camId + 1)
            color: theme.textSecondary
            font.pixelSize: 11
            font.family: "Roboto Mono"
        }
        Text {
            visible: cell.width >= 80
            text: cell.metric.toUpperCase() + "  " + cell.yMin.toFixed(2) + " – " + cell.yMax.toFixed(2)
            color: cell.traceColor
            font.pixelSize: 10
            font.family: "Roboto Mono"
        }
        Text {
            visible: cell.width >= 80 && cell.secondaryMetric.length > 0
            text: cell.secondaryMetric.toUpperCase() + "  " + cell.secondaryYMin.toFixed(2) + " – " + cell.secondaryYMax.toFixed(2)
            color: cell.secondaryColor
            font.pixelSize: 10
            font.family: "Roboto Mono"
        }
    }

    // ── Pan + wheel-zoom MouseArea ─────────────────────────────────────
    // Top of z-order (last sibling) so it captures mouse events from
    // the underlying Canvas + label children. Wheel zooms around the
    // cursor x; click-drag pans the time axis. Both apply globally via
    // panZoomTarget.setWindow(...) so every cell in the grid stays in
    // sync. Sets followLive=false on any interaction.
    MouseArea {
        id: panZoomArea
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton
        cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor

        // Drag snapshot — captured at press so the drag is computed
        // against a fixed origin instead of accumulating per-move
        // floating-point drift.
        property real _dragStartX: 0
        property real _dragStartWindowStartT: 0

        function _currentTHi() {
            if (cell.followLive)
                return cell.liveEdgeSnapshot
            return cell.windowStartT + cell.windowSeconds
        }

        onPressed: function(mouse) {
            panZoomArea._dragStartX = mouse.x
            panZoomArea._dragStartWindowStartT = panZoomArea._currentTHi() - cell.windowSeconds
        }

        onPositionChanged: function(mouse) {
            if (!pressed || !cell.panZoomTarget || cell.width <= 0) return
            // Drag right = scroll back in time = windowStartT decreases.
            var dx = mouse.x - panZoomArea._dragStartX
            var dt = (dx / cell.width) * cell.windowSeconds
            cell.panZoomTarget.setWindow(
                panZoomArea._dragStartWindowStartT - dt,
                cell.windowSeconds
            )
        }

        onWheel: function(wheel) {
            if (!cell.panZoomTarget || cell.width <= 0) {
                wheel.accepted = false
                return
            }
            // Up = zoom in (smaller window); down = zoom out.
            var factor = wheel.angleDelta.y > 0 ? 0.8 : 1.25
            var ratio = Math.max(0, Math.min(1, wheel.x / cell.width))
            var tHiNow = panZoomArea._currentTHi()
            var tLoNow = tHiNow - cell.windowSeconds
            var anchorT = tLoNow + ratio * cell.windowSeconds
            var newSec = cell.windowSeconds * factor
            var newStart = anchorT - ratio * newSec
            cell.panZoomTarget.setWindow(newStart, newSec)
            wheel.accepted = true
        }
    }
}
