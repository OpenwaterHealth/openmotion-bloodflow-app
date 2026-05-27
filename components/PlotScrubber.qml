import QtQuick 6.0

// Bottom timeline showing the full scan extent (0 → liveEdge) with the
// visible window highlighted as a draggable inset. Click outside the
// inset jumps the window so the click lands at its centre; drag the
// inset to pan.
//
// Inputs come from PlotViewer; setWindow callback applies the change.
// Cell-side scroll-and-zoom keep working as before — the scrubber is
// an alternative pan UI, not the only one.
Item {
    id: scrubber

    AppTheme { id: theme }

    // ── Inputs ─────────────────────────────────────────────────────────
    property real fullScanDuration: 0      // = liveEdgeSnapshot
    property real windowStartT: 0
    property real windowSeconds: 15
    property bool followLive: true

    // ── Output ─────────────────────────────────────────────────────────
    signal panRequested(real startT)

    // Effective span the scrubber covers — at least one full window
    // wide so the inset never gets clipped to zero width at scan start.
    readonly property real _span: Math.max(scrubber.fullScanDuration, scrubber.windowSeconds)
    readonly property real _insetX: scrubber._span > 0
        ? scrubber.width * (scrubber.windowStartT / scrubber._span)
        : 0
    readonly property real _insetW: scrubber._span > 0
        ? Math.max(8, scrubber.width * (scrubber.windowSeconds / scrubber._span))
        : 0

    // ── Background ─────────────────────────────────────────────────────
    Rectangle {
        anchors.fill: parent
        color: theme.bgPanel
        border.color: theme.borderSubtle
        border.width: 1
        radius: 4
    }

    // Visible-window inset — translucent so the scrubber background
    // remains visible underneath, indicating the proportion of the
    // full scan that the inset covers.
    Rectangle {
        id: windowInset
        x: scrubber._insetX
        width: scrubber._insetW
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.topMargin: 2
        anchors.bottomMargin: 2
        color: scrubber.followLive ? theme.statusBlue : theme.accentOrange
        opacity: 0.35
        radius: 3
    }

    // Right-edge "live" indicator — small bright tick at liveEdge so
    // the user can see the head of the recording at a glance.
    Rectangle {
        x: scrubber._span > 0
            ? Math.min(scrubber.width - 2, scrubber.width * (scrubber.fullScanDuration / scrubber._span))
            : 0
        width: 2
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        color: theme.statusGreen
        visible: scrubber.fullScanDuration > 0
    }

    // ── Interaction ────────────────────────────────────────────────────
    MouseArea {
        anchors.fill: parent
        cursorShape: _dragging ? Qt.ClosedHandCursor : Qt.OpenHandCursor

        property bool _dragging: false
        property real _dragStartX: 0
        property real _dragStartWindowT: 0

        onPressed: function(mouse) {
            var insetLeft = scrubber._insetX
            var insetRight = insetLeft + scrubber._insetW
            if (mouse.x >= insetLeft && mouse.x <= insetRight) {
                // Grab the inset — pan it.
                _dragging = true
                _dragStartX = mouse.x
                _dragStartWindowT = scrubber.windowStartT
            } else if (scrubber._span > 0 && scrubber.width > 0) {
                // Click outside: jump the window so the clicked time
                // lands at the centre of the visible window.
                var clickT = (mouse.x / scrubber.width) * scrubber._span
                scrubber.panRequested(Math.max(0, clickT - scrubber.windowSeconds / 2))
            }
        }

        onReleased: _dragging = false

        onPositionChanged: function(mouse) {
            if (!_dragging || scrubber.width <= 0 || scrubber._span <= 0) return
            var dx = mouse.x - _dragStartX
            var dT = (dx / scrubber.width) * scrubber._span
            scrubber.panRequested(Math.max(0, _dragStartWindowT + dT))
        }
    }
}
