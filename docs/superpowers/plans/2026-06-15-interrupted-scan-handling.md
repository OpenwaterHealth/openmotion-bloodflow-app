# Interrupted-Scan Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a scan is interrupted (e.g. the console/sensors drop off USB mid-scan), tell the user what happened and stop empty scans from masquerading as viewable history.

**Architecture:** The dark-correction stage emits corrected output per dark-bounded *interval* on the pipeline's `"final"` channel; `ScanDBSink` persists those frames. The final, still-open interval is always discarded (no terminal dark closes it), and a scan that ends before *any* interval closes persists nothing. We do **not** add a wall-clock threshold. Instead a new pure classifier turns two observable facts — how many corrected frames were persisted, and whether any terminal dark went missing — into a user-facing outcome (ok / partial / empty), shown as a toast. The History viewer guards against loading empty scans, and `ScanDBSink` deletes the session row it created when it persisted zero rows.

**Tech Stack:** Python 3.13, PyQt6 (signals/slots), pytest (`unit` marker = no app/hardware), SQLite via `omotion.ScanDatabase`. App repo `openmotion-bloodflow-app`; SDK repo `openmotion-sdk` (editable sibling).

**Why this matches the agreed strategy (no "15 seconds"):**
- *Clean early cancel* → firmware emits the terminal dark → all intervals saved → `final_frames>0`, no missing terminal dark → **ok, no alert**.
- *Disconnect after ≥1 interval closed* → closed intervals already persisted, open one discarded → `final_frames>0` + terminal dark missing → **partial, warning toast**.
- *Disconnect before any interval closed* → nothing persisted → `final_frames==0` (not canceled) → **empty, error toast + session deleted**.

---

## File Structure

**App repo (`openmotion-bloodflow-app`):**
- Create `scan_outcome.py` — pure `classify_scan_outcome()` + the `_ScanOutcomeSink` pipeline sink (no Qt; unit-testable). Keeps logic out of the already-4000-line `motion_connector.py`.
- Modify `data_sources.py` — add `buffers_are_empty()` helper.
- Modify `motion_connector.py` — wire the sink, the outcome toast signal/slot, the History `hasData` flag, and the empty-load guard.
- Modify `components/HistoryModal.qml` — show a "no data" banner and disable View/Export for empty scans.
- Create `tests/test_scan_outcome.py` — unit tests for classifier + sink.
- Modify `tests/test_data_sources.py` — unit tests for `buffers_are_empty()`.

**SDK repo (`openmotion-sdk`):**
- Modify `omotion/pipeline/sinks.py` — `ScanDBSink` tracks rows written and deletes its session if it wrote none.
- Create `tests/test_scan_db_sink_empty.py` — unit tests for the delete-empty behavior.

**Independence:** Part A (app) is self-sufficient — the toast and the History guard work even if Part B never lands (pre-existing empty sessions 10/11/12 are handled by the guard). Part B is the "truly not saved" refinement so *future* empty scans never appear in the list at all.

---

## PART A — App

### Task 1: Pure outcome classifier

