# Storage & Pipeline API Reference — OpenMotion Bloodflow Streamplot

## 1. Overview

The storage layer provides a three-table SQLite backend that replaces
the per-session flat-CSV architecture used by the original hardware capture
system.  `stream_proto.py` reads left- and right-side scan CSV files,
streams them through a dark-frame-correction pipeline, writes corrected
data to the database, and visualises live estimates in PyQtGraph.

```
  scan_data/
    scan_{session_id}_{date}_{time}_left_mask{hex}.csv
    scan_{session_id}_{date}_{time}_right_mask{hex}.csv
    scan_{session_id}_{date}_{time}_notes.txt
                │
                ▼
        CSVProducer (Thread)
         frame-metered at 40 Hz
                │ raw Sample objects
                ▼
       PipelineWorker (Thread)
          │               │
          ▼               ▼
       live_q          dark-window buffer (per side)
    (uncorrected          601 frames = 15 s
      BFI/BVI)                │ when window complete
          │               ▼
     QTimer poll   compute_dark_window()
          │               │
          ▼               ▼
    PyQtGraph plots     db_q
    (Left / Right)          │
                        DBConsumer (Thread)
                            │
                    ┌───────┴───────┐
                    ▼               ▼
               RawWriter       DataWriter
            (session_raw)   (session_data)
                    └───────┬───────┘
                            ▼
                      sessions.sqlite
```

---

## 2. SQLite Schema

### 2.1 `sessions`

| Column         | Type   | Notes                                              |
|----------------|--------|----------------------------------------------------|
| `session_id`   | TEXT PK| Alphanumeric ID from filename, e.g. `"ow98NSF5"`  |
| `session_start`| REAL   | Unix timestamp (float)                             |
| `session_end`  | REAL   | Unix timestamp; NULL until session closes          |
| `session_notes`| TEXT   | Multi-line free-text from `_notes.txt`             |
| `session_meta` | TEXT   | JSON: fps, mask_left, mask_right, …                |

### 2.2 `session_raw`

Stores every raw histogram frame exactly as captured.

| Column       | Type    | Notes                                        |
|--------------|---------|----------------------------------------------|
| `id`         | TEXT PK | UUID4                                        |
| `session_id` | TEXT FK | → `sessions.session_id`                      |
| `side`       | TEXT    | `'left'` or `'right'`                        |
| `cam_id`     | INTEGER |                                              |
| `frame_id`   | INTEGER | Hardware frame counter (wraps 0–255)         |
| `timestamp_s`| REAL    |                                              |
| `hist`       | BLOB    | 1024 × uint32, little-endian (4096 bytes)    |
| `temp`       | REAL    | Sensor temperature (°C)                      |
| `sum`        | INTEGER | Total photon count                           |
| `tcm`        | REAL    | Extra field from hardware (default 0)        |
| `tcl`        | REAL    | Extra field from hardware (default 0)        |
| `pdc`        | REAL    | Extra field from hardware (default 0)        |

### 2.3 `session_data`

Stores dark-frame-corrected BFI / BVI / contrast / mean values.
Written after every 600-frame (15-second) dark window.

| Column       | Type    | Notes                              |
|--------------|---------|------------------------------------|
| `id`         | TEXT PK | UUID4                              |
| `session_id` | TEXT FK | → `sessions.session_id`            |
| `cam_id`     | INTEGER |                                    |
| `side`       | TEXT    | `'left'` or `'right'`              |
| `time_s`     | REAL    | Frame timestamp (seconds)          |
| `bfi`        | REAL    | Blood-flow index (calibrated 0–10) |
| `bvi`        | REAL    | Blood-volume index (0–10)          |
| `contrast`   | REAL    | Speckle contrast K = σ/μ           |
| `mean`       | REAL    | Dark-corrected mean intensity      |

---

## 3. Data Types

### 3.1 `Sample` — `api/session_samples.py`

Immutable `NamedTuple` for one raw histogram frame.

