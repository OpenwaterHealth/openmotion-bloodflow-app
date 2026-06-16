# History → Data Management Tool — Design

**Date:** 2026-06-15
**Status:** Approved for planning
**Component:** `components/HistoryModal.qml`, `motion_connector.py`

## Problem

Today's History view ([HistoryModal.qml](../../../components/HistoryModal.qml)) is a single-select
`ComboBox` picker plus a details pane. It works for "open one past scan" but is not a tool for
*managing* a growing scan archive: you can't see all scans at a glance, can't sort or search,
can't act on several at once, and can't delete anything.

Rework it into a proper data-management tool: a sortable, searchable, multi-select **table** of
scans with a detail pane and Load / Export / Delete actions.

## Decisions (locked during brainstorming)

| Question | Decision |
|---|---|
| Container | **Modal** — rebuild the existing `HistoryModal`, not a new page. Lowest-risk, fits `ModalManager`. |
| List source | **DB `sessions` table only.** Drop the CSV-filename listing. Matches DB-as-system-of-record; CSVs are being phased out. Legacy pre-DB CSV-only scans (no session row) no longer appear. |
| Delete scope | **DB session only** — `delete_session(id)` (CASCADE-deletes `session_data`). No file deletion. |
| Delete auth | **Reuse the developer password** via the existing `PasswordPromptModal` + `checkDeveloperPassword()`. |
| Notes | **Read-only** in the detail pane (viewable, not editable). |
| "Time" column | **Duration** (`session_end − session_start`). |
| Mode column | **Omitted.** |
| Config column | Show **left / right** named configs, always both sides. |
| Export CSV | **Kept** (existing `exportScanCsv`). |

No SDK changes are required. Everything the table needs already exists in the scan DB:
`ScanDBSink` writes `session_meta` at scan start with
`sdk_flags.{left_camera_mask, right_camera_mask, reduced_mode}`, `duration_sec`, `operator`,
`started_at_iso`, and `scan_id`/`subject_id`; `session_start`/`session_end` give real duration;
`session_notes` holds notes. Empty/interrupted-before-any-data sessions are already pruned by
`ScanDBSink.on_complete`.

## UI

A modal (same dimmed-backdrop + centered panel as today, slightly larger) containing:

**Toolbar:** title · search field (filters by user label, case-insensitive substring) ·
`Config ▾` filter (All + each named config) · Open Folder · Refresh · ✕.

**Table** (header row + scrollable `ListView` of styled row delegates — matches the app's existing
hand-styled control idiom; not `TableView`):

| ☑ | User Label | Date / Time | Config (L / R) | Duration | status |
|---|---|---|---|---|---|

- Header checkbox = select-all / clear-all (respects the active search/config filter).
- Per-row checkbox = multiselect membership.
- Single-click anywhere on a row = **focus** it (highlights, fills the detail pane). Focus and
  checkbox-membership are independent.
- Clicking a column header sorts by it; click again to reverse. Default: **Date / Time descending.**
- `status`: green dot = ok; amber ⚠ = interrupted (`session_end` is null).

**Detail pane** (below the table, for the focused row): full label · operator · left/right mask
(hex) · Config (L / R) · started-at · sample-row count · **read-only** Notes box (scrollable).
Sample count is fetched lazily when a row is focused (keeps the list query cheap).

**Actions** (bottom): "N selected" counter · `Load in viewer →` · `Export CSV` · `🔒 Delete (N)`.
- **Load** — enabled when exactly one row is focused; calls the existing async
  `loadPastScan(label)` with the busy overlay, closes on success (unchanged behavior).
- **Export CSV** — acts on the focused row; existing `exportScanCsv(label, path)` + `FileDialog`.
- **Delete (N)** — acts on all checked rows. Opens `PasswordPromptModal`; on `accepted()` calls
  the new `deleteScans([sessionId…])`, then refreshes and clears the selection. Disabled while a
  scan is RUNNING (don't delete the in-flight session).

## Connector API (new)

All read/delete use a short-lived `ScanDatabase(db_path)` handle (WAL allows concurrent
read/write with a live scan). Best-effort: a missing/unreadable DB yields an empty list, never an
exception into QML.

- `get_scan_sessions() -> QVariantList[QVariantMap]` — one entry per `sessions` row, newest first.
  Each map: `sessionId` (int DB id), `label`, `userLabel`, `operator`, `dateTime` (display string),
  `timestamp` (raw `YYYYMMDD_HHMMSS` for sort), `durationSec` (float or −1 if open),
  `leftMask`/`rightMask` (int), `reducedMode` (bool), `notes`, `interrupted` (bool,
  `session_end is None`). Masks come from `session_meta.sdk_flags`; for older rows lacking meta,
  fall back to deriving per-camera masks from `session_data` (returns −1/unknown for reduced-mode
  rows that only stored the cam_id=−1 average). The list query does **not** count rows.
- `get_session_stats(sessionId: int) -> QVariantMap` — `{ "sampleCount": int }`. Called on focus so
  the per-row `COUNT(*)` cost is paid once per selection, not once per listed scan.
- `deleteScans(sessionIds: list[int]) -> int` — `delete_session()` per id; returns the count
  deleted. The password gate lives in QML (`PasswordPromptModal`); the slot itself is unguarded
  glue. Logs each deletion.

`loadPastScan` and `exportScanCsv` are reused unchanged. `get_scan_list` / `get_scan_details`
stay (still used by `loadPastScan` and existing tests).

## Config-name mapping

Masks → names mirror `CameraSelectionModal`'s pattern table:
`0x00→None, 0x5A→Near, 0x66→Middle, 0xC3→Far, 0x99→Outer, 0x0F→Left, 0xF0→Right, 0x42→Third Row,
0xFF→All`; anything else → `0xNN` hex; unknown (−1) → `—`. Column renders `"{left} / {right}"`.
Mapping lives in a new `components/ScanConfigNames.js` (`.pragma library`) imported by
`HistoryModal`; `CameraSelectionModal` is left untouched for now.

## Components & tidiness

- Rewrite `HistoryModal.qml` around the table. Reuse `PasswordPromptModal` (instantiated inside,
  like `DeveloperUnlockModal`), the busy overlay, the `friendlyDate`/`formatMasks` helpers, and the
  `MotionInterface` `Connections` block (`onPastScanLoadFinished`, `onDirectoryChanged`,
  `onErrorOccurred`).
- New `components/ScanConfigNames.js` for the mask→name mapping.
- New connector slots grouped next to `get_scan_list` (~line 1384). Keep them small and DB-only;
  do not grow the existing 4031-line file with table-rendering logic — that stays in QML.

## Error handling

- Empty archive → table shows an "No scans yet" empty state; actions disabled.
- DB read failure → empty list + a logged warning (current best-effort pattern).
- Delete of an already-removed id → `delete_session` returns False; counted as not-deleted, no error.
- Interrupted rows (`⚠`) → Load/Export disabled (no replayable data), Delete still allowed.

## Testing

- Connector unit tests (no hardware): `get_scan_sessions` shape + sort + mask/config derivation
  from `session_meta` and from `session_data` fallback; `deleteScans` removes rows and cascades;
  `get_session_stats` count. Build a temp `ScanDatabase`, insert sessions/data, assert.
- Update the existing UI tests that drive the old `ComboBox` History
  (`tests/test_history.py`, and `tests/test_scan_history_list.py` if it touches the modal) to the
  new table interaction.

## Out of scope

- Deleting on-disk CSV/raw/telemetry files (DB-only delete by decision).
- Editing notes from History (read-only by decision).
- A full-page History route, renaming labels, bulk export, date-range filtering.
