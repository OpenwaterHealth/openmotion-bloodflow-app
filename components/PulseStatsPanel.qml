import QtQuick 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  PulseStatsPanel — left-vs-right pulse-shape comparison.
 *
 *  Reads the two sides' PulseAnalysis QVariantMaps and lays out a compact
 *  table: one row per morphology metric, columns Left | Right | Δ (L−R),
 *  plus a template shape-similarity (Pearson r) readout that answers "how
 *  alike are the two pulse shapes".
 */
Rectangle {
    id: root

    property var leftSnap: null
    property var rightSnap: null

    color: AppTheme.bgCard
    radius: 10
    border.color: AppTheme.borderSoft
    border.width: 1

    // Size to the natural content height (+ the 14px top/bottom margins) so the
    // host lays the panel out exactly as tall as it needs — no cramped footnote.
    implicitHeight: statsCol.implicitHeight + 28

    readonly property var _lf: (leftSnap && leftSnap.features) ? leftSnap.features : null
    readonly property var _rf: (rightSnap && rightSnap.features) ? rightSnap.features : null
    readonly property bool _lReliable: _lf && _lf.reliable === true
    readonly property bool _rReliable: _rf && _rf.reliable === true

    function _get(f, k) { return (f && f[k] !== undefined) ? f[k] : NaN }
    // Format one cell, blanking gated pulse-shape metrics when that side has
    // no reliable pulse — a phantom / lead-off must not show a fake number.
    function _cell(f, reliable, k, d, gated) {
        if (gated && !reliable) return "—"
        var v = _get(f, k)
        return (v !== undefined && isFinite(v)) ? v.toFixed(d) : "—"
    }
    function _delta(k, d, gated) {
        if (gated && !(_lReliable && _rReliable)) return "—"
        var l = _get(_lf, k), r = _get(_rf, k)
        if (!isFinite(l) || !isFinite(r)) return "—"
        var dv = l - r
        return (dv >= 0 ? "+" : "") + dv.toFixed(d)
    }

    // [label, key, digits, unit, gated-on-reliability]
    readonly property var _metrics: [
        ["Heart rate",          "hr_bpm",       0, "bpm", true],
        ["Pulse amplitude",     "amp",          2, "BFI", true],
        ["Pulsatility index",   "pi",           2, "",    true],
        ["Resistivity index",   "ri",           2, "",    true],
        ["Area under curve",    "auc",          2, "",    true],
        ["Rise time",           "rise_time_ms", 0, "ms",  true],
        ["Augmentation index",  "aix",          2, "",    true],
        ["Beats analysed",      "beat_count",   0, "",    false],
        ["Signal periodicity",  "periodicity",  2, "",    false],
        ["Template consistency","consistency",  2, "",    false]
    ]

    function _pearson(a, b) {
        if (!a || !b || a.length === 0 || a.length !== b.length) return NaN
        var n = 0, sa = 0, sb = 0, i
        for (i = 0; i < a.length; i++)
            if (isFinite(a[i]) && isFinite(b[i])) { sa += a[i]; sb += b[i]; n++ }
        if (n < 2) return NaN
        var ma = sa / n, mb = sb / n, num = 0, da = 0, db = 0
        for (i = 0; i < a.length; i++) {
            if (isFinite(a[i]) && isFinite(b[i])) {
                var x = a[i] - ma, y = b[i] - mb
                num += x * y; da += x * x; db += y * y
            }
        }
        var den = Math.sqrt(da * db)
        return den > 0 ? num / den : NaN
    }

    readonly property real _shapeMatch:
        (leftSnap && rightSnap && leftSnap.beatCount > 0 && rightSnap.beatCount > 0)
        ? _pearson(leftSnap.template, rightSnap.template) : NaN

    ColumnLayout {
        id: statsCol
        anchors.fill: parent
        anchors.margins: 14
        spacing: 4

        // Header
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Text { text: "Pulse-shape comparison"; color: AppTheme.textSecondary
                   font.pixelSize: 14; font.bold: true; Layout.preferredWidth: 200 }
            Item { Layout.fillWidth: true }
            Text { text: "Left";  color: AppTheme.accentGreen; font.pixelSize: 13; font.bold: true
                   horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 90 }
            Text { text: "Right"; color: AppTheme.accentBlue; font.pixelSize: 13; font.bold: true
                   horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 90 }
            Text { text: "Δ (L−R)"; color: AppTheme.textTertiary; font.pixelSize: 13; font.bold: true
                   horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 80 }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: AppTheme.borderSubtle; opacity: 0.5 }

        // Pulse-detected indicator (drives whether the shape metrics below are
        // shown or blanked). A component so left/right render identically.
        component DetectBadge: Text {
            property bool ok: false
            property bool has: false
            horizontalAlignment: Text.AlignRight
            Layout.preferredWidth: 90
            font.pixelSize: 14; font.bold: true
            text: !has ? "—" : (ok ? "Yes" : "No")
            color: !has ? AppTheme.textTertiary
                        : (ok ? AppTheme.accentGreen : AppTheme.accentYellow)
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Text { text: "Pulse detected"; color: AppTheme.textSecondary
                   font.pixelSize: 13; font.bold: true; Layout.preferredWidth: 200 }
            Item { Layout.fillWidth: true }
            DetectBadge { has: root._lf !== null; ok: root._lReliable }
            DetectBadge { has: root._rf !== null; ok: root._rReliable }
            Item { Layout.preferredWidth: 80 }
        }

        Repeater {
            model: root._metrics
            delegate: RowLayout {
                required property var modelData
                Layout.fillWidth: true
                spacing: 8
                Text {
                    text: modelData[0] + (modelData[3] ? "  (" + modelData[3] + ")" : "")
                    color: AppTheme.textTertiary; font.pixelSize: 13
                    Layout.preferredWidth: 200; elide: Text.ElideRight
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: root._cell(root._lf, root._lReliable, modelData[1], modelData[2], modelData[4])
                    color: AppTheme.textPrimary; font.pixelSize: 14; font.family: "Roboto Mono"
                    horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 90
                }
                Text {
                    text: root._cell(root._rf, root._rReliable, modelData[1], modelData[2], modelData[4])
                    color: AppTheme.textPrimary; font.pixelSize: 14; font.family: "Roboto Mono"
                    horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 90
                }
                Text {
                    text: root._delta(modelData[1], modelData[2], modelData[4])
                    color: AppTheme.textSecondary; font.pixelSize: 14; font.family: "Roboto Mono"
                    horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 80
                }
            }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: AppTheme.borderSubtle
                    opacity: 0.5; Layout.topMargin: 6 }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Text {
                text: "Left/right shape similarity"
                color: AppTheme.textSecondary; font.pixelSize: 14; font.bold: true
                Layout.preferredWidth: 240
            }
            Item { Layout.fillWidth: true }
            Rectangle {
                Layout.preferredWidth: 130; Layout.preferredHeight: 24; radius: 12
                // Verdict tint derived from the semantic accent tokens so the
                // pill tracks the palette across dark / light / Liquid Glass.
                color: {
                    if (!isFinite(root._shapeMatch)) return AppTheme.bgElevated
                    var c = root._shapeMatch >= 0.95 ? AppTheme.accentGreen
                          : root._shapeMatch >= 0.85 ? AppTheme.accentYellow
                          :                            AppTheme.accentRed
                    return Qt.rgba(c.r, c.g, c.b, 0.22)
                }
                Text {
                    anchors.centerIn: parent
                    text: isFinite(root._shapeMatch)
                          ? "r = " + root._shapeMatch.toFixed(3) : "r = —"
                    color: AppTheme.textPrimary; font.pixelSize: 14; font.family: "Roboto Mono"
                }
            }
        }
        Text {
            Layout.fillWidth: true
            text: "1.00 = identical morphology; lower values indicate left/right asymmetry in the pulse shape."
            color: AppTheme.textTertiary; font.pixelSize: 11; wrapMode: Text.WordWrap
        }
    }
}
