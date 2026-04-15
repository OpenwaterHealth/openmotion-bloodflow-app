# Contact Quality Checking — Design

**Date:** 2026-04-15
**Branches:** `feature/contact-quality` (bloodflow-app), `feature/contact-quality` (SDK, after rename from `feature/contact-qualtity`)
**Scope:** SDK (`openmotion-sdk`) + Bloodflow App (`openmotion-bloodflow-app`)

## Overview

Add a "contact quality" assessment to OpenMOTION. The same algorithm runs in two contexts:

1. **On-demand quick-check** — a new sidebar button kicks off a 1 s acquisition (no live UI data) and pops a notification modal with the result.
2. **Live monitoring** — the algorithm runs continuously during normal scans and, on detecting a problem, pops the same notification modal with **Stop scan** / **Continue** actions.

The algorithm flags two conditions per camera:
- **Ambient light warning** — laser-off ("dark") frame mean exceeds an ambient-light threshold (suggests stray light leaking into the sensor).
- **Poor sensor contact warning** — laser-on ("light") frame mean stays below a contact threshold for ≥6 consecutive frames (suggests the laser/sensor is not coupled to the patient).

## Requirements

- Per-camera evaluation across all attached cameras (up to 16: 8 per sensor module).
- Quick-check uses camera mask `0xFF` on both sensor modules — every camera, regardless of the user's normal-scan camera selection.
- Thresholds are expressed as **background-subtracted** (pedestal-removed) values in `ContactQuality.py`. Comparisons add the current `PEDESTAL_HEIGHT` at evaluation time so future pedestal changes do not require re-tuning the constants.
- Initial values: `DARK_MEAN_THRESHOLD_DN = 10`, `LIGHT_MEAN_THRESHOLD_DN = 30`, `LOW_LIGHT_CONSEC_FRAMES = 6`.
- Comparisons operate on **raw** (uncorrected) per-frame means — not the pedestal-subtracted display means.
- Only one notification modal is ever on screen. Repeat warnings of the same `(camera, type)` pair within a single scan do not stack — they appear as one entry; new `(camera, type)` pairs append rows.

## Architecture

### SDK (`openmotion-sdk`)

**New module:** `omotion/ContactQuality.py`

- Threshold constants (background-subtracted, with pedestal noted in comments).
- `ContactQualityWarning` dataclass: `camera_id: int`, `warning_type: Enum{AMBIENT_LIGHT, POOR_CONTACT}`, `value: float`, `frame_index: int`.
- `ContactQualityMonitor` — stateful, per-camera:
  - `update(camera_id, raw_dark_mean, raw_light_mean, frame_index) -> list[ContactQualityWarning]`
  - Tracks per-camera `low_light_streak` and per-camera latches for each warning type.
  - Imports `PEDESTAL_HEIGHT` from `MotionProcessing.py`; comparison is `raw_dark_mean > DARK_MEAN_THRESHOLD_DN + PEDESTAL_HEIGHT`, etc.
  - Latch clears when condition is false for `LOW_LIGHT_CONSEC_FRAMES` frames so the warning can re-fire later in a long scan.

**Pipeline hook (`MotionProcessing.py` / `ScanWorkflow.py`):**

- Capture raw `u1` per camera before pedestal subtraction.
- Tag each row as dark/light using existing laser-state metadata.
- Feed `(camera_id, raw_dark_mean, raw_light_mean)` tuples to a registered `ContactQualityMonitor` instance.
- Surface emitted warnings via a callback / queue that the app layer subscribes to.

**New convenience method:** `MOTIONInterface.run_contact_quality_check(duration_s: float = 1.0) -> ContactQualityResult`

- Runs a 1 s acquisition with camera mask `0xFF` on both sensors.
- Does not emit live data to normal scan consumers.
- Returns `(ok: bool, warnings: list[ContactQualityWarning])`.

### Bloodflow App (`openmotion-bloodflow-app`)

**New component:** `components/ContactQualityModal.qml` — large custom modal with three states:

- **Checking** — spinner + "Checking contact quality…" (used during quick-check).
- **Result-OK** — green check + "Good signal quality" (quick-check success).
- **Result-Warnings** — red/amber header + accumulating list of `<camera_id> — <warning text>` rows. During a normal scan, footer shows **Stop scan** / **Continue**. During a quick-check, footer shows **Dismiss**.

