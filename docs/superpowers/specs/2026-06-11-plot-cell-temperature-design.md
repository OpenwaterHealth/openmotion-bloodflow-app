# Per-camera temperature in plot cells (dev mode, orange)

**Issue:** [#165](https://github.com/OpenwaterHealth/openmotion-bloodflow-app/issues/165) — "add the temperatures to the top right corner of the embedded plots for each sensor in developer mode in orange"

**Interpretation (confirmed with Ethan):** "each sensor" = each camera. In developer
mode the embedded plot is a grid of per-camera PlotCells; each cell shows its own
camera's latest temperature in that cell's top-right corner, in orange.

## Data plumbing (Python)

- `data_sources.py` — `LiveScanSource.append_uncorrected` gains an optional
  `temp: Optional[float] = None` parameter, appended under metric key `"temp"`
  when non-None. Identical pattern to the existing optional `mean` / `contrast`
  parameters: None means "metric not available for this sample", and no buffer
  is created until the first non-None value arrives.
- `motion_connector.py` — `_LivePlotSink.consume` already extracts
  `temp_c = float(batch.temperature_c[i, side_idx, cam_id])` per frame. Pass
  `temp=temp_c` to `append_uncorrected` for **light frames with finite temp_c
  only**; dark frames pass None (dark-frame temperature readings are not
  meaningful for display, per the existing temperature-alert comment).

The temperature then flows to QML through the existing
`ScanDataSource.value_at(side, cam_id, "temp", t)` slot — no new signals,
no new QML-facing API.

## QML display

- `components/PlotCell.qml` — new `property bool showTemperature: false`.
  A `Text` anchored to the cell's top-right corner (8 px margins, Roboto Mono
  10 px — matching the existing top-left value labels), colored
  `theme.readableInk(theme.accentOrange)` (#E67E22, guarded for light-mode
  legibility). Text reads
  `source.value_at(side, camId, "temp", liveEdgeSnapshot)` with a
  `void cell.paintTick` dependency so it refreshes at the throttled ~30 Hz
  in lockstep with the BFI/BVI labels. Format: `54.3°C` (one decimal).
  Hidden when:
  - `showTemperature` is false, or
  - the value is non-finite (covers past scans, reduced-mode cam_id=-1
    average cells, and pre-first-sample), or
  - the cell is narrower than 80 px (same rule as the value labels).
- `components/PlotViewer.qml` — the grid delegate binds
  `showTemperature: MotionInterface.appConfig.developerMode === true`.
  This is the issue's "developer mode" gate; clinical (reduced-mode) cells
  additionally never have a temp stream, so nothing can leak there.

## Out of scope (YAGNI)

- Temperature row in the hover tooltip.
- Temperature on past-scan replay (the corrected CSV carries `temp_l1..temp_r8`
  columns if this is ever wanted; `_bucketize_session_rows` /
  `_load_corrected_csv_into` are the hook points).
- Any new config flag — the existing `developerMode` gates the feature.
- Plotting temperature as a trace.

## Tests

Extend `tests/test_live_plot_sink.py` and `tests/test_data_sources.py`:

- `append_uncorrected(temp=...)` stores the value under metric `"temp"` and
  `value_at` returns it.
- `append_uncorrected(temp=None)` creates no `"temp"` buffer.
- `_LivePlotSink.consume` appends temp for light frames and skips it for dark
  frames.

## Verification

Run the app with `cameraFakeData: true` in `config/app_config.json` and
screenshot the plot grid — confirm the orange temperature readout appears in
each cell's top-right corner in developer mode and disappears when
`developerMode` is false.
