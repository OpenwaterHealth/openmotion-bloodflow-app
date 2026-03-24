# OpenWater Bloodflow App — Data Structure Documentation

## 1. Summary

The OpenWater Bloodflow App is a PyQt6/QML desktop application that interfaces with a custom optical hardware console and left/right sensor modules to capture near-infrared speckle histogram data, process it in real time, and compute Blood Flow Index (BFI) and Blood Volume Index (BVI) metrics. Captured binary streams are parsed into CSV files, post-processed with dark-frame subtraction and calibration, and visualized via matplotlib.

**Key Constraints:**
- **Real-time streaming**: Binary histogram packets arrive at ~40 Hz per camera; parsing and CSV writing must keep up without dropping frames.
- **Concurrency**: Multiple threads handle capture (per-side writer threads), telemetry polling (`ConsoleStatusThread`), correction (`_correction_worker`), and visualization (`_VizWorker`).
- **Scale**: Up to 16 cameras (8 per module × 2 modules), each producing 1024-bin histograms at 40 fps → ~65 KB/s per camera raw.
- **Storage**: Raw binary → CSV conversion produces large files (millions of rows per scan). In-memory numpy arrays used for post-processing.

---

## 2. Core Entities

### 2.1 `MOTIONConnector`
**Role:** Central bridge between the QML UI and all hardware/processing subsystems.

| Field | Type | Mutable | Description |
|---|---|---|---|
| `_interface` | `MOTIONInterface` | No | Singleton SDK handle |
| `_state` | `int` (enum: 0–4) | Yes | System FSM state (DISCONNECTED → READY → RUNNING) |
| `_leftSensorConnected` | `bool` | Yes | Left sensor USB connection status |         
| `_rightSensorConnected` | `bool` | Yes | Right sensor USB connection status |
| `_consoleConnected` | `bool` | Yes | Console USB connection status |
| `_laserOn` | `bool` | Yes | Laser active flag |
| `_safetyFailure` | `bool` | Yes | Safety interlock failure flag |
| `_trigger_state` | `str` ("ON"/"OFF") | Yes | Trigger running state |
| `_directory` | `str` | Yes | Scan data output directory |
| `_subject_id` | `str` | Yes | Subject identifier (format: `ow` + 6 alphanumeric chars) |
| `_scan_notes` | `str` | Yes | Free-text notes for current scan |
| `laser_params` | `List[dict]` | No (loaded once) | Laser I2C configuration parameters |
| `_tec_voltage_default` | `float` | No (loaded once) | TEC default voltage from config |
| `_data_RT` | `np.ndarray` (shape N×2) | No (loaded once) | Thermistor R-T lookup table |
| `_tcm`, `_tcl`, `_pdc` | `float` | Yes | Telemetry: trigger count MCU, trigger count laser, photodiode current |
| `_tec_voltage`, `_tec_temp`, `_tec_monV`, `_tec_monC`, `_tec_good` | `float`/`bool` | Yes | TEC status readings |
| `_pdu_raws` | `List[int]` (len 16) | Yes | Raw ADC values from PDU monitor (2×8 channels) |
| `_pdu_vals` | `List[float]` (len 16) | Yes | Scaled voltage values from PDU monitor |
| `_capture_running` | `bool` | Yes | Guard for single-capture-at-a-time |
| `_capture_left_path`, `_capture_right_path` | `str` | Yes | Active capture output CSV paths |

**ID Strategy:** `_subject_id` is auto-generated as `"ow"` + 6 random uppercase alphanumerics via `generate_subject_id()`. User may override via QML setter (normalized to `"ow"` + uppercase alphanumerics).

### 2.2 `DataProcessor`
**Role:** Stateless parser that converts binary histogram packets into structured CSV rows.

No persistent fields. Operates on raw `memoryview` buffers and writes to `csv.writer` objects.

### 2.3 `VisualizeBloodflow`
**Role:** Computes BFI/BVI from parsed CSV histogram data and generates plots.

