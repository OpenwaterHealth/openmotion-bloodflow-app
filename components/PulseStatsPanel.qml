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

    readonly property var _lf: (leftSnap && leftSnap.features) ? leftSnap.features : null
    readonly property var _rf: (rightSnap && rightSnap.features) ? rightSnap.features : null

    function _fmt(v, d) { return (v !== undefined && isFinite(v)) ? v.toFixed(d) : "—" }
    function _get(f, k) { return (f && f[k] !== undefined) ? f[k] : NaN }

    function _delta(k, d) {
        var l = _get(_lf, k), r = _get(_rf, k)
        if (!isFinite(l) || !isFinite(r)) return "—"
        var dv = l - r
        return (dv >= 0 ? "+" : "") + dv.toFixed(d)
    }

    // [label, key, digits]
    readonly property var _metrics: [
        ["Heart rate",         "hr_bpm",       0, "bpm"],
        ["Pulse amplitude",    "amp",          2, "BFI"],
        ["Pulsatility index",  "pi",           2, ""],
        ["Resistivity index",  "ri",           2, ""],
        ["Area under curve",   "auc",          2, ""],
        ["Rise time",          "rise_time_ms", 0, "ms"],
        ["Augmentation index", "aix",          2, ""],
        ["Beats analysed",     "beat_count",   0, ""],
        ["Template consistency","consistency", 2, ""]
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
                    text: root._fmt(root._get(root._lf, modelData[1]), modelData[2])
                    color: AppTheme.textPrimary; font.pixelSize: 14; font.family: "Roboto Mono"
                    horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 90
                }
                Text {
                    text: root._fmt(root._get(root._rf, modelData[1]), modelData[2])
                    color: AppTheme.textPrimary; font.pixelSize: 14; font.family: "Roboto Mono"
                    horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 90
                }
                Text {
                    text: root._delta(modelData[1], modelData[2])
                    color: AppTheme.textSecondary; font.pixelSize: 14; font.family: "Roboto Mono"
                    horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 80
                }
            }
        }

        Item { Layout.fillHeight: true }
        Rectangle { Layout.fillWidth: true; height: 1; color: AppTheme.borderSubtle; opacity: 0.5 }

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
                color: {
                    if (!isFinite(root._shapeMatch)) return AppTheme.bgElevated
                    if (root._shapeMatch >= 0.95) return Qt.rgba(0.18, 0.80, 0.44, 0.22)
                    if (root._shapeMatch >= 0.85) return Qt.rgba(0.95, 0.77, 0.06, 0.22)
                    return Qt.rgba(0.91, 0.30, 0.24, 0.22)
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