**Files:**
- Create: `scan_outcome.py`
- Test: `tests/test_scan_outcome.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scan_outcome.py
import pytest

from scan_outcome import ScanOutcome, classify_scan_outcome

pytestmark = pytest.mark.unit


def test_clean_scan_no_alert():
    out = classify_scan_outcome(
        final_frames=5000, terminal_dark_missing=False,
        canceled=False, disable_laser=False,
    )
    assert out.kind == "ok"
    assert out.severity == ""
    assert out.message == ""


def test_clean_user_cancel_no_alert():
    out = classify_scan_outcome(
        final_frames=800, terminal_dark_missing=False,
        canceled=True, disable_laser=False,
    )
    assert out.kind == "ok"


def test_disconnect_after_data_is_partial_warning():
    out = classify_scan_outcome(
        final_frames=800, terminal_dark_missing=True,
        canceled=False, disable_laser=False,
    )
    assert out.kind == "partial"
    assert out.severity == "warning"
    assert "partial" in out.message.lower()


def test_disconnect_before_any_data_is_empty_error():
    out = classify_scan_outcome(
        final_frames=0, terminal_dark_missing=True,
        canceled=False, disable_laser=False,
    )
    assert out.kind == "empty"
    assert out.severity == "error"
    assert "not saved" in out.message.lower()


def test_empty_user_cancel_is_silent():
    out = classify_scan_outcome(
        final_frames=0, terminal_dark_missing=False,
        canceled=True, disable_laser=False,
    )
    assert out.kind == "skipped"
    assert out.severity == ""


def test_laser_disabled_scan_never_alerts():
    out = classify_scan_outcome(
        final_frames=0, terminal_dark_missing=True,
        canceled=False, disable_laser=True,
    )
    assert out.kind == "skipped"
    assert out.severity == ""


def test_partial_suppressed_when_user_canceled():
    # A user-initiated stop that happens to miss a terminal dark on the
    # final partial interval is still a normal cancel, not an alarm.
    out = classify_scan_outcome(
        final_frames=800, terminal_dark_missing=True,
        canceled=True, disable_laser=False,
    )
    assert out.kind == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scan_outcome.py -m unit -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scan_outcome'`

- [ ] **Step 3: Write minimal implementation**

```python
# scan_outcome.py
"""Pure scan-outcome classification + the data-channel sink that feeds it.

Extracted from motion_connector.py (already ~4000 lines) so the decision
logic is unit-testable without Qt or hardware. See
docs/superpowers/plans/2026-06-15-interrupted-scan-handling.md.

An "interval" is a dark-bounded segment the dark-correction stage emits on
the pipeline's "final" channel; ScanDBSink persists those frames. The final,
still-open interval is always discarded (no terminal dark closes it), so an
unclean shutdown loses its tail — and a scan that ends before *any* interval
closes persists nothing at all. This module turns the two facts the sink can
observe (corrected frames persisted, and whether any terminal dark was
missing) into a user-facing outcome — no wall-clock thresholds.
"""

from __future__ import annotations

from typing import NamedTuple


class ScanOutcome(NamedTuple):
    kind: str       # "ok" | "partial" | "empty" | "skipped"
    severity: str   # "warning" | "error"  ("" when no alert)
    message: str    # "" when no alert should be shown


def classify_scan_outcome(
    *,
    final_frames: int,
    terminal_dark_missing: bool,
    canceled: bool,
    disable_laser: bool,
) -> ScanOutcome:
    """Decide what (if anything) to tell the user after a scan ends.

    - disable_laser scans legitimately produce no BFI/BVI → never alert.
    - final_frames <= 0:
        canceled  → user stopped before any interval closed; not an error.
        otherwise → interrupted before any data (e.g. device disconnect);
                    nothing was saved → error.
    - final_frames > 0:
        terminal_dark_missing and not canceled → final segment could not be
            corrected and was discarded → warning (partial save).
        otherwise → clean → no alert.
    """
    if disable_laser:
        return ScanOutcome("skipped", "", "")
    if final_frames <= 0:
        if canceled:
            return ScanOutcome("skipped", "", "")
        return ScanOutcome(
            "empty", "error",
            "Scan ended unexpectedly and no data was recorded (the device "
            "may have disconnected mid-scan). This scan was not saved.",
        )
    if terminal_dark_missing and not canceled:
        return ScanOutcome(
            "partial", "warning",
            "Scan ended unexpectedly — partial data was saved. The final "
            "segment could not be dark-corrected and was discarded.",
        )
    return ScanOutcome("ok", "", "")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scan_outcome.py -m unit -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add scan_outcome.py tests/test_scan_outcome.py
git commit -m "feat: pure scan-outcome classifier for interrupted scans

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `_ScanOutcomeSink` — tally the two facts from the pipeline

**Files:**
- Modify: `scan_outcome.py`
- Test: `tests/test_scan_outcome.py`

The sink is duck-typed like the other connector sinks (`channels` / `on_scan_start` / `consume` / `on_complete`). It subscribes to `"final"` (corrected intervals — same payloads `ScanDBSink` persists) and `"diagnostics"` (integrity events, including `TerminalDarkResult`).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_scan_outcome.py
from types import SimpleNamespace

from omotion.pipeline.batch import TerminalDarkResult  # real SDK event type

from scan_outcome import _ScanOutcomeSink


def test_sink_counts_final_frames():
    sink = _ScanOutcomeSink()
    sink.on_scan_start(meta=None)
    sink.consume("final", SimpleNamespace(frames=[object(), object()]))
    sink.consume("final", SimpleNamespace(frames=[object()]))
    assert sink.final_frames == 3
    assert sink.terminal_dark_missing is False


def test_sink_flags_missing_terminal_dark():
    sink = _ScanOutcomeSink()
    sink.on_scan_start(meta=None)
    sink.consume("diagnostics", TerminalDarkResult(
        side="left", cam_id=0, abs_frame_id=339,
        u1=171.6, threshold=133.0, found=False, identified_by="content",
    ))
    assert sink.terminal_dark_missing is True


def test_sink_ignores_present_terminal_dark():
    sink = _ScanOutcomeSink()
    sink.on_scan_start(meta=None)
    sink.consume("diagnostics", TerminalDarkResult(
        side="left", cam_id=0, abs_frame_id=49,
        u1=127.3, threshold=133.0, found=True, identified_by="fsync",
    ))
    assert sink.terminal_dark_missing is False


def test_sink_resets_on_scan_start():
    sink = _ScanOutcomeSink()
    sink.final_frames = 99
    sink.terminal_dark_missing = True
    sink.on_scan_start(meta=None)
    assert sink.final_frames == 0
    assert sink.terminal_dark_missing is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scan_outcome.py -m unit -v -k sink`
