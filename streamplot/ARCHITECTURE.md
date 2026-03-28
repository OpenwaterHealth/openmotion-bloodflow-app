# Streamplot — System Architecture

## 1. Purpose

The `streamplot` module is a prototyping workspace for real-time blood flow data
acquisition, computation, visualization, and storage. It processes raw 1024-bin
laser speckle histograms from OpenWater imaging hardware and derives two key
metrics:

- **BFI** (Blood Flow Index) — derived from speckle contrast (std / mean)
- **BVI** (Blood Volume Index) — derived from mean intensity

The module contains both a **real-time streaming pipeline** and a **batch
offline analysis** path, plus benchmarking tools for evaluating storage backends.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        stream_proto.py (main)                       │
│                                                                     │
│  ┌──────────────┐    ┌───────────────────┐    ┌──────────────────┐  │
│  │  DataProducer │───>│  DataProcessor    │───>│  DataStorage     │  │
│  │  Mockup       │    │  Mockup           │    │  Mockup          │  │
│  │  (Thread)     │    │  (Thread)         │    │  (Thread)        │  │
│  │               │    │                   │ ┌─>│  writes data.csv │  │
│  │ Reads CSV     │    │ Runs BFComputer   │ │  └──────────────────┘  │
│  │ via Session-  │    │ .compute() per    │ │                        │
│  │ Samples       │    │ sample            │ │  ┌──────────────────┐  │
│  └──────────────┘    │                   │ │  │  DataPlotter     │  │
│                       │  out_q_1 ─────────┘ │  │  (Thread)        │  │
│                       │  out_q_mp ──────────┘  │                  │  │
│                       └───────────────────┘    │  Drives BFPlot   │  │
│                                                │  widgets via     │  │
│                                                │  PyQtGraph/PyQt6 │  │
│                                                └──────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
Raw CSV file
  │
  ▼
SessionSamples.read_csv()          ← Parses CSV into numpy array
  │
  ▼
SessionSamples.get(i) → Sample     ← Returns named tuple per row
  │                                   (side, cam_id, frame_id,
  │                                    timestamp, hist[1024],
  │                                    temp, sum)
  ▼
DataProducerMockup                 ← Pushes Samples into queue
  │                                   at 40 Hz (25ms interval)
  ▼
DataProcessorMockup                ← Pulls from producer queue
  │  │
  │  └─ BFComputer.compute(sample)
  │       │
  │       ├─ Computes mean = dot(hist, bins) / sum
  │       ├─ Computes contrast = std / mean
  │       ├─ BFI = contrast × 10
  │       ├─ BVI = mean × 10
  │       └─ Estimator: averages across N cameras
  │            (emits result every Nth sample)
  │
  ├──► out_q_1 ──► DataStorageMockup ──► data.csv
  │
  └──► out_q_mp ─► DataPlotter
                      │
                      ├─ left_plot (BFPlot)  ── BFI (red, left axis)
                      │                         BVI (blue, right axis)
                      └─ right_plot (BFPlot) ── BFI (red, left axis)
                                                BVI (blue, right axis)
