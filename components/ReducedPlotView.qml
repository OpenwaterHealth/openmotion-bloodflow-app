import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

Rectangle {
    id: root
    color: "#1E1E20"
    radius: 12
    border.color: "#2A2A2E"
    border.width: 1

    property int  windowSeconds: 15
    property bool running: false
    property real latestTimestamp: 0
    property color bfiColor: "#E74C3C"
    property color bviColor: "#3498DB"
    property bool  bviLowPassEnabled: false
    property real  bviLowPassCutoffHz: 40.0
    readonly property real _bviAlpha: {
        var fs = 40.0
        var dt = 1.0 / fs
        var rc = 1.0 / (2.0 * Math.PI * Math.max(0.01, bviLowPassCutoffHz))
        return dt / (rc + dt)
    }

    // Per-side aggregated data. Pending entries are keyed by frameId; once enough
    // cameras have reported (or a newer frame arrives) the entry is averaged and
    // pushed onto the bfi/bvi series.
    property var leftData:  ({ bfi: [], bvi: [], pendingBfi: ({}), pendingBvi: ({}),
                                latestBfi: NaN, latestBvi: NaN, bviLpState: NaN })
    property var rightData: ({ bfi: [], bvi: [], pendingBfi: ({}), pendingBvi: ({}),
                                latestBfi: NaN, latestBvi: NaN, bviLpState: NaN })

    // Auto-scaled bounds (recomputed once per second)
    property var leftBfiBounds:  ({ minVal: 0.0, maxVal: 10.0, range: 10.0 })
    property var leftBviBounds:  ({ minVal: 0.0, maxVal: 10.0, range: 10.0 })
    property var rightBfiBounds: ({ minVal: 0.0, maxVal: 10.0, range: 10.0 })
    property var rightBviBounds: ({ minVal: 0.0, maxVal: 10.0, range: 10.0 })

    function reset() {
        leftData  = { bfi: [], bvi: [], pendingBfi: ({}), pendingBvi: ({}),
                      latestBfi: NaN, latestBvi: NaN, bviLpState: NaN }
        rightData = { bfi: [], bvi: [], pendingBfi: ({}), pendingBvi: ({}),
                      latestBfi: NaN, latestBvi: NaN, bviLpState: NaN }
        latestTimestamp = 0
    }

    function startScan() { reset(); running = true }
    function stopScan()  { running = false }

    function _flushEntry(data, field, fid) {
        var pending = (field === "bfi") ? data.pendingBfi : data.pendingBvi
        var e = pending[fid]
        if (!e || e.count === 0) return
        var avg = e.sum / e.count
        if (field === "bvi" && root.bviLowPassEnabled) {
            var prev = data.bviLpState
            if (!isFinite(prev)) prev = avg
            avg = prev + root._bviAlpha * (avg - prev)
            data.bviLpState = avg
        }
        data[field].push({ t: e.ts, v: avg })
        if (field === "bfi") data.latestBfi = avg
        else                 data.latestBvi = avg
        delete pending[fid]
    }

    function _ingest(data, field, frameId, ts, val) {
        if (!isFinite(val)) return
        var pending = (field === "bfi") ? data.pendingBfi : data.pendingBvi

        // Flush stale pending buckets (frameIds more than 2 behind current)
        for (var k in pending) {
            if (parseInt(k, 10) < frameId - 2) _flushEntry(data, field, k)
        }

        var key = String(frameId)
        var entry = pending[key]
        if (!entry) { entry = { sum: 0, count: 0, ts: ts }; pending[key] = entry }
        entry.sum   += val
        entry.count += 1

        if (entry.count >= 4) _flushEntry(data, field, key)

        if (ts > latestTimestamp) latestTimestamp = ts
    }

    function handleBfi(side, camId, frameId, ts, val) {
        if (!running) return
        _ingest(side === "left" ? leftData : rightData, "bfi", frameId, ts, val)
    }
    function handleBvi(side, camId, frameId, ts, val) {
        if (!running) return
        _ingest(side === "left" ? leftData : rightData, "bvi", frameId, ts, val)
    }

    function _prune(data, cutoff) {
        var i = 0
        while (i < data.bfi.length && data.bfi[i].t < cutoff) i++
        if (i > 0) data.bfi.splice(0, i)
        i = 0
        while (i < data.bvi.length && data.bvi[i].t < cutoff) i++
        if (i > 0) data.bvi.splice(0, i)
    }

    function _computeBounds(arr) {
        if (arr.length < 4) return null
        var vals = []
        for (var j = 0; j < arr.length; j++) {
            var v = arr[j].v
            if (isFinite(v)) vals.push(v)
        }
        if (vals.length < 4) return null
        vals.sort(function(a, b) { return a - b })
        var lo = vals[Math.floor(vals.length * 0.02)]
        var hi = vals[Math.floor(vals.length * 0.98)]
        if (lo === hi) { lo -= 0.5; hi += 0.5 }
        var pad = (hi - lo) * 0.25
        lo -= pad; hi += pad
        return { minVal: lo, maxVal: hi, range: (hi - lo) || 1.0 }
    }

    Timer {
        interval: 100
        repeat: true
        running: root.visible && root.running
        property int tickCount: 0
        onTriggered: {
            var nowTs  = root.latestTimestamp > 0 ? root.latestTimestamp : (Date.now() * 0.001)
            var cutoff = nowTs - root.windowSeconds
            root._prune(root.leftData,  cutoff)
            root._prune(root.rightData, cutoff)

            tickCount++
            if (tickCount >= 10) {
                tickCount = 0
                var b
                b = root._computeBounds(root.leftData.bfi);  if (b) root.leftBfiBounds  = b
                b = root._computeBounds(root.leftData.bvi);  if (b) root.leftBviBounds  = b
                b = root._computeBounds(root.rightData.bfi); if (b) root.rightBfiBounds = b
                b = root._computeBounds(root.rightData.bvi); if (b) root.rightBviBounds = b
            }
            // Force a property change so the readout texts refresh
            leftData  = leftData
            rightData = rightData
            leftCanvas.requestPaint()
            rightCanvas.requestPaint()
        }
    }

    Connections {
        target: MOTIONInterface
        function onScanBfiSampled(side, camId, frameId, timestampSec, bfiVal) {
            root.handleBfi(side, camId, frameId, timestampSec, bfiVal)
        }
        function onScanBviSampled(side, camId, frameId, timestampSec, bviVal) {
            root.handleBvi(side, camId, frameId, timestampSec, bviVal)
        }
        function onScanCorrectedBatch(samples) {
            // FDA mode does not in-place correct the averaged history; values are
            // close enough for the realtime display.
        }
    }

    // ── Painting helper ───────────────────────────────────────────────────────
    function _paintCanvas(ctx, w, h, data, bfiB, bviB) {
        ctx.clearRect(0, 0, w, h)
        var padL = 50, padR = 50, padT = 14, padB = 14
        var pw = w - padL - padR
        var ph = h - padT - padB
        if (pw <= 0 || ph <= 0) return

        var nowTs  = root.latestTimestamp > 0 ? root.latestTimestamp : (Date.now() * 0.001)
        var xMin   = nowTs - root.windowSeconds
        var xRange = root.windowSeconds

        // Grid
        ctx.strokeStyle = "#2A2A2E"
        ctx.lineWidth   = 0.5
        ctx.beginPath()
        for (var gi = 0; gi <= 4; gi++) {
            var gy = padT + (ph / 4) * gi
            ctx.moveTo(padL,      gy)
            ctx.lineTo(padL + pw, gy)
        }
        ctx.stroke()

        // Axes
        ctx.strokeStyle = "#3E4E6F"
        ctx.lineWidth   = 1
        ctx.beginPath()
        ctx.moveTo(padL,      padT)
        ctx.lineTo(padL,      padT + ph)
        ctx.lineTo(padL + pw, padT + ph)
        ctx.stroke()

        function drawSeries(series, color, b) {
            if (series.length < 2) return
            var invR = b.range > 0 ? 1.0 / b.range : 1.0
            ctx.strokeStyle = color
            ctx.lineWidth   = 2
            ctx.beginPath()
            var yFor = function(v) { return padT + ph - ((v - b.minVal) * invR) * ph }
            ctx.moveTo(padL + ((series[0].t - xMin) / xRange) * pw, yFor(series[0].v))
            for (var j = 1; j < series.length; j++) {
                var pt = series[j]
                ctx.lineTo(padL + ((pt.t - xMin) / xRange) * pw, yFor(pt.v))
            }
            ctx.stroke()
        }

        function drawLabels(b, color, isLeft) {
            ctx.font         = "10px sans-serif"
            ctx.textBaseline = "middle"
            ctx.fillStyle    = color
            for (var ti = 0; ti <= 2; ti++) {
                var frac = ti / 2.0
                var val  = b.minVal + frac * b.range
                var y    = padT + ph * (1.0 - frac)
                if (isLeft) {
                    ctx.textAlign = "right"
                    ctx.fillText(val.toFixed(2), padL - 4, y)
                } else {
                    ctx.textAlign = "left"
                    ctx.fillText(val.toFixed(2), padL + pw + 4, y)
                }
            }
        }

        drawSeries(data.bfi, root.bfiColor, bfiB)
        drawSeries(data.bvi, root.bviColor, bviB)
        drawLabels(bfiB, root.bfiColor, true)
        drawLabels(bviB, root.bviColor, false)

        var hasData = data.bfi.length > 0 || data.bvi.length > 0
        if (!hasData) {
            ctx.fillStyle    = "#7F8C8D"
            ctx.textAlign    = "center"
            ctx.textBaseline = "middle"
            ctx.font         = "14px sans-serif"
            ctx.fillText("Waiting for data...", padL + pw / 2, padT + ph / 2)
        }
    }

    // ── Layout ────────────────────────────────────────────────────────────────
    component SidePanel: Rectangle {
        id: sideRoot
        property string sideLabel: ""
        property var    sideData
        property var    bfiB
        property var    bviB
        property alias  plotCanvas: sideCanvas
        color:        "#141417"
        border.color: "#2A2A2E"
        radius: 8
        Layout.fillWidth:  true
        Layout.fillHeight: true

        RowLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 18

            ColumnLayout {
                Layout.preferredWidth: 280
                Layout.fillHeight: true
                spacing: 4

                Text {
                    text: sideRoot.sideLabel
                    color: "#BDC3C7"
                    font.pixelSize: 22
                    font.weight: Font.DemiBold
                }

                Item { Layout.fillHeight: true }

                Text {
                    text: "BFI"
                    color: root.bfiColor
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                }
                Text {
                    text: isFinite(sideRoot.sideData.latestBfi)
                          ? sideRoot.sideData.latestBfi.toFixed(2) : "--"
                    color: "#FFFFFF"
                    font.pixelSize: 72
                    font.weight: Font.Bold
                    font.family:  "Consolas"
                }

                Item { height: 12 }

                Text {
                    text: "BVI"
                    color: root.bviColor
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                }
                Text {
                    text: isFinite(sideRoot.sideData.latestBvi)
                          ? sideRoot.sideData.latestBvi.toFixed(2) : "--"
                    color: "#FFFFFF"
                    font.pixelSize: 72
                    font.weight: Font.Bold
                    font.family:  "Consolas"
                }

                Item { Layout.fillHeight: true }
            }

            Canvas {
                id: sideCanvas
                Layout.fillWidth:  true
                Layout.fillHeight: true
                onPaint: root._paintCanvas(getContext("2d"), width, height,
                                            sideRoot.sideData,
                                            sideRoot.bfiB,
                                            sideRoot.bviB)
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 12

        SidePanel {
            id: leftPanel
            sideLabel: "LEFT"
            sideData:  root.leftData
            bfiB:      root.leftBfiBounds
            bviB:      root.leftBviBounds
        }

        SidePanel {
            id: rightPanel
            sideLabel: "RIGHT"
            sideData:  root.rightData
            bfiB:      root.rightBfiBounds
            bviB:      root.rightBviBounds
        }
    }

    // Convenience aliases for the timer's requestPaint() calls
    property Canvas leftCanvas:  leftPanel.plotCanvas
    property Canvas rightCanvas: rightPanel.plotCanvas
}
