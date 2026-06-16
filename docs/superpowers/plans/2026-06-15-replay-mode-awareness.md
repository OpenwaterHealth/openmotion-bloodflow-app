# Replay Mode Awareness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make past-scan replay render in the mode the scan was recorded in (Part A), and hide non-reduced scans from History while the app is in reduced mode (Part B).

**Architecture:** A tri-state `reducedMode` indicator on `ScanDataSource` (derived from the loaded buffer layout on `PastScanSource`, `-1` on live), an `effectiveReduced` resolve in `PlotViewer.qml` that prefers the source's mode over the live config (mirrors the #175 mask resolve), and one filter condition in `HistoryModal.rebuildView()` keyed on the `reducedMode` field already present in each scan row.

**Tech Stack:** Python 3.13, PyQt6, QML (QtQuick 6), pytest.

**Spec:** [docs/superpowers/specs/2026-06-15-replay-mode-awareness-design.md](../specs/2026-06-15-replay-mode-awareness-design.md)

---

## File Structure

- **Modify** `data_sources.py` — add module helper `_derive_reduced_from_buffers()`, a `reducedMode` `pyqtProperty(int)` on `ScanDataSource` (backing field `self._reduced_mode`, default `-1`), and set it in `PastScanSource.__init__`.
- **Modify** `tests/test_data_sources.py` — append unit tests next to the existing #175 mask tests (~line 668).
- **Modify** `components/PlotViewer.qml` — add `effectiveReduced` computed property; swap the viewer's internal `reducedMode` reads over to it.
- **Modify** `components/HistoryModal.qml` — add the reduced-mode filter in `rebuildView()` + re-filter on `appConfigChanged`.

No connector changes, no `BloodFlow.qml` changes.

---

## Task 1: `data_sources.py` — tri-state `reducedMode` on the source

**Files:**
- Modify: `data_sources.py`
- Test: `tests/test_data_sources.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_data_sources.py` immediately after `test_past_scan_source_masks_unknown_when_no_per_cam_streams` (~line 668):

```python
# ── Replay mode awareness — source reports its recorded reduced/dev mode ──
# The viewer renders in the mode the scan was recorded in. reducedMode is a
# tri-state: -1 unknown, 0 per-camera, 1 reduced — derived from the loaded
# buffer cam_ids (reduced scans store only cam_id=-1; per-camera store 0..7).

from data_sources import _derive_reduced_from_buffers


def _one_sample_buffer():
    buf = _CameraBuffer(max_capacity=None)
    buf.append(t=0.0, v=1.0, frame_id=0)
    return buf


def test_derive_reduced_per_camera():
    buffers = {("left", 0, "bfi"): _one_sample_buffer(),
               ("right", 7, "bfi"): _one_sample_buffer()}
    assert _derive_reduced_from_buffers(buffers) == 0


def test_derive_reduced_side_average():
    buffers = {("left", -1, "bfi"): _one_sample_buffer(),
               ("right", -1, "bfi"): _one_sample_buffer()}
    assert _derive_reduced_from_buffers(buffers) == 1


def test_derive_reduced_empty_is_unknown():
    assert _derive_reduced_from_buffers({}) == -1
    # A buffer dict whose buffers hold no samples is also "unknown".
    assert _derive_reduced_from_buffers({("left", -1, "bfi"): _CameraBuffer()}) == -1


def test_past_scan_source_reduced_mode_for_side_average():
    buffers = {}
    for side in ("left", "right"):
        buffers[(side, -1, "bfi")] = _one_sample_buffer()
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers=buffers)
    assert src.reducedMode == 1


def test_past_scan_source_reduced_mode_for_per_camera():
    buffers = {}
    for cam_id in (0, 1, 6, 7):
        buffers[("left", cam_id, "bfi")] = _one_sample_buffer()
    src = PastScanSource(scan_db=None, session_id=1, preloaded_buffers=buffers)
    assert src.reducedMode == 0


def test_live_scan_source_reduced_mode_unknown():
    src = LiveScanSource(plot_t0=0.0)
    assert src.reducedMode == -1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_data_sources.py -k "reduced_mode or derive_reduced" -v`
