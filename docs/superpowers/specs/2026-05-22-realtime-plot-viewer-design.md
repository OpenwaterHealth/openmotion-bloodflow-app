# Real-time Plot Viewer Redesign — Design Spec

**Date:** 2026-05-22
**Issue:** _(none yet — file when this work is queued)_
**Feature:** Replace the bloodflow app's two real-time plot widgets (`EmbeddedRealtimePlot.qml`, `ReducedPlotView.qml`) and the matplotlib-popout-based history visualization (`processing/visualize_bloodflow.py`) with one unified, Saleae-Logic-inspired in-app viewer. Adds DVR-style scrollback during live scans, mouse-driven pan/zoom, a synced crosshair tooltip across all camera cells, and on-demand replay of any past scan from the existing `ScanDatabase` — all without leaving the BloodFlow page.

---

## Background

Today's bloodflow app has three separate paths for looking at BFI/BVI data:

1. **Live, developer mode** — `components/EmbeddedRealtimePlot.qml` (825 LOC). A `Canvas`-based per-camera grid (4×2 or 4×4) of BFI/BVI traces over a hard 15-second sliding window. The window prunes old samples; nothing pre-15s is reachable without leaving the page. The plot is feature-rich (per-cell dropout markers, dev-mode profiling HUD, optional mean/contrast view, autoscale modes) but coupled tightly to the live signal stream.
2. **Live, reduced (clinical) mode** — `components/ReducedPlotView.qml` (402 LOC). A second `Canvas`-based plot of per-side averages over a 15-second window. Different code, same scroll-prevention.
3. **Past data** — `components/HistoryModal.qml` (418 LOC). A modal scan-picker whose action buttons call `MOTIONInterface.visualize_*()` slots in `motion_connector.py` (lines 3138-3251), which in turn launch external **matplotlib** windows via `processing/visualize_bloodflow.py`. The matplotlib window is non-interactive in the app's UX sense — separate window, separate styling, separate set of axes.

The current 15-second wall hurts clinical and engineering use equally: an operator who notices something interesting cannot scroll back to it without stopping the scan; reviewing yesterday's recording means a matplotlib popout. The SDK-side data pipeline rearchitecture (see [openmotion-sdk @ feature/data-pipeline-tweaks](../../../../openmotion-sdk/docs/superpowers/specs/2026-05-22-data-pipeline-rearchitecture-design.md)) is paving the way to a `ScanDBSink` that already populates `session_data` rows on disk — i.e. every scan we run now has a queryable, fully-corrected record on disk. That backing store is what makes in-viewer past-scan replay feasible.

---

## Requirements

| # | Requirement |
|---|-------------|
| R1  | One viewer component renders both the per-camera (developer-mode) grid and the per-side averaged (reduced-mode) layout. No second plot component. |
| R2  | During an active scan the operator can pan and zoom in time without stopping the scan ("DVR scrollback"). New live data continues to accumulate; only the visible window pauses on the timestamp the operator has pinned. |
| R3  | A persistent **"Back to live"** affordance restores the visible window to track the live edge. Default at scan start is `followLive = true`. |
| R4  | Any pan, wheel-zoom, scrubber drag, or scrubber click during a live scan breaks `followLive`. There is no implicit auto-resume — only the explicit button restores it. |
| R5  | Hover anywhere in the viewer renders a **synced vertical crosshair** at the same timestamp on every cell, plus a single tooltip listing each visible camera's BFI/BVI (or μ/C in mean/contrast mode) at that timestamp and frame ID. The tooltip auto-flips at the viewport edge. |
| R6  | The time axis is shared across all cells (Saleae-style). Pan or zoom in any cell moves all cells. Y-axes remain independent. |
| R7  | A scan picker (the existing HistoryModal, no popout) lets the operator load any past scan in the local `ScanDatabase` into the same viewer. Loading clears the previous past-scan source and reassigns the viewer; the live source, if any, keeps streaming in the background. |
| R8  | The viewer supports a clean roundtrip past → live: the **"Back to live"** button reassigns to the in-progress live source when one exists, or is hidden when one does not. |
| R9  | The viewer queries the SDK's `ScanDatabase` directly for past scans (via `iter_session_data(session_id)`). It does not invoke the SDK pipeline. No matplotlib. |
| R10 | At least the following scan size is supported in memory without UI stutter: **30 minutes × 40 Hz × 16 cameras × 4 metrics** (~30 MB of `float32` samples). |
| R11 | Live data is **append-only into a Python-side numpy buffer**. The QML side does not maintain a parallel growing JS array. JS-side per-frame allocation does not occur. |
| R12 | The corrected-batch backfill semantics from today (`_on_corrected_batch` overwrites in-place at matching frame IDs) are preserved verbatim. The viewer sees both passes without any special handling. |
| R13 | Per-camera dropout markers from today (`markDroppedOut(side, camId, timeStr)`) are preserved. The marker is rendered as a vertical bar at the timestamp the dropout was first detected. |
| R14 | Rendering performance: pan/zoom is interactive (≤16 ms per gesture frame at 16 cells × 30 min of data). Achieved by binary-search index lookup + stride decimation to ≤ 2 × cell-pixel-width visible points. |
| R15 | Aesthetics: the viewer stays within today's `AppTheme.qml` palette. No global theme change. Improvements are limited to grid lines, axis labels with proper tick spacing, antialiased strokes, hover highlights, consistent typography. |
| R16 | The legacy popout matplotlib path (`processing/visualize_bloodflow.py`, `MOTIONInterface.visualize_bloodflow/visualize_corrected/visualize_corrected_signal`) is removed after the new viewer reaches stable in-tree status. |
| R17 | The migration is reversible at any phase via a single `app_config.json` flag (`useNewPlotViewer`, default `false` while building, flipped to `true` at Phase 3). |

