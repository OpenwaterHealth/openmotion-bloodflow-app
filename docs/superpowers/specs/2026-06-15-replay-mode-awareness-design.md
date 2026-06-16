# Replay Mode Awareness — Design

**Date:** 2026-06-15
**Status:** Approved for planning
**Components:** `data_sources.py`, `components/PlotViewer.qml`, `components/HistoryModal.qml`
**Rides on:** the History data-management branch (`claude/determined-kalam-a1d23d`, PR #194)

## Problem

The plot viewer's reduced-vs-per-camera **display mode** follows the live app config
(`reducedMode`), not the mode a loaded past scan was *recorded* in. A reduced-mode scan stores
its data under `cam_id = -1` (the side average); a per-camera/dev scan stores `cam_id = 0..7`. So
loading a reduced scan while the app is in dev mode (or vice-versa) renders a blank/empty plot —
the viewer queries cells that have no data.

This is the same class of mismatch that [#175](https://github.com/OpenwaterHealth/openmotion-bloodflow-app/pull/190)
fixed for camera *masks* (replay uses the scan's own config, not the live selection); it was
simply never extended to the reduced/dev mode itself.

Two complementary fixes:

- **A. The viewer adopts the loaded scan's recorded mode** (both directions), reverting to the
  live config on "Back to live".
- **B. In reduced mode, History hides scans not shot in reduced mode** — so a clinician in the
  reduced clinical view never sees or loads an engineering/dev scan they can't render.

Together they cover every case: reduced app shows only reduced scans (always renderable);
dev app shows everything, and a reduced scan loaded in dev mode renders correctly via A.

## Part A — Viewer adopts the loaded scan's recorded mode

### Decisions

- **Scope: plot viewer only.** Loading a scan changes only how the plot area renders. The sidebar,
  settings access, and `BloodFlow.qml` page layout stay on the live config. (User decision.)
- **Temporary.** The effective mode is tied to the active scan source, so it reverts to the live
  config automatically when the viewer returns to the live source ("Back to live").
- **Detection source: the loaded buffer layout**, not `session_meta`. What the viewer can actually
  render is determined by which `cam_id`s are present; deriving from the buffers guarantees the
  render mode matches renderable data (and is self-consistent with how #175 derives masks). For
  well-formed scans this is identical to `session_meta.sdk_flags.reduced_mode`.

### `data_sources.py`

Add a tri-state indicator on `ScanDataSource`, exposed exactly like `leftMask`/`rightMask`:

- New `pyqtProperty(int, constant=True) reducedMode` backed by `self._reduced_mode`, default `-1`.
  Values: `-1` unknown, `0` per-camera, `1` reduced.
- `LiveScanSource` leaves it `-1` (unknown → viewer follows the live config; no behavior change).
- `PastScanSource.__init__` sets `self._reduced_mode` from its loaded buffers, alongside the
  existing `_derive_masks_from_buffers` call, via a new module helper:

  ```python
  def _derive_reduced_from_buffers(buffers) -> int:
      """-1 unknown / 0 per-camera / 1 reduced, from which cam_ids a loaded
      scan carries. Reduced scans store only the cam_id=-1 side average;
      per-camera scans store cam_id 0..7. Mirrors _derive_masks_from_buffers."""
      if buffers_are_empty(buffers):
          return -1
      if any(0 <= key[1] < 8 for key in buffers):
          return 0
      if any(key[1] == -1 for key in buffers):
          return 1
      return -1
  ```

### `components/PlotViewer.qml`

- Add `readonly property bool effectiveReduced` resolving the source's mode with a fallback,
  mirroring the `_effectiveLeftMask` resolve at lines 168–174:

  ```qml
  readonly property bool effectiveReduced:
      (viewer.scanSource && viewer.scanSource.reducedMode !== undefined
       && viewer.scanSource.reducedMode >= 0)
          ? (viewer.scanSource.reducedMode === 1)
          : viewer.reducedMode
  ```

- Swap the viewer's **internal rendering/visibility** reads of `viewer.reducedMode` over to
  `viewer.effectiveReduced`. Known sites (verify exhaustively during implementation):
  `showCellValues` (72), `_activeCellModel` (221), the reduced side-readout footer `visible` (649),
  the cell grid `columns` (674), and the `!reducedMode`-gated toolbar/menu items (1017, 1096, 1115,
  1131). Leave the external **input** property `reducedMode` (68) and its `BloodFlow.qml` binding
  untouched — it remains the live-config fallback.

No connector and no `BloodFlow.qml` changes. Masks already follow the source (#175); with
`effectiveReduced` driving the cell model, replay now fully self-describes its layout.

## Part B — Reduced mode hides non-reduced scans in History

### Decision

One-directional: **when the app is in reduced mode, the History table omits any scan whose recorded
mode is not reduced.** Dev mode lists everything (reduced scans included, rendered correctly by
Part A). A legacy scan with no recorded mode is treated as non-reduced, so it is hidden in reduced
mode — the conservative choice for the clinical view. (User-confirmed.)

### `components/HistoryModal.qml`

The row map from `get_scan_sessions()` already carries `reducedMode`
(`_session_to_row` reads `session_meta.sdk_flags.reduced_mode`), so no connector change.

In `rebuildView()`, add one AND-condition to the existing filter chain (search + Config dropdown):
when the app is in reduced mode, drop rows where `reducedMode` is falsy. The app's reduced flag is
read from `MotionInterface.appConfig.reducedMode` (the same source `BloodFlow.qml` uses), reactive
via `appConfigChanged`. The existing empty-state ("No scans match the filter.") covers the case
where the filter removes everything.

## Error handling

- Unknown source mode (`-1`, e.g. a live source or an empty past scan) → viewer falls back to the
  live config, i.e. today's behavior. No blank-plot regression for empty scans (already guarded by
  `buffers_are_empty` in the load path).
- Part A and Part B both key off the scan's recorded mode; for well-formed scans the buffer-derived
  value (Part A) and `session_meta.reduced_mode` (Part B) agree. A malformed scan where they
  disagree is a pre-existing data defect, not introduced here.

## Testing

- **Unit (`tests/test_data_sources.py`):** `PastScanSource(preloaded_buffers=…).reducedMode` returns
  `1` for cam_id=-1-only buffers, `0` for per-camera buffers, `-1` for empty; and
  `_derive_reduced_from_buffers` directly for the three cases.
- **Manual:** launch against two seed DBs (one reduced `cam_id=-1` scan, one per-camera `cam_id=0..7`
  scan) and confirm: (1) in dev mode, loading the reduced scan renders the reduced panels and
  loading the per-camera scan renders the grid; (2) "Back to live" reverts to the config mode;
  (3) in reduced mode, the dev scan is absent from the History list.

## Out of scope

- Hiding reduced scans while in dev mode (only the reduced-mode direction was requested).
- Switching any UI outside the plot viewer (sidebar/settings/page layout stay on live config).
- Persisting any mode change to `app_config.json`.
