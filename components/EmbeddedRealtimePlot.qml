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

    property int windowSeconds: 15
    property bool running: false
    property var seriesData: ({})
    property var seriesOrder: []
    property real latestTimestamp: 0
    property color bfiColor: "#E74C3C"
    property color bviColor: "#3498DB"
    property int plotColumns: 4
    property int leftActiveCount: 0
    property int rightActiveCount: 0
    property int plotRows: (leftActiveCount === 8 || rightActiveCount === 8) ? 4 : 2
    property bool showBfiBvi: true  // true = BFI/BVI, false = Mean/StdDev

    // Preview masks — update the grid layout whenever camera selection changes,
    // even before scanning starts, so blank plot frames are always visible.
    property int previewLeftMask: 0x99
    property int previewRightMask: 0x00
    onPreviewLeftMaskChanged:  if (!running) _applyPreviewLayout()
    onPreviewRightMaskChanged: if (!running) _applyPreviewLayout()

    function _applyPreviewLayout() {
        const order = _buildSeriesOrder(previewLeftMask, previewRightMask)
        seriesOrder = order
        seriesData = ({})
        for (let i = 0; i < order.length; i++) _ensureSeries(order[i], false)
    }

    function _seriesKey(side, camId) {
        return side + ":" + camId
    }

    function _labelFor(key) {
        const parts = key.split(":")
        if (parts.length !== 2) return key
        const side = parts[0]
        const cam = parseInt(parts[1], 10)
        const camLabel = isNaN(cam) ? parts[1] : (cam + 1)
        return (side === "left" ? "L" : "R") + "-" + camLabel
    }

    function _ensureSeries(key, addToOrder) {
        if (seriesData[key] !== undefined) return
        seriesData[key] = ({ bfi: [], bvi: [], latestBfi: NaN, latestBvi: NaN })
        if (addToOrder) {
            seriesOrder = seriesOrder.concat([key])
        }
    }

    function _activeCamsFromMask(mask) {
        const cams = []
        for (let bit = 0; bit < 8; bit++) {
            if (mask & (1 << bit)) cams.push(bit)
        }
        return cams
    }

    function _buildSeriesOrder(leftMask, rightMask) {
        const leftCams = _activeCamsFromMask(leftMask)
        const rightCams = _activeCamsFromMask(rightMask)
        leftActiveCount = leftCams.length
        rightActiveCount = rightCams.length
        const rows = (leftCams.length === 8 || rightCams.length === 8) ? 4 : 2
        const lastIdx = (rows * 2) - 1
        const order = []
        for (let row = 0; row < rows; row++) {
            if (leftCams.length > 0) {
                const a = leftCams[row]; const b = leftCams[lastIdx - row]
                if (a !== undefined) order.push(_seriesKey("left", a))
                if (b !== undefined) order.push(_seriesKey("left", b))
            }
            if (rightCams.length > 0) {
                const a = rightCams[row]; const b = rightCams[lastIdx - row]
                if (a !== undefined) order.push(_seriesKey("right", a))
                if (b !== undefined) order.push(_seriesKey("right", b))
            }
        }
        return order
    }

    Component.onCompleted: _applyPreviewLayout()

    function reset() {
        seriesData = ({})
        seriesOrder = []
        latestTimestamp = 0
    }

    function startScan(leftMask, rightMask) {
        reset()
        running = true
        const order = _buildSeriesOrder(leftMask, rightMask)
        seriesOrder = order
        for (let i = 0; i < order.length; i++) _ensureSeries(order[i], false)
    }

    function stopScan() {
        running = false
        Qt.callLater(_applyPreviewLayout)  // restore blank frames after scan ends
    }

    function handleBfiSample(side, camId, timestampSec, bfiVal) {
        if (!running) return
        const key = _seriesKey(side, camId)
        _ensureSeries(key, false)
        seriesData[key].bfi.push({ t: timestampSec, v: bfiVal })
        seriesData[key].latestBfi = bfiVal
        seriesData = Object.assign({}, seriesData)
        if (timestampSec > latestTimestamp) latestTimestamp = timestampSec
    }

    function handleBviSample(side, camId, timestampSec, bviVal) {
        if (!running) return
        const key = _seriesKey(side, camId)
        _ensureSeries(key, false)
        seriesData[key].bvi.push({ t: timestampSec, v: bviVal })
        seriesData[key].latestBvi = bviVal
        seriesData = Object.assign({}, seriesData)
        if (timestampSec > latestTimestamp) latestTimestamp = timestampSec
    }

    function _pruneAndRepaint() {
        if (!visible) return
        const nowTs = latestTimestamp > 0 ? latestTimestamp : (Date.now() / 1000.0)
        const cutoff = nowTs - windowSeconds
        for (let i = 0; i < seriesOrder.length; i++) {
            const key = seriesOrder[i]
            const series = seriesData[key] || ({ bfi: [], bvi: [] })
            while (series.bfi.length > 0 && series.bfi[0].t < cutoff) series.bfi.shift()
            while (series.bvi.length > 0 && series.bvi[0].t < cutoff) series.bvi.shift()
        }
        for (let i = 0; i < plotRepeater.count; i++) {
            const item = plotRepeater.itemAt(i)
            if (item && item.plotCanvas) item.plotCanvas.requestPaint()
        }
    }

    function _formatValue(val) {
        return isFinite(val) ? val.toFixed(2) : "--"
    }

    function _seriesBounds(series) {
        let maxVal = -Infinity, minVal = Infinity
        for (let j = 0; j < series.length; j++) {
            const v = series[j].v
            if (v > maxVal) maxVal = v
            if (v < minVal) minVal = v
        }
        if (!isFinite(minVal) || !isFinite(maxVal)) { minVal = 0; maxVal = 1 }
        else if (minVal === maxVal) { minVal -= 1; maxVal += 1 }
        return { minVal: minVal, maxVal: maxVal, range: maxVal - minVal }
    }

    Timer {
        interval: 100; repeat: true
        running: plotArea.visible && plotArea.running
        onTriggered: plotArea._pruneAndRepaint()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 10
        spacing: 6

        // Header row
        RowLayout {
            Layout.fillWidth: true; spacing: 10
            Text {
                text: running ? "Realtime " + (showBfiBvi ? "BFI/BVI" : "Mean/StdDev") + " (Last " + windowSeconds + "s)" : "Data Viewer"
                color: "#FFFFFF"
                font.pixelSize: 16
                font.weight: Font.DemiBold
            }
            Item { Layout.fillWidth: true }
        }

        // Plot grid
        GridLayout {
            id: plotGrid
            Layout.fillWidth: true
            Layout.fillHeight: true
            columns: plotColumns
            rowSpacing: 6
            columnSpacing: 6

            Repeater {
                id: plotRepeater
                model: seriesOrder
                delegate: Rectangle {
                    property string seriesKey: modelData
                    property alias plotCanvas: plotCanvas
                    color: "#141417"
                    border.color: "#2A2A2E"
                    radius: 8
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.preferredHeight: (plotGrid.height - (plotArea.plotRows - 1) * plotGrid.rowSpacing) / plotArea.plotRows

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 6
                        spacing: 4

                        // Header row: label + latest BFI/BVI values
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6
                            Text {
                                text: plotArea._labelFor(seriesKey)
                                color: "#FFFFFF"
                                font.pixelSize: 12
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: "BFI: " + plotArea._formatValue((plotArea.seriesData[seriesKey] || {}).latestBfi)
                                color: plotArea.bfiColor
                                font.pixelSize: 12
                            }
                            Text {
                                text: "BVI: " + plotArea._formatValue((plotArea.seriesData[seriesKey] || {}).latestBvi)
                                color: plotArea.bviColor
                                font.pixelSize: 12
                            }
                        }

                        // Plot canvas
                        Canvas {
                            id: plotCanvas
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            onPaint: {
                                const ctx = getContext("2d")
                                ctx.clearRect(0, 0, width, height)

                                const pad = 20
                                const w = width - 2 * pad
                                const h = height - 2 * pad
                                if (w <= 0 || h <= 0) return

                                const nowTs = plotArea.latestTimestamp > 0 ? plotArea.latestTimestamp : (Date.now() / 1000.0)
                                const xMin = nowTs - plotArea.windowSeconds
                                const xMax = nowTs

                                const data = plotArea.seriesData[seriesKey] || ({ bfi: [], bvi: [] })
                                const bfiSeries = data.bfi || []
                                const bviSeries = data.bvi || []
                                const bfiBounds = plotArea._seriesBounds(bfiSeries)
                                const bviBounds = plotArea._seriesBounds(bviSeries)

                                // Background grid
                                ctx.strokeStyle = "#2A2A2E"
                                ctx.lineWidth = 0.5
                                for (let i = 0; i <= 4; i++) {
                                    const y = pad + (h / 4) * i
                                    ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(pad + w, y); ctx.stroke()
                                }

                                // Axes
                                ctx.strokeStyle = "#3E4E6F"
                                ctx.lineWidth = 1
                                ctx.beginPath()
                                ctx.moveTo(pad, pad); ctx.lineTo(pad, pad + h); ctx.lineTo(pad + w, pad + h)
                                ctx.stroke()

                                function drawSeries(series, color, bounds) {
                                    if (series.length < 2) return
                                    ctx.strokeStyle = color
                                    ctx.lineWidth = 2
                                    ctx.beginPath()
                                    for (let j = 0; j < series.length; j++) {
                                        const pt = series[j]
                                        const x = pad + ((pt.t - xMin) / (xMax - xMin)) * w
                                        const y = pad + h - ((pt.v - bounds.minVal) / bounds.range) * h
                                        if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
                                    }
                                    ctx.stroke()
                                }

                                drawSeries(bfiSeries, plotArea.bfiColor, bfiBounds)
                                drawSeries(bviSeries, plotArea.bviColor, bviBounds)

                                if (bfiSeries.length === 0 && bviSeries.length === 0) {
                                    ctx.fillStyle = "#7F8C8D"
                                    ctx.textAlign = "center"
                                    ctx.font = "12px sans-serif"
                                    ctx.fillText("Waiting for data...", pad + w / 2, pad + h / 2)
                                }
                            }
                        }
                    }
                }
            }
        }

        // Empty state when no series
        Text {
            visible: seriesOrder.length === 0
            text: running ? "Waiting for camera data..." : "Press Start to begin scanning"
            color: "#7F8C8D"
            font.pixelSize: 18
            horizontalAlignment: Text.AlignHCenter
            Layout.alignment: Qt.AlignHCenter
            Layout.fillWidth: true
        }
    }

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