| Field | Type | Mutable | Description |
|---|---|---|---|
| `left_csv` | `str` | No (init) | Path to left module CSV |
| `right_csv` | `Optional[str]` | No (init) | Path to right module CSV |
| `t1`, `t2` | `float` | No (init) | Time window (seconds) |
| `frequency_hz` | `int` | No (init) | Frame rate (default 40) |
| `dark_interval` | `int` | No (init) | Frames between dark measurements (default 600) |
| `noisy_bin_min` | `int` | No (init) | Noise floor threshold (default 10) |
| `I_min`, `I_max` | `np.ndarray` (2×8) | No (init) | Intensity calibration bounds per module × camera |
| `C_min`, `C_max` | `np.ndarray` (2×8) | No (init) | Contrast calibration bounds per module × camera |
| `_BFI` | `np.ndarray` (cams × frames) | Yes (after compute) | Blood Flow Index time series |
| `_BVI` | `np.ndarray` (cams × frames) | Yes (after compute) | Blood Volume Index time series |
| `_camera_inds` | `np.ndarray` | Yes (after compute) | Unique camera IDs present in data |
| `_contrast` | `np.ndarray` (cams × frames) | Yes (after compute) | Speckle contrast time series |
| `_mean` | `np.ndarray` (cams × frames) | Yes (after compute) | Mean intensity time series |
| `_sides` | `np.ndarray[str]` | Yes (after compute) | "left"/"right" label per camera |
| `_nmodules` | `int` | Yes (after compute) | Module count (1 or 2) |

### 2.4 `CSVIntegrityChecker`
**Role:** Validates parsed CSV files for data corruption.

| Field | Type | Mutable | Description |
|---|---|---|---|
| `cfg` | `CheckConfig` | No | Validation parameters |

**`CheckConfig`:**
| Field | Type | Default |
|---|---|---|
| `expected_sum` | `int` | 2,457,606 |
| `max_frame_id` | `int` | 255 |

**`CheckResult`:**
| Field | Type | Description |
|---|---|---|
| `passed` | `bool` | Overall pass/fail |
| `error_counts` | `Dict[str, int]` | Counts for `bad_sum`, `frame_id_skipped`, `bad_frame_cam_count` |
| `cam_hist_counts` | `Dict[int, int]` | Number of histogram rows per `cam_id` |
| `skipped_percentage` | `float` | % of skipped frame IDs |
| `details` | `Dict[str, object]` | `expected_cam_count`, `bad_sum_rows`, `skipped_expected_fids` |

### 2.5 `MOTIONInterface` (External SDK — singleton)
**Role:** Low-level USB interface to hardware.

| Field | Type | Description |
|---|---|---|
| `console_module` | object | Console hardware commands (trigger, TEC, I2C, RGB, fan) |
| `sensors` | `Dict[str, SensorModule]` | `{"left": ..., "right": ...}` — sensor hardware handles |

**Acquired once** via `MOTIONInterface.acquire_motion_interface()` which returns `(interface, console_connected, left_sensor, right_sensor)`.

---

## 3. Relationships

```
MOTIONInterface (singleton)
 ├── 1:1    console_module
 └── 1:N    sensors {"left", "right"}
                └── 1:1    uart.histo (streaming interface)

MOTIONConnector
 ├── 1:1    MOTIONInterface (via motion_singleton)
 ├── 1:1    ConsoleStatusThread (polling loop)
 ├── 1:N    CaptureWriter threads (1 per active side during scan)
 ├── 1:1    _ConfigureWorker (QThread, transient)
 ├── 1:1    _VizWorker (QThread, transient)
 └── 1:1    _correction_worker (daemon thread, permanent)

DataProcessor
 └── stateless, instantiated per-use

VisualizeBloodflow
 └── standalone per-analysis invocation
     └── reads CSV(s) produced by DataProcessor

CSVIntegrityChecker
 └── standalone per-file validation
```

