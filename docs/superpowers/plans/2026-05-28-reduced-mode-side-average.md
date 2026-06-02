# Reduced-Mode Side Average — Implementation Plan

> **For agentic workers:** execute task-by-task. Each task is TDD (write the
> failing test first), ends in one focused commit, and keeps its repo's suite
> green. Steps use `- [ ]` checkboxes.

**Goal:** Compute the reduced-mode per-side BFI/BVI average as one pure spatial
operation, applied on two paths — the **live/realtime** path for display and the
**corrected** path for the DB/replay record — and persist the corrected average
so replay reads it back instead of re-deriving it.

**Architecture:** A pure `spatial_side_average` helper is used by (A) a live
stage that replaces `SideAveragingStage` (per-capture spatial mean of `bfi_live`
→ display only) and (B) a new corrected final-path stage (per-capture spatial
mean of the enriched corrected frames → emitted for the DB). `ScanDBSink`
persists the corrected `cam_id=-1` rows in reduced mode and writes no per-camera
rows; replay reads `cam_id=-1` straight from the DB (the derive-on-read code is
deleted). Live (realtime) and DB (corrected) intentionally differ.

**Spec:** [../specs/2026-05-28-reduced-mode-side-average-design.md](../specs/2026-05-28-reduced-mode-side-average-design.md) (rev 2)

**Tech stack:** SDK `omotion.pipeline` (Python 3.12, numpy, pytest) on
`feature/side-avg-nanmean`; app (PyQt6, pytest) on `feature/realtime-plot-viewer`.

---

## Cross-repo sequencing

Land in this order; each repo's suite stays green at every commit. The two
branches still merge together (the app's reduced-mode display depends on the
SDK live-stage output shape).

1. **SDK Phase 1** — shared helper + live stage (replaces `SideAveragingStage`).
2. **SDK Phase 2** — corrected stage + DB persistence.
3. **App Phase 3** — live sink consumes the new live-stage output; replay reads
   the DB.
4. **Integration** — round-trip test + final review.

> The live stage's output-shape change (Task 2) and the app live-sink change
> (Task 7) are a matched pair — verify them together before declaring the live
> display fixed, even though they're separate commits in separate repos.

---

## File structure

**SDK (`feature/side-avg-nanmean`):**
- `omotion/pipeline/stages/side_avg.py` — rewrite: `LiveSideAverageStage`
  (spatial per-capture). Houses the shared `spatial_side_average` helper.
- `omotion/pipeline/stages/corrected_side_avg.py` — new: `CorrectedSideAverageStage`.
- `omotion/pipeline/batch.py` — no new event type needed (Stage B reuses
  `LiveEmit(channel="final_side", …)`); confirm during Task 4.
- `omotion/pipeline/factory.py` — swap stage A, add stage B.
- `omotion/pipeline/sinks.py` — `ScanDBSink`: consume `final_side`, reduced-mode
  gate on `_consume_live`, fix stale `_consume_final`.
- `tests/test_pipeline/…`

**App (`feature/realtime-plot-viewer`):**
- `motion_connector.py` — `_LivePlotSink`.
- `data_sources.py` — remove `PastScanSource._derive_side_averages`.
- `tests/test_live_plot_sink.py`, `tests/test_data_sources.py`.

---

## SDK Phase 1 — shared helper + live stage

### Task 1: `spatial_side_average` pure helper

**Files:** Modify `omotion/pipeline/stages/side_avg.py`; Test
`tests/test_pipeline/test_side_avg_stage.py`.

- [ ] **Write the failing test.** Given per-camera BFI for one side at one
  instant (a 1-D array indexed by camera, NaN for absent), assert the nan-aware
  mean over the *selected* cameras only; unselected cameras ignored; all-NaN → NaN.
- [ ] **Run** `pytest tests/test_pipeline/test_side_avg_stage.py -k spatial -v` → FAIL.
- [ ] **Implement** a module-level pure function, e.g.
  `spatial_side_average(values_by_cam: np.ndarray, cam_indices: np.ndarray) -> float`
  using `np.nanmean` with the "Mean of empty slice" warning suppressed.