| Field     | Type               | Description                              |
|-----------|--------------------|------------------------------------------|
| `side`    | `str`              | `'left'` or `'right'`                    |
| `cam_id`  | `np.uint32`        | Camera index                             |
| `frame_id`| `np.uint32`        | Hardware frame counter                   |
| `timestamp`| `np.float32`      | Capture time (s)                         |
| `hist`    | `NDArray[uint32]`  | 1024-bin histogram                       |
| `temp`    | `np.float32`       | Temperature (°C)                         |
| `summ`    | `np.uint64`        | Sum of histogram bins                    |
| `tcm`     | `np.float32`       | Hardware field (default 0.0)             |
| `tcl`     | `np.float32`       | Hardware field (default 0.0)             |
| `pdc`     | `np.float32`       | Hardware field (default 0.0)             |

---

## 4. Module Reference

### 4.1 `api/bfstorage.py`

#### `open_db(db_path) → sqlite3.Connection`
Open (or create) the database and apply the current schema (WAL mode,
foreign keys, all three tables + indexes).  Idempotent.

#### `init_db(conn)`
Apply schema to an already-open connection.  Called internally by `open_db`.

#### `create_session(conn, *, session_id, session_start, session_end=None, session_notes=None, session_meta=None) → str`
Insert a new `sessions` row.  Uses `INSERT OR IGNORE`, so calling it twice
with the same `session_id` is safe.  Returns `session_id`.

#### `close_session(conn, session_id, session_end)`
Set `session_end` on an existing session.

#### `list_sessions(conn) → list[dict]`
Return all sessions ordered by `session_start` with `raw_count` and
`data_count` sub-selects.

---

#### `class RawWriter`

Background-thread batched writer for `session_raw`.

```python
with RawWriter(db_path, session_id) as w:
    for sample in source:
        w.submit(sample)
```

| Constructor Parameter | Default | Description                      |
|-----------------------|---------|----------------------------------|
| `db_path`             | —       | SQLite file path                 |
| `session_id`          | —       | Must exist in `sessions`         |
| `batch_size`          | 200     | Rows per transaction             |
| `queue_size`          | 8192    | Internal queue depth             |

| Method         | Description                                       |
|----------------|---------------------------------------------------|
| `submit(sample)` | Enqueue a `Sample` for insertion               |
| `flush()`      | Block until all enqueued samples are committed    |
| `close()`      | Flush and stop the worker thread                  |

---

#### `class DataWriter`

Background-thread batched writer for `session_data`.

```python
with DataWriter(db_path, session_id) as w:
    for bfdict in corrected_values:
        w.submit(bfdict)
```

Each `bfdict` must contain: `cam_id`, `side`, `time_s`, `bfi`, `bvi`,
`contrast`, `mean`.

Same constructor parameters and methods as `RawWriter`.

---

#### `class RawReader`

Cursor-based generator that yields `Sample` objects from `session_raw`.

```python
reader = RawReader(conn, session_id)
for sample in reader.stream(side="left"):
    process(sample)
```

| Method                 | Returns              | Description                    |
|------------------------|----------------------|--------------------------------|
| `stream(side=None)`    | `Iterator[Sample]`   | All rows, ordered by `rowid`   |
| `count(side=None)`     | `int`                | Row count (optionally filtered)|

---

#### `class DataReader`

Cursor-based generator that yields dicts from `session_data`.

```python
reader = DataReader(conn, session_id)
for row in reader.stream(side="right"):
    print(row["bfi"])
```

Each yielded dict has keys: `cam_id`, `side`, `time_s`, `bfi`, `bvi`,
`contrast`, `mean`.

---

#### `export_raw_to_csv(db_path, session_id, out_csv_path, include_hist=True) → int`
Export `session_raw` to CSV.  Returns rows written.

CSV columns: `session_id`, `side`, `cam_id`, `frame_id`, `timestamp_s`,
`temp`, `sum`, `tcm`, `tcl`, `pdc` [, `hist[0]` … `hist[1023]`].

Pass `include_hist=False` to omit histogram columns (~30× smaller file).

#### `export_data_to_csv(db_path, session_id, out_csv_path) → int`
Export `session_data` to CSV.  Returns rows written.

CSV columns: `session_id`, `cam_id`, `side`, `time_s`, `bfi`, `bvi`,
`contrast`, `mean`.

---

### 4.2 `api/session_samples.py`

#### `class SessionSamples`

Reads a raw CSV file into memory and vends `Sample` objects.

```python
ss = SessionSamples()
n  = ss.read_csv("scan_ow98NSF5_..._left_mask66.csv", side="left")
for i in range(n):
    sample = ss.get(i)
```