Expected: FAIL — `ImportError: cannot import name '_ScanOutcomeSink'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to scan_outcome.py
class _ScanOutcomeSink:
    """Pipeline sink that tallies the two signals classify_scan_outcome needs.

    Pure Python — no Qt, no connector reference — so the completion handler
    reads .final_frames / .terminal_dark_missing directly off the instance
    after the scan.

      "final"       — corrected intervals (EnrichedCorrectedInterval). Each
                      carries .frames; summing their counts = corrected frames
                      persisted by ScanDBSink (same channel, same payloads).
      "diagnostics" — integrity events; a TerminalDarkResult with found=False
                      means a camera's terminal dark was missing/contaminated.
    """

    channels = frozenset({"final", "diagnostics"})

    def __init__(self) -> None:
        self.final_frames = 0
        self.terminal_dark_missing = False

    def on_scan_start(self, meta) -> None:
        self.final_frames = 0
        self.terminal_dark_missing = False

    def consume(self, channel: str, payload) -> None:
        if channel == "final":
            frames = getattr(payload, "frames", None)
            if frames:
                self.final_frames += len(frames)
            return
        if channel == "diagnostics":
            # Lazy-import the event type so this module loads against an SDK
            # that pre-dates TerminalDarkResult (mirrors _TriggerStateSink).
            try:
                from omotion.pipeline.batch import TerminalDarkResult
            except Exception:
                return
            if isinstance(payload, TerminalDarkResult) and not payload.found:
                self.terminal_dark_missing = True

    def on_complete(self) -> None:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scan_outcome.py -m unit -v`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add scan_outcome.py tests/test_scan_outcome.py
git commit -m "feat: _ScanOutcomeSink tallies persisted frames + missing terminal darks

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire the sink + outcome toast into the connector

**Files:**
- Modify: `motion_connector.py:43` (import)
- Modify: `motion_connector.py:465` (signal declaration)
- Modify: `motion_connector.py:~1973` (create sink before the completion closure)
- Modify: `motion_connector.py:~2033` (classify + emit inside `_on_pipeline_complete`)
- Modify: `motion_connector.py:2078-2083` (add sink to the `sinks=[...]` list)
- Modify: `motion_connector.py:~3305` (connect the new signal)
- Modify: `motion_connector.py` (new slot `_on_scan_outcome_warning`)

