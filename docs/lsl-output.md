# LSL Output

Open-Motion can publish a running scan as two
[Lab Streaming Layer](https://labstreaminglayer.org) (LSL) streams, so
LabRecorder or any other LSL client on the same network can record or
display the data alongside other devices.

**How to turn it on:** **Settings → Data Output → LSL output**. The switch
is shown on Research builds, and on a Clinical build only while engineering
mode is unlocked. It takes effect at the next scan start. The setting is
remembered between launches.

## The two streams

| Stream name | Values | When samples go out |
|---|---|---|
| `OpenMotion-Live` | The realtime values the live plot shows. | As each frame arrives, about 40 per second. |
| `OpenMotion-Corrected` | The final corrected values that the scan database stores and **History → Export CSV** writes. | In bursts of about 600 frames, every 15 s, once the next dark frame closes each interval. |

Both have stream type `BloodFlow`, a nominal rate of 40 Hz and
`double64` samples. The `source_id` is
`openmotion-bloodflow-<live|corrected>-<research|clinical>`. It stays the
same from scan to scan, so an LSL inlet left open picks up the next scan's
stream by itself. The streams exist only while a scan runs.

## Channels

One channel per column of the corrected scan CSV, with the same names in
the same order ([CSV File Formats](csv-formats.md#exported-scan-csv)). The
layout follows the build the scan runs on:

- **Research layout, 82 channels:** `frame_id`, `timestamp_s`,
  `bfi_l1` … `bfi_r8`, `bvi_l1` … `bvi_r8`, `mean_l1` … `mean_r8`,
  `contrast_l1` … `contrast_r8`, `temp_l1` … `temp_r8`.
- **Clinical layout, 6 channels:** `frame_id`, `timestamp_s`, `bfi_left`,
  `bfi_right`, `bvi_left`, `bvi_right`.

The export's `quality_*` columns are text, which a numeric LSL stream
can't carry, so they are not included. This matches the 82-column per-scan
CSV.

Each channel in the stream header has `label`, `unit` (`count`, `s`,
`index`, `DN`, `unitless` or `celsius`), `type` and, for camera
columns, `side` and `camera`. The header also carries `app_version`,
`layout`, `scan_id` and the camera masks. It does not carry the subject ID.

An empty CSV cell is `NaN` in the stream.

## Rows and timestamps

- Warm-up and rejected (stale) frames are never sent, the same as the CSV.
- A row is sent once every scanned camera has delivered that frame. If a
  camera drops a frame, the row goes out anyway with `NaN` for that camera:
  after about 1 s on the live stream, after two dark intervals (about 30 s)
  on the corrected one.
- Each sample's LSL timestamp is the frame's `timestamp_s` placed on the
  LSL clock, anchored when the first frame is sent. The live and the
  corrected sample for one frame carry the same LSL time, even though the
  corrected one is sent about 15 s later.

## Network

LSL streams are visible to every computer on the local network segment.
Use it on a network you trust. If liblsl can't load, the scan still runs,
and a notification says LSL output is unavailable.