| Method                       | Returns  | Description                           |
|------------------------------|----------|---------------------------------------|
| `read_csv(path, side="left")`| `int`    | Load CSV; return row count            |
| `size()`                     | `int`    | Number of rows loaded                 |
| `get(i)`                     | `Sample` | i-th sample                          |

#### `parse_session_csv_filename(filename) → dict | None`

Parse the scan CSV filename convention:
`scan_{session_id}_{YYYYMMDD}_{HHMMSS}_{side}_mask{hex}.csv`

Returns `{"session_id", "datetime", "side", "mask"}` or `None`.

#### `find_session_files(directory, session_id) → dict`

Locate `{"left": path|None, "right": path|None, "notes": path|None}`
for a given `session_id` within a directory.

---

### 4.3 `api/export_csv_cli.py`

```
python api/export_csv_cli.py <db_path> list
python api/export_csv_cli.py <db_path> raw  <session_id> <out.csv> [--no-hist] [--side left|right]
python api/export_csv_cli.py <db_path> data <session_id> <out.csv> [--side left|right]
```

| Subcommand | Description                                    |
|------------|------------------------------------------------|
| `list`     | Print all sessions with row counts             |
| `raw`      | Export `session_raw` to CSV                    |
| `data`     | Export `session_data` (BF values) to CSV       |

---

## 5. Pipeline Reference — `stream_proto.py`

### 5.1 Usage

```
python stream_proto.py <scan_dir> <session_id> [--db <path>] [--period <s>]
```

| Argument      | Default                  | Description                          |
|---------------|--------------------------|--------------------------------------|
| `scan_dir`    | —                        | Directory containing scan CSV files  |
| `session_id`  | —                        | Alphanumeric session ID              |
| `--db`        | `data/sessions.sqlite`   | SQLite database path                 |
| `--period`    | `0.025` (40 Hz)          | Simulated inter-frame delay (s)      |
| `--plot-size` | `500`                    | Rolling plot window width (points)   |

### 5.2 Dark-Frame Correction

`compute_dark_window(window_samples, side)` is called once every
`DARK_INTERVAL = 600` frames (nominally 15 seconds at 40 Hz).

Algorithm (ported from `processing/visualize_bloodflow.py`):
1. Group 601 samples per camera into `histos` array `(ncams, 601, 1024)`.
2. Subtract 6 from bin 0 (shot noise); zero bins below `NOISY_BIN_MIN = 10`.
3. Extract dark frames: index 0 (replaced with frame 9, first good dark)
   and index 600 (end of window).
4. Compute mean (`u1_dark`) and variance (`var_dark`) of dark histograms.
5. Linearly interpolate dark stats across all 601 frames.
6. Compute signal mean (`u1 - u1_dark`) and contrast (`σ/μ` after dark subtraction).
7. Apply per-camera calibration to produce BFI and BVI (range 0–10).
8. Emit one `bfdict` per frame per camera for frames 1–599 (dark frames excluded).

The last dark frame (index 600) is retained as the first frame of the
next window to ensure continuity.

### 5.3 Live Display

`PipelineWorker` also calls `BFComputer.compute()` on every incoming
`Sample` (no dark correction) and pushes `(side, bfi, bvi)` onto
`live_q`.  A `QTimer` polls `live_q` at the frame rate and calls
`BFPlot.update_plot_data()`.

Each side has one `BFPlot` widget showing BFI (left axis, red) and BVI
(right axis, blue) in a rolling window.

---

## 6. Filename Convention

```
scan_{session_id}_{YYYYMMDD}_{HHMMSS}_{side}_mask{hexBits}.csv
scan_{session_id}_{YYYYMMDD}_{HHMMSS}_notes.txt
```

| Part          | Example       | Meaning                                   |
|---------------|---------------|-------------------------------------------|
| `{session_id}`| `ow98NSF5`    | Alphanumeric hardware session ID          |
| `{YYYYMMDD}`  | `20260407`    | Date                                      |
| `{HHMMSS}`    | `152533`      | Time                                      |
| `{side}`      | `left`        | `left` or `right`                         |
| `{hexBits}`   | `66`          | Camera bitmask (0x66 = cameras 1,2,5,6)  |

Bitmask `FF` = all 8 cameras active; `00` = no cameras active.
