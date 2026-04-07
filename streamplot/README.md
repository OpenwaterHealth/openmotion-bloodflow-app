# Stream Plot Prototype

## What this module does
`stream_proto.py` is a prototype for a streaming blood-flow pipeline. It wires together:

- A mock data producer that replays rows from a CSV file as if they were live samples.
- A processing stage that runs a simplified BF/BV computation.
- Two storage stages (raw samples to SQLite, processed values to CSV).
- Two plotting consumers (PyQtGraph widgets and a QML signal producer).

The pipeline is multi-threaded and uses `Queue` objects to pass data between stages. A `SENTINEL` value is used to signal completion.

## Data flow
1. **DataProducerMockup**
   - Reads sample rows from a hard-coded CSV path.
   - Enqueues raw `Sample` objects into `out_q` and `out_q_raw_store`.
2. **DataProcessorMockup**
   - Dequeues raw samples, runs `BFComputer.compute()`.
   - Emits tuples `(timestamp, bfi_left, bvi_left, bfi_right, bvi_right)`.
   - Fans out to three queues: file storage, PyQtGraph plot, QML plot.
3. **RawDataStorageMockup**
   - Collects raw samples and stores them in a local SQLite DB via `SamplesDBsqlite`.
4. **ProcessedDataStorageMockup**
   - Writes processed values into `processed_data.csv` with `BFValue` headers.
5. **DataPlotter / DataPlotterQML**
   - PyQtGraph plotter updates two plots (Left/Right).
   - QML producer emits `bfUpdated(x, d1, d2, d3, d4)` signals.

## Data structures
### Baseline files
- Raw input baseline: [scan_data/scan_owZDZG9Y_20260330_124100_left_mask99.csv](scan_data/scan_owZDZG9Y_20260330_124100_left_mask99.csv)
- Processed output baseline: [scan_data/scan_owZDZG9Y_20260330_124100_bfi_results.csv](scan_data/scan_owZDZG9Y_20260330_124100_bfi_results.csv)

### Raw CSV rows
`SessionSamples.read_csv()` expects rows shaped like:

1. `cam_id` (float in CSV, cast to `uint32`)
2. `frame_id` (float in CSV, cast to `uint32`)
3. `timestamp_s` (float)
4. `hist[0..1023]` (1024 histogram bins)
5. `temperature` (float)
6. `sum` (float in CSV, cast to `uint64`)

Columns after `sum` (like `tcm`, `tcl`, `pdc`) are ignored by the current parser.

The baseline raw CSV header confirms this layout:

```text
cam_id,frame_id,timestamp_s,0,1,2,...,1023,temperature,sum,tcm,tcl,pdc
```

### `Sample` (raw frame)
From [streamplot/session_samples.py](streamplot/session_samples.py):

```python
class Sample(NamedTuple):
   side: np.uint32
   cam_id: np.uint32
   frame_id: np.uint32
   timestamp: np.float32
   hist: npt.NDArray[np.uint32]  # 1024 bins
   temp: np.float32
   summ: np.uint64
```

### `BFValue` (processed frame)
From [streamplot/bfcompute.py](streamplot/bfcompute.py):

```python
class BFValue(NamedTuple):
   side: np.uint32
   cam_id: np.uint32
   frame_id: np.uint32
   timestamp: np.float32
   temp: np.float32
   summ: np.uint64
   mean: np.float32
   contrast: np.float32
   bfi: np.float32
   bvi: np.float32
```

### Queue payloads
- Raw queue: `Sample` objects.
- Processed queues: a compact tuple `(timestamp, bfi_left, bvi_left, bfi_right, bvi_right)`.

### Processed CSV rows
The baseline processed CSV header is:

```text
camera,side,time_s,BFI,BVI
```

Field notes:
- `camera`: integer camera index.
- `side`: string label such as `left` or `right`.
- `time_s`: time in seconds (float).
- `BFI`: blood flow index (float).
- `BVI`: blood volume index (float).

## How to run
Run the module directly:

```bash
python stream_proto.py
```

A Qt window opens with PyQtGraph plots, and a QML window is also launched.

## Key files it depends on
- `session_samples.py`: reads CSV rows into `Sample` objects for replay.
- `bfcompute.py`: simplified BF/BV calculation logic.
- `bfplot.py`: PyQtGraph plotting helper (`BFPlot`).
- `bfstorage.py`: SQLite storage helpers for raw data.
- `bfplot_qml_app.qml`: QML UI that receives `bfUpdated` signals.

## Notes and limitations
- The CSV input path is hard-coded in `DataProducerMockup`.
- The prototype uses very large queue sizes.
- This is a prototype; many settings are fixed.