Expected: FAIL — `ImportError: cannot import name '_derive_reduced_from_buffers'`.

- [ ] **Step 3: Add the module helper**

In `data_sources.py`, immediately after the `_derive_masks_from_buffers` function, add:

```python
def _derive_reduced_from_buffers(buffers) -> int:
    """Tri-state recorded display mode from a loaded scan's buffer cam_ids:
    -1 unknown, 0 per-camera, 1 reduced. Reduced-mode scans store only the
    cam_id=-1 side average; per-camera (dev) scans store cam_id 0..7. Mirrors
    _derive_masks_from_buffers — the viewer prefers this over the live config
    so replay renders in the mode the scan was captured in."""
    if buffers_are_empty(buffers):
        return -1
    if any(0 <= key[1] < 8 for key in buffers):
        return 0
    if any(key[1] == -1 for key in buffers):
        return 1
    return -1
```

- [ ] **Step 4: Add the backing field + property to `ScanDataSource`**

In `data_sources.py`, in `ScanDataSource.__init__`, next to where `self._left_mask` / `self._right_mask` are initialized to `-1`, add:

```python
        # Recorded display mode, tri-state: -1 unknown / 0 per-camera / 1
        # reduced. -1 for live sources (viewer follows the live config);
        # PastScanSource derives the real value from its buffer layout.
        self._reduced_mode: int = -1
```

Then add this property next to the `leftMask` / `rightMask` `pyqtProperty` definitions:

```python
    @pyqtProperty(int, constant=True)
    def reducedMode(self) -> int:
        """Recorded display mode this source represents: -1 unknown, 0
        per-camera, 1 reduced. Exposed for QML (PlotViewer.effectiveReduced)
        so a replayed scan renders in its own mode rather than the live config.
        See ``leftMask`` for why this must be a pyqtProperty."""
        return self._reduced_mode
```

- [ ] **Step 5: Set it in `PastScanSource.__init__`**

In `data_sources.py`, in `PastScanSource.__init__`, immediately after the existing
`self._left_mask, self._right_mask = _derive_masks_from_buffers(self.buffers)` line, add:

```python
        self._reduced_mode = _derive_reduced_from_buffers(self.buffers)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_data_sources.py -k "reduced_mode or derive_reduced" -v`
Expected: PASS (6 tests).

- [ ] **Step 7: Run the full data_sources suite (no regression) + lint**

Run: `python -m pytest tests/test_data_sources.py -q`
Expected: all pass.
Run: `python -m flake8 data_sources.py tests/test_data_sources.py`
Expected: no new errors on the added lines.

- [ ] **Step 8: Commit**

```bash
git add data_sources.py tests/test_data_sources.py
git commit -m "feat: PastScanSource reports its recorded reduced/dev mode"
```

---

## Task 2: `PlotViewer.qml` — render in the scan's recorded mode

QML isn't unit-tested; verify by launching against reduced and per-camera seed scans.

**Files:**
- Modify: `components/PlotViewer.qml`

- [ ] **Step 1: Add the `effectiveReduced` resolve**

