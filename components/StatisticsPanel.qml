import QtQuick 6.0
import QtQuick.Controls 6.0
import OpenMotion 1.0

// Statistics pane (#635) — the column right of the plot grid while the ⋯
// menu's Statistics switch is on (PlotViewer.statsActive). One row per
// plot the current view draws, grouped by module. For each metric of the
// plotted pair: the latest value ("Live", the number the cell's value
// label shows when the pane is off), its average over the last 5 s, and
// its peak-to-peak (the 5 s rolling max − min, averaged over the last
// 5 s; see data_sources.window_stats). Below them the LEFT − RIGHT
// differential for every plot whose opposite-side plot is drawn too.
// Display-only; the numbers come from ScanDataSource.statistics_at.
Rectangle {
    id: panel

    // ── Inputs (bound by PlotViewer) ───────────────────────────────────
    property var host: null            // PlotViewer: labels and display clamps
    property var source: null          // ScanDataSource or null
    property var cells: []             // PlotViewer._activeCellModel
    property string primaryMetric: "bfi"
    property string secondaryMetric: "bvi"
    property color primaryColor: AppTheme.textPrimary
    property color secondaryColor: AppTheme.textPrimary
    // Readout time: the viewer's live-edge snapshot, the instant the cell
    // value labels read. poll() reads it rather than a binding, so the
    // pane refreshes at its own rate instead of on every paint.
    property real readoutT: 0.0

    // Upper bound on the pane's width (the viewer passes a share of its
    // own): the font shrinks, within its bounds, before the pane crowds
    // the plots.
    property real maxWidth: Infinity

    readonly property real windowSec: 5.0
    // ~2.6 ms per poll with 16 cameras; numbers repainting at the 30 Hz
    // plot rate would not be readable anyway.
    readonly property int refreshMs: 100
    // Per metric, left to right; rowValues returns them in this order.
    readonly property var _columns: [
        { field: "",     caption: "Live" },
        { field: "_avg", caption: panel.windowSec + " s avg" },
        { field: "_p2p", caption: "Peak-to-peak" }
    ]

    // ── Sizing ─────────────────────────────────────────────────────────
    // The numbers are as large as lets every line (the two header lines,
    // the section titles and the rows) fit the pane's height without
    // scrolling and the pane stay within maxWidth, between _minFontPx and
    // _maxFontPx: 16 cameras on a short window get the small end (and
    // scroll if even that does not fit), the Average view's few rows the
    // large end. Line pitch and column widths follow the font, so the
    // pane widens with it.
    readonly property int _minFontPx: 14
    readonly property int _maxFontPx: 22
    readonly property real _pad: 12
    readonly property real _sectionGap: 10
    readonly property int _lineCount: {
        var n = 2
        for (var i = 0; i < panel.model.length; i++)
            n += 1 + panel.model[i].rows.length
        return n
    }
    readonly property int _fontPx: {
        // Everything but the lines: padding, divider + its margins, and
        // the gaps between sections.
        var fixed = 2 * panel._pad + 9
            + panel._sectionGap * Math.max(0, panel.model.length - 1)
        var pitch = (panel.height - fixed) / Math.max(1, panel._lineCount)
        var byWidth = (panel.maxWidth - 2 * panel._pad) / panel._widthPerPx
        return Math.max(panel._minFontPx, Math.min(
            panel._maxFontPx, Math.floor(pitch / 1.5), Math.floor(byWidth)))
    }
    // Fixed per line, also so a label whose "−" falls back to another
    // font's taller line does not stretch its row.
    readonly property real _rowHeight: Math.floor(panel._fontPx * 1.5)
    readonly property int _labelFontPx: Math.round(panel._fontPx * 0.85)
    readonly property int _captionFontPx: Math.round(panel._fontPx * 0.72)
    readonly property real _groupGap: Math.round(panel._fontPx * 0.9)
    // Inset of the text from its stripe's edges.
    readonly property real _cellPad: Math.round(panel._fontPx * 0.35)
    // Widths per px of font size. Measured, not assumed (the app's "Roboto
    // Mono" is not bundled, so the text falls back to the platform font),
    // once at 100 px: text widths scale with the pixel size, and a fixed
    // reference keeps _fontPx's width bound free of a loop through itself.
    // A column fits its widest number or its caption (at 0.72 of the
    // font), plus a gap and the stripe inset.
    readonly property real _columnPerPx:
        Math.max(valueMetrics.advanceWidth, 0.72 * captionMetrics.advanceWidth) / 100
        + 0.5 + 0.35
    readonly property real _labelPerPx:
        0.85 * labelMetrics.advanceWidth / 100 + 0.6 + 0.35
    readonly property real _widthPerPx: _labelPerPx + 6 * _columnPerPx + 0.9
    readonly property real _valueWidth: Math.ceil(panel._columnPerPx * panel._fontPx)
    readonly property real _labelWidth: Math.ceil(panel._labelPerPx * panel._fontPx)
    implicitWidth: 2 * _pad + _labelWidth + 6 * _valueWidth + _groupGap

    // Lining, equal-width digits so the columns line up and read as a
    // table: "Roboto Mono" is not bundled, and some platform fallbacks
    // default to old-style figures that dip below the baseline.
    readonly property var _figureFeatures: ({ "lnum": 1, "tnum": 1 })

    // The widest number a column holds (a signed three-digit mean
    // differential; BFI/BVI stay within ±10.00).
    TextMetrics {
        id: valueMetrics
        font.family: "Roboto Mono"
        font.pixelSize: 100
        font.weight: Font.Medium
        font.features: panel._figureFeatures
        text: "+000.00"
    }
    TextMetrics {
        id: captionMetrics
        font.family: "Roboto Mono"
        font.pixelSize: 100
        text: {
            var longest = ""
            for (var i = 0; i < panel._columns.length; i++)
                if (panel._columns[i].caption.length > longest.length)
                    longest = panel._columns[i].caption
            return longest
        }
    }
    TextMetrics {
        id: labelMetrics
        font.family: "Roboto Mono"
        font.pixelSize: 100
        text: {
            var longest = ""
            for (var i = 0; i < panel.model.length; i++) {
                var rows = panel.model[i].rows
                for (var j = 0; j < rows.length; j++)
                    if (rows[j].label.length > longest.length) longest = rows[j].label
            }
            return longest
        }
    }

    color: AppTheme.plotCellBg
    border.color: AppTheme.borderSubtle
    border.width: 1
    radius: 4

    // The two modules sit rotated 180° from each other on the body, so
    // camera N faces camera 9 − N on the other side (zero-based c ↔ 7 − c).
    // Derived streams face their own id: the side average (−1) faces the
    // other side's average, and each Aggregate pair (1+8, 2+7, …) is
    // symmetric under the flip, so L1+8 faces R1+8.
    function oppositeCamId(camId) {
        return (camId >= 0 && camId < 8) ? 7 - camId : camId
    }

    // ── Data ───────────────────────────────────────────────────────────
    // "side:camId" → { <metric>: live, <metric>_avg, <metric>_p2p }.
    property var _snapshot: ({})

    function poll() {
        if (!panel.visible) return
        var cells = panel.cells || []
        var keys = []
        for (var i = 0; i < cells.length; i++)
            keys.push(cells[i].side + ":" + cells[i].camId)
        panel._snapshot = (panel.source && keys.length > 0)
            ? panel.source.statistics_at(
                  keys, [panel.primaryMetric, panel.secondaryMetric],
                  panel.readoutT, panel.windowSec)
            : ({})
    }

    Timer {
        interval: panel.refreshMs
        repeat: true
        running: panel.visible
        triggeredOnStart: true
        onTriggered: panel.poll()
    }
    onSourceChanged: panel.poll()
    onCellsChanged: panel.poll()
    onPrimaryMetricChanged: panel.poll()
    onSecondaryMetricChanged: panel.poll()

    function _clamp(metric, v) {
        if (v === undefined || v === null) return NaN
        return panel.host ? panel.host.clampForDisplay(metric, v) : v
    }

    // "L3", "L1+8" (Aggregate) or "L" (side average).
    function _shortLabel(side, camId) {
        if (camId === -1 || !panel.host) return side.charAt(0).toUpperCase()
        return panel.host._cellLabel(side, camId, true)
    }

    // Sections for the Repeater below: { title, diff, rows: [{ label, key,
    // oppKey }] }, key being the cell's "side:camId" and oppKey, on a
    // differential row, its opposite plot's. Numbers are not part of it:
    // it changes only with the plotted cells, so a refresh updates the
    // rows' Texts in place (rowValues) instead of recreating every row.
    readonly property var model: {
        var bySide = { left: [], right: [] }
        var cells = (panel.cells || []).slice().sort(
            function(a, b) { return a.camId - b.camId })
        for (var i = 0; i < cells.length; i++) {
            var c = cells[i]
            if (!bySide[c.side]) continue
            bySide[c.side].push({
                camId: c.camId,
                key: c.side + ":" + c.camId,
                label: c.camId === -1 ? "AVG" : panel._shortLabel(c.side, c.camId),
                shortLabel: panel._shortLabel(c.side, c.camId)
            })
        }
        var diffs = []
        for (var j = 0; j < bySide.left.length; j++) {
            var l = bySide.left[j]
            var opp = panel.oppositeCamId(l.camId)
            for (var k = 0; k < bySide.right.length; k++) {
                var rr = bySide.right[k]
                if (rr.camId !== opp) continue
                diffs.push({ label: l.shortLabel + " − " + rr.shortLabel,
                             key: l.key, oppKey: rr.key })
            }
        }
        var sections = []
        if (bySide.left.length > 0)
            sections.push({ title: "LEFT", diff: false, rows: bySide.left })
        if (bySide.right.length > 0)
            sections.push({ title: "RIGHT", diff: false, rows: bySide.right })
        if (diffs.length > 0)
            sections.push({ title: "LEFT − RIGHT", diff: true, rows: diffs })
        return sections
    }

    // One row's numbers from the latest poll, _columns for the primary
    // metric then for the secondary. Live and average are clamped for
    // display like every other readout of the metric; peak-to-peak is a
    // span, not a reading, so it is not. A differential row subtracts its
    // opposite plot's displayed numbers, so it is the difference of the
    // two rows above it.
    function rowValues(row) {
        var out = []
        var metrics = [panel.primaryMetric, panel.secondaryMetric]
        for (var m = 0; m < metrics.length; m++) {
            for (var c = 0; c < panel._columns.length; c++) {
                var field = metrics[m] + panel._columns[c].field
                var v = panel._shown(row.key, metrics[m], field)
                if (row.oppKey) v -= panel._shown(row.oppKey, metrics[m], field)
                out.push(v)
            }
        }
        return out
    }

    function _shown(key, metric, field) {
        var r = panel._snapshot[key]
        var v = r ? r[field] : NaN
        return field === metric + "_p2p"
            ? (v === undefined || v === null ? NaN : v)
            : panel._clamp(metric, v)
    }

    // Two decimals like the cell labels; a differential carries its sign
    // and never shows "-0.00".
    function _fmt(v, signed) {
        if (!isFinite(v)) return "--"
        var s = v.toFixed(2)
        if (!signed) return s
        if (s === "-0.00" || s === "0.00") return "0.00"
        return v > 0 ? "+" + s : s
    }

    // ── View ───────────────────────────────────────────────────────────
    Column {
        id: header
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: panel._pad

        // Metric names, each centered over its group of columns.
        Row {
            Item { width: panel._labelWidth; height: panel._rowHeight }
            Text {
                width: panel._columns.length * panel._valueWidth
                height: panel._rowHeight
                leftPadding: panel._cellPad
                rightPadding: panel._cellPad
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                text: panel.primaryMetric.toUpperCase()
                color: panel.primaryColor
                font.pixelSize: panel._fontPx
                font.weight: Font.DemiBold
                font.family: "Roboto Mono"
            }
            Item { width: panel._groupGap; height: panel._rowHeight }
            Text {
                width: panel._columns.length * panel._valueWidth
                height: panel._rowHeight
                leftPadding: panel._cellPad
                rightPadding: panel._cellPad
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                text: panel.secondaryMetric.toUpperCase()
                color: panel.secondaryColor
                font.pixelSize: panel._fontPx
                font.weight: Font.DemiBold
                font.family: "Roboto Mono"
            }
        }
        Row {
            Item { width: panel._labelWidth; height: panel._rowHeight }
            Repeater {
                model: panel._columns.concat(panel._columns)
                delegate: Text {
                    width: panel._valueWidth
                           + (index === panel._columns.length ? panel._groupGap : 0)
                    height: panel._rowHeight
                    rightPadding: panel._cellPad
                    horizontalAlignment: Text.AlignRight
                    verticalAlignment: Text.AlignVCenter
                    text: modelData.caption
                    color: AppTheme.textTertiary
                    font.pixelSize: panel._captionFontPx
                    font.family: "Roboto Mono"
                }
            }
        }
        Item { width: 1; height: 4 }
        Rectangle {
            width: parent.width
            height: 1
            color: AppTheme.borderSoft
        }
    }

    Flickable {
        id: body
        anchors.top: header.bottom
        anchors.topMargin: 4
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: panel._pad
        anchors.rightMargin: panel._pad
        anchors.bottomMargin: panel._pad
        clip: true
        contentWidth: width
        contentHeight: sectionsColumn.implicitHeight
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
            id: sectionsColumn
            width: body.width
            spacing: panel._sectionGap

            Repeater {
                model: panel.model
                delegate: Column {
                    id: section
                    readonly property var sectionData: modelData
                    width: sectionsColumn.width

                    Text {
                        height: panel._rowHeight
                        verticalAlignment: Text.AlignVCenter
                        text: section.sectionData.title
                        color: AppTheme.textSecondary
                        font.pixelSize: panel._labelFontPx
                        font.weight: Font.DemiBold
                        font.family: "Roboto Mono"
                    }
                    Repeater {
                        model: section.sectionData.rows
                        delegate: Item {
                            id: statRow
                            readonly property var rowData: modelData
                            // Re-read on every poll (_snapshot) in place.
                            readonly property var values: panel.rowValues(rowData)
                            width: section.width
                            height: panel._rowHeight

                            // Zebra stripe: keeps the eye on one row across
                            // the four columns.
                            Rectangle {
                                anchors.fill: parent
                                radius: 3
                                color: index % 2 === 0
                                    ? Qt.alpha(AppTheme.textPrimary, 0.06)
                                    : "transparent"
                            }
                            Row {
                                anchors.fill: parent
                                Text {
                                    width: panel._labelWidth
                                    height: panel._rowHeight
                                    leftPadding: panel._cellPad
                                    verticalAlignment: Text.AlignVCenter
                                    text: statRow.rowData.label
                                    color: AppTheme.textSecondary
                                    font.pixelSize: panel._labelFontPx
                                    font.family: "Roboto Mono"
                                }
                                Repeater {
                                    model: 2 * panel._columns.length
                                    delegate: Text {
                                        objectName: "statValue"
                                        width: panel._valueWidth
                                               + (index === panel._columns.length ? panel._groupGap : 0)
                                        height: panel._rowHeight
                                        rightPadding: panel._cellPad
                                        verticalAlignment: Text.AlignVCenter
                                        horizontalAlignment: Text.AlignRight
                                        text: panel._fmt(statRow.values[index],
                                                         section.sectionData.diff)
                                        color: AppTheme.textPrimary
                                        font.pixelSize: panel._fontPx
                                        font.weight: Font.Medium
                                        font.family: "Roboto Mono"
                                        font.features: panel._figureFeatures
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
