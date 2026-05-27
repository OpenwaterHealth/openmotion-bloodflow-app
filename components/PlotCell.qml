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
    property string metric: "bfi"
    property real   windowSeconds: 15

    // Display bounds — Phase 2a uses fixed [yMin, yMax]; Phase 2b adds
    // autoscale / per-cell fit.
    property real yMin: 0.0
    property real yMax: 10.0

    // Visual — defaults pulled from AppTheme; can be overridden per-cell.
    property color traceColor: theme.statusBlue
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
            if (s === cell.side && c === cell.camId && m === cell.metric)
                traceCanvas.requestPaint()
        }
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
            var pts = cell.source.points_for_window(
                cell.side, cell.camId, cell.metric,
                tLo, tHi, maxPts
            )
            if (pts.length < 2) return

            var dy = cell.yMax - cell.yMin
            if (dy <= 0) return

            ctx.beginPath()
            ctx.lineWidth = 1.5
            ctx.strokeStyle = cell.traceColor
            for (var i = 0; i < pts.length; i++) {
                var t = pts[i][0]
                var v = pts[i][1]
                if (!isFinite(v)) continue
                var x = ((t - tLo) / dt) * width
                var y = height - ((v - cell.yMin) / dy) * height
                if (i === 0) ctx.moveTo(x, y)
                else ctx.lineTo(x, y)
            }
            ctx.stroke()
        }
    }

    // Cell label — top-left, identifies which (side, cam, metric) this is.
    Text {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.margins: 8
        text: cell.side.toUpperCase() + " " + (cell.camId + 1) + " · " + cell.metric.toUpperCase()
        color: theme.textSecondary
        font.pixelSize: 11
        font.family: "Roboto Mono"
    }

    // Y-axis bound labels — hidden when cell is too narrow to fit them.
    Text {
        visible: cell.width >= 80
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: 8
        text: cell.yMax.toFixed(2)
        color: theme.textTertiary
        font.pixelSize: 10
        font.family: "Roboto Mono"
    }
    Text {
        visible: cell.width >= 80
        anchors.bottom: parent.bottom
        anchors.right: parent.right
        anchors.margins: 8
        text: cell.yMin.toFixed(2)
        color: theme.textTertiary
        font.pixelSize: 10
        font.family: "Roboto Mono"
    }
}
