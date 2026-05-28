# Realtime Plot Viewer — Session Status

**Branch:** `feature/realtime-plot-viewer` (bloodflow-app) + `feature/side-avg-nanmean` (openmotion-sdk)
**PR:** #142 (bloodflow-app). Companion SDK PR not yet opened.
**Last updated:** 2026-05-27

## What's Shipped (Most Recent First)

### openmotion-bloodflow-app
- `4ed0db3` — fix: define logger in data_sources (DB-tail NameError crash on first lazy-load)
- `ed6fa74` — log History modal button presses (console.warn diagnostics)
- `2dad750` — configurable live cache size (liveCacheMaxSeconds, default 1800 s)
- `0e43556` — fix: live plot 1 Hz regression from DB-tail guard (ring_trimmed flag)
- `cd35103` — corrected CSV opt-in: writeCorrectedCsv config flag (default False); DB is the store
- `28c759a` — **Phase 3 lazy-load**: LiveScanSource DB tail — pan past the in-memory window loads from DB
- `5c1f23d` — **Phase 3 swap**: useNewPlotViewer default flipped to true. Legacy Loaders stay as 1-release fallback.
- `258e0df` — **Phase 4**: hide matplotlib popouts in HistoryModal when new viewer is on
- `ddf760a` — docs: session status log
- `fa51bda` — docs: note SDK-side junk-frame filter in cell label
- `fdda357` — clearer loadPastScan diagnostic (source=db/csv/db-only-sentinel)
- `6d5b0f6` — PastScanSource reads per-cam from session_data (skips CSV fallback when DB has per-cam BFI)
- `1982eae` — past-scan replay from per-cam CSV + popup switch styling (PillSwitch from SettingsModal)
- `81d1465` — plot UI restructure: corner overlays + decimation tuning (stride floor 2→1, window = stride not stride*3)
- `54d3a2e` — profile HUD + decimation morph fix (time-keyed stride, not n_window-keyed)
- `adf22e5` — pinch-zoom on PlotCell for touchscreen
- `da8edae` — keyboard shortcuts (←→ pan, ↑↓ +/- zoom, Home/End, 0 reset, Esc)
- `b1dd074` — viewer .live invisible to QML + tooltip label "L AVG"
- `66f673f` — scrubber crash + off-edge pan
- `56a6980` — reduced-mode traces missing — drop strict NaN filter
- Earlier: Phase 1 data_sources + Phase 2a-c PlotViewer / toolbar / scrubber / crosshair / tooltip / dropout / past-scan replay

### openmotion-sdk (`feature/side-avg-nanmean` branch) — PR openmotion-sdk#61
- `d5ff501` — suppress "Mean of empty slice" warning in SideAveragingStage
- `83a78bf` — gate corrected CSV behind write_corrected; forced on when no scan DB configured
- `11f9cc7` — BfiBviStage sanity excludes exact upper extreme (BFI=10.0) — fixes task #50 last-frame junk
- `8fccedc` — BfiBviStage NaN-filters values outside [-2, 12] sanity range
- `c043782` — iter_session_data accepts optional t_lo/t_hi time range (Phase 3 prereq)
- `ef6e46d` — ScanDBSink writes per-cam BFI/BVI to session_data on "live" channel
- `2c1360b` — SideAveragingStage uses nanmean (fixes reduced-mode side-avg cells empty)

## Architectural Decisions

- **Phase 1+2+3 MVP done.** Per-cam BFI/BVI now lives in `session_data` during live scans; PastScanSource reads it directly from DB. Live source bounded at 30 min via existing `_MAX_CAPACITY` ring-trim.
- **Corner overlays replace top toolbar.** Top-right: back-to-live pill. Bottom-right: window-seconds pill + `⋯` settings menu (display mode, autoscale, profiler [dev-only]). Bottom-left: profiler HUD when toggled on.
- **Settings is source of truth for autoScale + displayMode.** Overlay popup writes back via signals; settingsModal owns persisted state. Bound from BloodFlow.qml.
- **Time-keyed decimation stride** (not n_window-keyed) — eliminates "trace morphs every few seconds" artifact at wide zoom.
- **Stride floor = 1 + window = stride** for the smoothing kernel. Earlier `max(2, …) × 3` over-filtered the 5 s zoom; now raw samples render at tight zoom, Nyquist-minimum smoothing kicks in once decimation is actually needed.
- **CSV fallback for old scans.** Pre-Phase-1 scans don't have per-cam BFI rows in DB; PastScanSource falls through to the per-cam corrected CSV (`{scan_id}.csv`, 82 columns, always written by CsvSink regardless of `writeRawCsv`).
- **Live cache default = 60 s** (`1c4be27`). Since the DB tail is verified, hold only 60 s in memory (~1 MB/buffer vs ~37 MB at 30 min); deep history serves from the DB. Configurable via `liveCacheMaxSeconds`. Watch-item: DB query on deep pan-back is synchronous on the QML thread — raise the value if it ever stutters.
- **loadPastScan diagnostic = KEEP** (decided 2026-05-28). The `[Plot] loaded past scan … source=db/csv` info line stays — it's a useful operational breadcrumb (which scan, from where, how many samples), not a debug hack.

## Open Items