In `components/PlotViewer.qml`, immediately after the `_effectiveRightMask` `readonly property`
block (the #175 mask resolve, ~lines 168–174), add:

```qml
    // Replay adopts the loaded scan's recorded mode: when the source reports a
    // known mode (reducedMode >= 0) use it, else fall back to the live-config
    // `reducedMode` input. Mirrors _effectiveLeftMask/_effectiveRightMask (#175).
    // Note: scanSource.reducedMode is an int tri-state (-1/0/1) from Python;
    // viewer.reducedMode is the bool live-config input.
    readonly property bool effectiveReduced:
        (viewer.scanSource && viewer.scanSource.reducedMode !== undefined
         && viewer.scanSource.reducedMode >= 0)
            ? (viewer.scanSource.reducedMode === 1)
            : viewer.reducedMode
```

- [ ] **Step 2: Swap the four `!viewer.reducedMode` reads**

Replace all occurrences of `!viewer.reducedMode` with `!viewer.effectiveReduced` in
`components/PlotViewer.qml` (5 sites: `showCellValues` at ~72, and the `visible: !viewer.reducedMode`
toolbar/menu gates at ~1017, ~1096, ~1115, ~1131). None of these is the input-property declaration
or the new `effectiveReduced` fallback (that fallback uses `viewer.reducedMode` *without* `!`), so a
global `!viewer.reducedMode` → `!viewer.effectiveReduced` replacement is safe.

Use Edit with `replace_all: true`, `old_string: "!viewer.reducedMode"`, `new_string: "!viewer.effectiveReduced"`.

- [ ] **Step 3: Swap the three remaining (non-`!`) rendering reads**

Three sites use `viewer.reducedMode` without a leading `!`. Edit each individually (do NOT touch the
property declaration `property bool reducedMode: false` at ~line 68, nor the `effectiveReduced`
fallback you just added):

a) The active cell model (~line 221):
```qml
    readonly property var _activeCellModel: viewer.reducedMode
        ? _reducedCellModel
        : _devCellModel
```
→ change the first line to `readonly property var _activeCellModel: viewer.effectiveReduced`.

b) The reduced side-readout footer `visible` (~line 649): `visible: viewer.reducedMode`
→ `visible: viewer.effectiveReduced`.

c) The cell grid columns (~line 674): `columns: viewer.reducedMode ? 1 : 4`
→ `columns: viewer.effectiveReduced ? 1 : 4`.

- [ ] **Step 4: Confirm all internal reads were swapped**

Run: `python -c "s=open(r'components/PlotViewer.qml',encoding='utf-8').read(); print('viewer.reducedMode:', s.count('viewer.reducedMode')); print('effectiveReduced:', s.count('effectiveReduced'))"`
Expected: `viewer.reducedMode` count is exactly **1** — the only remaining occurrence is the
`effectiveReduced` fallback (`: viewer.reducedMode`). The property *declaration* is
`property bool reducedMode` with no `viewer.` prefix, so it isn't counted. `effectiveReduced` count
is **≥ 9** (its own declaration + the 8 swapped sites). If `viewer.reducedMode` > 1, a swap was
missed — find and fix it.

- [ ] **Step 5: Seed a per-camera (dev) scan alongside the reduced ones**

The worktree already has a `scans.db` with reduced scans (`cam_id=-1`). Add one per-camera scan so
you can verify both render paths. Run (adjust the path if `dataDirectory` is set):

```bash
python -c "
from omotion.ScanDatabase import ScanDatabase
import math, time
db = ScanDatabase(db_path=r'scans.db')
sid = db.create_session(session_label='20260613_101500_DevScan', session_start=time.time(),
    session_end=time.time()+10, session_notes='Per-camera dev scan.',
    session_meta={'scan_id':'20260613_101500','subject_id':'DevScan','operator':'ethan',
        'duration_sec':10,'sdk_flags':{'reduced_mode':False,'left_camera_mask':0xC3,'right_camera_mask':0xC3}})
rows=[]
for i in range(400):
    t=i/40.0
    for side in (0,1):
        for cam in (0,1,6,7):
            rows.append(dict(session_id=sid,cam_id=cam,side=side,timestamp_s=t,frame_id=i,
                             bfi=1.0+0.3*math.sin(t+cam),bvi=2.0+0.4*math.sin(t*0.8+cam),mean=120.0,contrast=0.05))
db.insert_session_data_rows(rows); db.close(); print('seeded dev scan')
"
```

- [ ] **Step 6: Launch in DEV mode and verify both render paths**

Set `developerMode: true` and `reducedMode: false` in `config/app_config.json` (note the originals to
restore). Launch with the conda interpreter (the background Bash `python` is not on PATH — exits 127):

```
Start-Process -FilePath "C:\Users\ethan\miniconda3\python.exe" -ArgumentList "main.py" -WorkingDirectory "<worktree>" -PassThru
```

