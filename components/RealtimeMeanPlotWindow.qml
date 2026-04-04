import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import QtQuick.Window 6.0
import OpenMotion 1.0

Window {
    id: meanWindow
    width: 900
    height: 520
    visible: false
    title: "Realtime BFI/BVI"
    color: "#1E1E20"

    property int windowSeconds: 15
    property bool running: false
    property var seriesData: ({})
    property var seriesOrder: []
    property real latestTimestamp: 0
    property color bfiColor: "#E74C3C"
    property color bviColor: "#3498DB"
    property bool invertPlotAxes: MOTIONInterface.appConfig.invertPlotAxes !== undefined ? !!MOTIONInterface.appConfig.invertPlotAxes : true
    property int plotColumns: 2
    property int leftActiveCount: 0
    property int rightActiveCount: 0
    property int plotRows: (leftActiveCount === 8 || rightActiveCount === 8) ? 4 : 2

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
        if (seriesData[key] !== undefined)
            return
        seriesData[key] = ({ bfi: [], bvi: [], latestBfi: NaN, latestBvi: NaN })
        if (addToOrder) {
            // Reassign array so Repeater models update
            seriesOrder = seriesOrder.concat([key])
        }
    }

    function _activeCamsFromMask(mask) {
        const cams = []
        for (let bit = 0; bit < 8; bit++) {
            if (mask & (1 << bit)) {
                cams.push(bit)
            }
        }
        return cams
    }

    function _buildSeriesOrder(leftMask, rightMask) {
        const leftCams = _activeCamsFromMask(leftMask)
        const rightCams = _activeCamsFromMask(rightMask)
        leftActiveCount = leftCams.length
        rightActiveCount = rightCams.length
        // 2 columns per active side (each row shows a near/far pair per side)
        plotColumns = (leftCams.length > 0 ? 2 : 0) + (rightCams.length > 0 ? 2 : 0)

        const rows = (leftCams.length === 8 || rightCams.length === 8) ? 4 : 2
        const lastIdx = (rows * 2) - 1
        const order = []

        for (let row = 0; row < rows; row++) {
            if (leftCams.length > 0) {
                const a = leftCams[row]
                const b = leftCams[lastIdx - row]
                if (a !== undefined) order.push(_seriesKey("left", a))
                if (b !== undefined) order.push(_seriesKey("left", b))
            }
            if (rightCams.length > 0) {
                const a = rightCams[row]
                const b = rightCams[lastIdx - row]
                if (a !== undefined) order.push(_seriesKey("right", a))
                if (b !== undefined) order.push(_seriesKey("right", b))
            }
        }
        return order
    }

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
        for (let i = 0; i < order.length; i++) {
            _ensureSeries(order[i], false)
        }
        visible = true
        raise()
        requestActivate()
    }

    function stopScan() {
        running = false
    }

    function handleBfiSample(side, camId, timestampSec, bfiVal) {
        if (!running)
            return
        const key = _seriesKey(side, camId)
        _ensureSeries(key, false)
        seriesData[key].bfi.push({ t: timestampSec, v: bfiVal })
        seriesData[key].latestBfi = bfiVal
        // Reassign to trigger bindings for latest values
        seriesData = Object.assign({}, seriesData)
        if (timestampSec > latestTimestamp) {
            latestTimestamp = timestampSec
        }
    }

    function handleBviSample(side, camId, timestampSec, bviVal) {
        if (!running)
            return
        const key = _seriesKey(side, camId)
        _ensureSeries(key, false)
        seriesData[key].bvi.push({ t: timestampSec, v: bviVal })
        seriesData[key].latestBvi = bviVal
        // Reassign to trigger bindings for latest values
        seriesData = Object.assign({}, seriesData)
        if (timestampSec > latestTimestamp) {
            latestTimestamp = timestampSec
        }
    }

    function _pruneAndRepaint() {
        if (!visible)
            return
        const nowTs = latestTimestamp > 0 ? latestTimestamp : (Date.now() / 1000.0)
        const cutoff = nowTs - windowSeconds
        for (let i = 0; i < seriesOrder.length; i++) {
            const key = seriesOrder[i]
            const series = seriesData[key] || ({ bfi: [], bvi: [] })
            while (series.bfi.length > 0 && series.bfi[0].t < cutoff) {
                series.bfi.shift()
            }
            while (series.bvi.length > 0 && series.bvi[0].t < cutoff) {
                series.bvi.shift()
            }
        }
        for (let i = 0; i < plotRepeater.count; i++) {
            const item = plotRepeater.itemAt(i)
            if (item && item.plotCanvas) {
                item.plotCanvas.requestPaint()
            }
        }
    }

    function _formatValue(val) {
        return isFinite(val) ? val.toFixed(2) : "--"
    }

    function _seriesBounds(series) {
        let maxVal = -Infinity
        let minVal = Infinity
        for (let j = 0; j < series.length; j++) {
            const v = series[j].v
            if (v > maxVal) maxVal = v
            if (v < minVal) minVal = v
        }
        if (!isFinite(minVal) || !isFinite(maxVal)) {
            minVal = 0
            maxVal = 1
        } else if (minVal === maxVal) {
            minVal = minVal - 1
            maxVal = maxVal + 1
        }
        return { minVal, maxVal, range: maxVal - minVal }
    }

    onClosing: {
        running = false
    }

    Timer {
        interval: 100
        repeat: true
        running: meanWindow.visible
        onTriggered: meanWindow._pruneAndRepaint()
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Text {
                text: "Realtime BFI/BVI (Last " + windowSeconds + "s)"
                color: "#FFFFFF"
                font.pixelSize: 16
            }

            Item { Layout.fillWidth: true }
        }

        GridLayout {
            id: plotGrid
            Layout.fillWidth: true
            Layout.fillHeight: true
            columns: plotColumns
            rowSpacing: 10
            columnSpacing: 10

            Repeater {
                id: plotRepeater
                model: seriesOrder
                delegate: Rectangle {
                    property string seriesKey: modelData
                    property alias plotCanvas: plotCanvas
                    color: "#1E1E20"
                    border.color: "#3E4E6F"
                    radius: 6
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.preferredHeight: (plotGrid.height - (meanWindow.plotRows - 1) * plotGrid.rowSpacing) / meanWindow.plotRows

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 6

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Text {
                                text: meanWindow._labelFor(seriesKey)
                                color: "#FFFFFF"
                                font.pixelSize: 12
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: "BFI: " + meanWindow._formatValue((meanWindow.seriesData[seriesKey] || {}).latestBfi)
                                color: meanWindow.bfiColor
                                font.pixelSize: 12
                            }
                            Text {
                                text: "BVI: " + meanWindow._formatValue((meanWindow.seriesData[seriesKey] || {}).latestBvi)
                                color: meanWindow.bviColor
                                font.pixelSize: 12
                            }
                        }

                        Canvas {
                            id: plotCanvas
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            onPaint: {
                                const ctx = getContext("2d")
                                ctx.clearRect(0, 0, width, height)

                                const pad = 28
                                const w = width - 2 * pad
                                const h = height - 2 * pad
                                if (w <= 0 || h <= 0)
                                    return

                                const nowTs = meanWindow.latestTimestamp > 0 ? meanWindow.latestTimestamp : (Date.now() / 1000.0)
                                const xMin = nowTs - meanWindow.windowSeconds
                                const xMax = nowTs

                                const data = meanWindow.seriesData[seriesKey] || ({ bfi: [], bvi: [] })
                                const bfiSeries = data.bfi || []
                                const bviSeries = data.bvi || []

                                const bfiBounds = meanWindow._seriesBounds(bfiSeries)
                                const bviBounds = meanWindow._seriesBounds(bviSeries)

                                // Axes
                                ctx.strokeStyle = "#BDC3C7"
                                ctx.lineWidth = 1
                                ctx.beginPath()
                                ctx.moveTo(pad, pad)
                                ctx.lineTo(pad, pad + h)
                                ctx.lineTo(pad + w, pad + h)
                                ctx.stroke()

                                // X-axis labels
                                ctx.fillStyle = "#BDC3C7"
                                ctx.font = "10px sans-serif"
                                ctx.textAlign = "left"
                                ctx.fillText("-" + meanWindow.windowSeconds + "s", pad, pad + h + 16)
                                ctx.textAlign = "right"
                                ctx.fillText("now", pad + w, pad + h + 16)

                                // Y-axis label
                                ctx.save()
                                ctx.translate(12, pad + h / 2)
                                ctx.rotate(-Math.PI / 2)
                                ctx.textAlign = "center"
                                ctx.fillText("BFI/BVI", 0, 0)
                                ctx.restore()

                                function drawSeries(series, color, bounds) {
                                    if (series.length < 2)
                                        return
                                    const invert = meanWindow.invertPlotAxes
                                    ctx.strokeStyle = color
                                    ctx.lineWidth = 2
                                    ctx.beginPath()
                                    for (let j = 0; j < series.length; j++) {
                                        const pt = series[j]
                                        const x = pad + ((pt.t - xMin) / (xMax - xMin)) * w
                                        const frac = (pt.v - bounds.minVal) / bounds.range
                                        const y = invert
                                            ? pad + frac * h
                                            : pad + h - frac * h
                                        if (j === 0)
                                            ctx.moveTo(x, y)
                                        else
                                            ctx.lineTo(x, y)
                                    }
                                    ctx.stroke()
                                }

                                drawSeries(bfiSeries, meanWindow.bfiColor, bfiBounds)
                                drawSeries(bviSeries, meanWindow.bviColor, bviBounds)

                                if (bfiSeries.length === 0 && bviSeries.length === 0) {
                                    ctx.fillStyle = "#7F8C8D"
                                    ctx.textAlign = "center"
                                    ctx.fillText("Waiting for data...", pad + w / 2, pad + h / 2)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Connections {
        target: MOTIONInterface
        function onScanMeanSampled(side, camId, timestampSec, meanVal) {
            // Mean samples are used by backend correction worker.
        }
        function onScanBfiCorrectedSampled(side, camId, timestampSec, bfiVal) {
            meanWindow.handleBfiSample(side, camId, timestampSec, bfiVal)
        }
        function onScanBviCorrectedSampled(side, camId, timestampSec, bviVal) {
            meanWindow.handleBviSample(side, camId, timestampSec, bviVal)
        }
    }
}