**Lifecycle Rules:**
- `MOTIONInterface` lives for the application lifetime. Created once at import of `motion_singleton.py`.
- `MOTIONConnector` is created once in `main.py` and registered as a QML singleton.
- `ConsoleStatusThread` starts when the console connects, stops on disconnect or shutdown.
- Capture writer threads are created per `startCapture()` call and joined when capture completes or is canceled.
- `_VizWorker` and `_ConfigureWorker` are ephemeral QThread workers created on demand and destroyed on completion.
- Per-run log files and CSV telemetry logs are opened at trigger start (`_start_runlog`) and closed at trigger stop (`_stop_runlog`).

---

## 4. Data Structures

### 4.1 Histogram Packet (Binary Protocol)

```
┌─────────────────────────────────────────────────────────────┐
│ Header (6 B): SOF(0xAA) | type(0x00) | packet_len(u32-LE)  │
├─────────────────────────────────────────────────────────────┤
│ [Optional] Timestamp (4 B): milliseconds (u32-LE)          │
├─────────────────────────────────────────────────────────────┤
│ Block 0:                                                    │
│   SOH(0xFF) | cam_id(u8) | histogram[1024](u32-LE each)    │
│   | temperature(f32-LE) | EOH(0xEE)                        │
├─────────────────────────────────────────────────────────────┤
│ Block 1..N: (same structure)                                │
├─────────────────────────────────────────────────────────────┤
│ Footer (3 B): CRC16(u16-LE) | EOF(0xDD)                    │
└─────────────────────────────────────────────────────────────┘
```

**Structure type:** Byte-packed framed protocol  
**Why:** Wire-efficient for USB streaming; CRC provides integrity; variable camera count per packet.  
**Access pattern:** Sequential scan with packet resync on error (search for `0xAA 0x00 0x41` magic).  
**Complexity:** `O(n)` linear scan, `O(1)` per field extraction via `struct.unpack_from`.

### 4.2 Parsed Histogram Row (CSV)

| Column | Type | Description |
|---|---|---|
| `cam_id` | int | Camera identifier (0–7 per module) |
| `frame_id` | int | Frame sequence (0–255, rolls over) |
| `timestamp_s` | float | Timestamp in seconds (from packet or 0.0) |
| `0..1023` | int | 1024 histogram bin counts |
| `temperature` | float | Sensor temperature (°C) |
| `sum` | int | Sum of all 1024 bins |
| `tcm` | int | Trigger count MCU (appended during streaming) |
| `tcl` | int | Trigger count laser (appended during streaming) |
| `pdc` | float | Photodiode current mA (appended during streaming) |

**Structure type:** Flat CSV table (columnar)  
**Why:** Compatible with pandas for downstream analysis; appendable during streaming; human-readable.  
**Access pattern:** Sequential append during capture; full-file read for post-processing.  
**Row count:** ~40 rows/sec × cameras × duration. A 2-min scan with 8 cameras ≈ 38,400 rows.

### 4.3 BFI/BVI Computation Arrays (In-Memory)

| Array | Shape | Type | Description |
|---|---|---|---|
| `histos` | `(ncameras, ntimepts, 1024)` | `float64` | 3D histogram tensor |
| `histos_dark` | `(ncameras, ndark, 1024)` | `float64` | Dark-frame histograms |
| `mean` | `(ncameras, ntimepts)` | `float64` | Mean intensity per camera per frame |
| `contrast` | `(ncameras, ntimepts)` | `float64` | Speckle contrast (σ/μ) |
| `BFI` | `(ncameras, nframes)` | `float64` | Blood Flow Index (calibrated) |
| `BVI` | `(ncameras, nframes)` | `float64` | Blood Volume Index (calibrated) |
| `u1_dark`, `var_dark` | `(ncameras, ntimepts)` | `float64` | Interpolated dark baselines |
| `camera_inds` | `(ncameras,)` | `int/float` | Unique camera IDs |

**Structure type:** NumPy ndarrays (dense tensors)  
**Why:** Vectorized operations for compute-heavy statistical processing; numpy's BLAS/LAPACK backend for performance.  
**Access pattern:** Built once from CSV, multiple passes (dark subtraction, moment calculation, calibration normalization). Read-only after `compute()`.  
**Complexity:** `O(cameras × frames × bins)` for moment computation (`O(C·T·1024)`).

### 4.4 Calibration Matrices