```

---

## 3. Module Descriptions

### 3.1 stream_proto.py — Real-Time Pipeline Entry Point

Orchestrates the streaming pipeline. Contains:

| Class | Role | Threading |
|---|---|---|
| `ProducerBase` | Abstract base: owns output queue + stop event | Thread |
| `DataProducerMockup` | Reads CSV rows as simulated camera feed | Thread |
| `DataProcessorMockup` | Computes BFI/BVI from raw histograms | Thread |
| `DataStorageMockup` | Writes (timestamp, BFI) pairs to CSV | Thread |
| `DataPlotter` | Feeds processed data into BFPlot widgets | Thread |

Also provides `run_data_plot()` for running the plotter in a **separate process**
via `multiprocessing.Process`.

### 3.2 bfplot.py — Real-Time Plot Widget

`BFPlot` class wraps a `pyqtgraph.PlotWidget` with:

- Dual Y-axes: left (BFI, red) and right (BVI, blue)
- Scrolling window of fixed size (`window_size` parameter)
- QTimer-driven redraw at 10ms intervals (100 Hz polling rate)
- Auto-scaling on the Y-axis
- Thread-safe data update via `threading.Event` signaling

### 3.3 bfcompute.py — Simplified BFI/BVI Computation

Streaming-oriented computation, simplified from the full batch algorithm:

- `BFComputer.compute(sample)` — single-sample BFI/BVI from histogram
- `Estimator` — accumulates values across N cameras, emits averaged result
- `CamCalib` — placeholder for per-camera calibration data (not used in simplified path)
- `BFValue` — named tuple output: (side, cam_id, frame_id, timestamp, temp, sum, mean, contrast, bfi, bvi)

### 3.4 session_samples.py — CSV Data Loader

Reads raw scan CSV files into a numpy array and provides indexed access.

**CSV format** (per row):
```
cam_id, frame_id, timestamp_s, bin_0 ... bin_1023, temperature, sum, tcm, tcl, pdc
```
- 1032 columns per row
- Columns 3–1026: 1024-bin histogram
- Typical file: ~2592 rows (e.g., 4 cameras × 648 frames)

### 3.5 vbf_stat.py — Batch Analysis & Profiling

`VisualizeBloodflow` dataclass: full offline BFI/BVI computation with:

- Dark frame subtraction (every 600 frames)
- Dark stats interpolation across acquisition window
- Calibration normalization (C_min/C_max for contrast, I_min/I_max for intensity)
- Quadratic interpolation to fill dark frame gaps
- Plotting via matplotlib and fastplotlib
- CSV export of results

`main_profile()`: end-to-end benchmarking of compression, DB I/O, and compute.

### 3.6 hist_db.py — Storage Backend Toolkit

Utilities for histogram data storage and retrieval:

| Category | Functions |
|---|---|
| **Compression** | `compress_zlib`, `compress_zstd`, `csv_to_gzip_blob`, `delta_encode/decode` |
| **SQLite** | `init_sqlite_db`, `insert_blob_sqlite`, `retrieve_blob_sqlite` |
| **MongoDB** | `init_mongodb`, `store_csv_blob_to_mongo`, `retrieve_csv_blob_from_mongo` |
| **File I/O** | `read_csv_file`, `write_csv_file`, `csv_to_binary_dataframe` |

### 3.7 profile_hist.py — Profiling Infrastructure

- `ProfileHist` — streaming histogram (via `streamhist`) for recording distributions
  of measured values (execution times, etc.)
- `ExecutionTimer` — wraps any callable, measures `perf_counter` duration per call,
  accumulates into ProfileHist for statistical analysis

---

## 4. SQLite Data Organization

### Schema

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE samples (
    ts          INTEGER NOT NULL,   -- Timestamp (Unix epoch, integer seconds)
    sensor_id   INTEGER NOT NULL,   -- Camera/sensor identifier
    is_keyframe INTEGER NOT NULL,   -- 1 = keyframe (full data), 0 = delta frame
    hist        BLOB    NOT NULL    -- Compressed histogram data (entire scan file)
);
```

### Design Notes

- **No primary key or indexes**: queries by `(ts, sensor_id)` currently require
  full table scans.
- **WAL mode**: enables concurrent readers and a single writer, suitable for the
  threaded pipeline where producer writes and consumers may read simultaneously.
- **BLOB granularity**: each row stores an **entire compressed scan file**, not
  individual per-frame histograms. The blob contains all cameras and all frames
  from one acquisition session.
- **Compression options tested**: zlib (levels 3, 6) and zstd (levels 3, 6).
  The `delta_encode/decode` functions in hist_db.py suggest a planned optimization
  where delta encoding is applied before compression to improve ratios.