Open History → load the **reduced** scan (e.g. `Patient14`): the plot must show the two big BFI/BVI
**side panels** (reduced layout) with data. Then "Back to live" / load the **DevScan**: the plot must
show the **per-camera grid** with traces. Screenshot the window (PrintWindow flag 2) and confirm.
Check the newest `app-logs/ow-bloodflowapp-*.log` for zero QML errors. Restart the app to pick up QML
edits (QML does not hot-reload).

- [ ] **Step 7: Restore config and commit**

Restore `developerMode` / `reducedMode` in `config/app_config.json` to their original values.
```bash
git add components/PlotViewer.qml
git commit -m "feat: replay renders in the scan's recorded reduced/dev mode"
```
(Do not commit `scans.db` — it is gitignored.)

---

## Task 3: `HistoryModal.qml` — reduced mode hides non-reduced scans

**Files:**
- Modify: `components/HistoryModal.qml`

- [ ] **Step 1: Add the filter condition in `rebuildView()`**

In `components/HistoryModal.qml`, in `rebuildView()`, the filter currently reads:

```qml
        var q = (searchText || "").toLowerCase()
        var cfg = configFilter
        var arr = scans.filter(function(r) {
            if (q.length > 0
                && (r.userLabel || "").toLowerCase().indexOf(q) < 0
                && (r.label || "").toLowerCase().indexOf(q) < 0)
                return false
            if (cfg !== "All" && r.configL !== cfg && r.configR !== cfg)
                return false
            return true
        })
```

Insert an app-reduced read before the filter and an AND-condition inside it:

```qml
        var q = (searchText || "").toLowerCase()
        var cfg = configFilter
        // In reduced (clinical) mode, omit scans not shot in reduced mode —
        // the reduced viewer can't render their per-camera data. Dev mode
        // lists everything. A scan with no recorded mode counts as non-reduced.
        var appReduced = MotionInterface.appConfig.reducedMode === true
        var arr = scans.filter(function(r) {
            if (appReduced && !r.reducedMode)
                return false
            if (q.length > 0
                && (r.userLabel || "").toLowerCase().indexOf(q) < 0
                && (r.label || "").toLowerCase().indexOf(q) < 0)
                return false
            if (cfg !== "All" && r.configL !== cfg && r.configR !== cfg)
                return false
            return true
        })
```

- [ ] **Step 2: Re-filter when the app's reduced flag changes**

In `components/HistoryModal.qml`, in the existing `Connections { target: MotionInterface ... }` block,
add a handler so a runtime mode change while History is open re-applies the filter:

```qml
        function onAppConfigChanged() { if (root.visible) root.rebuildView() }
```

- [ ] **Step 3: Verify in both modes**

With the reduced + dev seed scans from Task 2 present:
- Launch with `reducedMode: true` → open History → the `DevScan` row must be **absent**; only the
  reduced scans (Patient14 / BaselineA) appear.
- Launch with `reducedMode: false` (dev) → open History → **all** scans appear, `DevScan` included.

Screenshot each and confirm. Check the newest app log for zero QML errors. Restore
`config/app_config.json` afterward.

- [ ] **Step 4: Commit**

```bash
git add components/HistoryModal.qml
git commit -m "feat: hide non-reduced scans from History while in reduced mode"
```

---

## Final verification

- [ ] `python -m pytest tests/test_data_sources.py -q` → all pass (new + existing).
- [ ] `python -m pytest tests/test_history_sessions.py -q` → still pass (no regression).
- [ ] `python -m flake8 data_sources.py tests/test_data_sources.py` → clean on new code.
- [ ] Manual matrix confirmed: dev mode loads reduced→side-panels and dev→grid; reduced mode hides the dev scan from History; "Back to live" reverts the viewer to the config mode.
- [ ] `config/app_config.json` restored to its original `developerMode` / `reducedMode` values.

## Spec coverage check

- Part A: source reports recorded mode (Task 1); viewer prefers it, reverts on Back-to-live (Task 2). ✓
- Part B: reduced mode hides non-reduced scans, dev mode unfiltered (Task 3). ✓
- Buffer-derived detection; viewer-only scope; no connector/BloodFlow/config-persistence changes. ✓
- Unit tests for the derivation; manual matrix for the QML wiring. ✓