Modal singleton: only one instance. New warnings append rows to the existing instance rather than opening a second modal.

**Sidebar button (`components/SidebarMenu.qml`):**

- New entry at index 2: label "Check", signal-bars / stethoscope icon (`\uf012` or similar from existing icon font).
- Disabled while a normal scan is running.

**Connector (`motion_connector.py`):**

- Slot: `runContactQualityCheck()` → opens modal in **Checking**, calls SDK, transitions to result state.
- Signal: `contactQualityWarning(camera_id, warning_type, value)` — fires during normal scans whenever a new warning is emitted.
- Signal: `contactQualityCheckFinished(ok, warnings)` — fires when quick-check completes.
- Hooks the SDK callback during normal scans so live warnings drive the modal.

## Data Flow

**Per-frame (shared by both contexts):**

1. Histogram frames arrive via sensor USB stream → `MotionProcessing` parses each row.
2. Raw `u1` (mean) captured per camera before pedestal subtraction; tagged dark/light by laser state.
3. `(camera_id, raw_dark_mean, raw_light_mean)` fed to `ContactQualityMonitor.update()`.
4. Monitor logic per camera:
   - `raw_dark_mean > DARK_MEAN_THRESHOLD_DN + PEDESTAL_HEIGHT` and not latched → emit `AMBIENT_LIGHT`, latch.
   - Increment `low_light_streak` if `raw_light_mean < LIGHT_MEAN_THRESHOLD_DN + PEDESTAL_HEIGHT`, else reset. If streak ≥ `LOW_LIGHT_CONSEC_FRAMES` and not latched → emit `POOR_CONTACT`, latch.
   - Latch clears when condition is false for `LOW_LIGHT_CONSEC_FRAMES` frames.
5. Warnings emitted to registered callback.

**Quick-check:**

- Sidebar **Check** click → connector opens modal in **Checking** state, calls `MOTIONInterface.run_contact_quality_check(1.0)`.
- SDK runs 1 s scan (~40 frames) with camera mask `0xFF` on both sensors, feeds frames through monitor, returns aggregated result.
- Connector emits `contactQualityCheckFinished(ok, warnings)` → modal transitions to **Result-OK** or **Result-Warnings** with **Dismiss** footer.

**Normal scan:**

- Existing scan workflow runs unchanged; monitor is attached and forwards warnings live via Qt signal.
- First warning opens the modal in **Result-Warnings** with **Stop scan** / **Continue** footer.
- Further warnings append rows; duplicate `(camera, type)` pairs are suppressed by the modal.
- **Stop scan** invokes existing scan-stop path in connector. Modal persists until user dismisses.

## Edge Cases

- Quick-check invoked while a normal scan is running → button disabled.
- Monitor receives frames before laser state is known → skip until first valid dark/light pairing.
- Camera disconnected mid-scan → its streak state and latches reset; no spurious warnings on reconnect.
- Modal already open when new warning arrives → append row; do not stack a second modal.
- SDK quick-check hardware failure → modal shows error state with message and **Dismiss**.

## Testing

**SDK (`openmotion-sdk`):**

- Unit tests for `ContactQualityMonitor` covering:
  - Threshold-with-pedestal arithmetic.
  - Latch / re-arm behavior across frame sequences.
  - Consecutive-frame counting (boundary at `LOW_LIGHT_CONSEC_FRAMES`).
  - Per-camera state independence.
  - Pedestal-change test: changing `PEDESTAL_HEIGHT` shifts comparison thresholds correctly without re-tuning constants.

**Bloodflow App:**

- Manual smoke tests:
  - Quick-check with good signal → modal shows **Result-OK**.
  - Quick-check with covered/uncovered sensors → modal shows expected warnings per camera.
  - Forced mid-scan warning to verify modal append behavior, single-instance enforcement, and **Stop scan** / **Continue** actions.
- No QML automated tests.

## Out of Scope

- Persisting contact-quality history to disk.
- Per-camera threshold overrides (single global threshold pair for now).
- Auto-recovery actions (e.g. retrying acquisition) — modal is informational only.
- Test-app integration — feature is bloodflow-app only at this stage.
