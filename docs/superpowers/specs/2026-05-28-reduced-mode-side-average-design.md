# Reduced-Mode Side Average — Design Spec

**Date:** 2026-05-28
**Status:** Proposed (review before implementation) — rev 2
**Branches:** SDK `feature/side-avg-nanmean` · app `feature/realtime-plot-viewer` (must merge together)
**Related:** [2026-05-22-realtime-plot-viewer-design.md](./2026-05-22-realtime-plot-viewer-design.md)

---

## Problem

In reduced (clinical) mode the viewer shows one averaged BFI/BVI trace per side
(`cam_id = -1`). Today that value is reconstructed in several places that have
drifted apart, and the reconstruction conflates two different ideas of
"average." Two clarifications drive this rev:

### Two averaging axes — keep them separate

- **Spatial** — average the *selected cameras* of one side at a **single
  capture instant** → one left value, one right value. This is the only
  averaging the reduced-mode side trace needs.
- **Temporal (rolling / windowed)** — smooth one stream over *time*. There is
  **no shared rolling-average endpoint or stage** in the sink pipeline (the
  monolith's `on_rolling_avg_fn` / `rolling_avg_*` is gone). Temporal averaging
  is **owned by each consumer**: `ContactQualityWorkflow` keeps its own
  per-`(side,cam)` rolling window over light-frame means; `CalibrationWorkflow`
  (calibration *and* test) does its own windowed average over the corrected
  stream; the viewer does display-time mean-binning. None of these is touched by
  this design — the reduced-mode **spatial** average is a separate concern.

The `SideAveragingStage` patch made this session computes a *running* average —
it holds each camera's latest value and averages those, which mixes cameras from
**different instants** whenever they report at slightly different times. That is
a temporal smear leaking into what must be a pure spatial average. The clean
operation uses only the cameras of the **same** capture (same `frame_id` /
timestamp), with no carry-forward.

### Two data paths — live (realtime) vs corrected

The pipeline produces BFI/BVI twice:

- **Live / realtime** — `bfi_live` / `bvi_live`, computed from the realtime dark
  estimator (`mean_dc_rt` / `contrast_sn_rt`) + calibration, per frame. Immediate,
  best-effort. Drives the live display.
- **Corrected** — when a dark interval closes, `DarkCorrectionStage._enrich_corrected_frame`
  recomputes each camera's BFI/BVI from the **interpolated** dark baseline +
  shot-noise + calibration, producing `EnrichedCorrectedFrame(side, cam_id, mean,
  std, contrast, bfi, bvi)`. Accurate, retroactive. This is the "actual
  dark-frame-corrected" data.

The **live display should use the live/realtime average; the DB/CSV record
should use the corrected average.** They are intentionally different — the
stored record is the more-accurate one. Today neither is done cleanly:
`ScanDBSink._consume_final` receives the enriched corrected frames but **throws
the per-camera BFI/BVI away** (writes `cam_id=-1, side=0, bfi=None`, keeping only
mean/contrast — there's a stale "PR 3 will carry per-cam data" comment). So the
corrected BFI/BVI is computed and discarded at persistence, and the DB instead
gets re-derived realtime values.

## Root cause

The side average is computed-on-read in multiple ways, the realtime/corrected
paths are conflated at the sink, and the spatial average accidentally carries a
temporal component. Fix: **define one pure spatial-average operation, apply it
to each path explicitly, compute it once per path, and persist the corrected
one.**

## Decisions (locked with stakeholder)

1. **Spatial side-average is one pure operation** — average the selected
   cameras at a single capture instant. No carry-forward, no temporal element.
   Drop the running-average behavior.
2. **Live and DB differ by design** — live display = realtime side average; DB /
   replay = corrected side average.
3. **The corrected side-average is built in a dedicated final-path stage** (not
   in the sink), since the corrected path emits per-`(side, cam)` intervals that
   must be gathered across cameras.
4. **Replace `SideAveragingStage`** with the live spatial-average stage.
5. **Reduced mode persists the average only** — the corrected `cam_id=-1` rows;
   no per-camera BFI/BVI rows.
6. **Per-camera live values keep flowing to the live sink in memory** (NOT
   NaN-ed) so the camera-dropout watchdog still sees each camera.

---

## Design

### Shared operation: `spatial_side_average`

A pure helper (no state, no time): given the per-camera BFI/BVI for the active
cameras of one side at one capture instant, return the (nan-aware) mean. Used by
both stages so there is exactly one definition of "side average."

### Stage A — live spatial average (display) — replaces `SideAveragingStage`

- Placement unchanged: after `BfiBviStage` + `DarkFrameHoldStage`, gated on
  `metadata.reduced_mode`.
- Input: `bfi_live` / `bvi_live` (`(n, 2, 8)`, one camera finite per row).
- Groups the rows of each capture by `frame_id` (rows of one capture are
  contiguous; a tiny accumulator carries the in-progress capture across a batch
  boundary), applies `spatial_side_average`, and emits **one (left, right) per
  capture** as `LiveEmit(channel="live_side", SideAverageSample)` — flushed at
  `on_scan_stop`. No carry-forward of camera values across captures.
  *(As built: a dedicated event, not the originally-specced finite-at-row
  `bfi_live_side` array — the array can't flush the final capture through the
  zero-row `on_scan_stop` batch, and the event is symmetric with Stage B. The
  `bfi_live_side`/`bvi_live_side` FrameBatch fields were removed.)*
- Consumed by the app `_LivePlotSink` → in-memory `cam_id=-1` buffer (realtime
  display). **Not persisted.**

> The accumulator here is *only* streaming plumbing to gather a capture's
> cameras (they arrive as separate one-camera rows). It resets every capture —
> it is not a temporal average.

### Stage B — corrected spatial average (DB / replay) — NEW, final-path

- Consumes the per-`(side, cam)` `EnrichedCorrectedInterval` events
  (`IntervalClosed`) on the final path.
- Gathers, per side, the enriched frames across the active cameras of the same
  interval (all cameras of a side share the same dark-bounded `(left_abs,
  right_abs)`), groups them by timestamp, and applies `spatial_side_average` →
  a **side-aggregated corrected series** (one left/right value per capture
  timestamp). Flushes the final interval at `on_scan_stop`.
- Emits `LiveEmit(channel="final_side", SideAverageSample)` per capture that
  `ScanDBSink` writes verbatim. Purely spatial (group-by-timestamp) — the
  cross-camera/-event gathering is plumbing, like Stage A's accumulator.

### Persistence — `ScanDBSink`

- **Reduced mode:** the `"final_side"` channel writes Stage B's corrected output
  as `cam_id=-1` rows (bfi, bvi, mean, contrast; side 0/1), buffered like the raw
  rows. `_consume_live` writes **nothing** (the live side average is display-
  only). Net: `session_data` holds corrected `cam_id=-1` rows only.
- **Normal mode:** unchanged — `_consume_live` writes per-camera realtime rows.
  (Persisting *corrected* per-camera in normal mode is a natural follow-up, out
  of scope here.)
- The stale `"final"`-channel `_consume_final` (NULL-bfi placeholders) was
  removed; ScanDBSink no longer subscribes to `"final"`.

### Live view — app `_LivePlotSink`

- Subscribes to `{"live", "live_side"}`; appends `cam_id=-1` from the
  `live_side` `SideAverageSample` (the stage owns the per-capture dedup).
- **Keep** appending per-camera values to in-memory buffers and updating the
  dropout-watchdog heartbeat. Reduced mode just doesn't render per-camera cells.

### Replay — app `data_sources.py`

- **Remove** `PastScanSource._derive_side_averages` and the merge logic.
  `_bucketize_session_rows` already buckets `cam_id=-1` rows into
  `(side, -1, bfi/bvi)`, so `PastScanSource` and the `LiveScanSource` DB-tail get
  the **corrected** side average straight from the DB. The empty-pan-back bug
  disappears with no extra code. Replay now shows the corrected trace (more
  accurate than the live realtime trace was — expected, per decision #2).

### Dropout watchdog (why per-camera live values stay alive)

`_LivePlotSink` uses per-camera BFI arrival as the dropout heartbeat, and that
update sits *after* the `isfinite(bfi)` guard. NaN-ing per-camera BFI would make
every camera look dropped. Hence decision #6: leave per-camera `bfi_live` /
`bvi_live` untouched; only the *new* `cam_id=-1` averages are the reduced-mode
display/persist paths.

### Disposition of the running-average commit

The `SideAveragingStage` running-average (SDK `69d573e`) is superseded by Stage
A and will be replaced, not separately reverted — leaving it in place until the
replacement lands avoids re-introducing the live jumpiness in the interim.

---

## Data flow (reduced mode, after this change)

| Source | Path | Spatial avg | Consumer |
|---|---|---|---|
| `bfi_live` (realtime) | live | **Stage A**, per capture | in-memory `cam_id=-1` → live display |
| `EnrichedCorrectedFrame` (corrected) | final | **Stage B**, per capture | side-agg payload → `ScanDBSink` `cam_id=-1` → replay |
| per-camera `bfi_live` | live | — | in-memory buffers (dropout watchdog only) |

Two streams, two intentionally-different averages, each computed once.

---

## Test plan

The dense-batch unit tests hid the original bug because they never fed the
realistic one-camera-per-row layout. The plan closes that and pins the
spatial-only and live-vs-corrected guarantees.

1. **`spatial_side_average` unit test** — pure-function: correct nan-aware mean
   over selected cameras; ignores unselected cameras; all-NaN → NaN.
2. **Stage A, realistic one-camera-per-row** — feed a capture's cameras as
   consecutive one-cam rows; assert the emitted side value is the spatial mean of
   *that capture's* cameras, emitted **once per capture** (~40 Hz), at the
   capture's timestamp. Explicitly assert **no temporal carry**: a camera missing
   from a later capture does not contribute a stale value.
3. **Stage A straddle** — split a capture across two batches; emitted once, right
   value/timestamp; final capture flushed at `on_scan_stop`.
4. **Stage B gather-across-cameras** — feed per-`(side, cam)` enriched intervals
   for one window; assert the side-aggregated series is the per-timestamp spatial
   mean across the active cameras, flushed at scan stop.
5. **Round-trip: stored == corrected-path average** — run a synthetic reduced
   scan through pipeline + `ScanDBSink`; read the `cam_id=-1` series back; assert
   it equals **Stage B's corrected output** (NOT the live display — those differ
   by design).
6. **DB persistence** — reduced mode writes corrected `cam_id=-1` rows and **no**
   per-camera rows; normal mode writes per-camera and no side rows.
7. **Replay reads DB** — `PastScanSource` and the DB-tail return the stored
   `cam_id=-1` series with no derivation (assert the derive code is gone).
8. **Dropout watchdog intact** — reduced mode: per-camera heartbeats still fire
   (no spurious dropouts) even though per-camera values aren't displayed/persisted.

---

## Open questions

- **Old scans** (recorded before this change) have no stored corrected
  `cam_id=-1` average → reduced-mode replay of them would be empty. Recommend
  accepting it (BETA); a thin legacy fallback is possible but reintroduces a
  derive path.
- **Normal-mode per-camera: live vs corrected.** The corrected per-camera BFI/BVI
  is now available (enriched frames) and currently discarded. Persisting
  *corrected* per-camera rows in normal mode (instead of realtime) would make the
  whole DB record corrected, consistent with the reduced-mode decision. Proposed
  as a follow-up, not part of this change.
- **Mean/Contrast in reduced mode** aren't displayed (switch hidden) and so
  aren't separately surfaced; Stage B can still carry corrected mean/contrast on
  the `cam_id=-1` rows for completeness.