| Array | Shape | Description |
|---|---|---|
| `C_min` | `(2, 8)` | Contrast minimum per (module, camera_position) |
| `C_max` | `(2, 8)` | Contrast maximum per (module, camera_position) |
| `I_min` | `(2, 8)` | Intensity minimum per (module, camera_position) |
| `I_max` | `(2, 8)` | Intensity maximum per (module, camera_position) |

**Structure type:** Small fixed-size 2D numpy arrays  
**Why:** Direct index lookup by `(module_idx, cam_pos)` → O(1). Hardcoded defaults; no runtime IO needed.

### 4.5 Laser Parameters (Config)

```json
[
  {
    "muxIdx": 1,
    "channel": 4,
    "i2cAddr": 65,
    "offset": 0,
    "dataToSend": [27, 6, 0]
  },
  ...
]
```

**Structure type:** JSON array of dicts → Python `List[dict]`  
**Why:** Declarative hardware configuration; easy to edit without code changes. Iterated sequentially to send I2C write commands.  
**Access pattern:** Loaded once at startup, iterated in order for laser power configuration.

### 4.6 TEC Parameters (Config)

```json
{
  "TEC_VOLTAGE_DEFAULT": 1.1
}
```

**Structure type:** JSON object → single float extraction  
**Why:** Simple key-value config for thermoelectric cooler voltage.

### 4.7 R-T Lookup Table

**Source:** `models/10K3CG_R-T.csv` (thermistor resistance-to-temperature curve)  
**Structure type:** `np.ndarray` shape `(N, 2)` — column 0 = temperature (°C), column 1 = resistance (Ω)  
**Why:** `np.interp` performs binary search interpolation in O(log N) for ADC-to-temperature conversion.

### 4.8 System State Machine

```
DISCONNECTED (0) ──► SENSOR_CONNECTED (1)
       │                      │
       ▼                      ▼
CONSOLE_CONNECTED (2) ──► READY (3) ──► RUNNING (4)
```

**Structure type:** Integer enum (constants)  
**Why:** Minimal FSM for UI state gating. Transitions driven by `on_connected` / `on_disconnected` callbacks.

### 4.9 Scan File Naming Convention

```
scan_{subjectId}_{YYYYMMDD_HHMMSS}_{side}_mask{XX}.csv   # histogram data
scan_{subjectId}_{YYYYMMDD_HHMMSS}_notes.txt              # scan notes
scan_{subjectId}_{YYYYMMDD_HHMMSS}_bfi_results.csv        # computed BFI/BVI
```

**Structure type:** Filesystem directory listing, glob-matched  
**Access pattern:** `get_scan_list()` globs for `scan_*_notes.txt`, extracts `{subjectId}_{timestamp}`, sorts by timestamp descending.

### 4.10 Real-Time Correction State (Per-Camera)

```python
per_camera_state[("left"|"right", cam_id)] = {
    "count": int,         # frames seen
    "last_bfi": float,   # last valid BFI
    "last_bvi": float,   # last valid BVI
}
```

**Structure type:** `Dict[Tuple[str,int], Dict]` (hash map)  
**Why:** O(1) lookup per incoming frame. Holds-and-repeats last valid BFI/BVI when mean intensity drops below threshold (66), suppressing noise during dark frames.  
**Fed by:** `queue.Queue` (`_corr_queue`) — producer/consumer pattern from writer threads to correction daemon.

### 4.11 Streaming Queues

| Queue | Producer | Consumer | Item Type |
|---|---|---|---|
| `sensor.uart.histo` queue | USB driver (SDK) | `_write_stream_to_file` thread | `bytes` (raw binary) |
| `_corr_queue` | Writer thread `_on_row` callback | `_correction_worker` daemon | `Tuple[str, int, float, float, float, float]` |

**Structure type:** `queue.Queue` (thread-safe FIFO)  
**Why:** Decouples producer rate from consumer processing; built-in blocking with timeout.

### 4.12 Telemetry CSV Log (Per-Run)

| Column | Type |
|---|---|
| `timestamp` | ISO 8601 string |
| `unix_ms` | int |
| `tcm` | int |
| `tcl` | int |
| `pdc` | float (3 decimal places) |