This task is Qt-bound (connector + live pipeline), so it is verified by reproduction rather than a unit test. Tasks 1–2 already cover the logic.

- [ ] **Step 1: Add the import**

At `motion_connector.py:43`, change:

```python
from data_sources import LiveScanSource, PastScanSource, ScanDataSource
```
to:
```python
from data_sources import LiveScanSource, PastScanSource, ScanDataSource
from scan_outcome import _ScanOutcomeSink, classify_scan_outcome
```

- [ ] **Step 2: Declare the cross-thread signal**

After `motion_connector.py:465` (`_pastScanBuffersReady = pyqtSignal(...)`), add:

```python
    # Worker->main: interrupted/empty-scan outcome toast (message, severity).
    # Emitted from _on_pipeline_complete (pipeline worker thread); delivered
    # on the GUI thread via the auto-queued connection wired in connect_signals.
    _scanOutcomeWarningSignal = pyqtSignal(str, str)
```

- [ ] **Step 3: Create the sink before the completion closure**

After `motion_connector.py:1973` (`self._nan_gap_tracker = nan_gap_tracker`), add:

```python
        # Interrupted-scan outcome tracker — bound to a local so the
        # completion closure and the sink list provably share THIS scan's
        # instance (same pattern as nan_gap_tracker above).
        outcome_sink = _ScanOutcomeSink()
```

- [ ] **Step 4: Classify + emit inside `_on_pipeline_complete`**

In `_on_pipeline_complete`, immediately after `self._persist_scan_notes(session_label)` (`motion_connector.py:2033`), add:

```python
            # Interrupted-scan outcome. An interrupted scan loses its open
            # interval; one that ends before any interval closes saves
            # nothing. Surface a toast; ScanDBSink deletes the empty session
            # row and the History viewer guards against loading it.
            try:
                outcome = classify_scan_outcome(
                    final_frames=outcome_sink.final_frames,
                    terminal_dark_missing=outcome_sink.terminal_dark_missing,
                    canceled=canceled,
                    disable_laser=disable_laser,
                )
                if outcome.severity:
                    logger.warning(
                        "Scan outcome: %s — %s", outcome.kind, outcome.message
                    )
                    self._scanOutcomeWarningSignal.emit(
                        outcome.message, outcome.severity
                    )
            except Exception:
                logger.exception("scan-outcome classification failed")
```

- [ ] **Step 5: Add the sink to the pipeline**

At `motion_connector.py:2078-2083`, change the `sinks=[...]` list to include `outcome_sink`:

```python
            sinks=[
                _LivePlotSink(connector=self, plot_t0=plot_t0, live_source=live_source,
                              nan_gap_tracker=nan_gap_tracker),
                _TriggerStateSink(connector=self),
                outcome_sink,
                _CompletionSink(connector=self, on_complete_cb=_on_pipeline_complete),
            ],
```

- [ ] **Step 6: Wire the signal in connect_signals**

After `motion_connector.py:3305` (`self._pastScanBuffersReady.connect(self._on_past_scan_buffers_ready)`), add:

```python
        # Auto-queued: emitted on the pipeline worker thread, runs the toast
        # on the GUI thread (same marshalling pattern as _pastScanBuffersReady).
        self._scanOutcomeWarningSignal.connect(self._on_scan_outcome_warning)
```

- [ ] **Step 7: Add the GUI-thread slot**

Add this method to the connector (next to `_on_past_scan_buffers_ready`, around `motion_connector.py:1708`):

```python
    @pyqtSlot(str, str)
    def _on_scan_outcome_warning(self, message: str, severity: str) -> None:
        """GUI thread: surface an interrupted/empty-scan toast. Errors are
        sticky (duration_ms=0) so a 'not saved' scan can't be missed."""
        duration = 0 if severity == "error" else 8000
        self.notify(message, severity, duration_ms=duration, tag="scan-outcome")
```

