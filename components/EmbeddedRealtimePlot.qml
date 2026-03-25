import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Rectangle {
    id: plotArea
    color: "#1E1E20"
    radius: 12
    border.color: "#2A2A2E"
    border.width: 1

    property int  windowSeconds: 15
    property bool running: false
    property var  seriesOrder: []
    property real latestTimestamp: 0
    property color bfiColor: "#E74C3C"
    property color bviColor: "#3498DB"
    property int  plotColumns: 4
    property int  leftActiveCount: 0
    property int  rightActiveCount: 0
    property int  plotRows: (leftActiveCount === 8 || rightActiveCount === 8) ? 4 : 2
    property bool showBfiBvi: true

    // Preview masks — update grid layout on camera selection change, even before scanning
    property int previewLeftMask: 0x99
    property int previewRightMask: 0x00
    onPreviewLeftMaskChanged:  if (!running) _applyPreviewLayout()
    onPreviewRightMaskChanged: if (!running) _applyPreviewLayout()

    // ── Internal JS-only store (mutated in-place; no QML property-change spam) ──────
    // _store[key] = { bfi:[], bvi:[], latestBfi, latestBvi, bfiBounds, bviBounds }
    property var _store: ({})
    property var _dirty: ({})   // key → true when new samples have arrived

    // ── Display values updated once per timer tick (drives Text bindings at ~10 Hz) ──
    // Shape: { key: { bfi: "1.23", bvi: "0.45" } }
    property var displayValues: ({})

    // ── Profiling ────────────────────────────────────────────────────────────────────
    property bool showProfiling:      false
    property int  _profSampleCount:   0      // cumulative samples received this run
    property real _profSampleRateHz:  0.0    // EMA of sample rate (samples/sec, all cameras)
    property real _profRenderMs:      0.0    // wall-clock time for last _timerTick (ms)
    property real _profCanvasMsAvg:   0.0    // mean canvas onPaint time this tick (ms)
    property int  _profTotalPoints:   0      // sum of all data points currently in memory
    property real _profLastSampleWall: 0.0   // Date.now() of most recent sample
    property var  _profPaintAccum:    []     // paint-time samples within a tick

    // ── Series helpers ────────────────────────────────────────────────────────────────

    function _seriesKey(side, camId) { return side + ":" + camId }

    function _labelFor(key) {
        const parts = key.split(":")
        if (parts.length !== 2) return key
        const cam = parseInt(parts[1], 10)
        return (parts[0] === "left" ? "L" : "R") + "-" + (isNaN(cam) ? parts[1] : (cam + 1))
    }

    function _activeCamsFromMask(mask) {
        const cams = []
        for (let bit = 0; bit < 8; bit++) { if (mask & (1 << bit)) cams.push(bit) }
        return cams
    }

    function _buildSeriesOrder(leftMask, rightMask) {
        const leftCams  = _activeCamsFromMask(leftMask)
        const rightCams = _activeCamsFromMask(rightMask)
        leftActiveCount  = leftCams.length
        rightActiveCount = rightCams.length
        const rows    = (leftCams.length === 8 || rightCams.length === 8) ? 4 : 2
        const lastIdx = (rows * 2) - 1
        const order   = []
        for (let row = 0; row < rows; row++) {
            if (leftCams.length > 0) {
                const a = leftCams[row], b = leftCams[lastIdx - row]
                if (a !== undefined) order.push(_seriesKey("left",  a))
                if (b !== undefined) order.push(_seriesKey("left",  b))
            }
            if (rightCams.length > 0) {
                const a = rightCams[row], b = rightCams[lastIdx - row]
                if (a !== undefined) order.push(_seriesKey("right", a))
                if (b !== undefined) order.push(_seriesKey("right", b))
            }
        }
        return order
    }

    function _ensureEntry(key) {
        if (_store[key]) return
        _store[key] = {
            bfi: [], bvi: [],
            latestBfi: NaN, latestBvi: NaN,
            bfiBounds: { minVal: 0, maxVal: 1, range: 1 },
            bviBounds: { minVal: 0, maxVal: 1, range: 1 }
        }
        _dirty[key] = false
    }

    function _applyPreviewLayout() {
        const order = _buildSeriesOrder(previewLeftMask, previewRightMask)
        seriesOrder  = order
        _store       = ({})
        _dirty       = ({})
        displayValues = ({})
        for (let i = 0; i < order.length; i++) _ensureEntry(order[i])
    }

    Component.onCompleted: _applyPreviewLayout()

    function reset() {
        _store        = ({})
        _dirty        = ({})
        displayValues = ({})
        seriesOrder   = []
        latestTimestamp = 0
        // Reset profiling counters
        _profSampleCount    = 0
        _profSampleRateHz   = 0.0
        _profRenderMs       = 0.0
        _profCanvasMsAvg    = 0.0
        _profTotalPoints    = 0
        _profLastSampleWall = 0.0
        _profPaintAccum     = []
    }

    function startScan(leftMask, rightMask) {
        reset()
        running = true
        const order = _buildSeriesOrder(leftMask, rightMask)
        seriesOrder = order
        for (let i = 0; i < order.length; i++) _ensureEntry(order[i])
    }

    function stopScan() {
        running = false
        Qt.callLater(_applyPreviewLayout)
    }

    // ── Sample ingestion ─────────────────────────────────────────────────────────────
    // These are called from signal handlers and must be as fast as possible.
    // No Object.assign, no QML property assignments other than latestTimestamp.

    function handleBfiSample(side, camId, ts, val) {
        if (!running) return

        // Profiling: track inter-sample interval for rate estimate
        const nowMs = Date.now()
        if (_profLastSampleWall > 0) {
            const dtSec = (nowMs - _profLastSampleWall) * 0.001
            if (dtSec > 0)
                _profSampleRateHz = 0.85 * _profSampleRateHz + 0.15 / dtSec
        }
        _profLastSampleWall = nowMs
        _profSampleCount++

        const key = _seriesKey(side, camId)
        _ensureEntry(key)
        _store[key].bfi.push({ t: ts, v: val })
        _store[key].latestBfi = val
        _dirty[key] = true
        if (ts > latestTimestamp) latestTimestamp = ts
    }

    function handleBviSample(side, camId, ts, val) {
        if (!running) return
        const key = _seriesKey(side, camId)
        _ensureEntry(key)
        _store[key].bvi.push({ t: ts, v: val })
        _store[key].latestBvi = val
        _dirty[key] = true
        if (ts > latestTimestamp) latestTimestamp = ts
    }

    // ── Bounds (called only when dirty, result cached in _store) ─────────────────────

    function _calcBounds(arr) {
        const n = arr.length
        if (n === 0) return { minVal: 0, maxVal: 1, range: 1 }
        let mx = -1e38, mn = 1e38
        for (let i = 0; i < n; i++) {
            const v = arr[i].v
            if (v > mx) mx = v
            if (v < mn) mn = v
        }
        if (mn === mx) { mn -= 1; mx += 1 }
        return { minVal: mn, maxVal: mx, range: mx - mn }
    }

    // ── Timer tick: prune → update bounds/display → repaint ──────────────────────────

    function _timerTick() {
        if (!visible) return
        const tickT0 = Date.now()
        _profPaintAccum = []   // reset per-canvas paint accumulator each tick

        const nowTs = latestTimestamp > 0 ? latestTimestamp : (Date.now() * 0.001)
        const cutoff = nowTs - windowSeconds

        let newDisplay      = Object.assign({}, displayValues)
        let anyDisplayDirty = false
        let totalPts        = 0

        const n = seriesOrder.length
        for (let i = 0; i < n; i++) {
            const key = seriesOrder[i]
            const s   = _store[key]
            if (!s) continue

            // Prune via binary search + single splice (O(log N) + O(1) amortised)
            let lo = 0, hi = s.bfi.length
            while (lo < hi) { const mid = (lo + hi) >>> 1; s.bfi[mid].t < cutoff ? lo = mid + 1 : hi = mid }
            if (lo > 0) s.bfi.splice(0, lo)

            lo = 0; hi = s.bvi.length
            while (lo < hi) { const mid = (lo + hi) >>> 1; s.bvi[mid].t < cutoff ? lo = mid + 1 : hi = mid }
            if (lo > 0) s.bvi.splice(0, lo)

            totalPts += s.bfi.length + s.bvi.length

            // Update cached bounds and display strings only if new data arrived
            if (_dirty[key]) {
                s.bfiBounds = _calcBounds(s.bfi)
                s.bviBounds = _calcBounds(s.bvi)
                newDisplay[key] = {
                    bfi: isFinite(s.latestBfi) ? s.latestBfi.toFixed(2) : "--",
                    bvi: isFinite(s.latestBvi) ? s.latestBvi.toFixed(2) : "--"
                }
                anyDisplayDirty = true
                _dirty[key] = false
            }

            // Trigger canvas repaint for this series
            const item = plotRepeater.itemAt(i)
            if (item && item.plotCanvas) item.plotCanvas.requestPaint()
        }

        // Single QML property write for all display text — one change notification
        if (anyDisplayDirty) displayValues = newDisplay

        // Profiling bookkeeping (deferred so paint times can accumulate)
        _profTotalPoints = totalPts
        _profRenderMs    = Date.now() - tickT0
    }

    // Called by each canvas after it finishes painting to log its render time
    function _recordPaintTime(ms) {
        _profPaintAccum.push(ms)
        if (_profPaintAccum.length > 0) {
            let sum = 0
            for (let i = 0; i < _profPaintAccum.length; i++) sum += _profPaintAccum[i]
            _profCanvasMsAvg = sum / _profPaintAccum.length
        }
    }

    Timer {
        interval: 100    // 10 Hz render tick
        repeat:   true
        running:  plotArea.visible && plotArea.running
        onTriggered: plotArea._timerTick()
    }

    // ── Layout ───────────────────────────────────────────────────────────────────────

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 10
        spacing: 6

        GridLayout {
            id: plotGrid
            Layout.fillWidth:  true
            Layout.fillHeight: true
            columns:     plotColumns
            rowSpacing:  6
            columnSpacing: 6

            Repeater {
                id: plotRepeater
                model: seriesOrder

                delegate: Rectangle {
                    property string seriesKey:   modelData
                    property alias  plotCanvas:  plotCanvas
                    color:        "#141417"
                    border.color: "#2A2A2E"
                    radius: 8
                    Layout.fillWidth:  true
                    Layout.fillHeight: true
                    Layout.preferredHeight:
                        (plotGrid.height - (plotArea.plotRows - 1) * plotGrid.rowSpacing)
                        / plotArea.plotRows

                    ColumnLayout {
                        anchors.fill:    parent
                        anchors.margins: 6
                        spacing: 4

                        // Header: label + BFI/BVI values (updated at timer rate, not per-sample)
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6
                            Text {
                                text:           plotArea._labelFor(seriesKey)
                                color:          "#FFFFFF"
                                font.pixelSize: 12
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text:  "BFI: " + ((plotArea.displayValues[seriesKey] || {}).bfi || "--")
                                color: plotArea.bfiColor
                                font.pixelSize: 12
                            }
                            Text {
                                text:  "BVI: " + ((plotArea.displayValues[seriesKey] || {}).bvi || "--")
                                color: plotArea.bviColor
                                font.pixelSize: 12
                            }
                        }

                        Canvas {
                            id: plotCanvas
                            Layout.fillWidth:  true
                            Layout.fillHeight: true

                            onPaint: {
                                const paintT0 = Date.now()
                                const ctx = getContext("2d")
                                ctx.clearRect(0, 0, width, height)

                                const pad = 20
                                const w   = width  - 2 * pad
                                const h   = height - 2 * pad
                                if (w <= 0 || h <= 0) return

                                const nowTs = plotArea.latestTimestamp > 0
                                    ? plotArea.latestTimestamp : (Date.now() * 0.001)
                                const xMin   = nowTs - plotArea.windowSeconds
                                const xRange = plotArea.windowSeconds   // == xMax - xMin

                                // Read from in-place JS store (no QML property overhead)
                                const s   = plotArea._store[seriesKey]
                                    || { bfi: [], bvi: [],
                                         bfiBounds: { minVal: 0, maxVal: 1, range: 1 },
                                         bviBounds: { minVal: 0, maxVal: 1, range: 1 } }
                                const bfiSeries = s.bfi
                                const bviSeries = s.bvi

                                // Background grid — all 5 horizontals in one path
                                ctx.strokeStyle = "#2A2A2E"
                                ctx.lineWidth   = 0.5
                                ctx.beginPath()
                                for (let gi = 0; gi <= 4; gi++) {
                                    const gy = pad + (h / 4) * gi
                                    ctx.moveTo(pad,     gy)
                                    ctx.lineTo(pad + w, gy)
                                }
                                ctx.stroke()

                                // Axes — L + bottom in one path
                                ctx.strokeStyle = "#3E4E6F"
                                ctx.lineWidth   = 1
                                ctx.beginPath()
                                ctx.moveTo(pad,     pad)
                                ctx.lineTo(pad,     pad + h)
                                ctx.lineTo(pad + w, pad + h)
                                ctx.stroke()

                                // Draw a data series with adaptive subsampling.
                                // We cap visible vertices at MAX_PTS to bound render time
                                // regardless of how much data has accumulated.
                                function drawSeries(series, color, bounds) {
                                    const n = series.length
                                    if (n < 2) return
                                    const MAX_PTS = 400
                                    const step    = n > MAX_PTS ? Math.ceil(n / MAX_PTS) : 1
                                    const invR    = bounds.range > 0 ? 1.0 / bounds.range : 1.0
                                    const mn      = bounds.minVal
                                    ctx.strokeStyle = color
                                    ctx.lineWidth   = 2
                                    ctx.beginPath()
                                    // First point
                                    let pt = series[0]
                                    ctx.moveTo(pad + ((pt.t - xMin) / xRange) * w,
                                               pad + h - ((pt.v - mn) * invR) * h)
                                    for (let j = step; j < n; j += step) {
                                        pt = series[j]
                                        ctx.lineTo(pad + ((pt.t - xMin) / xRange) * w,
                                                   pad + h - ((pt.v - mn) * invR) * h)
                                    }
                                    // Always include the final point for accuracy
                                    if (step > 1) {
                                        pt = series[n - 1]
                                        ctx.lineTo(pad + ((pt.t - xMin) / xRange) * w,
                                                   pad + h - ((pt.v - mn) * invR) * h)
                                    }
                                    ctx.stroke()
                                }

                                drawSeries(bfiSeries, plotArea.bfiColor, s.bfiBounds)
                                drawSeries(bviSeries, plotArea.bviColor, s.bviBounds)

                                if (bfiSeries.length === 0 && bviSeries.length === 0) {
                                    ctx.fillStyle  = "#7F8C8D"
                                    ctx.textAlign  = "center"
                                    ctx.font       = "12px sans-serif"
                                    ctx.fillText("Waiting for data...", pad + w / 2, pad + h / 2)
                                }

                                // Report paint time to profiler
                                plotArea._recordPaintTime(Date.now() - paintT0)
                            }
                        }
                    }
                }
            }
        }

        Text {
            visible:            seriesOrder.length === 0
            text:               running ? "Waiting for camera data..." : "Press Start to begin scanning"
            color:              "#7F8C8D"
            font.pixelSize:     18
            horizontalAlignment: Text.AlignHCenter
            Layout.alignment:   Qt.AlignHCenter
            Layout.fillWidth:   true
        }
    }

    // ── Profiling overlay ─────────────────────────────────────────────────────────────

    // Toggle button — always visible in top-right corner
    Rectangle {
        id: profToggleBtn
        anchors.top:   parent.top
        anchors.right: parent.right
        anchors.topMargin:   12
        anchors.rightMargin: 12
        width: 36; height: 22; radius: 4
        color:        toggleMa.containsMouse ? "#2A3040" : "transparent"
        border.color: plotArea.showProfiling ? "#4A90E2" : "#3A3A4A"
        border.width: 1
        z: 30

        Text {
            anchors.centerIn: parent
            text:           "⏱"
            font.pixelSize: 14
            color: plotArea.showProfiling ? "#4A90E2" : "#555570"
        }
        MouseArea {
            id: toggleMa
            anchors.fill: parent
            hoverEnabled: true
            cursorShape:  Qt.PointingHandCursor
            onClicked:    plotArea.showProfiling = !plotArea.showProfiling
        }
    }

    // Profiling panel — appears below the toggle button
    Rectangle {
        id: profPanel
        visible:       plotArea.showProfiling
        anchors.top:   profToggleBtn.bottom
        anchors.right: parent.right
        anchors.topMargin:   4
        anchors.rightMargin: 12
        width:  230
        height: profCol.implicitHeight + 18
        color:        "#DD0D1117"
        radius:       6
        border.color: "#3E4E6F"
        border.width: 1
        z: 30

        Column {
            id: profCol
            anchors {
                left: parent.left; right: parent.right
                top:  parent.top;  margins: 9
            }
            spacing: 3

            // ── Header ──
            Text {
                text:           "─── GUI Profiling ───"
                color:          "#4A90E2"
                font.pixelSize: 11
                font.weight:    Font.Bold
            }

            // ── SDK throughput (if this is low, the bottleneck is the SDK) ──
            Text {
                text: {
                    const r   = plotArea._profSampleRateHz
                    const exp = (plotArea.leftActiveCount + plotArea.rightActiveCount) * 40
                    return "SDK rate:   %1 / %2 smp/s".arg(r.toFixed(0)).arg(exp)
                }
                color:          "#C9D1D9"
                font.pixelSize: 11
                font.family:    "Consolas"
            }

            // ── GUI render time (if this approaches 100 ms, the GUI is the bottleneck) ──
            Text {
                text: {
                    const ms  = plotArea._profRenderMs
                    const pct = (ms / 100.0 * 100).toFixed(0)
                    return "Tick work:  %1 ms  (%2% budget)".arg(ms.toFixed(1)).arg(pct)
                }
                color: plotArea._profRenderMs > 60 ? "#E74C3C"
                     : plotArea._profRenderMs > 30 ? "#F39C12" : "#C9D1D9"
                font.pixelSize: 11
                font.family:    "Consolas"
            }

            // ── Canvas render time ──
            Text {
                text: {
                    const ms  = plotArea._profCanvasMsAvg
                    const tot = ms * (plotArea.leftActiveCount + plotArea.rightActiveCount)
                    return "Canvas/avg: %1 ms  (×%2 = %3 ms)"
                        .arg(ms.toFixed(1))
                        .arg(plotArea.leftActiveCount + plotArea.rightActiveCount)
                        .arg(tot.toFixed(0))
                }
                color: plotArea._profCanvasMsAvg > 15 ? "#E74C3C"
                     : plotArea._profCanvasMsAvg > 8  ? "#F39C12" : "#C9D1D9"
                font.pixelSize: 11
                font.family:    "Consolas"
            }

            // ── Data points ──
            Text {
                text:           "Data pts:   %1 total".arg(plotArea._profTotalPoints)
                color:          "#C9D1D9"
                font.pixelSize: 11
                font.family:    "Consolas"
            }

            // ── Sample count ──
            Text {
                text:           "Samples rx: %1".arg(plotArea._profSampleCount)
                color:          "#C9D1D9"
                font.pixelSize: 11
                font.family:    "Consolas"
            }

            // ── Guide ──
            Rectangle { width: parent.width; height: 1; color: "#2A3040" }
            Text {
                text:           "Red/orange = that layer is the\nbottleneck. SDK rate low → SDK;\nTick/canvas high → GUI."
                color:          "#555570"
                font.pixelSize: 10
                wrapMode:       Text.Wrap
                width:          parent.width
            }
        }
    }

    // ── Signal connections ────────────────────────────────────────────────────────────

    Connections {
        target: MOTIONInterface
        function onScanBfiCorrectedSampled(side, camId, timestampSec, bfiVal) {
            plotArea.handleBfiSample(side, camId, timestampSec, bfiVal)
        }
        function onScanBviCorrectedSampled(side, camId, timestampSec, bviVal) {
            plotArea.handleBviSample(side, camId, timestampSec, bviVal)
        }
    }
}