- [ ] **Run** → PASS.
- [ ] **Commit** `refactor(pipeline): extract pure spatial_side_average helper`.

### Task 2: `LiveSideAverageStage` (replaces `SideAveragingStage`)

**Files:** Rewrite `omotion/pipeline/stages/side_avg.py`; Modify
`omotion/pipeline/factory.py` and `tests/test_pipeline/test_factory.py`; Tests in
`tests/test_pipeline/test_side_avg_stage.py` (+ retire the running-average tests
in `test_side_avg_reduced_live.py`).

- [ ] **Write the failing tests** (TDD), all on realistic one-camera-per-row input:
  - per-capture spatial mean of *that capture's* cameras, emitted once per
    `frame_id` at the capture's timestamp (finite at one representative row,
    NaN elsewhere);
  - **no temporal carry** — a camera absent from a later capture contributes no
    stale value;
  - capture straddling a batch boundary → emitted once, correct value/timestamp,
    final capture flushed at `on_scan_stop`;
  - dense input still reduces to the per-row mean (back-compat).
- [ ] **Run** the stage tests → FAIL.
- [ ] **Implement** `LiveSideAverageStage` (`name = "live_side_average"`):
  per-side accumulator keyed by the in-progress `frame_id` (sum+count of finite
  per-cam BFI/BVI), fold each row, emit via `bfi_live_side`/`bvi_live_side`
  finite only at the representative row on capture change; `reset()` and
  `on_scan_stop()` flush. No carry-forward of camera values.
- [ ] Swap it into `default_pipeline` where `SideAveragingStage` was; update the
  `test_factory` expected stage-name list (`side_averaging` → `live_side_average`).
- [ ] **Run** `pytest tests/test_pipeline/ -q` (deselect the USB-hardware smoke) → PASS.
- [ ] **Commit** `feat(pipeline): LiveSideAverageStage — pure per-capture spatial average`.

---

## SDK Phase 2 — corrected stage + persistence

### Task 3: `CorrectedSideAverageStage` (final-path)

**Files:** Create `omotion/pipeline/stages/corrected_side_avg.py`; Test
`tests/test_pipeline/test_corrected_side_avg_stage.py`.

- [ ] **Write the failing test.** Feed per-`(side,cam)` `EnrichedCorrectedInterval`
  events (via `batch.events`) for one dark-bounded window across several cameras;
  assert the stage emits, per side, the per-timestamp spatial mean across the
  active cameras (using `spatial_side_average`), and flushes at `on_scan_stop`.
- [ ] **Run** → FAIL.
- [ ] **Implement** the stage: scan `batch.events` for `IntervalClosed` carrying an
  `EnrichedCorrectedInterval`; gather enriched frames per side keyed by the
  interval bounds `(left_abs, right_abs)`; once all active cameras for that window
  are present (or at `on_scan_stop`), group frames by timestamp, average BFI/BVI
  (and mean/contrast) across cameras, and append
  `LiveEmit(channel="final_side", payload=<side-aggregated corrected series>)`.
- [ ] **Run** → PASS.
- [ ] **Commit** `feat(pipeline): CorrectedSideAverageStage — per-side corrected average`.

### Task 4: Wire Stage B into the pipeline

**Files:** Modify `omotion/pipeline/factory.py`, `tests/test_pipeline/test_factory.py`.

- [ ] **Write/extend** `test_factory` to expect `corrected_side_average` in the
  stage list (after `DarkCorrectionStage`, gated on `reduced_mode`).
- [ ] **Run** → FAIL.
- [ ] **Implement:** add `CorrectedSideAverageStage(enabled=metadata.reduced_mode, …)`
  after the dark-correction stage so it sees the emitted `IntervalClosed` events.
  Confirm `LiveEmit(channel="final_side")` routes with no runner change (the
  runner already dispatches `LiveEmit` by channel).
- [ ] **Run** `pytest tests/test_pipeline/ -q` → PASS.
- [ ] **Commit** `feat(pipeline): wire CorrectedSideAverageStage into default_pipeline`.

### Task 5: `ScanDBSink` — persist corrected average, reduced-mode gate