- [ ] **Step 8: Verify by reproduction (no hardware)**

Confirm the wiring imports cleanly and the classifier path is reachable:

Run:
```bash
python -c "import motion_connector; from scan_outcome import _ScanOutcomeSink, classify_scan_outcome; print('wired OK')"
```
Expected: `wired OK` (no ImportError).

Then run the unit suite to confirm nothing regressed:
Run: `python -m pytest tests/test_scan_outcome.py tests/test_live_plot_sink.py -m unit -v`
Expected: PASS.

> Full end-to-end (toast actually appears) requires hardware or a mid-scan disconnect; note this in the PR for HIL verification.

- [ ] **Step 9: Commit**

```bash
git add motion_connector.py
git commit -m "feat: toast on interrupted/empty scans via _ScanOutcomeSink

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Guard the History viewer against empty scans

**Files:**
- Modify: `data_sources.py` (add `buffers_are_empty`)
- Modify: `tests/test_data_sources.py` (unit test)
- Modify: `motion_connector.py:1726` (empty-load guard in `_on_past_scan_buffers_ready`)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_data_sources.py
from data_sources import buffers_are_empty, _CameraBuffer


def test_buffers_are_empty_true_for_none_and_empty():
    assert buffers_are_empty(None) is True
    assert buffers_are_empty({}) is True


def test_buffers_are_empty_true_when_all_buffers_have_no_samples():
    buffers = {("left", -1, "bfi"): _CameraBuffer(max_capacity=None)}
    assert buffers_are_empty(buffers) is True


def test_buffers_are_empty_false_when_any_sample_present():
    buf = _CameraBuffer(max_capacity=None)
    buf.append(t=0.0, v=1.0, frame_id=0)
    assert buffers_are_empty({("left", -1, "bfi"): buf}) is False
```

