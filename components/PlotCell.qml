import QtQuick 6.0
import OpenMotion 1.0

// Phase 2a — single-trace Canvas bound to one (side, cam_id, metric) key
// on a ScanDataSource. The cell tracks the live edge of the source
// and renders the last `windowSeconds` of samples, strided to at most
// 2 * width samples per paint. Y-axis tick labels (max/mid/min) render
// per metric: primary on the left edge, secondary on the right.
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
    property color traceColor: AppTheme.statusBlue

    // Secondary trace (optional) — set secondaryMetric to a non-empty
    // string to render a second overlaid trace with its own y-mapping.
    // Used for BFI+BVI / Mean+Contrast paired display modes.
    property string secondaryMetric: ""
    property real secondaryYMin: 0.0
    property real secondaryYMax: 10.0
    property color secondaryColor: AppTheme.statusBlue

    // Visual — defaults pulled from AppTheme; can be overridden per-cell.
    property color frameColor: AppTheme.borderSubtle
    property color bgColor: AppTheme.plotCellBg

    // DVR target — set to PlotViewer; cell calls .setWindow(startT, sec)
    // on pan/zoom interactions. Cell itself owns no time-axis state;
    // viewer is the single source of truth for windowStartT / followLive /
    // windowSeconds across all cells.
    property var panZoomTarget: null

    // Crosshair: NaN means hide. The viewer broadcasts the same value
    // to every cell so the vertical line stays synced across the grid.
    property real cursorT: NaN

    // Top-left live value labels — toggleable so clinical mode (which
    // shows the same numbers on the large side panels) can hide them.
    property bool showValueLabels: true

    // Y-axis tick labels (max/mid/min). Toggleable from the ⋯ popup;
    // default comes from appConfig.showAxisLabels (false = labels off).
    property bool showAxisLabels: true

    // Top-right temperature readout — engineering mode only; the viewer
    // binds this from appConfig.engineeringMode (issue #165).
    property bool showTemperature: false

    // Connection-lost badge (issue #174) — the viewer sets this true
    // while the connector's dropout watchdog has this (side, camId)
    // marked Connection Lost; clears when frames resume.
    property bool connectionLost: false

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
    // Bound changes must repaint directly — on a static (past-scan)
    // source no samplesAppended tick arrives to flush a Settings edit
    // or an autoscale recompute into the axis labels / trace mapping.
    onYMinChanged: traceCanvas.requestPaint()
    onYMaxChanged: traceCanvas.requestPaint()
    onSecondaryYMinChanged: traceCanvas.requestPaint()
    onSecondaryYMaxChanged: traceCanvas.requestPaint()
    onShowAxisLabelsChanged: traceCanvas.requestPaint()
    // Note: cursorT changes do NOT trigger a direct repaint — that
    // would spam paints on every mouse-move event. Instead the viewer
    // sets _dirty on cursorAt() so the next paintThrottle tick
    // (≤ 33 ms) repaints with the new crosshair position.

    // GUI-only display clamp — delegates to the viewer (panZoomTarget)
    // so the per-metric clamp bounds live in one place. The trace
    // geometry and underlying data are untouched.
    function _displayValue(metricName, v) {
        if (cell.panZoomTarget && cell.panZoomTarget.clampForDisplay)
            return cell.panZoomTarget.clampForDisplay(metricName, v)
        return v
    }

    // Y-axis tick decimals chosen per axis from its span so all three
    // ticks format consistently — whole numbers for wide ranges (mean's
    // 0–500), one decimal for BFI/BVI's 0–10, two for contrast's 0–1
    // (where the 0.35 midpoint would otherwise round to "0.3").
    function _fmtAxis(v, span) {
        return v.toFixed(span >= 100 ? 0 : span >= 2 ? 1 : 2)
    }

    // Y-axis tick labels — primary bounds down the left edge, secondary
    // down the right, colored to match their traces. Drawn even when no
    // source is bound so an idle grid still shows its scale. Skipped for
    // very narrow cells alongside the midline gridline.
    function _drawAxisLabels(ctx, w, h) {
        if (w < 60) return
        // Context2D validates font families strictly (unlike Text, which
        // silently falls back), so an unregistered "Roboto Mono" warns on
        // every paint. Keep it as the preferred family but add a generic
        // monospace fallback to satisfy the parser and silence the spam.
        ctx.font = "9px 'Roboto Mono', monospace"
        var midY = Math.floor(h / 2)
        var span = cell.yMax - cell.yMin
        ctx.fillStyle = cell.traceColor
        ctx.textAlign = "left"
        ctx.fillText(_fmtAxis(cell.yMax, span), 2, 9)
        ctx.fillText(_fmtAxis((cell.yMin + cell.yMax) / 2, span), 2, midY - 3)
        ctx.fillText(_fmtAxis(cell.yMin, span), 2, h - 3)
        if (cell.secondaryMetric.length > 0) {
            span = cell.secondaryYMax - cell.secondaryYMin
            ctx.fillStyle = cell.secondaryColor
            ctx.textAlign = "right"
            ctx.fillText(_fmtAxis(cell.secondaryYMax, span), w - 2, 9)
            ctx.fillText(_fmtAxis((cell.secondaryYMin + cell.secondaryYMax) / 2, span), w - 2, midY - 3)
            ctx.fillText(_fmtAxis(cell.secondaryYMin, span), w - 2, h - 3)
        }
    }

    // Render one trace inside the current Canvas context using the given
    // metric/color/yMin/yMax. Defined as a JS function on the cell so
    // both primary and secondary draws share it. Returns the number of
    // points actually painted so the profile HUD can sum across cells.
    function _drawTrace(ctx, metricName, color, yMinVal, yMaxVal,
                       tLo, tHi, dt, maxPts, w, h) {
        if (!metricName || metricName.length === 0) return 0
        var pts = cell.source.points_for_window(
            cell.side, cell.camId, metricName, tLo, tHi, maxPts
        )
        if (pts.length < 2) return 0
        var dy = yMaxVal - yMinVal
        if (dy <= 0) return 0
        ctx.beginPath()
        ctx.lineWidth = 1.5
        ctx.strokeStyle = color
        // Pen-up at non-finite samples: a NaN run (unlit camera during a
        // contact loss) must render as a blank gap, not a straight line
        // bridging the last point before the gap to the first one after.
        var penDown = false
        for (var i = 0; i < pts.length; i++) {
            var t = pts[i][0]
            var v = pts[i][1]
            if (!isFinite(v)) { penDown = false; continue }
            var x = ((t - tLo) / dt) * w
            var y = h - ((v - yMinVal) / dy) * h
            if (!penDown) { ctx.moveTo(x, y); penDown = true }
            else ctx.lineTo(x, y)
        }
        ctx.stroke()
        return pts.length
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
            // Profile-HUD timing: only sample wall-time when the HUD is
            // visible to keep clinical-build paint hot path identical.
            var profOn = cell.panZoomTarget
                         && cell.panZoomTarget.recordCellPaint
                         && cell.panZoomTarget._hudVisible
            var profT0 = profOn ? Date.now() : 0
            var profPts = 0

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

            if (!cell.source) {
                if (cell.showAxisLabels) cell._drawAxisLabels(ctx, width, height)
                if (profOn) cell.panZoomTarget.recordCellPaint(Date.now() - profT0, 0)
                return
            }

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

            profPts += cell._drawTrace(ctx, cell.metric, cell.traceColor,
                            cell.yMin, cell.yMax,
                            tLo, tHi, dt, maxPts, width, height)
            profPts += cell._drawTrace(ctx, cell.secondaryMetric, cell.secondaryColor,
                            cell.secondaryYMin, cell.secondaryYMax,
                            tLo, tHi, dt, maxPts, width, height)

            // Dropout marker — vertical red bar at the recorded
            // dropout time for this (side, cam).
            var dropT = cell.source.dropped_at_for(cell.side, cell.camId)
            if (isFinite(dropT) && dropT >= tLo && dropT <= tHi) {
                var dropX = ((dropT - tLo) / dt) * width
                ctx.strokeStyle = AppTheme.accentRed
                ctx.lineWidth = 2
                ctx.beginPath()
                ctx.moveTo(Math.floor(dropX) + 0.5, 0)
                ctx.lineTo(Math.floor(dropX) + 0.5, height)
                ctx.stroke()
            }

            // Crosshair — vertical line at cursorT (broadcast by the
            // viewer so every cell stays in sync on hover).
            if (isFinite(cell.cursorT) && cell.cursorT >= tLo && cell.cursorT <= tHi) {
                var crossX = ((cell.cursorT - tLo) / dt) * width
                ctx.strokeStyle = AppTheme.textTertiary
                ctx.lineWidth = 1
                ctx.beginPath()
                ctx.moveTo(Math.floor(crossX) + 0.5, 0)
                ctx.lineTo(Math.floor(crossX) + 0.5, height)
                ctx.stroke()
            }

            // Last so the tick labels stay readable over the traces.
            if (cell.showAxisLabels) cell._drawAxisLabels(ctx, width, height)

            if (profOn) cell.panZoomTarget.recordCellPaint(Date.now() - profT0, profPts)
        }
    }

    // Cell label — top-left, camera identity plus per-metric range labels
    // colored to match each trace. When secondaryMetric is empty, only
    // the primary metric row is shown.
    Column {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.margins: 8
        // Clear the primary metric's top tick label in the corner above —
        // but only when it's drawn; reclaim the space when axis labels are off.
        anchors.topMargin: cell.showAxisLabels ? 18 : 8
        spacing: 1

        Text {
            // cam_id = -1 is the side-averaged stream fed by the SDK's
            // SideAveragingStage in clinical mode — hide the label there;
            // the large side panel next to the plot already names the side.
            visible: cell.camId !== -1
            text: cell.side.toUpperCase() + " " + (cell.camId + 1)
            color: AppTheme.textSecondary
            font.pixelSize: 11
            font.family: "Roboto Mono"
        }
        Text {
            visible: cell.showValueLabels && cell.width >= 80
            // Show the most recent sample value rather than the
            // autoscale-derived range — clinicians care about "what's
            // it reading right now", not the y-axis extent. paintTick
            // is a dependency so the text refreshes at the throttled
            // rate. Junk last-frame values are filtered at the SDK side
            // (BfiBviStage NaN's anything outside [-2, 10) exclusive),
            // so the absolute-last sample is safe to read directly.
            text: {
                void cell.paintTick  // dependency
                if (!cell.source) return cell.metric.toUpperCase() + "  --"
                var v = cell._displayValue(cell.metric,
                    cell.source.value_at(cell.side, cell.camId, cell.metric, cell.liveEdgeSnapshot))
                return cell.metric.toUpperCase() + "  "
                       + (isFinite(v) ? v.toFixed(2) : "--")
            }
            color: cell.traceColor
            font.pixelSize: 10
            font.family: "Roboto Mono"
        }
        Text {
            visible: cell.showValueLabels && cell.width >= 80
                     && cell.secondaryMetric.length > 0
            text: {
                void cell.paintTick
                if (!cell.source) return cell.secondaryMetric.toUpperCase() + "  --"
                var v = cell._displayValue(cell.secondaryMetric,
                    cell.source.value_at(cell.side, cell.camId, cell.secondaryMetric, cell.liveEdgeSnapshot))
                return cell.secondaryMetric.toUpperCase() + "  "
                       + (isFinite(v) ? v.toFixed(2) : "--")
            }
            color: cell.secondaryColor
            font.pixelSize: 10
            font.family: "Roboto Mono"
        }
    }

    // Camera temperature — top-right, orange, dev mode only. Reads the
    // "temp" metric stream (light frames only; absent for the clinical-
    // mode cam_id=-1 average and for past scans) and hides itself when
    // no finite reading exists.
    Text {
        property real tempC: {
            void cell.paintTick  // dependency
            if (!cell.source || !cell.showTemperature) return NaN
            return cell.source.value_at(cell.side, cell.camId, "temp",
                                        cell.liveEdgeSnapshot)
        }
        visible: cell.showTemperature && cell.width >= 80 && isFinite(tempC)
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: 8
        // Clear the secondary metric's top tick label in the corner above —
        // but only when it's drawn; reclaim the space when axis labels are off.
        anchors.topMargin: cell.showAxisLabels ? 18 : 8
        text: isFinite(tempC) ? tempC.toFixed(1) + "°C" : ""
        color: AppTheme.readableInk(AppTheme.accentOrange)
        font.pixelSize: 10
        font.family: "Roboto Mono"
    }

    // Connection-lost badge — centered red pill shown while the dropout
    // watchdog has this camera marked Connection Lost (issue #174). The
    // trace freezes when frames stop, so the badge is the in-plot
    // explanation; it disappears if frames resume (watchdog re-arm).
    Rectangle {
        visible: cell.connectionLost
        anchors.centerIn: parent
        width: lostLabel.implicitWidth + 16
        height: lostLabel.implicitHeight + 8
        radius: height / 2
        color: AppTheme.plotCellBg
        border.color: AppTheme.accentRed
        border.width: 1
        opacity: 0.92
        Text {
            id: lostLabel
            anchors.centerIn: parent
            text: "CONNECTION LOST"
            color: AppTheme.accentRed
            font.pixelSize: cell.width >= 160 ? 11 : 9
            font.weight: Font.DemiBold
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
        hoverEnabled: true

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

        function _cursorTFromMouseX(mx) {
            var tHiNow = panZoomArea._currentTHi()
            var tLoNow = tHiNow - cell.windowSeconds
            return tLoNow + (mx / cell.width) * cell.windowSeconds
        }

        onPressed: function(mouse) {
            panZoomArea._dragStartX = mouse.x
            panZoomArea._dragStartWindowStartT = panZoomArea._currentTHi() - cell.windowSeconds
            // Pull keyboard focus back to the viewer — if the user just
            // clicked into a text input elsewhere, this restores the
            // shortcut bindings on the next click into the plot grid.
            if (cell.panZoomTarget) cell.panZoomTarget.forceActiveFocus()
        }

        onPositionChanged: function(mouse) {
            if (pressed) {
                if (!cell.panZoomTarget || cell.width <= 0) return
                // Drag right = scroll back in time = windowStartT decreases.
                var dx = mouse.x - panZoomArea._dragStartX
                var dt = (dx / cell.width) * cell.windowSeconds
                cell.panZoomTarget.setWindow(
                    panZoomArea._dragStartWindowStartT - dt,
                    cell.windowSeconds,
                    "drag-pan"
                )
            } else {
                // Hover: broadcast cursor time to the viewer.
                if (cell.panZoomTarget && cell.width > 0)
                    cell.panZoomTarget.cursorAt(panZoomArea._cursorTFromMouseX(mouse.x))
            }
        }

        onEntered: {
            if (cell.panZoomTarget && cell.width > 0)
                cell.panZoomTarget.cursorAt(panZoomArea._cursorTFromMouseX(mouseX))
        }

        onExited: {
            if (cell.panZoomTarget) cell.panZoomTarget.cursorAt(NaN)
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
            cell.panZoomTarget.setWindow(newStart, newSec, "wheel-zoom")
            wheel.accepted = true
        }
    }

    // ── Touch pinch-zoom ───────────────────────────────────────────────
    // Mouse wheel is mouse-only — touch devices (Surface, etc.) need a
    // pinch handler. Single-finger drag still flows through the
    // MouseArea above (Qt auto-translates one touch to mouse events).
    // The handler snapshots windowSeconds and the anchor at pinch
    // start, then computes newSec = baseSec / scale so the pinch
    // gesture's cumulative scale maps smoothly to the visible window.
    PinchHandler {
        id: pinchZoom
        target: null  // we drive the viewer manually via setWindow

        property real _baseWindowSeconds: 0
        property real _baseTLo: 0
        property real _anchorRatio: 0.5

        onActiveChanged: {
            if (active && cell.width > 0) {
                _baseWindowSeconds = cell.windowSeconds
                _baseTLo = panZoomArea._currentTHi() - cell.windowSeconds
                _anchorRatio = Math.max(0, Math.min(1, centroid.position.x / cell.width))
                // Keyboard focus back to viewer on any touch interaction.
                if (cell.panZoomTarget) cell.panZoomTarget.forceActiveFocus()
            }
        }

        onScaleChanged: {
            if (!active || !cell.panZoomTarget || cell.width <= 0) return
            // Pinch out (scale > 1) = zoom in (smaller window); pinch
            // in (scale < 1) = zoom out. Matches platform conventions.
            var newSec = pinchZoom._baseWindowSeconds / scale
            var anchorT = pinchZoom._baseTLo
                          + pinchZoom._anchorRatio * pinchZoom._baseWindowSeconds
            cell.panZoomTarget.setWindow(
                anchorT - pinchZoom._anchorRatio * newSec,
                newSec,
                "pinch-zoom"
            )
        }
    }
}