### Bugs / Polish
- ~~**Task #50**: junk values in last frame at scan stop~~ **DONE** in SDK `11f9cc7`. BfiBviStage now NaN's BFI/BVI outside `[-2, 10)` (lower inclusive for legit BFI=0 occlusion readings, upper exclusive for the formula's degenerate extreme). Verified: scan 111 has 0 BFI=10.0 rows (was 7 in scan 110), cell labels show clean values like `BFI -0.18 BVI 4.79`.

### Spec phases not yet done
- ~~**Phase 3 full lazy-load**~~ **DONE + HARDWARE-VERIFIED** (`28c759a` + `4ed0db3`). LiveScanSource falls through to a transient DB window when `t_lo < buffer's oldest in-memory timestamp`. Verified 2026-05-28 via `liveCacheMaxSeconds: 60` test config: a 7-min scan ring-trimmed, pan-back logged `DB tail engaged (session_id=120)`, no crash. **Two bugs found+fixed during verification:** (1) `data_sources.py` had no `logger` defined → NameError on first DB-tail use (`4ed0db3`); (2) SDK `side_avg.py` "Mean of empty slice" warning spam (`d5ff501`). `liveCacheMaxSeconds` is a new config knob (default 1800 s) added to shrink the cache for this kind of testing.
- ~~**Phase 3 swap to default**~~ **DONE** in `5c1f23d`. Legacy Loaders stay as 1-release fallback per spec.
- ~~**Phase 4 cleanup**~~ **DONE** in `258e0df`. All matplotlib popouts now gate on `useNewPlotViewer !== true`.
- **Phase 5 cleanup**: delete `EmbeddedRealtimePlot.qml`, `ReducedPlotView.qml`, `PlotToolbar.qml` (dead), `processing/visualize_bloodflow.py`, legacy QML per-sample signals. After Phase 3 is stable for one release.

### Test coverage gaps
- Headless QML smoke test (needs pytest-qt; deferred)
- Lazy-load query path (when Phase 3 full lands)

## Gotchas For Next Session

- **`live` must be `@pyqtProperty(bool, constant=True)`** on ScanDataSource — plain `self.live = True` is invisible to QML and resolves to `undefined`. (Bug we already fixed; if anyone touches the class don't regress.)
- **`scrubber.top` from outside the ColumnLayout reads as 0** — Layout children's positions don't project cleanly to external anchors. Bottom overlays use `parent.bottom` with explicit `_overlayBottomMarginPx = 60` (12 outer + 28 scrubber + 8 spacing + 12 rim).
- **`writeRawCsv: false` only disables the multi-MB raw histogram CSV** (`_raw.csv`). The per-cam corrected CSV (`{scan_id}.csv`, 82 cols) is always written by `CsvSink` — that's how matplotlib popout works regardless of the setting.
- **User reverts `config/app_config.json` frequently** — never include it in commits. Their local `useNewPlotViewer: true` toggle is for testing only.
- **Reduced mode requires 2 Start clicks**: first click runs CQ, second click ("Start Scan") begins the actual capture. HIL automation must wait for `CQ Final Compare` log line between clicks. The button label changes but the panel cache keys on the literal label so `click_panel('Start Scan')` is the right call after CQ.
- **Dev mode is single-click Start.**
- **Once a scan is running, `click_panel('Start')` still works** to stop — the button position is cached under "Start" even though the label says "Stop". The panel cache positions are by label-at-calibration-time.
- **Decimation tests bake in the formulas.** When tuning `window_decimated`, expect to update `test_camera_buffer_window_decimated_*` tests to match new stride/window math.
- **The companion SDK PR has 3 commits** — must merge first (or together) before bloodflow-app PR #142 can land. User has said they're holding the PR merges until everything is finished.

## Quick Reference — Where Things Live

- **PlotViewer.qml** — root component. Corner overlays + ColumnLayout (grid + scrubber). 940+ lines.
- **PlotCell.qml** — single trace canvas. PinchHandler for touch, MouseArea for mouse, keyboard focus on click.
- **PlotScrubber.qml** — bottom timeline. Blue inset = followLive, orange = paused, green tick = liveEdge.
- **PlotToolbar.qml** — **dead code now**, kept for one release as fallback. Can delete in Phase 5 cleanup.
- **data_sources.py** — `_CameraBuffer`, `ScanDataSource`, `LiveScanSource`, `PastScanSource`. Past now CSV-aware (`_load_corrected_csv`) + DB-aware.
- **motion_connector.py** — `MOTIONConnector` (4000+ lines). `loadPastScan` (1772), `showLiveSource` (1763), `_LivePlotSink.consume` (171).
- **openmotion-sdk/omotion/pipeline/sinks.py** — `ScanDBSink._consume_live` (Phase 1)
- **openmotion-sdk/omotion/ScanDatabase.py** — `iter_session_data` now accepts t_lo/t_hi

## How To Resume

1. Read this file first.
2. `git log feature/realtime-plot-viewer --oneline -20` to refresh the commit list.
3. `git log feature/side-avg-nanmean --oneline -10` in the SDK repo for companion changes.
4. Check task list (TaskList) for outstanding items.
5. If picking up Phase 3 full lazy-load: see `data_sources.py:_CameraBuffer._ring_trim` for the current memory cap; need to add DB-fallback path in `LiveScanSource.points_for_window` / `value_at`.