- **Keyframe column**: suggests a planned keyframe/delta storage pattern where
  full histogram snapshots are stored periodically with deltas in between, but
  this is not yet wired into the pipeline.
- **No uniqueness constraint**: duplicate `(ts, sensor_id)` pairs can be inserted.

### Data Flow: CSV → SQLite

```
Raw CSV file (on disk)
  │
  ▼
read_csv_file() → raw bytes
  │
  ▼
compress_zstd() or compress_zlib() → compressed bytes
  │
  ▼
insert_blob_sqlite(conn, ts, sensor_id, is_keyframe, blob)
  │
  ▼
┌────────────────────────────────────┐
│ samples table                      │
│ ts=111, sensor_id=1, is_kf=1,     │
│ hist=<compressed scan file bytes>  │
└────────────────────────────────────┘
  │
  ▼
retrieve_blob_sqlite(conn, ts, sensor_id) → compressed bytes
  │
  ▼
decompress_csv_zlib() → CSV string
```

### MongoDB (Alternative Backend)

Same logical schema, stored as BSON documents:
```json
{
    "ts": 111,
    "sensor_id": 1,
    "is_keyframe": 1,
    "hist": BinData(compressed_bytes)
}
```

Collection: `test_db.samples`, accessed via `pymongo` on `localhost:27017`.

---

## 5. Threading & Concurrency Model

```
Main Thread (Qt Event Loop)
  │
  ├── QTimer (10ms) ──► BFPlot.set_data()    [GUI redraw]
  │
  ├── Thread: DataProducerMockup             [data source, 40 Hz]
  │     └── Queue(10000) ──────────────┐
  │                                     ▼
  ├── Thread: DataProcessorMockup      [BFI/BVI compute]
  │     ├── Queue(10000) ──► Thread: DataStorageMockup  [CSV write]
  │     └── mp.Queue(10000) ──► Thread: DataPlotter     [plot update]
  │
  └── (Optional) Process: run_data_plot  [separate process for plotting]
```

### Synchronization Primitives

| Primitive | Used By | Purpose |
|---|---|---|
| `threading.Event` (stop_event) | All threads | Graceful shutdown signal |
| `threading.Event` (produced_event) | Producer → Storage | Signals data availability |
| `threading.Event` (set_data_event) | DataPlotter → BFPlot | Triggers GUI redraw |
| `queue.Queue` | Producer → Processor → Storage | Thread-safe data passing |
| `multiprocessing.Queue` | Processor → Plotter | Cross-process data passing |
| `SENTINEL (None)` | All stages | Signals end-of-stream |

---

## 6. Configuration & Constants

| Parameter | Value | Location |
|---|---|---|
| Camera count | 4 (hardcoded) | stream_proto.py:82, bfcompute.py:141 |
| Acquisition rate | 40 Hz | stream_proto.py:224, vbf_stat.py:55 |
| Producer period | 25ms | stream_proto.py:224 |
| Queue max size | 10,000 | stream_proto.py:32, 76, 77 |
| Plot window size | 250–500 points | stream_proto.py:222, bfplot.py:112 |
| Plot redraw interval | 10ms | bfplot.py:53 |
| Dark interval | 600 frames | vbf_stat.py:56 |
| Histogram bins | 1024 | bfcompute.py:83, vbf_stat.py:142 |
| Profiling cycles | 100 | vbf_stat.py:588 |

---

## 7. Dependencies

| Package | Purpose |
|---|---|
| `numpy`, `pandas`, `scipy` | Numerical computation, CSV parsing, signal processing |
| `PyQt6` | GUI framework |
| `pyqtgraph` | Real-time plotting widgets |
| `matplotlib` | Batch/offline plotting |
| `fastplotlib` | GPU-accelerated interactive plotting (batch path) |
| `pymongo` | MongoDB client |
| `zstandard` | Zstd compression |
| `streamhist` | Streaming histogram for profiling |
| `tqdm` | Progress bars |