**Structure type:** Append-only CSV file  
**Access pattern:** Written by `ConsoleStatusThread` poll loop; one row per ~1s polling interval.

### 4.13 PDU Monitor Data

```python
_pdu_raws = [int] * 16   # 2 ADCs × 8 channels, raw ADC counts
_pdu_vals = [float] * 16  # scaled voltage values
```

**Structure type:** Fixed-length lists  
**Why:** Direct index mapping to ADC0 channels [0:8] and ADC1 channels [8:16]. Updated atomically by `pdu_mon()`.

---

## 5. Storage Strategy

| Data | Storage | Format | Notes |
|---|---|---|---|
| Raw sensor stream | In-memory queue → disk | Binary packets → CSV | Streamed, not held entirely in memory |
| Histogram CSVs | Local filesystem | CSV (1024+ columns) | Primary archival format |
| BFI/BVI results | Local filesystem | CSV (camera, side, time_s, BFI, BVI) | Compact results file |
| Scan notes | Local filesystem | Plain text | One file per scan |
| Run logs | Local filesystem | `.log` (text) + `.csv` (telemetry) | Per-trigger session |
| App logs | Local filesystem | `.log` (text) | Per-application launch |
| Config (laser, TEC) | Local filesystem | JSON | Read-only at startup |
| Calibration (R-T) | Local filesystem | CSV (2-column) | Read-only at startup |
| Calibration (BFI/BVI) | Hardcoded | numpy arrays | Compile-time constants in `VisualizeBloodflow` |

**Indexing:** Filesystem glob patterns serve as the "index" for scan discovery. No database.  
**Caching:** R-T lookup table loaded once into `_data_RT` numpy array. Calibration arrays are class-level constants.

---

## 6. Scale & Risks

### Bottlenecks
- **CSV write throughput**: At 16 cameras × 40 Hz, the writer thread must flush ~640 rows/sec with 1027+ columns each. Disk I/O on slow storage (e.g., USB drives) could cause queue backup and frame drops.
- **Post-processing memory**: `VisualizeBloodflow._readdata()` loads the entire CSV into a numpy array. A 10-min scan with 16 cameras produces ~384K rows × 1027 columns ≈ 3 GB float64 tensor. This can exhaust memory on 8 GB machines.
- **Binary parser resync**: On packet corruption, linear scan for resync pattern `0xAA 0x00 0x41` may skip valid data if the pattern appears inside histogram payload.

### Concurrency Concerns
- **`_telemetry_lock`**: Protects `_tcm`, `_tcl`, `_pdc` shared between `ConsoleStatusThread` and writer threads. Minimal contention (read-heavy in writers, write-only by status thread).
- **`_runlog_csv_lock`**: Protects CSV telemetry log writes. Low frequency (~1 Hz), no real contention risk.
- **No lock on `_capture_running`/`_capture_thread`**: These boolean/thread guards are set/checked across threads without synchronization. Race condition possible if `startCapture` is called rapidly, though the QML UI practically prevents this.
- **Qt signal/slot across threads**: `scanMeanSampled`, `scanBfiSampled` etc. are emitted from writer threads. Qt's queued connection mechanism handles cross-thread delivery, but high-frequency emission (640 signals/sec) may saturate the event loop.

### Data Growth
- **Scan data**: ~20 MB CSV per minute per camera on disk. Extended sessions or many subjects will fill local storage.
- **Run logs**: Modest (KB per session), but accumulate indefinitely in `run-logs/` and `app-logs/` directories. No automatic cleanup or rotation.
- **No data lifecycle management**: Old scans, logs, and results persist until manually deleted.

### Assumptions Made
1. The `omotion` SDK (`MOTIONInterface`) is a closed-source external dependency; its internal data structures are opaque.
2. Camera IDs within a module are in range 0–7; maximum 8 cameras per sensor module.
3. The dark interval (600 frames) and frame rate (40 Hz) are hardware-dictated constants for the current device generation.
4. All captured data is stored locally — no network/cloud storage path exists.