---

## Architecture

### One viewer, one data abstraction, two data sources

```
                ┌──────────────────────┐
                │   PlotViewer.qml     │   pan / zoom / crosshair
                │                      │   tooltip / follow-live
                │ ─ pure UI state ─    │   are all here
                └──────────┬───────────┘
                           │ subscribes
                           ▼
                ┌──────────────────────┐
                │   ScanDataSource     │   numpy buffers, per-(side, cam, metric)
                │   (Python QObject)   │   emits samplesAppended(side, cam, …)
                └──────┬───────────┬───┘
                       │           │
            ┌──────────▼──┐    ┌───▼──────────────┐
            │ LiveScan    │    │ PastScanSource   │
            │ Source      │    │ — queries SDK    │
            │ — adapts    │    │   ScanDatabase   │
            │   today's   │    │   .iter_session_ │
            │   connector │    │   data(sid)      │
            │   callbacks │    │                  │
            └─────────────┘    └──────────────────┘
```

- **The viewer is dumb.** It renders whatever the source gives it and owns view-state only: `windowStartT`, `windowSeconds`, `followLive`, `cursorT` (for hover). No data accumulation.
- **The source owns the data.** Append-only `numpy.ndarray` buffers, one pair `(t, v)` per `(side, cam_id, metric)`. Resized in-place (`numpy.resize`) when the per-buffer capacity is exceeded. Throttled change signal at ~10 Hz so the viewer's paint cadence is bounded by paint cost, not by sample rate.
- **`motion_connector` owns source lifecycle.** It exposes `currentScanSource` as a `pyqtProperty`. Scan-start constructs a fresh `LiveScanSource` and assigns it. Switching scans is one property assignment. The live source keeps streaming in the background while a past source is currentl displayed; "Back to live" is one assignment back.

### The viewer is mostly decoupled from the SDK pipeline rework

The SDK is mid-rearchitecture (see [`openmotion-sdk feature/data-pipeline-tweaks`](../../../../openmotion-sdk/docs/superpowers/specs/2026-05-22-data-pipeline-rearchitecture-design.md)). For this viewer:

- **Past-scan replay** reads `ScanDatabase.iter_session_data(session_id)`. That API existed before the rearchitecture and isn't part of the rework — `session_data` rows are the corrected output already, no pipeline involvement at view time.
- **Live data** hooks `motion_connector`'s existing `_on_uncorrected` / `_on_corrected_batch` / `_on_temp_sample` callbacks. When SDK PR 3 swaps `ScanWorkflow` to the channel/sink protocol, only the `LiveScanSource` adapter changes — the viewer doesn't.

The viewer work ships independently of the pipeline PRs.

