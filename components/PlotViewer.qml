import QtQuick 6.0
import QtQuick.Controls 6.0
import QtQuick.Layouts 6.0
import OpenMotion 1.0

// Phase 2b-i — multi-cell viewer. Always renders 16 cells (2 sides × 8 cams).
// Inactive cameras just render an empty Canvas + label; no per-mask branching.
// Clinical-mode 2-cell layout is Phase 2b-ii. Toolbar + autoscale arrive in
// Task 5; the current header text is the Phase 2a placeholder.
// Geometry is owned by the instantiation site (BloodFlow.qml anchors the
// viewer beside the icon bar) — no anchors here. A root-level
// `anchors.fill: parent` used to be harmless behind a Loader, but once
// inlined it overrode the caller's anchors and slid under the ButtonPanel.
Rectangle {
    id: viewer
    color: AppTheme.bgPlot
    radius: 8
    border.color: AppTheme.borderSoft
    border.width: 1

    // Keyboard focus — viewer starts focused so spec keys (← → +/- 0
    // Home End Esc) work without a prior click. Click on a cell or
    // the scrubber re-focuses; Esc clears focus so text inputs (notes
    // editor, subject ID) take precedence afterward.
    focus: true
    activeFocusOnTab: true

    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Left) {
            var dt = (event.modifiers & Qt.ShiftModifier)
                ? -viewer.windowSeconds : -1
            viewer._kbPan(dt)
            event.accepted = true
        } else if (event.key === Qt.Key_Right) {
            var dt2 = (event.modifiers & Qt.ShiftModifier)
                ? viewer.windowSeconds : 1
            viewer._kbPan(dt2)
            event.accepted = true
        } else if (event.key === Qt.Key_Plus || event.key === Qt.Key_Equal
                   || event.key === Qt.Key_Up) {
            // Equal key shares the physical key with Plus on US layouts
            // — accept it so the user doesn't need to hold Shift. ↑/↓
            // mirror mouse-wheel direction: wheel-up zooms in.
            viewer._kbZoom(0.8)
            event.accepted = true
        } else if (event.key === Qt.Key_Minus || event.key === Qt.Key_Underscore
                   || event.key === Qt.Key_Down) {
            viewer._kbZoom(1.25)
            event.accepted = true
        } else if (event.key === Qt.Key_0) {
            viewer._kbReset()
            event.accepted = true
        } else if (event.key === Qt.Key_Home) {
            viewer.setWindow(0, viewer.windowSeconds, "keyboard")
            event.accepted = true
        } else if (event.key === Qt.Key_End) {
            viewer._kbEnd()
            event.accepted = true
        } else if (event.key === Qt.Key_Escape) {
            viewer.focus = false
            event.accepted = true
        }
    }


    // ── Inputs ─────────────────────────────────────────────────────────
    property bool clinicalMode: false   // honored in Phase 2b-ii
    // Per-cell top-left value labels. Default off in clinical mode — the
    // large side panels show the same numbers there — and toggleable at
    // runtime from the bottom-right ⋯ popup.
    property bool showCellValues: !viewer.effectiveClinical
    // Y-axis tick labels (max/mid/min per metric). Config is the single
    // source of truth: the ⋯ popup toggle (hidden in clinical mode) writes
    // through MotionInterface.setConfig, which persists to app_config.json
    // and notifies appConfigChanged — this binding then updates.
    property bool showAxisLabels: MotionInterface.appConfig.showAxisLabels === true
    // displayMode pair selector — driven externally (BloodFlow.qml binds
    // it from settingsModal.showBfiBvi). "bfi_bvi" overlays BFI+BVI on
    // each cell; "mean_contrast" overlays Mean+Contrast.
    property string displayMode: "bfi_bvi"
    // autoScale — driven externally from Settings; when true, the 3 s
    // _recomputeAutoscale timer below repins primary/secondary YMin/YMax
    // to per-metric percentile bounds across the buffer.
    property bool autoScale: true
    // Camera masks — bound from BloodFlow.qml so the grid reacts to
    // Scan Settings changes, not just the persisted config defaults.
    property int leftMask:  0x66
    property int rightMask: 0x66

    // ── State (owned here; pushed to every cell) ───────────────────────
    // windowSeconds is internal — the bottom-right pill overlay edits it
    // via _windowSecondsRequested. Each metric has its own y-axis mapping
    // (primary*/secondary*) so the two traces don't squash each other
    // when their ranges differ wildly.
    property real windowSeconds: 15

    // When displayMode changes (e.g. from the Settings modal flipping
    // BFI/BVI ↔ Mean/Contrast), the previously-fit y-bounds will be
    // for the wrong pair — mean ~100 + contrast ~0.3 would render
    // entirely off-axis until the next 3 s autoscale tick. Force an
    // immediate recompute so the switch lands cleanly.
    onDisplayModeChanged: viewer._recomputeAutoscale()

    // ── Time-axis state ────────────────────────────────────────────────
    // followLive = true: cells render the last `windowSeconds` of data
    // up to source.liveEdge (DVR "live" mode). False: the visible window
    // is pinned at [windowStartT, windowStartT + windowSeconds] and any
    // new samples scroll on without moving the view (DVR "paused" mode).
    // Any pan/wheel-zoom interaction sets followLive=false; only the
    // "Back to live" button restores it.
    property bool followLive: true
    property real windowStartT: 0.0

    // Y-axis bounds — when autoScale is true, _autoPrimaryYMin/Max are
    // written by _recomputeAutoscale (every 3 s + on displayMode change)
    // and primaryYMin/Max read them through the derived bindings below.
    // When autoScale is false, primaryYMin/Max instead read the per-
    // metric clamps from settingsModal (settingBfiMin/Max etc.) so the
    // operator's preferred fixed scale applies.
    property real _autoPrimaryYMin: 0.0
    property real _autoPrimaryYMax: 10.0
    property real _autoSecondaryYMin: 0.0
    property real _autoSecondaryYMax: 10.0

    // Manual-bound inputs — BloodFlow.qml binds these from settingsModal.
    property real settingBfiMin: 0.0
    property real settingBfiMax: 10.0
    property real settingBviMin: 0.0
    property real settingBviMax: 10.0
    property real settingMeanMin: 0.0
    property real settingMeanMax: 500.0
    property real settingContrastMin: 0.0
    property real settingContrastMax: 1.0

    function _settingBound(metric, which) {
        if (metric === "bfi")      return which === "min" ? settingBfiMin      : settingBfiMax
        if (metric === "bvi")      return which === "min" ? settingBviMin      : settingBviMax
        if (metric === "mean")     return which === "min" ? settingMeanMin     : settingMeanMax
        if (metric === "contrast") return which === "min" ? settingContrastMin : settingContrastMax
        return which === "min" ? 0.0 : 10.0
    }

    readonly property real primaryYMin: autoScale
        ? _autoPrimaryYMin
        : _settingBound(_displayPair.primary, "min")
    readonly property real primaryYMax: autoScale
        ? _autoPrimaryYMax
        : _settingBound(_displayPair.primary, "max")
    readonly property real secondaryYMin: autoScale
        ? _autoSecondaryYMin
        : _settingBound(_displayPair.secondary, "min")
    readonly property real secondaryYMax: autoScale
        ? _autoSecondaryYMax
        : _settingBound(_displayPair.secondary, "max")

    // ── Source subscription ────────────────────────────────────────────
    readonly property var scanSource: MotionInterface.currentScanSource

    // ── Effective camera masks ─────────────────────────────────────────
    // The grid lays out from the camera config of whatever's being shown.
    // A replayed past scan exposes the masks it actually recorded
    // (leftMask/rightMask >= 0); use those so its layout doesn't inherit
    // the operator's current Scan Settings selection (issue #175). A live
    // source — or no source, before the first scan — reports -1 (unknown),
    // so the grid keeps following the live leftMask/rightMask selection.
    //
    // For the live fallback, additionally gate each side on its sensor
    // actually being connected (issue #298): the live leftMask/rightMask
    // carry the config default (e.g. 0x99 = 4 cams) regardless of which
    // ports are populated, so a single sensor on the right port would
    // otherwise draw 4 phantom left plots. A disconnected side contributes
    // no cells; the binding re-flows on connect/disconnect because
    // leftSensorConnected/rightSensorConnected notify connectionStatusChanged.
    // Past scans are unaffected — their recorded masks win before this gate.
    readonly property int _effLeftMask:
        (viewer.scanSource && viewer.scanSource.leftMask !== undefined
         && viewer.scanSource.leftMask >= 0)
            ? viewer.scanSource.leftMask
            : (MotionInterface.leftSensorConnected ? viewer.leftMask : 0x00)
    readonly property int _effRightMask:
        (viewer.scanSource && viewer.scanSource.rightMask !== undefined
         && viewer.scanSource.rightMask >= 0)
            ? viewer.scanSource.rightMask
            : (MotionInterface.rightSensorConnected ? viewer.rightMask : 0x00)

    // Replay adopts the loaded scan's recorded mode: when the source reports a
    // known mode (clinicalMode >= 0) use it, else fall back to the live-config
    // `clinicalMode` input. Mirrors _effLeftMask/_effRightMask (#175). Note:
    // scanSource.clinicalMode is an int tri-state (-1/0/1) from Python;
    // viewer.clinicalMode is the bool live-config input.
    readonly property bool effectiveClinical:
        (viewer.scanSource && viewer.scanSource.clinicalMode !== undefined
         && viewer.scanSource.clinicalMode >= 0)
            ? (viewer.scanSource.clinicalMode === 1)
            : viewer.clinicalMode

    // ── Side break ─────────────────────────────────────────────────────
    // A bit of extra margin between the left and right sensor modules in
    // dev (non-clinical) mode. The grid reserves an empty spacer column
    // (col 2) between them, but only when BOTH sides have active cameras —
    // a single-sided scan keeps filling the width with no dangling gap.
    readonly property bool _sideGapActive:
        !viewer.effectiveClinical
        && (viewer._effLeftMask & 0xFF) !== 0
        && (viewer._effRightMask & 0xFF) !== 0
    readonly property real _sideGapPx: 3

    // ── Grid model ─────────────────────────────────────────────────────
    // Dev mode (default) — one cell per active camera (bits set in
    // leftMask / rightMask), arranged to mirror the physical U-shape of
    // each sensor module displayed upside-down (issue #164). Cameras 1-8
    // run down one arm of the U and back up the other — 1|8 at the top
    // of the U, 4|5 at the bottom — so display row r pairs 1-based cams
    // (4-r | 5+r): row 0 is 4|5, row 3 is 1|8. Left module owns columns
    // 0-1, right module 3-4 — col 2 is the empty side-break spacer when
    // both sides are active (a module with nothing selected gives up its
    // column pair). Rows empty on both sides are dropped and the rest
    // compact upward; an unselected camera in a kept row just leaves its
    // slot blank.
    readonly property var _devCellModel: {
        var masks = [viewer._effLeftMask & 0xFF, viewer._effRightMask & 0xFF]
        var sides = ["left", "right"]
        var colBase = [0, masks[0] !== 0 ? 3 : 0]
        var entries = []
        var displayRow = 0
        for (var r = 0; r < 4; r++) {
            var armA = 3 - r   // zero-based camId, 1-based cam 4-r
            var armB = 4 + r   // zero-based camId, 1-based cam 5+r
            var rowBits = (1 << armA) | (1 << armB)
            if (!((masks[0] | masks[1]) & rowBits)) continue
            for (var s = 0; s < 2; s++) {
                if (masks[s] & (1 << armA)) {
                    entries.push({ side: sides[s], camId: armA,
                                   row: displayRow, col: colBase[s] })
                }
                if (masks[s] & (1 << armB)) {
                    entries.push({ side: sides[s], camId: armB,
                                   row: displayRow, col: colBase[s] + 1 })
                }
            }
            displayRow++
        }
        return entries
    }

    // Clinical mode — one cell per ACTIVE side, each rendering the
    // side-averaged stream (cam_id=-1, fed by SDK's SideAveragingStage
    // via _LivePlotSink.consume). Stacked vertically in a single column.
    // A side with no active cameras (disconnected sensor / recorded mask 0)
    // is dropped and the survivor compacts to row 0 so a single-sided
    // clinical scan doesn't reserve a blank row (issue #298).
    readonly property var _clinicalCellModel: {
        var entries = []
        var displayRow = 0
        if ((viewer._effLeftMask & 0xFF) !== 0)
            entries.push({ side: "left", camId: -1, row: displayRow++, col: 0 })
        if ((viewer._effRightMask & 0xFF) !== 0)
            entries.push({ side: "right", camId: -1, row: displayRow++, col: 0 })
        return entries
    }

    readonly property var _activeCellModel: viewer.effectiveClinical
        ? _clinicalCellModel
        : _devCellModel

    // Clinical readout column gate — the whole 250 px column collapses when
    // no side has active cameras, not just its individual panels (#298):
    // an empty-but-visible column would push the "No active cameras
    // selected" placeholder off the canvas center (issue #487).
    readonly property bool _showClinicalPanels:
        viewer.effectiveClinical && viewer._activeCellModel.length > 0

    // ── Autoscale recompute (shared by Timer + displayMode change) ────
    // Writes to _auto* — the derived primaryYMin/Max bindings above
    // pick the _auto vs setting-bound value based on autoScale.
    function _recomputeAutoscale() {
        if (!viewer.scanSource) return
        var bp = viewer.scanSource.compute_bounds_for_metric(viewer._displayPair.primary)
        if (bp && typeof bp.yMin === "number" && typeof bp.yMax === "number") {
            viewer._autoPrimaryYMin = bp.yMin
            viewer._autoPrimaryYMax = bp.yMax
        }
        var bs = viewer.scanSource.compute_bounds_for_metric(viewer._displayPair.secondary)
        if (bs && typeof bs.yMin === "number" && typeof bs.yMax === "number") {
            viewer._autoSecondaryYMin = bs.yMin
            viewer._autoSecondaryYMax = bs.yMax
        }
        viewer._dirty = true
    }

    // ── DVR controls (called from PlotCell MouseArea) ─────────────────
    // Window-bounds: hard floor at 0.5 s so wheel-zoom can't collapse
    // the visible window to nothing; ceiling at 600 s to keep the
    // decimation point count sane for very-long scans.
    readonly property real _minWindowSeconds: 0.5
    readonly property real _maxWindowSeconds: 600.0

    function _ensureFrozen(cause) {
        // Capture the currently-visible window start so pan/zoom from
        // followLive transitions smoothly (no visual jump). After this
        // the caller mutates windowStartT/windowSeconds freely.
        if (viewer.followLive) {
            // Use the snapshot (matches what's actually drawn) rather
            // than re-reading source.liveEdge — avoids a one-frame jump
            // at the moment of pan/zoom on a fast source.
            viewer.windowStartT = Math.max(0, viewer.liveEdgeSnapshot - viewer.windowSeconds)
            // One line per live→paused edge — the recovery already logs
            // as "[Plot] back-to-live" — so a debug bundle can show WHEN
            // the view stopped following live and what input did it.
            console.info("[Plot] live-follow paused (" + (cause || "pan/zoom") + ")")
        }
        viewer.followLive = false
    }

    function setWindow(startT, seconds, cause) {
        _ensureFrozen(cause)
        viewer.windowSeconds = Math.max(_minWindowSeconds, Math.min(_maxWindowSeconds, seconds))
        // Cap startT so the window can't extend past the live edge —
        // otherwise the scrubber inset slides off into empty future
        // space when the user keeps dragging right. Lower bound 0
        // (scan start); upper bound puts the rightmost data at the
        // right edge of the visible plot.
        var maxStart = Math.max(0, viewer.liveEdgeSnapshot - viewer.windowSeconds)
        viewer.windowStartT = Math.max(0, Math.min(maxStart, startT))
        viewer._dirty = true
    }

    function backToLive() {
        viewer.followLive = true
        viewer._dirty = true
    }

    // ── Keyboard shortcut helpers ──────────────────────────────────────
    // Default windowSeconds matches PlotToolbar's "15 s" combo entry —
    // the `0` shortcut resets to this canonical value.
    readonly property real _defaultWindowSeconds: 15

    function _kbPan(seconds) {
        _ensureFrozen("keyboard")
        setWindow(viewer.windowStartT + seconds, viewer.windowSeconds, "keyboard")
    }

    function _kbZoom(factor) {
        _ensureFrozen("keyboard")
        // Zoom around the center of the currently-visible window so the
        // operator's mental anchor stays put on the screen.
        var center = viewer.windowStartT + viewer.windowSeconds / 2
        var newSec = viewer.windowSeconds * factor
        setWindow(center - newSec / 2, newSec, "keyboard")
    }

    function _kbEnd() {
        // Past mode: pin window so the rightmost data sits at the right
        // edge. Live mode: resume followLive (snaps to liveEdge AND
        // keeps tracking new samples as they arrive).
        if (viewer.scanSource && viewer.scanSource.live) {
            backToLive()
        } else {
            setWindow(
                Math.max(0, viewer.liveEdgeSnapshot - viewer.windowSeconds),
                viewer.windowSeconds,
                "keyboard"
            )
        }
    }

    function _kbReset() {
        viewer.windowSeconds = _defaultWindowSeconds
        backToLive()
    }

    // ── Hover crosshair ────────────────────────────────────────────────
    // Cells broadcast cursor time here via cursorAt(); cells read
    // viewer.cursorT to draw the synced vertical line. NaN means hide.
    property real cursorT: NaN

    function cursorAt(t) {
        viewer.cursorT = t
        // Mark dirty so the next paintThrottle tick (≤ 33 ms) repaints
        // every cell with the new crosshair x — no per-mousemove paints.
        viewer._dirty = true
    }

    // ── Trace color per metric ─────────────────────────────────────────
    // BFI/BVI colors come from app config (bfiColor / bviColor) via the
    // bindings below; mean/contrast keep the legacy palette (no config
    // keys exist for them). Every color is passed through the theme's
    // readability guard so a palette tuned for one mode (e.g. white BFI
    // on dark) doesn't render invisible in the other.
    property color bfiColor: "#E74C3C"
    property color bviColor: "#3498DB"
    readonly property color bfiInk: AppTheme.readableInk(viewer.bfiColor)
    readonly property color bviInk: AppTheme.readableInk(viewer.bviColor)

    function _traceColorForMetric(m) {
        if (m === "bfi")      return viewer.bfiInk
        if (m === "bvi")      return viewer.bviInk
        if (m === "mean")     return AppTheme.readableInk("#2ECC71")  // green
        if (m === "contrast") return AppTheme.readableInk("#9B59B6")  // purple
        return viewer.bviInk
    }

    // ── Display clamps (GUI-only) ──────────────────────────────────────
    // Numeric readouts for BFI/BVI saturate into [low, high] before
    // formatting. Display-side only — the data stream, CSVs, and trace
    // geometry are untouched. Bounds come from app config (0–10 default).
    readonly property real _bfiClampLow:
        MotionInterface.appConfig.bfiClampLow !== undefined ? MotionInterface.appConfig.bfiClampLow : 0.0
    readonly property real _bfiClampHigh:
        MotionInterface.appConfig.bfiClampHigh !== undefined ? MotionInterface.appConfig.bfiClampHigh : 10.0
    readonly property real _bviClampLow:
        MotionInterface.appConfig.bviClampLow !== undefined ? MotionInterface.appConfig.bviClampLow : 0.0
    readonly property real _bviClampHigh:
        MotionInterface.appConfig.bviClampHigh !== undefined ? MotionInterface.appConfig.bviClampHigh : 10.0

    function clampForDisplay(m, v) {
        if (!isFinite(v)) return v
        if (m === "bfi") return Math.min(Math.max(v, _bfiClampLow), _bfiClampHigh)
        if (m === "bvi") return Math.min(Math.max(v, _bviClampLow), _bviClampHigh)
        return v
    }

    // ── Display-mode pair resolution ───────────────────────────────────
    // Maps the displayMode toggle to the primary/secondary metric pair
    // pushed to every cell.
    readonly property var _displayPair: {
        if (viewer.displayMode === "mean_contrast")
            return { primary: "mean", secondary: "contrast" }
        return { primary: "bfi", secondary: "bvi" }
    }

    // ── Viewer-driven paint throttle ───────────────────────────────────
    // Single dirty flag set by ANY samplesAppended emission. A 33 ms
    // Timer ticks paintTick (consumed by every PlotCell) whenever dirty.
    // Caps total cell-paint rate to ~30 Hz regardless of how many
    // samplesAppended signals fire in between, and renders all cells
    // in one pass so they stay visually in lockstep.
    property int paintTick: 0
    property bool _dirty: true   // start true so cells paint at least once

    // Snapshot of source.liveEdge captured once per paintTick. All cells
    // read this (not source.liveEdge directly) so their windows stay
    // perfectly synced even when individual cell paints land at slightly
    // different wall-clock times under load.
    property real liveEdgeSnapshot: 0.0

    Connections {
        target: viewer.scanSource
        ignoreUnknownSignals: true
        function onSamplesAppended(s, c, m, n) {
            viewer._dirty = true
            viewer._profSamplesAccum += n
        }
        // Live source only (ignoreUnknownSignals covers PastScanSource):
        // an async DB-tail window finished loading off-thread — repaint so
        // the history appears even when no live samples are ticking the
        // throttle (e.g. panned back after the scan ended).
        function onHistoryWindowLoaded() {
            viewer._dirty = true
        }
    }

    // ── Per-camera "Connection Lost" badges (issue #174) ───────────────
    // Map "side:camId" → true while the connector's dropout watchdog has
    // that camera marked Connection Lost. Fed by cameraDropoutDetected /
    // cameraDropoutRecovered (both delivered queued on the GUI thread);
    // cleared when a fresh live source arrives (scan start). Reassigned
    // wholesale — never mutated in place — so the cell delegate bindings
    // re-evaluate. Badges only display on the live source (see the
    // delegate's connectionLost binding), so panning to a past scan hides
    // them without losing the state.
    property var _lostCameras: ({})

    function _setCameraLost(side, camId, lost) {
        var m = {}
        for (var k in viewer._lostCameras) m[k] = viewer._lostCameras[k]
        if (lost) m[side + ":" + camId] = true
        else delete m[side + ":" + camId]
        viewer._lostCameras = m
    }

    Connections {
        target: MotionInterface
        function onCameraDropoutDetected(side, camId, elapsed) {
            viewer._setCameraLost(side, camId, true)
        }
        function onCameraDropoutRecovered(side, camId, elapsed) {
            viewer._setCameraLost(side, camId, false)
        }
    }

    // ── Profile HUD state ──────────────────────────────────────────────
    // Hidden behind engineeringMode && showProfiling — clinical users
    // never see this overlay. Counters accumulate per tick; the 1 Hz
    // _profHudTimer below converts them to display values.
    property int _profSamplesAccum: 0        // samples since last 1Hz roll-up
    property real _profPaintTickMs: 0.0      // last tick's dispatch duration
    property real _profCanvasMsSum: 0.0      // current-tick canvas-ms accumulator
    property int _profCanvasCount: 0         // # cells that reported this tick
    property int _profPointsAccum: 0         // points painted this tick
    // Display values — read by the HUD overlay, refreshed at 1 Hz.
    property real profSampleRateHz: 0.0
    property real profPaintTickMsAvg: 0.0
    property real profCanvasMsAvg: 0.0
    property real profCanvasMsMax: 0.0
    property int profPointsLastTick: 0
    property real _profHudLastWall: 0

    // PlotCell calls this from its onPaint with the wall-time it took
    // and the points-painted count. We accumulate, the 1 Hz HUD timer
    // computes the rolling average / max.
    function recordCellPaint(ms, points) {
        viewer._profCanvasMsSum += ms
        viewer._profCanvasCount += 1
        viewer._profPointsAccum += points
        if (ms > viewer.profCanvasMsMax) viewer.profCanvasMsMax = ms
    }

    Timer {
        id: profHudTimer
        interval: 1000
        repeat: true
        // No need to run when the HUD isn't visible — saves the per-tick
        // EWMA + division on every clinical-user session.
        running: viewer._hudVisible
        triggeredOnStart: true
        onTriggered: {
            var now = Date.now()
            if (viewer._profHudLastWall > 0) {
                var dtSec = (now - viewer._profHudLastWall) * 0.001
                if (dtSec > 0) {
                    var instantHz = viewer._profSamplesAccum / dtSec
                    // EWMA α=0.15 matches the legacy plot's rate display.
                    viewer.profSampleRateHz = (viewer.profSampleRateHz === 0)
                        ? instantHz
                        : 0.85 * viewer.profSampleRateHz + 0.15 * instantHz
                }
            }
            viewer._profSamplesAccum = 0
            viewer._profHudLastWall = now
            // Snapshot per-tick canvas stats then reset for the next sec.
            if (viewer._profCanvasCount > 0) {
                viewer.profCanvasMsAvg = viewer._profCanvasMsSum / viewer._profCanvasCount
            }
            viewer.profPointsLastTick = viewer._profPointsAccum
            viewer._profCanvasMsSum = 0
            viewer._profCanvasCount = 0
            viewer._profPointsAccum = 0
            viewer.profCanvasMsMax = 0
        }
    }

    // Runtime toggle — initialized from app_config.showProfiling but
    // flippable from the PlotToolbar's Profiler checkbox at runtime.
    // The toolbar checkbox itself is hidden outside engineering mode,
    // and _hudVisible further gates on engineeringMode so the HUD can't
    // appear in clinical builds even if showProfiling is flipped.
    property bool showProfiling: MotionInterface.appConfig.showProfiling === true
    readonly property bool _hudVisible: MotionInterface.appConfig.engineeringMode === true
                                        && viewer.showProfiling

    Timer {
        id: paintThrottle
        // 33 ms = 30 Hz. Each tick moves the trace ~1 sample at the
        // data rate (40 Hz), so the scroll feels continuous instead
        // of stepwise.
        interval: 33
        running: viewer.scanSource !== null
        repeat: true
        onTriggered: {
            if (viewer._dirty) {
                var t0 = viewer._hudVisible ? Date.now() : 0
                viewer._dirty = false
                viewer.liveEdgeSnapshot = viewer.scanSource ? viewer.scanSource.liveEdge : 0
                viewer.paintTick++
                if (viewer._hudVisible) {
                    var dt = Date.now() - t0
                    viewer.profPaintTickMsAvg = (viewer.profPaintTickMsAvg === 0)
                        ? dt
                        : 0.85 * viewer.profPaintTickMsAvg + 0.15 * dt
                }
            }
        }
    }

    // Source change forces an immediate paint without waiting for the
    // first samplesAppended; also resets followLive so each new source
    // (live or past) opens at its own latest window.
    onScanSourceChanged: {
        viewer.liveEdgeSnapshot = viewer.scanSource ? viewer.scanSource.liveEdge : 0
        viewer.followLive = true
        viewer._dirty = true
        // Fresh live source = new scan → clear stale Connection Lost
        // badges. A past (non-live) source keeps the state; its badges
        // are already hidden by the delegate's live-only binding.
        if (viewer.scanSource && viewer.scanSource.live === true)
            viewer._lostCameras = ({})
        // Re-fit y-axis to the new source's data immediately, otherwise
        // a past scan loaded with very different value ranges would draw
        // off-axis until the next autoscale tick.
        viewer._recomputeAutoscale()
    }

    // Autoscale tick — every 3 s. compute_bounds_for_metric walks every
    // sample across all buffers, so the per-call cost scales with scan
    // duration; 3 s amortizes that work without making the y-axis feel
    // unresponsive (a 3-second delay between bound adjustments is hard
    // to notice during live monitoring).
    Timer {
        interval: 3000
        running: viewer.autoScale && viewer.scanSource !== null
        repeat: true
        triggeredOnStart: true
        onTriggered: viewer._recomputeAutoscale()
    }

    // Window-seconds options — duplicated here from the old PlotToolbar
    // so the new bottom-right overlay can use the same list. Keeping
    // the labels formatter symmetric (15 → "15 s", 300 → "5 min").
    readonly property var _windowOptions: [
        { value: 5,   label: "5 s" },
        { value: 15,  label: "15 s" },
        { value: 30,  label: "30 s" },
        { value: 60,  label: "1 min" },
        { value: 300, label: "5 min" }
    ]

    function _labelForWindowSeconds(s) {
        for (var i = 0; i < viewer._windowOptions.length; i++) {
            if (viewer._windowOptions[i].value === s)
                return viewer._windowOptions[i].label
        }
        // Custom (wheel-zoomed) value — show as "<n>s" or "<m>m" tersely.
        if (s >= 60) return Math.round(s / 60 * 10) / 10 + " min"
        return Math.round(s * 10) / 10 + " s"
    }

    // Triggered by the bottom-right back-to-live overlay button.
    function _backToLiveRequested() {
        if (viewer.scanSource && viewer.scanSource.live === false
                && MotionInterface.liveSourceAvailable) {
            MotionInterface.showLiveSource()
            console.info("[Plot] back-to-live (past → live source)")
        } else {
            viewer.backToLive()
            console.info("[Plot] back-to-live (followLive → true)")
        }
    }

    // Triggered by the bottom-right window-seconds menu.
    function _windowSecondsRequested(s) {
        if (viewer.followLive) {
            viewer.windowSeconds = s
        } else {
            viewer.setWindow(viewer.windowStartT, s)
        }
        viewer._dirty = true
        console.info("[Plot] windowSeconds → " + s + " s")
    }

    // Large per-side BFI/BVI readout panel — clinical mode
    // only, one per plot row, sitting to the left of the plot. Mirrors
    // the legacy ReducedPlotView side column: side label up top, big
    // mono values beneath. Values are the side-averaged stream
    // (cam_id = -1), refreshed on paintTick and clamped for display.
    component SideMetricPanel: Item {
        id: panel
        property string panelSide: "left"

        function _displayText(metricName) {
            void viewer.paintTick  // dependency
            if (!viewer.scanSource) return "--"
            var v = viewer.scanSource.value_at(
                panel.panelSide, -1, metricName, viewer.liveEdgeSnapshot)
            if (!isFinite(v)) return "--"
            return viewer.clampForDisplay(metricName, v).toFixed(2)
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 16
            spacing: 2

            Text {
                text: panel.panelSide.toUpperCase()
                color: AppTheme.textSecondary
                font.pixelSize: 20
                font.weight: Font.DemiBold
            }

            Item { Layout.fillHeight: true }

            Text {
                text: "BFI"
                color: viewer.bfiInk
                font.pixelSize: 24
                font.weight: Font.DemiBold
            }
            Text {
                text: panel._displayText("bfi")
                color: AppTheme.textPrimary
                font.pixelSize: 60
                font.weight: Font.Bold
                font.family: "Roboto Mono"
            }

            Item { height: 10 }

            Text {
                text: "BVI"
                color: viewer.bviInk
                font.pixelSize: 24
                font.weight: Font.DemiBold
            }
            Text {
                text: panel._displayText("bvi")
                color: AppTheme.textPrimary
                font.pixelSize: 60
                font.weight: Font.Bold
                font.family: "Roboto Mono"
            }

            Item { Layout.fillHeight: true }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 8

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 8

            // Clinical mode: large readouts beside the plots, one panel
            // per stacked plot row. Equal fillHeight keeps each panel
            // aligned with its row. fillWidth must be EXPLICITLY false:
            // nested layouts default it to true, which would make this
            // column compete with the grid for the whole row width.
            ColumnLayout {
                visible: viewer._showClinicalPanels
                Layout.fillWidth: false
                Layout.fillHeight: true
                Layout.preferredWidth: 250
                spacing: 6

                // Each side's readout panel is shown only when that side
                // has active cameras, mirroring _clinicalCellModel — a
                // disconnected sensor (or a single-sided recorded scan)
                // leaves no dead "--" panel behind (issue #298).
                SideMetricPanel {
                    panelSide: "left"
                    visible: (viewer._effLeftMask & 0xFF) !== 0
                    Layout.fillWidth: true
                    Layout.fillHeight: visible
                    Layout.preferredHeight: visible ? -1 : 0
                }
                SideMetricPanel {
                    panelSide: "right"
                    visible: (viewer._effRightMask & 0xFF) !== 0
                    Layout.fillWidth: true
                    Layout.fillHeight: visible
                    Layout.preferredHeight: visible ? -1 : 0
                }
            }

            GridLayout {
                id: grid
                visible: viewer._activeCellModel.length > 0
                Layout.fillWidth: true
                Layout.fillHeight: true
                // Clinical mode: single column, 2 stacked cells. Engineering mode:
                // 4 columns; +1 spacer column (col 2) when both sides are
                // active, for a visual break between the modules.
                columns: viewer.effectiveClinical ? 1 : (viewer._sideGapActive ? 5 : 4)
                rowSpacing: 6
                columnSpacing: 6

                // Side-break spacer — fixed-width empty column 2 between the
                // left (cols 0-1) and right (cols 3-4) modules. fillWidth is
                // false so it stays a fixed gap; the plot cells (fillWidth)
                // absorb the rest of the row width around it.
                Item {
                    visible: viewer._sideGapActive
                    Layout.row: 0
                    Layout.column: 2
                    Layout.fillWidth: false
                    Layout.fillHeight: false
                    Layout.preferredWidth: viewer._sideGapPx
                }

                Repeater {
                    model: viewer._activeCellModel
                    delegate: PlotCell {
                        Layout.row: modelData.row
                        Layout.column: modelData.col
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        source: viewer.scanSource
                        side: modelData.side
                        camId: modelData.camId
                        windowSeconds: viewer.windowSeconds
                        followLive: viewer.followLive
                        windowStartT: viewer.windowStartT
                        metric: viewer._displayPair.primary
                        yMin: viewer.primaryYMin
                        yMax: viewer.primaryYMax
                        traceColor: viewer._traceColorForMetric(viewer._displayPair.primary)
                        secondaryMetric: viewer._displayPair.secondary
                        secondaryYMin: viewer.secondaryYMin
                        secondaryYMax: viewer.secondaryYMax
                        secondaryColor: viewer._traceColorForMetric(viewer._displayPair.secondary)
                        showValueLabels: viewer.showCellValues
                        showAxisLabels: viewer.showAxisLabels
                        showTemperature: MotionInterface.appConfig.engineeringMode === true
                        paintTick: viewer.paintTick
                        liveEdgeSnapshot: viewer.liveEdgeSnapshot
                        panZoomTarget: viewer
                        cursorT: viewer.cursorT
                        // Live-source only: past scans have no "current"
                        // connection state to report (issue #174).
                        connectionLost: viewer.scanSource !== null
                            && viewer.scanSource.live === true
                            && viewer._lostCameras[modelData.side + ":" + modelData.camId] === true
                    }
                }
            }

            // Placeholder when no cameras are active (both masks 0). Lets the
            // viewer still show its toolbar + scan-source state without an
            // empty grid below it.
            Item {
                visible: viewer._activeCellModel.length === 0
                Layout.fillWidth: true
                Layout.fillHeight: true
                Text {
                    anchors.centerIn: parent
                    text: "No active cameras selected"
                    color: AppTheme.textTertiary
                    font.pixelSize: 14
                    font.family: "Roboto Mono"
                }
            }
        }

        PlotScrubber {
            id: scrubber
            Layout.fillWidth: true
            Layout.preferredHeight: 28
            visible: viewer.scanSource !== null
            focusTarget: viewer
            fullScanDuration: viewer.liveEdgeSnapshot
            // When followLive, project the visible window onto the live
            // edge so the inset stays glued to the right; when paused
            // (panned/zoomed), reflect the user-set windowStartT.
            windowStartT: viewer.followLive
                ? Math.max(0, viewer.liveEdgeSnapshot - viewer.windowSeconds)
                : viewer.windowStartT
            windowSeconds: viewer.windowSeconds
            followLive: viewer.followLive

            onPanRequested: function(startT) {
                viewer.setWindow(startT, viewer.windowSeconds, "scrubber")
                console.info("[Plot] scrubber pan → " + startT.toFixed(2) + " s")
            }
        }
    }

    // ── Hover tooltip ──────────────────────────────────────────────────
    // Top-right floating panel: appears when cursorT is finite (hover
    // active over any cell), lists the time + per-cell primary/secondary
    // values at that time. Per-cell values are queried from the source
    // via value_at() each tick — bound to paintTick so the tooltip
    // refreshes in lockstep with the cells.
    Rectangle {
        id: hoverTooltip
        visible: isFinite(viewer.cursorT) && viewer.scanSource !== null
                  && viewer._activeCellModel.length > 0
        anchors.top: parent.top
        anchors.right: parent.right
        // When the back-to-live pill is showing in the top-right corner,
        // push the tooltip down past it (BTL height + same rim margin
        // we use between the BTL pill and the plot rim) so the two
        // don't overlap. Otherwise sit at the standard edge margin.
        anchors.topMargin: viewer._overlayEdgeMarginPx
                           + (viewer._showBackToLive
                              ? backToLiveOverlay.height + viewer._overlayMarginPx
                              : 0)
        anchors.rightMargin: viewer._overlayEdgeMarginPx
        color: AppTheme.overlayBgSolid
        border.color: AppTheme.borderSubtle
        border.width: 1
        radius: 4
        width: tooltipColumn.implicitWidth + 16
        height: tooltipColumn.implicitHeight + 12

        // Force a recompute of the rows when paintTick advances. We bind
        // to paintTick rather than cursorT so the tooltip only refreshes
        // at the throttled tick rate, not on every mousemove.
        property var _rows: {
            void viewer.paintTick  // dependency
            if (!isFinite(viewer.cursorT) || !viewer.scanSource) return []
            var t = viewer.cursorT
            var primMetric = viewer._displayPair.primary
            var secMetric = viewer._displayPair.secondary
            var primColor = viewer._traceColorForMetric(primMetric)
            var secColor = viewer._traceColorForMetric(secMetric)
            var rows = []
            for (var i = 0; i < viewer._activeCellModel.length; i++) {
                var c = viewer._activeCellModel[i]
                var pv = viewer.clampForDisplay(primMetric,
                    viewer.scanSource.value_at(c.side, c.camId, primMetric, t))
                var sv = viewer.clampForDisplay(secMetric,
                    viewer.scanSource.value_at(c.side, c.camId, secMetric, t))
                // Clinical mode uses camId=-1 for the side-averaged stream;
                // (c.camId + 1) would render "L0"/"R0" instead of the
                // cell's own "LEFT AVG" / "RIGHT AVG" label.
                var label = c.camId === -1
                    ? c.side.charAt(0).toUpperCase() + " AVG"
                    : c.side.charAt(0).toUpperCase() + (c.camId + 1)
                rows.push({
                    label: label,
                    pVal: pv, pColor: primColor,
                    sVal: sv, sColor: secColor,
                })
            }
            return rows
        }

        Column {
            id: tooltipColumn
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.margins: 8
            spacing: 1

            Text {
                text: {
                    var t = viewer.cursorT
                    if (!isFinite(t)) return ""
                    var mins = Math.floor(t / 60)
                    var secs = (t - mins * 60).toFixed(3)
                    return "t = " + mins + ":" + (secs < 10 ? "0" + secs : secs) + " s"
                }
                color: AppTheme.textSecondary
                font.pixelSize: 11
                font.family: "Roboto Mono"
                font.bold: true
            }
            Repeater {
                model: hoverTooltip._rows
                delegate: Row {
                    spacing: 6
                    Text {
                        text: modelData.label
                        width: 30
                        color: AppTheme.textSecondary
                        font.pixelSize: 10
                        font.family: "Roboto Mono"
                    }
                    Text {
                        text: isFinite(modelData.pVal) ? modelData.pVal.toFixed(2) : "—"
                        width: 50
                        horizontalAlignment: Text.AlignRight
                        color: modelData.pColor
                        font.pixelSize: 10
                        font.family: "Roboto Mono"
                    }
                    Text {
                        text: isFinite(modelData.sVal) ? modelData.sVal.toFixed(2) : "—"
                        width: 50
                        horizontalAlignment: Text.AlignRight
                        color: modelData.sColor
                        font.pixelSize: 10
                        font.family: "Roboto Mono"
                    }
                }
            }
        }
    }

    // ── Overlay margins ───────────────────────────────────────────────
    // Plot grid lives inside a ColumnLayout that's anchored fill to the
    // viewer with a 12 px outer margin. Visual rim margin we want
    // between an overlay and the plot cell edge is another 12 px. So
    // for the LEFT/RIGHT/TOP edges (anchored to viewer's edges) the
    // total margin is outer-layout-margin + rim-margin. The BOTTOM
    // edge is further inset by the scrubber area (scrubber height +
    // ColumnLayout.spacing).
    readonly property real _overlayMarginPx: 12      // rim margin (inside grid)
    readonly property real _outerLayoutMarginPx: 12  // ColumnLayout.anchors.margins
    readonly property real _scrubberAreaPx: 28 + 8   // scrubber height + Layout.spacing
    // Convenience: full edge margin from viewer.{right,top,left}.
    readonly property real _overlayEdgeMarginPx: _outerLayoutMarginPx + _overlayMarginPx
    // Convenience: full bottom margin from viewer.bottom.
    readonly property real _overlayBottomMarginPx: _outerLayoutMarginPx + _scrubberAreaPx + _overlayMarginPx

    // Signals back to the host (BloodFlow.qml) for state that's owned by
    // settingsModal — Autoscale and BFI/BVI ↔ Mean/Contrast persist in
    // app config and are also reachable from the Settings modal, so the
    // bottom-right popup switches just request changes; BloodFlow.qml
    // applies them to settingsModal which then flows back into the
    // viewer's `autoScale` / `displayMode` bindings.
    signal autoScaleToggleRequested(bool enabled)
    signal displayModeToggleRequested(bool bfiBviMode)

    // ── Top-right overlay: back-to-live ───────────────────────────────
    // Same spacing guidelines as bottom-right overlay group.
    readonly property bool _showBackToLive:
        viewer.scanSource !== null
        && (
            (viewer.scanSource.live === false && MotionInterface.liveSourceAvailable)
            || (viewer.scanSource.live === true && !viewer.followLive)
        )

    Rectangle {
        id: backToLiveOverlay
        visible: viewer._showBackToLive
        width: backToLiveText.implicitWidth + 28
        height: 36
        radius: 18
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: viewer._overlayEdgeMarginPx
        anchors.rightMargin: viewer._overlayEdgeMarginPx
        z: 6
        color: AppTheme.overlayBg
        border.color: AppTheme.accentRed
        border.width: 1

        Text {
            id: backToLiveText
            anchors.centerIn: parent
            text: (viewer.scanSource !== null
                   && viewer.scanSource.live === false
                   && MotionInterface.liveSourceAvailable)
                  ? "← Back to live scan"
                  : "● Back to live"
            color: AppTheme.accentRed
            font.pixelSize: 13
            font.family: "Roboto Mono"
        }

        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: viewer._backToLiveRequested()
        }
    }

    // ── Top-center overlay: "Viewing" scan badge (issue #245) ──────────
    // Names the replayed past scan so it's always clear which scan the plots
    // show. Past-scan only — hidden during live monitoring (live is the
    // implied default) and when nothing is loaded. The source carries the
    // identity (userLabel/dateTime), derived from the same session row the
    // History list renders, so the badge can't disagree with the clicked row.
    readonly property string _scanBadgeLabel:
        (viewer.scanSource && viewer.scanSource.userLabel) || ""
    readonly property string _scanBadgeDate: {
        var d = (viewer.scanSource && viewer.scanSource.dateTime) || ""
        // Trim "YYYY-MM-DD HH:MM:SS" → "YYYY-MM-DD HH:MM" for a tighter badge.
        return d.length >= 16 ? d.substring(0, 16) : d
    }
    readonly property string _scanBadgeText:
        viewer._scanBadgeLabel.length > 0
            ? (viewer._scanBadgeLabel + " · " + viewer._scanBadgeDate)
            : viewer._scanBadgeDate
    readonly property bool _showScanBadge:
        viewer.scanSource !== null
        && viewer.scanSource.live === false
        && viewer._scanBadgeText.length > 0

    Rectangle {
        id: scanBadge
        // No MouseArea: a bare Rectangle doesn't consume pointer events, so
        // plot interaction beneath the badge passes straight through (same
        // as the profiler HUD overlay).
        visible: viewer._showScanBadge
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.topMargin: viewer._overlayEdgeMarginPx
        z: 6
        width: scanBadgeRow.implicitWidth + 28
        height: 36
        radius: 18
        color: AppTheme.overlayBg
        border.color: AppTheme.borderSubtle
        border.width: 1

        Row {
            id: scanBadgeRow
            anchors.centerIn: parent
            spacing: 8
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Viewing"
                color: AppTheme.textTertiary
                font.pixelSize: 12
                font.family: "Roboto Mono"
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: viewer._scanBadgeText
                color: AppTheme.textPrimary
                font.pixelSize: 13
                font.weight: Font.DemiBold
                font.family: "Roboto Mono"
            }
        }
    }

    // ── Bottom-right overlay: window-seconds pill + three-dot menu ────
    // Window-seconds pill shows the current zoom and opens a dropdown
    // menu of the canonical zoom options when clicked. The three-dot
    // button to its right opens a popup with switches for display mode,
    // autoscale, and (dev-only) profiler.
    Row {
        id: bottomRightOverlay
        visible: viewer.scanSource !== null
        spacing: 8
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.rightMargin: viewer._overlayEdgeMarginPx
        anchors.bottomMargin: viewer._overlayBottomMarginPx
        z: 6

        Rectangle {
            id: windowSecondsPill
            width: windowSecondsText.implicitWidth + 38
            height: 36
            radius: 18
            color: AppTheme.overlayBg
            border.color: AppTheme.borderSubtle
            border.width: 1

            Text {
                id: windowSecondsText
                anchors.left: parent.left
                anchors.leftMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                text: viewer._labelForWindowSeconds(viewer.windowSeconds)
                color: AppTheme.textPrimary
                font.pixelSize: 13
                font.family: "Roboto Mono"
            }
            Text {
                anchors.right: parent.right
                anchors.rightMargin: 12
                anchors.verticalCenter: parent.verticalCenter
                text: "▾"
                color: AppTheme.textTertiary
                font.pixelSize: 12
            }

            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: windowSecondsMenu.popup(windowSecondsPill, 0, windowSecondsPill.height + 4)
            }

            Menu {
                id: windowSecondsMenu
                // Match the bottom-right settings popup's visual style
                // — same translucent dark card, subtle border, rounded
                // corners — so the two corner overlays read as parts
                // of the same control system. Keeping the default
                // MenuItem contentItem (instead of overriding it) so
                // implicitWidth resolves correctly; a custom Text
                // contentItem leaves the menu zero-width and the items
                // un-clickable.
                padding: 6
                implicitWidth: 120
                background: Rectangle {
                    color: AppTheme.overlayBgSolid
                    border.color: AppTheme.borderSubtle
                    border.width: 1
                    radius: 8
                }
                Repeater {
                    model: viewer._windowOptions
                    MenuItem {
                        text: modelData.label
                        font.family: "Roboto Mono"
                        font.pixelSize: 13
                        onTriggered: viewer._windowSecondsRequested(modelData.value)
                    }
                }
            }
        }

        Rectangle {
            id: settingsMenuButton
            // In clinical mode the popup's only rows (display mode +
            // autoscale) are hidden and Cell values is gone, leaving the
            // dev-only Profiler as the sole possible entry. Hide the
            // button entirely when it would open an empty card.
            visible: !viewer.effectiveClinical
                     || MotionInterface.appConfig.engineeringMode === true
            width: 36
            height: 36
            radius: 18
            color: settingsPopup.opened
                   ? Qt.alpha(AppTheme.accentInteractive, 0.85)
                   : AppTheme.overlayBg
            border.color: settingsPopup.opened ? AppTheme.accentInteractive : AppTheme.borderSubtle
            border.width: 1

            Text {
                anchors.centerIn: parent
                text: "⋯"
                color: settingsPopup.opened ? "white" : AppTheme.textPrimary
                font.pixelSize: 20
                font.bold: true
            }

            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: settingsPopup.opened ? settingsPopup.close() : settingsPopup.open()
            }

            // Popup opens UPWARD from the button so it doesn't cover
            // the scrubber. Width tracks the longest switch row.
            Popup {
                id: settingsPopup
                parent: settingsMenuButton
                x: settingsMenuButton.width - width
                y: -height - 6
                padding: 0
                width: popupColumn.implicitWidth + 24
                height: popupColumn.implicitHeight + 16
                modal: false
                focus: true
                closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent

                background: Rectangle {
                    color: AppTheme.overlayBgSolid
                    border.color: AppTheme.borderSubtle
                    border.width: 1
                    radius: 8
                }

                // Mini Switch styled to match SettingsModal's PillSwitch —
                // accent-background pill with white thumb when on, plain
                // background when off. Animated thumb glide on toggle.
                component PopupPillSwitch: Switch {
                    id: psCtrl
                    scale: 0.9
                    indicator: Rectangle {
                        x:      psCtrl.leftPadding
                        y:      (psCtrl.height - height) / 2
                        width:  44; height: 24; radius: 12
                        color:  psCtrl.checked ? AppTheme.accentInteractive : AppTheme.bgInput
                        border.color: psCtrl.checked ? AppTheme.accentInteractive : AppTheme.borderSoft
                        border.width: 1
                        Behavior on color { ColorAnimation { duration: 120 } }

                        Rectangle {
                            x:      psCtrl.checked ? parent.width - width - 3 : 3
                            y:      3; width: 18; height: 18; radius: 9
                            color:  "#FFFFFF"
                            Behavior on x { NumberAnimation { duration: 120 } }
                        }
                    }
                }

                contentItem: Column {
                    id: popupColumn
                    spacing: 6
                    padding: 12

                    Row {
                        // Hidden in clinical mode — that view only
                        // ever shows the BFI/BVI side averages, so the
                        // Mean/Contrast alternative isn't offered there.
                        visible: !viewer.effectiveClinical
                        spacing: 8
                        PopupPillSwitch {
                            id: bfiBviSwitch
                            checked: viewer.displayMode === "bfi_bvi"
                            onToggled: viewer.displayModeToggleRequested(checked)
                        }
                        Text {
                            anchors.verticalCenter: bfiBviSwitch.verticalCenter
                            text: bfiBviSwitch.checked ? "BFI / BVI" : "Mean / Contrast"
                            color: AppTheme.textPrimary
                            font.pixelSize: 12
                            font.family: "Roboto Mono"
                        }
                    }
                    Row {
                        // Hidden in clinical mode — autoscale is
                        // not offered there; the view always uses the
                        // configured manual bounds.
                        visible: !viewer.effectiveClinical
                        spacing: 8
                        PopupPillSwitch {
                            id: autoScaleSwitch
                            checked: viewer.autoScale
                            onToggled: viewer.autoScaleToggleRequested(checked)
                        }
                        Text {
                            anchors.verticalCenter: autoScaleSwitch.verticalCenter
                            text: "Autoscale"
                            color: AppTheme.textPrimary
                            font.pixelSize: 12
                            font.family: "Roboto Mono"
                        }
                    }
                    Row {
                        visible: !viewer.effectiveClinical
                        spacing: 8
                        PopupPillSwitch {
                            id: axisLabelsSwitch
                            checked: viewer.showAxisLabels
                            onToggled: MotionInterface.setConfig("showAxisLabels", checked)
                        }
                        Text {
                            anchors.verticalCenter: axisLabelsSwitch.verticalCenter
                            text: "Axis labels"
                            color: AppTheme.textPrimary
                            font.pixelSize: 12
                            font.family: "Roboto Mono"
                        }
                    }
                    Row {
                        visible: MotionInterface.appConfig.engineeringMode === true
                        spacing: 8
                        PopupPillSwitch {
                            id: profilerSwitch
                            checked: viewer.showProfiling
                            onToggled: viewer.showProfiling = checked
                        }
                        Text {
                            anchors.verticalCenter: profilerSwitch.verticalCenter
                            text: "Profiler"
                            color: "#4A90E2"
                            font.pixelSize: 12
                            font.family: "Roboto Mono"
                        }
                    }
                }
            }
        }
    }

    // ── Bottom-left overlay: profiler HUD ─────────────────────────────
    // Engineering-only HUD; toggled from the bottom-right settings menu's
    // Profiler switch. Renders as a static overlay anchored at the
    // bottom-left corner of the plot grid, with the same rim margin as
    // the other overlays.
    Rectangle {
        id: profileHud
        visible: MotionInterface.appConfig.engineeringMode === true
                 && viewer.showProfiling
                 && viewer.scanSource !== null
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        anchors.leftMargin: viewer._overlayEdgeMarginPx
        anchors.bottomMargin: viewer._overlayBottomMarginPx
        z: 5
        width: hudColumn.implicitWidth + 16
        height: hudColumn.implicitHeight + 12
        color: Qt.rgba(0.10, 0.10, 0.12, 0.85)
        border.color: "#4A90E2"
        border.width: 1
        radius: 4

        Column {
            id: hudColumn
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.margins: 8
            spacing: 1

            Text {
                text: "PROFILE"
                color: "#4A90E2"
                font.pixelSize: 11
                font.family: "Roboto Mono"
                font.bold: true
            }
            Text {
                text: "rate    " + viewer.profSampleRateHz.toFixed(1) + " Hz"
                color: AppTheme.textSecondary
                font.pixelSize: 10
                font.family: "Roboto Mono"
            }
            Text {
                text: "tick    " + viewer.profPaintTickMsAvg.toFixed(2) + " ms"
                color: AppTheme.textSecondary
                font.pixelSize: 10
                font.family: "Roboto Mono"
            }
            Text {
                text: "canvas  " + viewer.profCanvasMsAvg.toFixed(2) + " ms avg"
                color: AppTheme.textSecondary
                font.pixelSize: 10
                font.family: "Roboto Mono"
            }
            Text {
                text: "points  " + viewer.profPointsLastTick
                color: AppTheme.textSecondary
                font.pixelSize: 10
                font.family: "Roboto Mono"
            }
        }
    }
}
