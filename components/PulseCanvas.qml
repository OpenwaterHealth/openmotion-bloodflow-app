import QtQuick 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

/*  PulseCanvas — one side's cardiac pulse window.
 *
 *  Renders, back to front:
 *    1. a faint grid,
 *    2. the min/max envelope over recent beats (translucent band),
 *    3. the p25-p75 band (stronger fill),
 *    4. the ensemble-average "template" pulse (bold line),
 *    5. the live in-progress beat, sweeping left → right (bright accent),
 *  on a shared x = cardiac phase (0-100%), y = BFI axis.
 *
 *  `snap` is one side's PulseAnalysis as a QVariantMap (see
 *  PulseAnalysis.to_qvariant): keys phase/template/envMin/envMax/envP25/
 *  envP75/livePhase/liveValue/features/beatCount. yMin/yMax are supplied by
 *  the parent so left and right share one honest scale.
 */
Item {
    id: root

    property var snap: null
    property string title: "LEFT"
    property color accent: AppTheme.accentGreen
    property real yMin: 0.0
    property real yMax: 10.0

    readonly property var _f: (snap && snap.features) ? snap.features : null
    readonly property bool _hasData: snap && snap.beatCount > 0
    // A genuine, regular cardiac pulse (not phantom / lead-off noise).
    readonly property bool _reliable: _f && _f.reliable === true

    onSnapChanged: canvas.requestPaint()
    onYMinChanged: canvas.requestPaint()
    onYMaxChanged: canvas.requestPaint()

    Rectangle {
        anchors.fill: parent
        radius: 10
        color: AppTheme.bgPlot
        border.color: AppTheme.borderSoft
        border.width: 1
    }

    Canvas {
        id: canvas
        anchors.fill: parent
        anchors.margins: 1
        renderStrategy: Canvas.Cooperative
        // De-emphasise the trace when the "pulse" is really just noise.
        opacity: (root._hasData && !root._reliable) ? 0.35 : 1.0

        readonly property real padL: 46
        readonly property real padR: 14
        readonly property real padT: 34
        readonly property real padB: 26

        function _x(p) { return padL + p * (width - padL - padR) }
        function _y(v) {
            var span = (root.yMax - root.yMin) || 1
            var f = (v - root.yMin) / span
            return padT + (1 - f) * (height - padT - padB)
        }

        function _band(ctx, phase, lo, hi) {
            ctx.beginPath()
            var started = false
            for (var i = 0; i < phase.length; i++) {
                if (!isFinite(hi[i])) continue
                var x = _x(phase[i]), y = _y(hi[i])
                if (!started) { ctx.moveTo(x, y); started = true }
                else ctx.lineTo(x, y)
            }
            for (var j = phase.length - 1; j >= 0; j--) {
                if (!isFinite(lo[j])) continue
                ctx.lineTo(_x(phase[j]), _y(lo[j]))
            }
            ctx.closePath()
            ctx.fill()
        }

        function _line(ctx, phase, vals, from) {
            ctx.beginPath()
            var started = false
            for (var i = 0; i < phase.length; i++) {
                if (!isFinite(vals[i]) || phase[i] < from - 1e-9) continue
                var x = _x(phase[i]), y = _y(vals[i])
                if (!started) { ctx.moveTo(x, y); started = true }
                else ctx.lineTo(x, y)
            }
            ctx.stroke()
        }

        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            ctx.clearRect(0, 0, width, height)

            // ── grid ──────────────────────────────────────────────────
            ctx.strokeStyle = AppTheme.plotGrid
            ctx.lineWidth = 1
            ctx.globalAlpha = 0.5
            for (var gy = 0; gy <= 4; gy++) {
                var y = padT + gy / 4 * (height - padT - padB)
                ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(width - padR, y); ctx.stroke()
            }
            for (var gx = 0; gx <= 4; gx++) {
                var x = padL + gx / 4 * (width - padL - padR)
                ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, height - padB); ctx.stroke()
            }
            ctx.globalAlpha = 1.0

            // ── axis labels ───────────────────────────────────────────
            ctx.fillStyle = AppTheme.plotLabel
            ctx.font = "10px sans-serif"
            ctx.textAlign = "right"; ctx.textBaseline = "middle"
            for (var ly = 0; ly <= 4; ly++) {
                var val = root.yMax - ly / 4 * (root.yMax - root.yMin)
                ctx.fillText(val.toFixed(1), padL - 6, padT + ly / 4 * (height - padT - padB))
            }
            ctx.textAlign = "center"; ctx.textBaseline = "top"
            var labels = ["0", "25", "50", "75", "100%"]
            for (var lx = 0; lx <= 4; lx++)
                ctx.fillText(labels[lx], padL + lx / 4 * (width - padL - padR), height - padB + 5)

            if (!root._hasData) {
                ctx.fillStyle = AppTheme.textTertiary
                ctx.font = "13px sans-serif"
                ctx.textAlign = "center"; ctx.textBaseline = "middle"
                ctx.fillText("Waiting for beats…", (padL + width - padR) / 2, (padT + height - padB) / 2)
                return
            }

            var phase = snap.phase
            // ── min/max envelope ──────────────────────────────────────
            ctx.fillStyle = root.accent
            ctx.globalAlpha = 0.14
            _band(ctx, phase, snap.envMin, snap.envMax)
            // ── p25-p75 band ──────────────────────────────────────────
            ctx.globalAlpha = 0.24
            _band(ctx, phase, snap.envP25, snap.envP75)
            ctx.globalAlpha = 1.0

            // ── template ──────────────────────────────────────────────
            ctx.strokeStyle = root.accent
            ctx.lineWidth = 2.5
            _line(ctx, phase, snap.template, -1)

            // ── live in-progress beat ─────────────────────────────────
            if (snap.livePhase && snap.livePhase.length > 1) {
                ctx.strokeStyle = AppTheme.textPrimary
                ctx.lineWidth = 1.6
                ctx.globalAlpha = 0.9
                _line(ctx, snap.livePhase, snap.liveValue, -1)
                ctx.globalAlpha = 1.0
                // Leading dot at the sweeping edge.
                var k = snap.livePhase.length - 1
                if (isFinite(snap.liveValue[k])) {
                    ctx.fillStyle = root.accent
                    ctx.beginPath()
                    ctx.arc(_x(snap.livePhase[k]), _y(snap.liveValue[k]), 3.2, 0, 2 * Math.PI)
                    ctx.fill()
                }
            }
        }
    }

    // ── title + live readout ─────────────────────────────────────────
    RowLayout {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: 10
        spacing: 8

        Rectangle {
            width: 10; height: 10; radius: 5; color: root.accent
            Layout.alignment: Qt.AlignVCenter
        }
        Text {
            text: root.title
            color: AppTheme.textSecondary
            font.pixelSize: 15; font.bold: true
        }
        Item { Layout.fillWidth: true }
        // Heart-rate readout is shown only when a real pulse is detected;
        // otherwise it would report a fake rate off band-limited noise.
        Text {
            visible: root._reliable
            text: (root._f && isFinite(root._f.hr_bpm))
                  ? Math.round(root._f.hr_bpm) + " bpm" : "-- bpm"
            color: AppTheme.textPrimary
            font.pixelSize: 15; font.family: "Roboto Mono"
        }
        Text {
            visible: root._reliable
            text: (root._f && isFinite(root._f.pi))
                  ? "PI " + root._f.pi.toFixed(2) : "PI --"
            color: AppTheme.textTertiary
            font.pixelSize: 13; font.family: "Roboto Mono"
        }
        Text {
            visible: root._hasData && !root._reliable
            text: "No pulse detected"
            color: AppTheme.accentYellow
            font.pixelSize: 14; font.bold: true
        }
    }

    // Centered "No pulse detected" banner when there is a signal but no
    // genuine cardiac pulse (static phantom, poor contact, lead-off).
    Rectangle {
        visible: root._hasData && !root._reliable
        anchors.centerIn: parent
        width: noPulseLabel.width + 28; height: noPulseLabel.height + 16
        radius: 8
        color: Qt.rgba(0.95, 0.77, 0.06, 0.14)
        border.color: AppTheme.accentYellow; border.width: 1
        Text {
            id: noPulseLabel
            anchors.centerIn: parent
            text: "No pulse detected"
            color: AppTheme.accentYellow
            font.pixelSize: 15; font.bold: true
        }
    }
}