---

## Components and files

### New (Python)

- `data_sources.py` (new, top-level next to `motion_connector.py`)
  - `class ScanDataSource(QObject)` — base. Owns the per-`(side, cam_id, metric)` numpy arrays, `metadata`, `liveEdge`, `live` flag, and the throttled `samplesAppended(side: str, cam_id: int, metric: str, addedCount: int)` signal.
  - `class LiveScanSource(ScanDataSource)` — constructed at scan start. Methods: `append_uncorrected(side, cam_id, frame_id, t, bfi, bvi, mean, contrast)`, `append_temperature(side, cam_id, temp)`, `apply_corrected_batch(corrected_batch)` (overwrite-in-place at matching frame IDs), `mark_dropped(side, cam_id, t)`.
  - `class PastScanSource(ScanDataSource)` — constructed from a `session_id`. Reads via `omotion.ScanDatabase.iter_session_data(session_id)`, bucketizes into the same numpy layout, sets `live = False` and `liveEdge = duration`.
  - `class _CameraBuffer` — internal helper. Holds `t: np.ndarray`, `v: np.ndarray`, `n: int` (high-water mark), `dropped_at: Optional[float]`. Method `append(t, v)` doubles capacity on overflow. Method `window_indices(t_lo, t_hi)` returns `(i_lo, i_hi)` via `np.searchsorted` (binary search, O(log n)).

### New (QML)

- `components/PlotViewer.qml` — top-level. Subscribes to `MOTIONInterface.currentScanSource`. Owns view-state. Lays out cells in a `GridLayout`. Captures wheel / drag / hover at the viewport level. Holds the global crosshair overlay (`Item`, not part of any `Canvas`) and the tooltip popup.
- `components/PlotCell.qml` — single trace `Canvas`. Pure render. Receives `windowStartT`, `windowSeconds`, `bounds`, `series` reference. Repaints only on data growth (signaled by viewer) and on view-state change.
- `components/PlotToolbar.qml` — top bar: scan label, **"● Back to live"** button (visible per R8), `Window: 15s ▾` dropdown (values: `5s, 15s, 30s, 1m, 5m, Full scan`), auto-scale toggle (single global toggle; the existing per-plot autoscale variant is dropped — defer reintroducing if anyone misses it), metric switcher (BFI/BVI ↔ μ/C in dev mode), "Open scan…" button → opens HistoryModal.
- `components/PlotScrubber.qml` — bottom timeline showing the full scan extent with the visible window as a draggable inset rectangle.

### Modified

