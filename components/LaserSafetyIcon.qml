import QtQuick 6.0

// Laser-hazard warning sign (the IEC 60825-1 symbol: a starburst with a
// beam inside a warning triangle), drawn rather than taken from the icon
// font, which has no laser glyph. Used on the panel's laser optimizer
// button (#746). Sized like a 26 px icon-font glyph.
Canvas {
    id: icon
    property color color: "#FFFFFF"
    implicitWidth: 30
    implicitHeight: 27
    onColorChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

    onPaint: {
        var ctx = getContext("2d")
        ctx.reset()
        ctx.strokeStyle = icon.color
        ctx.fillStyle = icon.color
        ctx.lineJoin = "round"
        ctx.lineCap = "round"

        var w = width, h = height, m = 1.5
        // Right edge of the triangle at height y (for the beam's end).
        function rightEdge(y) {
            return w / 2 + (y - m) / (h - 2 * m) * (w / 2 - m)
        }

        // Warning triangle.
        ctx.lineWidth = 1.8
        ctx.beginPath()
        ctx.moveTo(w / 2, m)
        ctx.lineTo(w - m, h - m)
        ctx.lineTo(m, h - m)
        ctx.closePath()
        ctx.stroke()

        // Starburst: a dot with rays, left of centre in the lower half.
        var cx = w * 0.40, cy = h * 0.66
        var r0 = w * 0.065, r1 = w * 0.19
        ctx.beginPath()
        ctx.arc(cx, cy, r0, 0, 2 * Math.PI)
        ctx.fill()
        ctx.lineWidth = 1.1
        for (var i = 1; i < 12; i++) {      // i = 0 is where the beam leaves
            var a = i * Math.PI / 6
            ctx.beginPath()
            ctx.moveTo(cx + Math.cos(a) * (r0 + 1.2), cy + Math.sin(a) * (r0 + 1.2))
            ctx.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1)
            ctx.stroke()
        }

        // The beam, out to the triangle's right side.
        ctx.lineWidth = 1.7
        ctx.beginPath()
        ctx.moveTo(cx + r0, cy)
        ctx.lineTo(rightEdge(cy) - 2.2, cy)
        ctx.stroke()
    }
}
