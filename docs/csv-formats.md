# CSV File Formats

The Open-Motion app keeps every scan in its local scan database (`scans.db`).
It writes CSV files only when you export a scan or turn on an optional
per-scan output. This page describes each CSV file, column by column.

| File | You get it when | Builds |
|---|---|---|
| [Exported scan](#exported-scan-csv) `<label>_export.csv` | **History → Export CSV** | All |
| [Raw histograms](#raw-histogram-csv) `<label>_<side>_mask<XX>_raw.csv` | **Settings → Data Output → Save raw CSV** is on during a scan | Research |
| [Console telemetry](#console-telemetry-csv) `<label>_telemetry.csv` | Engineering mode is unlocked and **Settings → Engineering → Save telemetry CSV** is on during a scan | All, engineering mode only |
| [Audit log](#other-csv-files) | Audit-log viewer → **Export CSV** | All |
| [Calibration / test results](#other-csv-files) | A calibration or test run | All |

Per-scan files (raw and telemetry) land in the `data/` folder under the app's
data directory, the same folder that holds `scans.db`. Exports go where you
choose.

---

## Conventions shared by every file

- UTF-8, comma-separated, one header row. Rows end in CRLF.
- **An empty cell means no value.** The exceptions are called out where they
  apply.
- **`<label>`** is `{scan_id}_{subject_id}`, the same label the History list
  shows. `scan_id` is the scan's start time in **UTC**, formatted
  `YYYYMMDD_HHMMSS`. `subject_id` is the subject ID entered for the scan, for
  example `ow9YC09G`.
- **Cameras:** each sensor module (left and right) has 8 cameras. Column
  *names* number them 1–8 (`bfi_l3` is left camera 3). Columns that hold a
  camera *value* (`cam_id`) number them 0–7, so `cam_id = 2` is camera 3.
- **Frames:** the system captures 40 frames per second, about 25 ms apart.
  Frame 1 is at `timestamp_s = 0`. Frames 1–9 are warm-up frames. Frame 10 and
  then every 600th frame (601, 1201, 1801, …, one every 15 s) is a **dark
  frame**: the laser is off, and the camera's reading on that frame is the
  baseline subtracted from the frames around it.

---

## Exported scan CSV

**How to get it:** open **History**, tick one or more scans, and click
**Export CSV**.

- **One scan:** a save dialog opens, suggesting `<label>_export.csv` in the
  data folder.
- **Several scans:** you pick a folder, and each scan is written there as
  `<label>_export.csv`.

Interrupted scans can't be exported.

The export is built from the scan database, so it contains the final
corrected values: each frame dark-subtracted against the dark frames on
either side of it. The live plot during a scan shows a quicker estimate
that can differ slightly from these values.

The file has one of two layouts. Which one you get depends on the build that
**recorded** the scan, not the build you export from.

### Research layout (98 columns)

Written for scans recorded on a Research build.

| Columns | Count | Meaning |
|---|---|---|
| `frame_id` | 1 | Frame number, counted from 1 at scan start. |
| `timestamp_s` | 1 | Seconds since scan start (up to 6 decimals). |
| `bfi_l1` … `bfi_l8`, `bfi_r1` … `bfi_r8` | 16 | Blood Flow Index per camera. |
| `bvi_l1` … `bvi_r8` | 16 | Blood Volume Index per camera. |
| `mean_l1` … `mean_r8` | 16 | Dark-subtracted mean intensity, in DN (camera pixel-value units). |
| `contrast_l1` … `contrast_r8` | 16 | Speckle contrast (unitless), corrected for dark level and shot noise. |
| `temp_l1` … `temp_r8` | 16 | Camera temperature, °C. |
| `quality_l1` … `quality_r8` | 16 | How the frame's timing was recorded (see below). |

All 16 cameras always have columns. Cameras that weren't scanned have empty
columns.

### Clinical layout (6 columns)

Written for scans recorded on a Clinical build.

| Column | Meaning |
|---|---|
| `frame_id` | Frame number, counted from 1 at scan start. |
| `timestamp_s` | Seconds since scan start. |
| `bfi_left`, `bfi_right` | BFI averaged across that module's scanned cameras at this frame. |
| `bvi_left`, `bvi_right` | BVI averaged the same way. |

Values have up to 6 decimals. A camera with no value at a frame is left out
of that frame's average. If a module's average can't be computed at a
frame, its cell is empty. For example, when every camera on one module is
covered, BVI is still computed but BFI is not, so that module's `bfi_` cell
is empty and its `bvi_` cell has a value.

### Rows

- One row per frame that has at least one value. Rows are ordered by
  `frame_id`.
- The first row is normally frame 10 (the first dark frame). Warm-up frames
  1–9 never appear.
- The scan's final frame is always a dark frame and has no row.
- **Dark-frame rows hold interpolated values.** The laser is off on a dark
  frame, so its values are estimated from the two frames on each side.
- A frame that no camera delivered has no row, so `frame_id` can skip.
- `timestamp_s` is the earliest timestamp among the cameras in the row. The
  left and right modules have separate clocks, so their timestamps for the
  same frame can differ by a few milliseconds.

### What the values mean

BFI and BVI come from the device's per-camera calibration (`c_min`, `c_max`,
`i_min`, `i_max`, stored on the console):

```
bfi = (1 − (contrast − c_min) / (c_max − c_min)) × 10
bvi = (1 − (mean     − i_min) / (i_max − i_min)) × 10
```

On a calibrated device both indices nominally run from 0 to 10, but the file
holds the computed value without clamping, so values below 0 or above 10 do
appear.

A per-camera cell is empty when that camera has no valid measurement at that
frame. Three things cause that: the frame was lost in transfer; the camera
saw no light above its dark level (covered or unlit); or its variance fell
below the shot-noise floor, which leaves `contrast` and `bfi` empty while
`mean` and `bvi` can still have values. The full processing chain is
described in the SDK's
[Science Pipeline reference](https://github.com/OpenwaterHealth/openmotion-sdk/blob/next/docs/SciencePipeline.md#58-darkcorrectionstage).

**`quality`** values:

| Value | Meaning |
|---|---|
| `ok` | Recorded as captured. |
| `ts_corrected` | The frame arrived with a timestamp off the 25 ms cadence; `timestamp_s` was replaced by one interpolated from its neighbours. The other values are measured. |
| `nan_filled` | A placeholder for a frame that never arrived. Such frames have no values, so in practice they rarely appear. |

### Older scans

- Scans recorded before August 2026 have empty `temp_*` columns: the scan
  database didn't store camera temperature until then.
- Scans recorded before the database stored per-frame frame numbers can't be
  exported. The export reports an error.

### Reading an export

```python
import pandas as pd

df = pd.read_csv("20261001_185429_owR3LAZW_export.csv")
left3 = df[["timestamp_s", "bfi_l3", "bvi_l3"]].dropna()
```

The SDK's `scripts/visualize_scan.py` turns any of the CSVs on this page into
an interactive HTML plot.

### Per-scan CSV written during the scan (not in released builds)

The SDK can also write the same table live, during the scan, as
`data/<label>.csv`. Released builds never do: the `writeCorrectedCsv` setting
is compiled off. A source run can turn it on with
`--config-override '{"writeCorrectedCsv": true}'`. That file differs from an
export in three ways:

- It has no `quality_*` columns (82 columns in the Research layout).
- A measurement that couldn't be computed is written as `nan` instead of an
  empty cell. Columns for cameras that weren't scanned are still empty.
- It is written as the scan runs, so a scan that ends abnormally can leave a
  partial file.

---

## Raw histogram CSV

**How to get it:** on a Research build, turn on **Settings → Data Output →
Save raw CSV** before the scan. **Raw CSV duration** limits how many seconds
are written. Leave it blank to write the whole scan. Writing stops at the
first batch of frames past the limit, so the file can run a fraction of a
second longer.

**Files:** one per sensor module, in `data/`:

```
<label>_left_mask<XX>_raw.csv
<label>_right_mask<XX>_raw.csv
```

`<XX>` is that module's camera mask as two uppercase hex digits. Bit *n* set
means camera `cam_id = n` (camera *n* + 1) was scanned. For example `C3` is
binary `11000011`: cameras 1, 2, 7 and 8.

This file holds the data **as the sensor sent it**, before the SDK repairs
or filters anything. It is the only place the raw histograms are kept; the
scan database stores only the computed values. Expect about 6 MB per camera
per minute. The size varies with the signal.

### Columns (1,033)

| Column | Meaning |
|---|---|
| `cam_id` | Camera, 0–7. |
| `frame_id` | The sensor's 8-bit frame counter as received, **not** the absolute frame number: it runs 1, 2, …, 255, 0, 1, … and wraps every 256 frames (6.4 s). Count the wraps per camera to get the absolute frame number. |
| `timestamp_s` | Seconds since scan start, from the sensor's clock, as received (before the SDK repairs off-cadence timestamps). Written at full float precision, e.g. `44.95099999999991`. |
| `type` | `warmup`, `dark`, `light` or `stale` (see below). |
| `0` … `1023` | The 1024-bin intensity histogram: the number of pixels at each value, 0 to 1023 DN. Unfiltered. |
| `temperature` | Camera temperature reported with the histogram, °C. Empty if none was reported. |
| `sum` | Sum of the 1024 bins, which equals the frame's pixel count plus 6: **2,457,606** for a full 1920 × 1280 frame, 2,201,606 for the debug-cropped 1720 × 1280 frame. Frames with any other sum are dropped before they are written. |
| `tcm` | Console MCU trigger count from the most recent telemetry poll at or before this frame. |
| `tcl` | Console laser trigger count, same sampling. |
| `pdc` | Photodiode current in mA, same sampling. |

`tcm`, `tcl` and `pdc` come from console telemetry, which the app polls
separately from the camera data. Each frame carries the latest poll's
values, not a measurement taken at that frame:

- `tcm` and `tcl` are whole numbers written with a decimal point
  (`4010.0`).
- Before the first poll of the scan, `tcm` and `tcl` are `0.0` (not empty)
  and `pdc` is empty.
- All three are empty if console telemetry wasn't running.

### Rows

- One row per camera per frame, in arrival order. The cameras of one frame
  are consecutive rows and share `frame_id` and `timestamp_s`.
- Warm-up and stale rows are kept.
- The SDK labels a row **`stale`** when it rejects the frame: a frame
  counter that went backwards or repeated, a counter jump the timestamps
  don't support, or leftover data at scan start before the camera's
  frame 1. Stale rows are kept as evidence and are excluded from every
  computed value.

### Reading a raw file

```python
import pandas as pd

raw = pd.read_csv("20260909_185839_ow9YC09G_left_maskC3_raw.csv")
light = raw[raw["type"] == "light"]
hist = light[[str(b) for b in range(1024)]].to_numpy()   # (rows, 1024)
```

---

## Console telemetry CSV

**How to get it:** unlock engineering mode, then turn on **Settings →
Engineering → Save telemetry CSV** before the scan. Engineering mode lasts
for the current session only, so the file is written only for scans started
while it is unlocked.

**File:** `data/<label>_telemetry.csv`, one row per console telemetry poll
during the scan.

The app aims to poll 10 times per second, but each poll waits for its console
reads to finish, so rows are often further apart: a scan recorded in
September 2026 has a row about every 1.4 s. Use `timestamp` rather than
assuming a fixed rate.

### Columns (50)

| Column | Meaning |
|---|---|
| `timestamp` | Host computer time when the poll finished: Unix time, in seconds. |
| `tcm` | Console MCU trigger count. |
| `tcl` | Laser trigger count. |
| `pdc` | Photodiode current, mA. |
| `tec_v_raw` | TEC thermistor voltage, V (the measured laser temperature, unconverted). |
| `tec_set_raw` | TEC setpoint voltage, V (the target temperature, unconverted). |
| `tec_curr_raw` | TEC current monitor voltage, V. |
| `tec_volt_raw` | TEC voltage monitor, V. |
| `tec_good` | `1` = no TEC over-temperature trip. `0` = the console tripped and shut the laser down, **or** the TEC wasn't read on this poll (check `read_ok`). |
| `tec_temp_c` | `tec_v_raw` converted to °C (2 decimals). |
| `tec_set_c` | `tec_set_raw` converted to °C (2 decimals). |
| `tec_curr_a` | TEC current, A (3 decimals). |
| `tec_volt_v` | TEC voltage, V (3 decimals). |
| `pdu_raw_0` … `pdu_raw_15` | Power-distribution monitor, raw 12-bit ADC counts (0–4095): channels 0–7 of the first ADC, then channels 0–7 of the second. Empty if fewer channels were read. |
| `pdu_volt_0` … `pdu_volt_15` | The same 16 channels as the voltage at the ADC input (count × 2.5 V / 4096). This is the voltage at the ADC pin, not necessarily the voltage of the rail that channel monitors. |
| `safety_se` | Raw status byte from the EE laser-safety monitor. |
| `safety_so` | Raw status byte from the OPT laser-safety monitor. |
| `safety_ok` | `1` when neither monitor reports a fault (the low 4 bits of both bytes are zero). |
| `read_ok` | `1` when every read in this poll succeeded, `0` otherwise. |
| `error` | The error message when `read_ok` is `0`; otherwise empty. |

On a row where a read failed, `tec_good` and `safety_ok` hold their defaults
(`0` and `1`), not measurements.

The converted TEC columns hold `nan` if the conversion table couldn't be
loaded.

---

## Other CSV files

- **Audit log export:** `ts_iso, ts_epoch, event_type, details`. See
  [Audit Log → Exporting to CSV](audit-log.md#exporting-to-csv).
- **Calibration and test results:** `data/calibrations/calibration-<timestamp>.csv`
  and `test-<timestamp>.csv`, one row per camera. Each row has the measured
  mean, contrast, BFI, BVI and dark level, a pass/fail result for each, and
  the device serial numbers. The column list is in `write_result_csv` in the
  SDK's `omotion/CalibrationWorkflow.py`.
- **Per-camera telemetry** (`<label>_<side>_cam<N>_telemetry.csv`): the SDK
  can log per-camera condition once a second, but the app never turns it on.
  See the SDK's `omotion/camera_telemetry_csv.py`.