- `motion_connector.py`
  - Add `currentScanSource: ScanDataSource` as a `pyqtProperty` with a notify signal.
  - At scan start, construct a fresh `LiveScanSource` and assign to `currentScanSource`. Wire the existing `_on_uncorrected` / `_on_corrected_batch` / `_on_temp_sample` / dropout callbacks into the new source's `append_*` methods *in addition to* today's QML signals during Phase 1. By Phase 3, the QML signals are unwired.
  - Add `@pyqtSlot(int) def loadPastScan(self, session_id: int) -> None`. Runs the `ScanDatabase` query on a `QThread` (HistoryModal's existing "Processing…" overlay covers the wait). Assigns the resulting `PastScanSource` to `currentScanSource`.
- `pages/BloodFlow.qml`
  - Replace the `EmbeddedRealtimePlot` / `ReducedPlotView` conditional with a single `PlotViewer { reducedMode: MOTIONInterface.appConfig.reducedMode }`.
- `components/HistoryModal.qml`
  - Add one new primary action button: **"View in plot →"**. `onClicked`: `MOTIONInterface.loadPastScan(selected.sessionId); root.close()`.
  - Hide the legacy "Visualize BFI/BVI (legacy)" / "Visualize Contrast/Mean (legacy)" / "Visualize BFI/BVI" / "Visualize Contrast/Mean" buttons behind `appConfig.developerMode && !appConfig.useNewPlotViewer` for the bridge releases; remove entirely in Phase 5.

### Deprecated, removed in Phase 5

- `components/EmbeddedRealtimePlot.qml` (825 LOC)
- `components/ReducedPlotView.qml` (402 LOC)
- `processing/visualize_bloodflow.py`
- `MOTIONInterface.visualize_bloodflow`, `.visualize_corrected`, `.visualize_corrected_signal` slots and their matplotlib imports
- Legacy QML signals on `MOTIONInterface` for per-sample BFI/BVI/mean/contrast emission (after Phase 3 confirms no other consumer)

---

## Data flow — live

```
SDK ScanWorkflow callbacks
   _on_uncorrected(sample)  ─┐
   _on_corrected_batch(...) ─┼─► motion_connector  ─►  LiveScanSource.append_*()
   _on_temp_sample(...)     ─┘                          │
                                                        │  numpy array
                                                        ▼  append, batched
                                          buffer[(side,cam,metric)]:
                                              t  = np.empty(64k); n_t = 0
                                              v  = np.empty(64k); n_v = 0
                                                        │
                                          every 100 ms  │  emits
                                                        ▼
                                          samplesAppended(side, camId, metric, added)
                                                        │
                                                        ▼
                                          PlotViewer.requestRepaint()
```

- **Per-(side, cam, metric) buffer grows in place.** Initial capacity 64k slots (≈25 min @ 40 Hz). Double on overflow. No per-sample Python allocation.
- **Corrected-batch overwrites in place.** `apply_corrected_batch(batch)` looks up each `(side, cam_id, frame_id)` in a small `dict[frame_id → index]` side-table (kept on the buffer) and rewrites the value. Preserves today's "live shows best-effort, corrected backfills" behavior verbatim. No new signal class — the viewer sees the rewritten values on the next repaint.
- **Throttled UI notify.** The source coalesces appends into a single `samplesAppended` emission at most every 100 ms even if 40 frames arrived in that window. Repaint is paint-cost-bounded, not data-rate-bounded.
- **Dropout marker is data, not a separate signal.** `LiveScanSource.mark_dropped(side, cam_id, t)` sets `buffer.dropped_at`. The viewer renders a vertical bar at that timestamp the next time the cell repaints. Replaces today's separate `markDroppedOut` JS call.

## Data flow — past

```
HistoryModal              motion_connector             SDK ScanDatabase
─────────                 ───────────────              ───────────────
[View in plot]  ─click─► loadPastScan(scanId)
                              │ (QThread)
                              │
                              │  iter_session_data(session_id)
                              │
                              ▼─────────────────────────►   indexed
                                                            on
                                                            (session_id,
                              ◄─── rows ────────────────────ts)
                              │
                              │  build PastScanSource:
                              │    arrays_per_(side,cam,metric)
                              │
                              │  currentScanSource = past
                              ▼
                          PlotViewer    (reactive — same code
                          property      path as live; only
                          change)       difference is `live=False`)
```

- **One SQL read per scan.** `iter_session_data` is an indexed range scan; a 30-min scan = ~1.15M rows × 7 cols. Bucketing into numpy should be sub-second on modern hardware. If measured timing exceeds 250 ms, the load runs on a `QThread` with HistoryModal's existing "Processing…" overlay.
- **Past-source metadata fills the toolbar.** `scan_id`, subject label, start time, duration, masks. The bloodflow app already serializes the scan's `reducedMode` and side masks into the `sessions.session_meta` JSON blob (see `MOTIONInterface.get_scan_details`). `PastScanSource` reads those first; if absent for older DBs, it falls back to inferring the masks from which `(side, cam_id)` keys actually appear in `session_data`.
- **Side encoding mismatch.** `session_data.side` is `INTEGER (0/1)` while `session_raw.side` is `TEXT ('left'/'right')`. `PastScanSource` normalizes to `'left'/'right'` internally so its buffer keys match `LiveScanSource`'s.
- **Memory eviction is simple.** When `currentScanSource` is reassigned, the previous source loses its only reference. Python GC reclaims. No tile cache, no LRU — at ~30 MB ceiling there is no payoff and YAGNI applies.
- **Switching back to live.** `currentScanSource = liveScanSource` if `liveScanSource is not None`. The live source kept streaming and accumulating in the background while past was shown.

---

## Interaction model

```
┌─────────────────────────────────────────────────────────────────┐
│  Toolbar:  [● Live]  ⌚ Window: 15s ▾   📐 Auto-scale  μ/C ↔ BFI │
├─────────────────────────────────────────────────────────────────┤
│   L-1    │    L-2    │    R-1    │    R-2                       │
│ ─╱╲──┼─  │ ──╲╱┼───  │ ─╱─╲┼──   │ ──╲╱┼──    ◄ synced crosshair│
│      ┊   │       ┊   │       ┊   │       ┊                      │
│ ──────────────── tooltip: t=02:14.325, frame 5373 ─────────────  │
│ ──────────────── L-1 BFI 4.62 BVI 3.18 │ L-2 BFI 4.71 BVI 3.22 ─ │
│   …                                                              │
├─────────────────────────────────────────────────────────────────┤
│ [██████████░░░░░░░░░] ◀──── full scan: 0:00 ──── 04:32 ────▶    │
│                          ↑ visible window (draggable)            │
└─────────────────────────────────────────────────────────────────┘
```

### Cursor + tooltip (always sync'd across all cells)

- **Hover** → vertical crosshair at cursor x on every cell. One tooltip near the cursor lists each visible camera's BFI/BVI (or μ/C in dev-mode mean/contrast) at the cursor timestamp + the per-side frame ID. Tooltip auto-flips at the viewport edge.
- **Cursor leaves the viewer** → crosshair + tooltip clear.

### Pan + zoom (also sync'd)

- **Mouse wheel** on any cell → zoom time-axis around cursor x. Y-axis fixed (or fit-visible per toolbar toggle).
- **Click-drag horizontal** → pan time. Y-axis untouched.
- **Double-click on a cell** → fit that cell's Y to the visible-time window.
- **`Shift + double-click`** → fit Y on every cell.
- **Scrubber drag** → moves the visible window across the full scan extent.
- **Scrubber always reflects the full scan.** During a live scan the scrubber's full-extent indicator grows in real-time even when the visible window is pinned — so the operator can see how far behind the live edge they have wandered.

### Follow-live

- `followLive: bool` lives on the viewer. `true` at scan start.
- While `true`, the visible window's right edge tracks `currentScanSource.liveEdge` at the existing 10 Hz repaint tick. Window length is `windowSeconds` (toolbar dropdown, default 15 s).
- Any pan, wheel-zoom, scrubber drag, or scrubber click sets `followLive = false`. No implicit auto-resume.
- **"● Back to live"** button (toolbar): clicked → sets `followLive = true` and snaps the window to `[liveEdge - windowSeconds, liveEdge]`. Visible when `followLive == false && currentScanSource is liveScanSource`.
- When a *past* source is current, the button reads **"Back to live →"** and reassigns `currentScanSource = liveScanSource` (and sets `followLive = true`). Hidden when no live scan exists.

### Keyboard (active when the viewer has focus)

- `← / →` pan by 1 s. `Shift+←/→` pan by `windowSeconds`.
- `+ / -` zoom in / out. `0` resets to default `windowSeconds`.
- `Home` jump to scan start. `End` jump to scan end (past mode) or live edge (live mode, also sets `followLive = true`).
- `Esc` clear viewer focus.

### Explicitly NOT in v1

- Box-select / range-zoom rectangle (Saleae has this; defer).
- User annotations / markers (defer; needs a DB column).
- Two scans overlaid for comparison (the chosen picker model is one at a time).

---

## Rendering + performance

- **Stay with QML `Canvas`.** It is what the current plots use, it is already tuned (dev-mode profiler proves performance is workable), and "targeted polish on the existing theme" does not justify a renderer swap.
- **Decimate at draw time.** Per cell, per paint: ask the source for `window_indices(side, cam, metric, t_lo, t_hi) → (i_lo, i_hi)` (binary search, O(log n) in Python). Then stride to ≤ 2 × cell-pixel-width samples. Worst case (30-min scan zoomed out, 200-px-wide cell): ~400 pts drawn instead of 72,000. Zoomed in (1 s window): all 40 pts drawn. Loss is below pixel resolution.
- **Index lookup happens in Python** on the source, not in QML/JS. Keeps the JS side trivial — just receives an index range plus a typed-array view of values to draw.
- **Crosshair + tooltip are separate QML `Item`s**, not part of the `Canvas` paint. Cursor movement repositions the overlay; no canvas repaint per hover frame. Cells repaint only on data growth and on view-state change.
- **Paint cadence stays at the current 10 Hz tick** during live. On pan/zoom it fires once per gesture frame (the gesture's `onPositionChanged` debounced to one repaint per ~16 ms via `Qt.callLater`).
- **Antialiased strokes** with `ctx.lineWidth = 1.5` plus integer-pixel snapping for grid lines. The visible polish win is mostly clean grid + crisp ticks + theme-aligned axis labels, not the trace itself.
- **Profile HUD ports forward.** The dev-mode `_profSampleRateHz`, `_profRenderMs`, `_profCanvasMsAvg`, `_profTotalPoints` counters from `EmbeddedRealtimePlot.qml` move into `PlotViewer.qml`. Visible only with `developerMode && showProfiling`.

---

## Error handling

| Failure | Where | Response |
|---|---|---|
| **DB read fails** (file gone, schema mismatch, corrupt row) | `PastScanSource.__init__` raises | `motion_connector.loadPastScan` catches, emits the existing `errorOccurred(msg)` signal. HistoryModal already binds it to a message dialog. `currentScanSource` is left untouched. |
| **Empty scan / no `session_data` rows** | `PastScanSource.__init__` | Build an empty source with `metadata.duration = 0`. Viewer renders a centered "No corrected samples in this scan" placeholder instead of an empty grid. |
| **Live signal arrives with NaN / non-finite value** | `LiveScanSource.append_*` | Store NaN. The renderer already skips non-finite (`isFinite()` guards in current code). Tooltip shows `--` for that camera at that timestamp. |
| **Live numpy buffer overflows** (scan > 25 min) | `_CameraBuffer._grow` | Double the array. One `logging.WARNING`. No user-visible behavior. |
| **Past-scan load takes > 250 ms** | `motion_connector.loadPastScan` | Run on a `QThread`. Reuse HistoryModal's "Processing…" overlay until the new `currentScanSource` is ready. Cancel button drops the load. |
| **Camera dropout mid-scan** | `LiveScanSource.mark_dropped` | Sets `buffer.dropped_at`. Viewer draws a vertical bar at that x. Past sources never set it (replay shows the full recorded stream). |
| **Window or zoom math goes out of bounds** (`t_lo > t_hi`, NaN propagation) | viewer | Clamp to source's `[0, duration]`. Don't crash. |
| **Source assignment during paint** (race: scan stop swaps to past mid-frame) | viewer | All paint reads in a cell go through a `currentScanSource` snapshot captured at paint start. The next tick picks up the new source. |

No new error categories the existing app does not already handle — this design routes them through the new source/viewer instead of through the matplotlib popout.

---

## Testing

| Layer | What | How | Hardware? |
|---|---|---|---|
| `data_sources.py` unit | Append, grow, `window_indices`, corrected-batch overwrite-in-place, dropout marker | pytest, pure Python | No |
| `PastScanSource` integration | Construct from a synthetic SQLite `session_data` (built in `tmp_path` via `sqlite3` stdlib); assert shapes, timestamps, mask inference | pytest + sqlite3 stdlib | No |
| Live wiring | `motion_connector` calls `LiveScanSource.append_*` from each existing callback exactly once per sample | Inject a fake source; assert call counts | No |
| QML smoke | `PlotViewer` constructs, switches sources, doesn't print QML warnings | Add a headless test to `tests/` that loads `BloodFlow.qml` with a stub source | No |
| Manual / HIL | Pan-during-scan, jump-to-live, crosshair sync, load-past-from-history, dropout indicator, both modes (dev grid + reduced) | Manual checklist in this spec's PR description; runs once per release on hardware | Yes |

No new hardware-only tests. The pure-software unit tests cover the bulk of the logic and run in CI without changes to the existing harness markers.

---

## Phased rollout

Each phase is one PR against `next`, independently shippable, independently reversible.

1. **Phase 1 — `ScanDataSource` Python.** Add `data_sources.py` with `ScanDataSource`, `LiveScanSource`, `PastScanSource`, `_CameraBuffer`. Wire `motion_connector` to construct & append into a `LiveScanSource` *in addition to* today's QML signals — nothing in QML consumes the source yet. Full unit + `PastScanSource` integration coverage. Apps still ship the legacy plots.
2. **Phase 2 — `PlotViewer` component, feature-flagged.** Build `PlotViewer.qml` + `PlotCell.qml` + `PlotToolbar.qml` + `PlotScrubber.qml`. Add `MOTIONInterface.loadPastScan` slot. Gate the BloodFlow.qml swap behind `app_config.json` key `useNewPlotViewer: false`. Toggle locally to dev and iterate.
3. **Phase 3 — Swap in `BloodFlow.qml`.** Replace `EmbeddedRealtimePlot` and `ReducedPlotView` with `PlotViewer { reducedMode: appConfig.reducedMode }`. Flip the flag default to `true`. Old components stay in tree as fallback.
4. **Phase 4 — HistoryModal integration.** Add "View in plot →" button; existing matplotlib buttons stay under `developerMode && !useNewPlotViewer` for one release as fallback.
5. **Phase 5 — Cleanup.** Delete `EmbeddedRealtimePlot.qml`, `ReducedPlotView.qml`, `processing/visualize_bloodflow.py`, the legacy `visualize_*` slots and matplotlib imports, the legacy QML per-sample signals (after Phase 3 confirms no other consumer), and the `useNewPlotViewer` flag. Happens after one full release where Phases 1–4 are stable.

### Risk mitigation per step

- **After Phase 1:** nothing in QML changed. Unit tests cover the new code. Legacy plots run unchanged.
- **After Phase 2:** new code exists but is dark in production builds (flag default `false`).
- **After Phase 3:** the new viewer is live by default. If clinical use surfaces a regression, flip the flag and the old plots come back. No source revert needed.
- **After Phase 4:** matplotlib popout still reachable via developerMode fallback for one release. Clinical operators never see it.
- **After Phase 5:** the legacy paths are gone. Any new bug is in the new code only.

---

## Non-goals

- The SDK pipeline rearchitecture is **not** in scope. The viewer reads from `ScanDatabase.iter_session_data` (a stable, pre-rearchitecture API) and the existing `motion_connector` callbacks. The adapter layer is the only thing that changes when SDK PR 3 lands.
- A new `AppTheme.qml` or any other change outside the viewer's own files. Aesthetic improvements are limited to grid lines, ticks, antialiased strokes, hover highlights, and consistent typography pulled from existing theme tokens.
- Marker / annotation persistence. Defer until a `session_data_annotations` table exists.
- Cross-scan overlays. Defer; the chosen picker model is single-source.
- Replacement of `HistogramView.qml`. That is a separate widget for raw histogram inspection and is out of scope here.

## Out-of-scope follow-ups

These become easier after the viewer lands but are not part of this work:

- Box-select / range-zoom rectangle (Saleae-style click-and-drag area zoom).
- User-placed timeline markers with notes (needs a DB column).
- Cross-scan overlays via tab/multi-source viewer.
- Pre-aggregated/decimated DB views for very-long-scan support (>30 min); only needed if scan duration ceiling moves up.

---

## References

- [`openmotion-bloodflow-app/components/EmbeddedRealtimePlot.qml`](../../../components/EmbeddedRealtimePlot.qml) — current developer-mode plot being replaced.
- [`openmotion-bloodflow-app/components/ReducedPlotView.qml`](../../../components/ReducedPlotView.qml) — current reduced-mode plot being replaced.
- [`openmotion-bloodflow-app/components/HistoryModal.qml`](../../../components/HistoryModal.qml) — scan picker being repurposed as the past-scan launcher.
- [`openmotion-bloodflow-app/motion_connector.py`](../../../motion_connector.py) — adds `currentScanSource` and `loadPastScan` (lines 1647, 1716 are the relevant existing live-stream callbacks).
- [`openmotion-sdk/omotion/ScanDatabase.py`](../../../../openmotion-sdk/omotion/ScanDatabase.py) — `iter_session_data(session_id)` is the past-source read path.
- [`openmotion-sdk/docs/superpowers/specs/2026-05-22-data-pipeline-rearchitecture-design.md`](../../../../openmotion-sdk/docs/superpowers/specs/2026-05-22-data-pipeline-rearchitecture-design.md) — concurrent SDK rework. The viewer is decoupled from its timeline.
- Saleae Logic 2 documentation, used as the interaction-model reference.
