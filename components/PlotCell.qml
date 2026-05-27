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

    AppTheme { id: theme }

    // ── Repaint plumbing ───────────────────────────────────────────────
    // liveEdge is a plain Python @property with no notify signal — QML
    // can't bind to it. Instead, samplesAppended drives the repaint,
    // and onPaint reads source.liveEdge directly each time.
    onWidthChanged: traceCanvas.requestPaint()
    onHeightChanged: traceCanvas.requestPaint()
    onSourceChanged: traceCanvas.requestPaint()

    Connections {
        target: cell.source
        ignoreUnknownSignals: true
        function onSamplesAppended(s, c, m, n) {
            if (s !== cell.side || c !== cell.camId) return
            if (m === cell.metric || m === cell.secondaryMetric)
                traceCanvas.requestPaint()
        }
    }

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

            // Read liveEdge fresh each paint — it has no notify signal so
            // we can't cache it in a QML binding.
            var tHi = cell.source.liveEdge
            var tLo = Math.max(0, tHi - cell.windowSeconds)
            var dt = tHi - tLo
            if (dt <= 0) return

            var maxPts = Math.max(50, Math.floor(width * 2))

            cell._drawTrace(ctx, cell.metric, cell.traceColor,
                            cell.yMin, cell.yMax,
                            tLo, tHi, dt, maxPts, width, height)
            cell._drawTrace(ctx, cell.secondaryMetric, cell.secondaryColor,
                            cell.secondaryYMin, cell.secondaryYMax,
                            tLo, tHi, dt, maxPts, width, height)
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
}
