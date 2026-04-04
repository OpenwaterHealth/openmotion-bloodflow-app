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
    property bool developerMode: (AppFlags && AppFlags.developerMode) ? true : false

    // Fixed plot bounds — configurable from Settings
    property real bfiMin: 0.0
    property real bfiMax: 10.0
    property real bviMin: 0.0
    property real bviMax: 10.0
    property real meanMin: 0.0
    property real meanMax: 500.0
    property real contrastMin: 0.0
    property real contrastMax: 1.0
    readonly property var bfiBounds:      ({ minVal: bfiMin,      maxVal: bfiMax,      range: (bfiMax      - bfiMin)      || 1.0 })
    readonly property var bviBounds:      ({ minVal: bviMin,      maxVal: bviMax,      range: (bviMax      - bviMin)      || 1.0 })
    readonly property var meanBounds:     ({ minVal: meanMin,     maxVal: meanMax,     range: (meanMax     - meanMin)     || 1.0 })
    readonly property var contrastBounds: ({ minVal: contrastMin, maxVal: contrastMax, range: (contrastMax - contrastMin) || 1.0 })

    property color meanColor:     "#2ECC71"
    property color contrastColor: "#9B59B6"

    // Preview masks — update grid layout on camera selection change, even before scanning
    property int previewLeftMask: 0x99
    property int previewRightMask: 0x00
    onPreviewLeftMaskChanged:  if (!running) _applyPreviewLayout()
    onPreviewRightMaskChanged: if (!running) _applyPreviewLayout()

    // Data store: _store[key] = { bfi: [{t, v, fid},...], bvi: [{t, v, fid},...],
    //                              latestBfi, latestBvi, bfiBounds, bviBounds }
    property var _store: ({})

    // Display values shown in each panel header (updated once per timer tick)
    property var displayValues: ({})

    // Profiling
    property bool showProfiling:      false
    property int  _profSampleCount:   0
    property real _profSampleRateHz:  0.0
    property real _profRenderMs:      0.0
    property real _profCanvasMsAvg:   0.0
    property int  _profTotalPoints:   0
    property var  _profPaintAccum:    []
    property int  _profLastTickSampleCount: 0
    property real _profLastTickWall:        0.0

    // ── Series helpers ────────────────────────────────────────────────────────

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
            bfi: [], bvi: [], mean: [], contrast: [],
            latestBfi: NaN, latestBvi: NaN,
            latestMean: NaN, latestContrast: NaN,
            latestTemp: NaN
        }
    }

    function _applyPreviewLayout() {
        const order = _buildSeriesOrder(previewLeftMask, previewRightMask)
        seriesOrder   = order
        _store        = ({})
        displayValues = ({})
        for (let i = 0; i < order.length; i++) _ensureEntry(order[i])
    }

    Component.onCompleted: _applyPreviewLayout()

    function reset() {
        _store        = ({})
        displayValues = ({})
        seriesOrder   = []
        latestTimestamp = 0
        _profSampleCount         = 0
        _profSampleRateHz        = 0.0
        _profRenderMs            = 0.0
        _profCanvasMsAvg         = 0.0
        _profTotalPoints         = 0
        _profPaintAccum          = []
        _profLastTickSampleCount = 0
        _profLastTickWall        = 0.0
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

    // ── Sample ingestion ──────────────────────────────────────────────────────

    function handleBfiSample(side, camId, frameId, ts, val) {
        if (!running) return
        _profSampleCount++
        const key = _seriesKey(side, camId)
        _ensureEntry(key)
        _store[key].bfi.push({ t: ts, v: val, fid: frameId })
        _store[key].latestBfi = val
        if (ts > latestTimestamp) latestTimestamp = ts
    }

    function handleBviSample(side, camId, frameId, ts, val) {
        if (!running) return
        const key = _seriesKey(side, camId)
        _ensureEntry(key)
        _store[key].bvi.push({ t: ts, v: val, fid: frameId })
        _store[key].latestBvi = val
        if (ts > latestTimestamp) latestTimestamp = ts
    }

    function handleTempSample(side, camId, tempC) {
        if (!running) return
        const key = _seriesKey(side, camId)
        _ensureEntry(key)
        _store[key].latestTemp = tempC
    }

    function handleMeanSample(side, camId, ts, val) {
        if (!running) return
        const key = _seriesKey(side, camId)
        _ensureEntry(key)
        _store[key].mean.push({ t: ts, v: val })
        _store[key].latestMean = val
        if (ts > latestTimestamp) latestTimestamp = ts
    }

    function handleContrastSample(side, camId, ts, val) {
        if (!running) return
        const key = _seriesKey(side, camId)
        _ensureEntry(key)
        _store[key].contrast.push({ t: ts, v: val })
        _store[key].latestContrast = val
        if (ts > latestTimestamp) latestTimestamp = ts
    }

    // Called when the SDK emits a corrected batch. Overwrites stored uncorrected
    // values in-place for matching frame IDs.
    function handleCorrectedBatch(samples) {
        if (!running) return
        for (let i = 0; i < samples.length; i++) {
            const s = samples[i]
            const key = _seriesKey(s.side, s.camId)
            if (!_store[key]) continue
            // Find matching frame by linear scan
            for (let j = 0; j < _store[key].bfi.length; j++) {
                if (_store[key].bfi[j].fid === s.frameId) {
                    _store[key].bfi[j].v = s.bfi
                    _store[key].latestBfi = s.bfi
                    break
                }
            }
            for (let j = 0; j < _store[key].bvi.length; j++) {
                if (_store[key].bvi[j].fid === s.frameId) {
                    _store[key].bvi[j].v = s.bvi
                    _store[key].latestBvi = s.bvi
                    break
                }
            }
        }
    }

    // ── Timer tick: prune old data → repaint ─────────────────────────────────

    function _timerTick() {
        if (!visible) return
        const tickT0 = Date.now()
        _profPaintAccum = []

        // Sample rate calculation
        if (_profLastTickWall > 0) {
            const dtSec = (tickT0 - _profLastTickWall) * 0.001
            if (dtSec > 0) {
                const deltaSamples = _profSampleCount - _profLastTickSampleCount
                const instantHz    = deltaSamples / dtSec
                _profSampleRateHz  = 0.85 * _profSampleRateHz + 0.15 * instantHz
            }
        }
        _profLastTickSampleCount = _profSampleCount
        _profLastTickWall        = tickT0

        const nowTs  = latestTimestamp > 0 ? latestTimestamp : (Date.now() * 0.001)
        const cutoff = nowTs - windowSeconds

        let totalPts   = 0
        let newDisplay = {}

        for (let i = 0; i < seriesOrder.length; i++) {
            const key = seriesOrder[i]
            const s   = _store[key]
            if (!s) continue

            // Remove points older than the window (simple forward scan)
            let removeCount = 0
            while (removeCount < s.bfi.length      && s.bfi[removeCount].t      < cutoff) removeCount++
            if (removeCount > 0) s.bfi.splice(0, removeCount)

            removeCount = 0
            while (removeCount < s.bvi.length      && s.bvi[removeCount].t      < cutoff) removeCount++
            if (removeCount > 0) s.bvi.splice(0, removeCount)

            removeCount = 0
            while (removeCount < s.mean.length     && s.mean[removeCount].t     < cutoff) removeCount++
            if (removeCount > 0) s.mean.splice(0, removeCount)

            removeCount = 0
            while (removeCount < s.contrast.length && s.contrast[removeCount].t < cutoff) removeCount++
            if (removeCount > 0) s.contrast.splice(0, removeCount)

            totalPts += s.bfi.length + s.bvi.length + s.mean.length + s.contrast.length

            newDisplay[key] = {
                bfi:      isFinite(s.latestBfi)      ? s.latestBfi.toFixed(2)      : "--",
                bvi:      isFinite(s.latestBvi)      ? s.latestBvi.toFixed(2)      : "--",
                mean:     isFinite(s.latestMean)     ? s.latestMean.toFixed(1)     : "--",
                contrast: isFinite(s.latestContrast) ? s.latestContrast.toFixed(3) : "--",
                temp:     isFinite(s.latestTemp)     ? s.latestTemp.toFixed(1)     : "--"
            }

            // Repaint this series' canvas
            const item = plotRepeater.itemAt(i)
            if (item && item.plotCanvas) item.plotCanvas.requestPaint()
        }

        displayValues    = newDisplay
        _profTotalPoints = totalPts
        _profRenderMs    = Date.now() - tickT0
    }

    function _recordPaintTime(ms) {
        _profPaintAccum.push(ms)
        let sum = 0
        for (let i = 0; i < _profPaintAccum.length; i++) sum += _profPaintAccum[i]
        _profCanvasMsAvg = sum / _profPaintAccum.length
    }

    Timer {
        interval: 100    // 10 Hz render tick
        repeat:   true
        running:  plotArea.visible && plotArea.running
        onTriggered: plotArea._timerTick()
    }

    // ── Layout ────────────────────────────────────────────────────────────────

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
                                visible:        plotArea.developerMode
                                text:           ((plotArea.displayValues[seriesKey] || {}).temp || "--") + "°C"
                                color:          "#F39C12"
                                font.pixelSize: 11
                                font.family:    "Consolas"
                            }
                            Text {
                                text:  plotArea.showBfiBvi
                                       ? "BFI: "  + ((plotArea.displayValues[seriesKey] || {}).bfi      || "--")
                                       : "Mean: " + ((plotArea.displayValues[seriesKey] || {}).mean     || "--")
                                color: plotArea.showBfiBvi ? plotArea.bfiColor : plotArea.meanColor
                                font.pixelSize: 12
                            }
                            Text {
                                text:  plotArea.showBfiBvi
                                       ? "BVI: "  + ((plotArea.displayValues[seriesKey] || {}).bvi      || "--")
                                       : "Cont: " + ((plotArea.displayValues[seriesKey] || {}).contrast || "--")
                                color: plotArea.showBfiBvi ? plotArea.bviColor : plotArea.contrastColor
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

                                // Asymmetric padding: left/right make room for Y-axis labels
                                const padL = 38, padR = 24, padT = 16, padB = 16
                                const w = width  - padL - padR
                                const h = height - padT - padB
                                if (w <= 0 || h <= 0) return

                                const nowTs  = plotArea.latestTimestamp > 0
                                    ? plotArea.latestTimestamp : (Date.now() * 0.001)
                                const xMin   = nowTs - plotArea.windowSeconds
                                const xRange = plotArea.windowSeconds

                                const s = plotArea._store[seriesKey]
                                         || { bfi: [], bvi: [], mean: [], contrast: [] }
                                const showBfi = plotArea.showBfiBvi

                                // Background grid
                                ctx.strokeStyle = "#2A2A2E"
                                ctx.lineWidth   = 0.5
                                ctx.beginPath()
                                for (let gi = 0; gi <= 4; gi++) {
                                    const gy = padT + (h / 4) * gi
                                    ctx.moveTo(padL,         gy)
                                    ctx.lineTo(padL + w,     gy)
                                }
                                ctx.stroke()

                                // Axes
                                ctx.strokeStyle = "#3E4E6F"
                                ctx.lineWidth   = 1
                                ctx.beginPath()
                                ctx.moveTo(padL,     padT)
                                ctx.lineTo(padL,     padT + h)
                                ctx.lineTo(padL + w, padT + h)
                                ctx.stroke()

                                // Draw a data series
                                function drawSeries(series, color, bounds) {
                                    if (series.length < 2) return
                                    const invR = bounds.range > 0 ? 1.0 / bounds.range : 1.0
                                    const mn   = bounds.minVal
                                    ctx.strokeStyle = color
                                    ctx.lineWidth   = 2
                                    ctx.beginPath()
                                    ctx.moveTo(padL + ((series[0].t - xMin) / xRange) * w,
                                               padT + h - ((series[0].v - mn) * invR) * h)
                                    for (let j = 1; j < series.length; j++) {
                                        const pt = series[j]
                                        ctx.lineTo(padL + ((pt.t - xMin) / xRange) * w,
                                                   padT + h - ((pt.v - mn) * invR) * h)
                                    }
                                    ctx.stroke()
                                }

                                // Y-axis label helper — 3 ticks: min, mid, max
                                function fmtVal(v, range) {
                                    if (range <= 2)  return v.toFixed(2)
                                    if (range <= 20) return v.toFixed(1)
                                    return v.toFixed(0)
                                }
                                function drawYLabels(bounds, color, isLeft) {
                                    ctx.font         = "9px sans-serif"
                                    ctx.textBaseline = "middle"
                                    ctx.fillStyle    = color
                                    for (let ti = 0; ti <= 2; ti++) {
                                        const frac = ti / 2.0
                                        const val  = bounds.minVal + frac * bounds.range
                                        const y    = padT + h * (1.0 - frac)
                                        if (isLeft) {
                                            ctx.textAlign = "right"
                                            ctx.fillText(fmtVal(val, bounds.range), padL - 4, y)
                                        } else {
                                            ctx.textAlign = "left"
                                            ctx.fillText(fmtVal(val, bounds.range), padL + w + 4, y)
                                        }
                                    }
                                }

                                // Draw series and labels based on mode
                                if (showBfi) {
                                    drawSeries(s.bfi, plotArea.bfiColor, plotArea.bfiBounds)
                                    drawSeries(s.bvi, plotArea.bviColor, plotArea.bviBounds)
                                    drawYLabels(plotArea.bfiBounds, plotArea.bfiColor, true)
                                    drawYLabels(plotArea.bviBounds, plotArea.bviColor, false)
                                } else {
                                    drawSeries(s.mean,     plotArea.meanColor,     plotArea.meanBounds)
                                    drawSeries(s.contrast, plotArea.contrastColor, plotArea.contrastBounds)
                                    drawYLabels(plotArea.meanBounds,     plotArea.meanColor,     true)
                                    drawYLabels(plotArea.contrastBounds, plotArea.contrastColor, false)
                                }

                                // "Waiting for data" placeholder
                                const hasData = showBfi
                                    ? (s.bfi.length > 0 || s.bvi.length > 0)
                                    : (s.mean.length > 0 || s.contrast.length > 0)
                                if (!hasData) {
                                    ctx.fillStyle    = "#7F8C8D"
                                    ctx.textAlign    = "center"
                                    ctx.textBaseline = "middle"
                                    ctx.font         = "12px sans-serif"
                                    ctx.fillText("Waiting for data...", padL + w / 2, padT + h / 2)
                                }

                                plotArea._recordPaintTime(Date.now() - paintT0)
                            }
                        }
                    }
                }
            }
        }

        Text {
            visible:             seriesOrder.length === 0
            text:                running ? "Waiting for camera data..." : "Press Start to begin scanning"
            color:               "#7F8C8D"
            font.pixelSize:      18
            horizontalAlignment: Text.AlignHCenter
            Layout.alignment:    Qt.AlignHCenter
            Layout.fillWidth:    true
        }
    }

    // ── Profiling overlay ─────────────────────────────────────────────────────

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

            Text {
                text:           "─── GUI Profiling ───"
                color:          "#4A90E2"
                font.pixelSize: 11
                font.weight:    Font.Bold
            }

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

            Text {
                text:           "Data pts:   %1 total".arg(plotArea._profTotalPoints)
                color:          "#C9D1D9"
                font.pixelSize: 11
                font.family:    "Consolas"
            }

            Text {
                text:           "Samples rx: %1".arg(plotArea._profSampleCount)
                color:          "#C9D1D9"
                font.pixelSize: 11
                font.family:    "Consolas"
            }

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

    // ── Signal connections ────────────────────────────────────────────────────

    Connections {
        target: MOTIONInterface
        function onScanBfiSampled(side, camId, frameId, timestampSec, bfiVal) {
            plotArea.handleBfiSample(side, camId, frameId, timestampSec, bfiVal)
        }
        function onScanBviSampled(side, camId, frameId, timestampSec, bviVal) {
            plotArea.handleBviSample(side, camId, frameId, timestampSec, bviVal)
        }
        function onScanMeanSampled(side, camId, timestampSec, meanVal) {
            plotArea.handleMeanSample(side, camId, timestampSec, meanVal)
        }
        function onScanContrastSampled(side, camId, timestampSec, contrastVal) {
            plotArea.handleContrastSample(side, camId, timestampSec, contrastVal)
        }
        function onScanCorrectedBatch(samples) {
            plotArea.handleCorrectedBatch(samples)
        }
        function onScanCameraTemperature(side, camId, tempC) {
            plotArea.handleTempSample(side, camId, tempC)
        }
    }
}
