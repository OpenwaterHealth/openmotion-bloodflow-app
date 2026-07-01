# Audit Log — User & Auditor Guide

The OpenWater Bloodflow app keeps an **audit log**: a machine-readable,
append-only record of significant system events. It exists primarily for
**auditors** who need to reconstruct what the system and its operators did,
and when. This guide explains how to open it, what it records, and how to
read or export it.

---

## Opening the audit log

1. Open **Settings** (gear icon).
2. Scroll to the **Audit Log** section and click **View Logs**.
3. Enter the access password when prompted.

> The password is the same one used for other protected actions in the app
> (e.g. starting a calibration or deleting scans). Ask your system
> administrator if you don't have it. The audit log is read-only from the
> UI — there is no way to edit or delete individual entries through the app.

The viewer lists entries **newest first**, with three columns:

| Column | Meaning |
|---|---|
| **Time** | Local timestamp the event was recorded (ISO-8601 with UTC offset, e.g. `2026-06-15T09:14:02-07:00`). |
| **Event** | The event type (see the table below). |
| **Details** | A compact JSON object with event-specific fields. |

Use **Refresh** to reload, and **Export CSV** to save the full log to a file
(see [Exporting](#exporting-to-csv)).

---

## What gets recorded

Each entry is intentionally **minimal and structured** — a short event type
plus a small JSON `details` payload. The app records the following events:

| Event type | When it happens | Key details |
|---|---|---|
| `system_startup` | The app launches. | `app_version`, `sdk_version`, `data_dir` |
| `system_info` | At launch, right after startup. | `hostname`, `platform`, `system`, `arch`, `processor`, `python`, `ram_gb` |
| `system_shutdown` | The app closes cleanly. | `clean` |
| `device_connected` | The console or a sensor connects. | `device` (`console`/`left`/`right`), `reason` |
| `device_disconnected` | The console or a sensor disconnects. | `device`, `reason` |
| `device_stats` | When a device connects, once its IDs are readable. | `device`, `hardware_id`, `firmware_version` |
| `scan_started` | A scan begins. | `label`, `left_mask`, `right_mask` |
| `scan_ended` | A scan finishes or is stopped. | `label`, `session_label`, `duration_s`, `outcome` |
| `calibration_started` | A calibration begins. | `target` (`both`/`left`/`right`) |
| `calibration_ended` | A calibration finishes. | `target`, `outcome` (`passed`/`failed`/`aborted`), `reason` |
| `settings_changed` | A setting is changed and saved. | `changes` — only the keys that actually changed, each as `{ "old": …, "new": … }` |
| `scan_viewed` | A past scan is opened in the viewer. | `label` |
| `scan_deleted` | One or more scans are deleted. | `session_ids`, `count` |
| `audit_log_viewed` | The audit log itself is opened. | `entry_count` |
| `audit_log_exported` | The audit log is exported to CSV. | `dest`, `row_count` |
| `debug_bundle_created` | The "Send Debug Logs" button is used. | `dest`, `file_count`, `log_count`, `bytes`, `window_hours` |

Notes for auditors:

- **The audit log audits itself.** Opening the viewer and exporting it are
  both recorded (`audit_log_viewed`, `audit_log_exported`), so access to the
  audit data leaves its own trail.
- **System statistics** are split across two events: host/OS facts at launch
  (`system_info`) and per-device hardware/firmware IDs on connect
  (`device_stats`).
- **Settings changes record a diff**, not the full configuration — only the
  keys whose values changed, with their old and new values. A save that
  changes nothing produces no entry.
- Some payloads use a stable scan **label** (e.g. `ow3SW4HD`) and/or a
  **session label** (e.g. `20260610_163915_ow3SW4HD`) as the identifier,
  consistent with how scans are named elsewhere in the app and in the scan
  database.

---

## Where the data lives

Audit entries are stored in a table named **`logs`** inside the app's scan
database, `scans.db`, which lives in your configured **data directory** (the
same folder as your scan CSVs and the scan history). The table is
append-only; the app never updates or removes rows.

Schema:

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER | Auto-incrementing primary key (also the insertion order). |
| `ts_epoch` | REAL | Event time as a Unix timestamp (seconds since 1970-01-01 UTC). |
| `ts_iso` | TEXT | Event time as a local ISO-8601 string with UTC offset (unambiguous across DST), e.g. `2026-06-15T09:14:02-07:00`. |
| `event_type` | TEXT | One of the event types in the table above. |
| `details` | TEXT | Compact JSON object, or `NULL` when the event has no payload. |

Because it's plain SQLite, auditors can also query the table directly with
any SQLite tool, for example:

```sql
SELECT ts_iso, event_type, details
FROM logs
ORDER BY id DESC
LIMIT 50;
```

---

## Sending debug logs to Openwater

The **Send Debug Logs** button (top of the audit-log viewer) packages the
app's diagnostic logs for support. It writes a zip to
`<dataDirectory>/data/debug-bundles/debug-bundle-<timestamp>.zip`
containing the app log files from the last 48 hours, `app_config.json`,
and a `system_info.txt` (app/SDK version + host details). The file
explorer opens with the zip selected, and a message shows the path —
**email that zip to support@openwater.cc**. No data is sent
automatically, and the bundle contains no scan data or patient
information.

## Exporting to CSV

In the audit-log viewer, click **Export CSV** and choose a destination. The
exported file is UTF-8 and ordered **oldest first** (stable for diffing and
archiving), with one header row and one row per event:

```
ts_iso,ts_epoch,event_type,details
2026-06-15T09:14:02-07:00,1750000442.12,system_startup,"{""app_version"":""1.2.3"",""data_dir"":""C:\\…"",""sdk_version"":""…""}"
2026-06-15T09:14:02-07:00,1750000442.13,system_info,"{""arch"":""AMD64"",""hostname"":""LAB-PC-01"",…}"
```

The `details` column holds the raw JSON for each event, quoted per standard
CSV rules. Every common spreadsheet and data tool can read this directly;
the JSON keys within `details` are sorted alphabetically so the format is
deterministic.

---

## Frequently asked

**Can entries be edited or removed?**
Not through the app. The log is append-only and the viewer is read-only.

**Does deleting a scan remove its audit entries?**
No. Deleting scans removes the scan data, but the `scan_deleted` event (and
all earlier `scan_started` / `scan_viewed` entries for that scan) remain in
the audit log.

**What if the data directory is unavailable at startup?**
Audit logging fails safe: if the database can't be opened, the app keeps
running normally and simply records nothing — a failed audit write never
interrupts a scan or crashes the app.

**Is patient/subject data stored in the log?**
The log records scan **labels** and masks, not histogram data or computed
BFI/BVI values. Treat labels according to your site's data-handling policy.