(`tests/test_data_sources.py` already exists and is `unit`-marked; if its `pytestmark` is missing, add `pytestmark = pytest.mark.unit` at the top.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_data_sources.py -m unit -v -k buffers_are_empty`
Expected: FAIL — `ImportError: cannot import name 'buffers_are_empty'`

- [ ] **Step 3: Implement the helper**

Add to `data_sources.py` (top-level function, near `load_past_scan_buffers`):

```python
def buffers_are_empty(buffers: Optional[dict]) -> bool:
    """True when a loaded past-scan buffer dict holds no samples at all.

    A scan interrupted before any dark interval closed yields a session row
    but zero session_data and no corrected CSV, so the loader returns an
    empty (or sample-less) dict. The viewer uses this to show a 'no data'
    error instead of silently opening an empty plot."""
    if not buffers:
        return True
    return all(getattr(b, "n", 0) == 0 for b in buffers.values())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_data_sources.py -m unit -v -k buffers_are_empty`
Expected: PASS (3 passed)

- [ ] **Step 5: Use the helper in the connector**

In `motion_connector.py:43`, extend the data_sources import to include the helper:

```python
from data_sources import (
    LiveScanSource, PastScanSource, ScanDataSource, buffers_are_empty,
)
```

In `_on_past_scan_buffers_ready`, immediately after the existing `if buffers is None:` block (`motion_connector.py:1726-1732`) and before `try: past = PastScanSource(...)`, add:

```python
        if buffers_are_empty(buffers):
            logger.info(
                "loadPastScan: %r contains no samples — not displaying",
                session_label,
            )
            self.errorOccurred.emit(
                f"Scan '{session_label}' contains no data.\n"
                "It was interrupted before any data could be recorded."
            )
            self.pastScanLoadFinished.emit(session_label, False)
            return
```

- [ ] **Step 6: Verify import + reproduce the empty-load path**

Run:
```bash
python -c "from data_sources import buffers_are_empty; print(buffers_are_empty({}), buffers_are_empty(None))"
```
Expected: `True True`

Run the unit suite: `python -m pytest tests/test_data_sources.py -m unit -v`
Expected: PASS.

> HistoryModal already wires `onErrorOccurred` to a dialog and clears the busy overlay, so the empty-load error surfaces with no extra QML for this task.

- [ ] **Step 7: Commit**

```bash
git add data_sources.py tests/test_data_sources.py motion_connector.py
git commit -m "feat: History viewer rejects empty scans instead of loading a blank plot

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Flag empty scans in the History modal

**Files:**
- Modify: `motion_connector.py:1411-1442` (add `hasData` to `get_scan_details`)
- Modify: `components/HistoryModal.qml` (banner + button gating)

This task is QML + connector glue; verify visually (no unit test). Per project convention, screenshot the History modal after the change.

- [ ] **Step 1: Compute `hasData` in `get_scan_details`**

In `get_scan_details`, the DB block currently reads notes only (`motion_connector.py:1410-1424`). Replace that block with one that also checks for persisted rows:

```python
        # Notes live in the scan DB (sessions.session_notes, keyed by
        # session_label == scan_id). Scans from before the DB migration
        # only have a *_notes.txt on disk — fall back to that.
        notes = ""
        has_db_rows = False
        db_path = getattr(self._interface, "scan_db_path", None)
        if db_path:
            try:
                from omotion.ScanDatabase import ScanDatabase
                db = ScanDatabase(db_path)
                try:
                    session = db.get_session_by_label(scan_id)
                    if session and session.get("session_notes"):
                        notes = session["session_notes"]
                    if session:
                        row = next(
                            db._connection().execute(
                                "SELECT EXISTS(SELECT 1 FROM session_data "
                                "WHERE session_id = ? LIMIT 1)",
                                (int(session["id"]),),
                            ),
                            None,
                        )
                        has_db_rows = bool(row[0]) if row else False
                finally:
                    db.close()
            except Exception:
                logger.warning("get_scan_details: could not read notes/rows from DB",
                               exc_info=True)
        if not notes:
            try:
                notes = notes_path.read_text(encoding="utf-8")
            except Exception:
                pass
```

Then add `hasData` to the returned dict (`motion_connector.py:1431-1442`). A scan has data if the DB holds rows OR a corrected/raw CSV exists on disk (covers legacy CSV-only scans):

```python
        return {
            "userLabel": subject,
            "sessionId": f"{ts}_{subject}",
            "timestamp": ts,
            "leftMask": left_mask,
            "rightMask": right_mask,
            "leftPath": str(left) if left else "",
            "rightPath": str(right) if right else "",
            "correctedPath": str(corrected) if corrected else "",
            "notesPath": str(notes_path),
            "notes": notes,
            "hasData": bool(has_db_rows or corrected or left or right),
        }
```

- [ ] **Step 2: Show a "no data" banner in HistoryModal**

In `components/HistoryModal.qml`, inside the metadata `ColumnLayout` (after the `GridLayout` that ends at line 280, before the `Text { text: "Notes:" ... }` at line 282), add:

```qml
                        // Interrupted-scan banner (issue: empty scans loaded blank).
                        // `hasData === false` only when the connector explicitly
                        // reported no rows/CSV; undefined (legacy details) stays hidden.
                        Text {
                            visible: selected.hasData === false
                            Layout.fillWidth: true
                            wrapMode: Text.Wrap
                            text: "⚠ No data recorded — this scan was interrupted "
                                  + "before any data could be saved."
                            color: "#E67E22"
                            font.pixelSize: 13
                            font.weight: Font.Bold
                        }
```

- [ ] **Step 3: Disable View/Export for empty scans**

In `components/HistoryModal.qml`, change the `enabled:` binding on **both** the "View in plot →" button (line 312) and the "Export CSV" button (line 337) from:

```qml
                            enabled: scans.length > 0 && scanPicker.currentIndex >= 0
```
to:
```qml
                            enabled: scans.length > 0 && scanPicker.currentIndex >= 0
                                     && selected.hasData !== false
```

- [ ] **Step 4: Verify visually**

QML does not hot-reload — restart the app. With the existing empty sessions in the working DB (e.g. `20260615_194235_owYQYT4Y`):

Run: `python main.py`

Then in the app: open History, select an empty scan, confirm (a) the orange "No data recorded" banner shows, (b) "View in plot →" and "Export CSV" are disabled, and (c) selecting a scan *with* data hides the banner and re-enables the buttons. Screenshot the modal for the PR (project convention: PrintWindow flag 2).

- [ ] **Step 5: Commit**

```bash
git add motion_connector.py components/HistoryModal.qml
git commit -m "feat: flag empty scans in History modal (banner + disabled actions)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## PART B — SDK (`openmotion-sdk`)

### Task 6: Delete the session row when a scan persisted nothing

**Files:**
- Modify: `omotion/pipeline/sinks.py:604-613` (`ScanDBSink.__init__` — add counter)
- Modify: `omotion/pipeline/sinks.py:759-769` (`_flush` — count rows written)
- Modify: `omotion/pipeline/sinks.py:652-680` (`on_complete` — delete if zero)
- Create: `tests/test_scan_db_sink_empty.py`

> **Decision note:** this hard-deletes the empty session (matches the agreed "not saved at all"). If clinical V&V later wants an audit trail of failed scans instead, swap the `delete_session` for an `update_session(session_meta={... "status": "no_data" ...})` and have the app's History list filter those out — the app-side guard (Task 4/5) already handles either representation.

Work in the SDK repo: `cd ../openmotion-sdk` (or the SDK path).

- [ ] **Step 1: Write the failing test**

```python
# openmotion-sdk/tests/test_scan_db_sink_empty.py
from types import SimpleNamespace

import pytest

from omotion.pipeline.sinks import ScanDBSink
from omotion.ScanDatabase import ScanDatabase

pytestmark = pytest.mark.unit


def _meta():
    return SimpleNamespace(
        scan_id="20260615_000000", subject_id="subjX", operator="test",
        started_at_iso="2026-06-15T00:00:00+00:00", duration_sec=10,
        reduced_mode=True, left_camera_mask=195, right_camera_mask=195,
    )


def test_empty_scan_session_is_deleted(tmp_path):
    db_path = str(tmp_path / "scans.db")
    sink = ScanDBSink(db_path)
    sink.on_scan_start(_meta())
    sink.on_complete()  # no "final" frames fed
    db = ScanDatabase(db_path)
    try:
        assert db.get_session_by_label("20260615_000000_subjX") is None
    finally:
        db.close()


def test_nonempty_scan_session_is_retained(tmp_path):
    db_path = str(tmp_path / "scans.db")
    sink = ScanDBSink(db_path)
    sink.on_scan_start(_meta())
    frame = SimpleNamespace(
        cam_id=-1, side="left", abs_frame_id=1, t=0.1,
        bfi=1.0, bvi=2.0, mean=3.0, contrast=0.4, quality="ok",
    )
    sink.consume("final", SimpleNamespace(frames=[frame]))
    sink.on_complete()
    db = ScanDatabase(db_path)
    try:
        sess = db.get_session_by_label("20260615_000000_subjX")
        assert sess is not None
        assert next(db.iter_session_data(int(sess["id"])), None) is not None
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run (from the SDK repo): `python -m pytest tests/test_scan_db_sink_empty.py -m unit -v`
Expected: `test_empty_scan_session_is_deleted` FAILS (session still present — currently `close_session` is called unconditionally); `test_nonempty_...` passes.

- [ ] **Step 3: Add the row counter in `__init__`**

In `ScanDBSink.__init__` (`omotion/pipeline/sinks.py:611-613`), after `self._buffer: list = []` add:

```python
        self._rows_written = 0
```

- [ ] **Step 4: Count rows in `_flush`**

Replace `_flush` (`omotion/pipeline/sinks.py:759-769`) with:

```python
    def _flush(self) -> None:
        if not self._buffer or self._db is None:
            return
        n = len(self._buffer)
        try:
            self._db.insert_session_data_rows(self._buffer)
            self._rows_written += n
        except Exception:
            logger.exception(
                "ScanDBSink: failed to insert %d corrected rows", n
            )
        self._buffer = []
```

- [ ] **Step 5: Delete-if-empty in `on_complete`**

In `on_complete` (`omotion/pipeline/sinks.py:659-671`), replace the inner block that writes diagnostics and closes the session:

```python
            if self._db is not None and self._session_id is not None:
                if self._diag and self._session_meta is not None:
                    try:
                        self._db.update_session(
                            self._session_id,
                            session_meta={**self._session_meta,
                                          "diagnostics": self._diag},
                        )
                    except Exception:
                        logger.exception(
                            "ScanDBSink: failed to write diagnostics summary"
                        )
                self._db.close_session(self._session_id, time.time())
```

with:

```python
            if self._db is not None and self._session_id is not None:
                if self._rows_written == 0:
                    # Nothing was persisted (scan interrupted before any dark
                    # interval closed). Drop the orphan session header so it
                    # never appears as a viewable scan in History.
                    scan_id = (self._session_meta or {}).get("scan_id", "?")
                    logger.warning(
                        "ScanDBSink: session %d (%s) recorded no corrected "
                        "rows — deleting empty session row.",
                        self._session_id, scan_id,
                    )
                    self._db.delete_session(self._session_id)
                else:
                    if self._diag and self._session_meta is not None:
                        try:
                            self._db.update_session(
                                self._session_id,
                                session_meta={**self._session_meta,
                                              "diagnostics": self._diag},
                            )
                        except Exception:
                            logger.exception(
                                "ScanDBSink: failed to write diagnostics summary"
                            )
                    self._db.close_session(self._session_id, time.time())
```

- [ ] **Step 6: Run test to verify it passes**

Run (from the SDK repo): `python -m pytest tests/test_scan_db_sink_empty.py -m unit -v`
Expected: PASS (2 passed)

- [ ] **Step 7: Commit (SDK repo)**

```bash
git add omotion/pipeline/sinks.py tests/test_scan_db_sink_empty.py
git commit -m "feat(pipeline): ScanDBSink deletes its session when no rows were persisted

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **App unit suite:** from `openmotion-bloodflow-app`, run
  `python -m pytest tests/test_scan_outcome.py tests/test_data_sources.py tests/test_live_plot_sink.py -m unit -v` → all PASS.
- [ ] **SDK unit test:** from `openmotion-sdk`, run
  `python -m pytest tests/test_scan_db_sink_empty.py -m unit -v` → PASS.
- [ ] **Manual / HIL (note in PR):** trigger a mid-scan disconnect (or Shelly power-cycle the console mid-scan) and confirm: (1) an error toast appears and no new empty session is listed in History; (2) a scan interrupted after data has been recorded shows a warning toast and the partial scan is still viewable; (3) a clean scan and a clean early cancel show no toast and load normally.
- [ ] **Regression — existing empty sessions:** open History on the current working DB and confirm the pre-existing zero-row scans (e.g. `20260615_194235_owYQYT4Y`) show the "No data recorded" banner with View/Export disabled, and that clicking View on one (if forced) surfaces the "contains no data" dialog rather than a blank plot.

## Self-review notes
- **Spec coverage:** #1 alert → Tasks 1–3 (classifier, sink, toast). #2 cleanup/flagging → Task 4 (load guard), Task 5 (History banner + `hasData`), Task 6 (delete empty session). Agreed "no wall-clock" → encoded in `classify_scan_outcome` (Task 1).
- **Type consistency:** `ScanOutcome(kind, severity, message)`, sink attrs `final_frames`/`terminal_dark_missing`, helper `buffers_are_empty`, detail key `hasData`, sink counter `_rows_written` — used identically across tasks.
- **Root cause is hardware:** the console USB watchdog drop-off is the underlying trigger; this plan is graceful degradation, not a fix for the disconnect itself (tracked separately).