**Files:** Modify `omotion/pipeline/sinks.py`; Test
`tests/test_pipeline/test_sinks_db.py` (or the existing DB-sink test).

- [ ] **Write the failing tests:**
  - reduced mode: a `final_side` payload writes `cam_id=-1` rows (bfi/bvi/mean/
    contrast, side 0/1); `_consume_live` writes **no** per-camera rows;
  - normal mode: per-camera live rows still written; no side rows;
  - the stale `_consume_final` no longer drops per-camera bfi/bvi to None
    (either it's superseded by `final_side` or fixed — assert the corrected
    side rows carry real bfi/bvi).
- [ ] **Run** → FAIL.
- [ ] **Implement:** add `"final_side"` to `ScanDBSink.channels`; `_consume_side`
  writes `cam_id=-1` corrected rows. Gate `_consume_live` on `self._meta.reduced_mode`
  to skip per-camera writes in reduced mode. Remove/replace the stale
  `_consume_final` placeholder.
- [ ] **Run** `pytest tests/test_pipeline/ -q` → PASS.
- [ ] **Commit** `feat(sdk): ScanDBSink persists corrected side average; reduced mode = average only`.

---

## App Phase 3 — live display + replay

### Task 6: `_LivePlotSink` consumes the live-stage output

**Files:** Modify `motion_connector.py`; Modify `tests/test_live_plot_sink.py`.

- [ ] **Update the tests:** `cam_id=-1` appended from the finite
  `bfi_live_side`/`bvi_live_side` samples (one per capture); the per-`frame_id`
  dedup added earlier is removed (the stage owns it); per-camera buffers + the
  dropout-watchdog heartbeat are still updated in reduced mode.
- [ ] **Run** `pytest tests/test_live_plot_sink.py -q` → FAIL.
- [ ] **Implement:** append `cam_id=-1` wherever `bfi_live_side[i,side]` is finite;
  drop `_last_side_avg_fid`; leave the per-camera append/heartbeat path intact.
- [ ] **Run** → PASS.
- [ ] **Commit** `fix: live sink reads per-capture side average from the stage`.

### Task 7: Replay reads the DB; remove the derive

**Files:** Modify `data_sources.py`; Modify `tests/test_data_sources.py`.

- [ ] **Update the tests:** `PastScanSource` and the `LiveScanSource` DB-tail
  return the stored `cam_id=-1` series directly (a fixture with `cam_id=-1` rows);
  assert no derivation runs (the derive method is gone). Remove the old
  merge-derive tests.
- [ ] **Run** `pytest tests/test_data_sources.py -q` → FAIL (derive removed).
- [ ] **Implement:** delete `PastScanSource._derive_side_averages` and its call;
  `_bucketize_session_rows` already buckets `cam_id=-1` into `(side,-1,bfi/bvi)`.
- [ ] **Run** → PASS.
- [ ] **Commit** `fix: replay reads stored cam_id=-1 average; drop derive-on-read`.

---

## Integration

### Task 8: Round-trip + final review

**Files:** Test `openmotion-sdk/tests/test_pipeline/test_reduced_roundtrip.py`.

- [ ] **Write the test:** run a synthetic reduced-mode scan through
  `default_pipeline` + `ScanDBSink`; read the `cam_id=-1` series back from the DB;
  assert it equals **Stage B's corrected output** sample-for-sample (NOT the live
  display — those differ by design).
- [ ] **Run** → PASS.
- [ ] **Final review:** full SDK pipeline suite + app `test_data_sources.py` /
  `test_live_plot_sink.py` green; grep for dead references to
  `SideAveragingStage` / `_derive_side_averages`; confirm `default_pipeline`
  stage order; update the realtime-plot-viewer status doc.
- [ ] **Commit** `test(pipeline): reduced-mode round-trip — stored == corrected average`.

---

## Self-review checks (run before calling it done)

- Old reduced scans (no stored `cam_id=-1`) replay empty — expected (BETA); not a regression to fix here.
- Normal-mode per-camera rows still realtime (corrected-per-cam persistence is a separate follow-up — do not scope-creep into it).
- No `app_config.json` commits; no scan-output artifacts staged.
- SDK and app changes reviewed together before any merge (they're a matched pair).
