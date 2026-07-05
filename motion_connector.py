from PyQt6.QtCore import (
    QObject,
    pyqtSignal,
    pyqtProperty,
    pyqtSlot,
    QVariant,
    QTimer,
    QRecursiveMutex,
    QCoreApplication,
    QMetaObject,
    Qt,
)
from pathlib import Path
from typing import NamedTuple, Optional
import itertools
import logging
import math
import base58
import threading
import json
import os
import sys
import datetime
import time
import random
import re
import string

from omotion import MotionInterface
from omotion.firmware_update import (
    FirmwareKind,
    check_latest,
    is_update_available,
)

from omotion.config import (
    DEBUG_FLAG_USB_PRINTF,
    DEBUG_FLAG_FAKE_DATA,
    DEBUG_FLAG_HISTO_THROTTLE,
    DEBUG_FLAG_HISTO_CMP,
    DEBUG_FLAG_COMM_VERBOSE,
    DEBUG_FLAG_CMD_VERBOSE,
    DEBUG_FLAG_SEND_DEFER,
    DEBUG_FLAG_HISTO_STALL,
)
from omotion.MotionProcessing import process_bin_file
from omotion.ScanWorkflow import ConfigureRequest, ScanRequest
from motion_config import (
    TEC_TRIP_MAX_C,
    TEC_TRIP_MIN_C,
    TecTripOutcome,
    ensure_tec_trip,
    load_tec_params,
)
import error_codes
import bug_report
from nan_gap_tracker import NanGapTracker, gap_note_line
from utils.resource_path import resource_path
from utils import app_paths, config_store
from data_sources import (
    LiveScanSource, PastScanSource, ScanDataSource, buffers_are_empty,
)

# constants for calculations
SCALE_V = 0.0909
SCALE_I = 0.25
# TEC ADC conversion constants + RT lookup moved to omotion.console_telemetry_conversions
# (V_REF / R_1 / R_2 / R_3 / R_s / 10K3CG_R-T.csv).

# Contact-quality quick-check defaults (overridable via app_config keys
# cq_dark_threshold_per_camera / cq_light_threshold_per_camera /
# cq_rolling_avg_window).
_CQ_DEFAULT_DARK_THRESHOLD_DN = 3.0
_CQ_DEFAULT_LIGHT_THRESHOLD_DN = 15.0
_CQ_DEFAULT_ROLLING_WINDOW = 10

# Console front-panel RGB LED states — wire values of the firmware's
# OW_CTRL_SET_IND command (console-fw led_driver.c, via
# omotion.MotionConsole.set_rgb_led): 0=off, 1=red, 2=green, 3=blue
# (console-fw led_driver.h: LED_NONE=0, LED_RED=1, LED_GREEN=2, LED_BLUE=3).
# The firmware itself shows solid green when idle and solid blue while the
# trigger/laser is active; its hard-fault Error_Handler blinks blue at
# 500 ms. The app-side error blink below mirrors that error indication.
_RGB_LED_OFF = 0
_RGB_LED_BLUE = 3
_RGB_LED_GREEN = 2
_ERROR_LED_BLINK_MS = 500  # on/off half-period, matches firmware Error_Handler

# ── Engineering-mode unlock ──────────────────────────────────────────────
# Hardcoded engineering-mode password. Double-clicking the Openwater logo
# opens a prompt; entering this value sets engineeringMode=true (persisted).
# This is the ONLY place the literal is defined. The check lives in Python
# (not QML) so the literal never ships inside readable QML text.
_ENGINEERING_PASSWORD = "OpenwaterHealth"


def engineering_password_matches(pw) -> bool:
    """Return True iff ``pw`` equals the engineering-mode password."""
    return isinstance(pw, str) and pw == _ENGINEERING_PASSWORD


# Flip to True once release builds are Authenticode-signed; until then an
# unsigned (NotSigned) update bundle is allowed through with a logged warning.
_REQUIRE_SIGNED_UPDATES = False


def _select_update_asset(assets: list, is_research: bool):
    """Return download URL of the Setup bundle matching the running variant.

    Research builds match ``Open-Motion-Research-Setup-*.exe``; clinical
    builds match ``Open-Motion-Setup-*.exe``. Variant detection is a
    case-insensitive "research" substring check so the pre-1.4.0 asset
    names (``Openwater-Setup-*[_Research].exe``) still match. Returns
    None if no match.
    """
    for asset in assets:
        name = (asset.get("name") or "")
        low = name.lower()
        if not low.endswith(".exe"):
            continue
        asset_is_research = "research" in low
        if asset_is_research == is_research:
            return asset.get("browser_download_url")
    return None


def _authenticode_status(path: str) -> str:
    """Return the Authenticode signature status of ``path``.

    Uses PowerShell's Get-AuthenticodeSignature (always present on Windows).
    Returns one of 'Valid', 'NotSigned', 'HashMismatch', 'UnknownError', ... or
    'Error' if the check itself could not run.
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-AuthenticodeSignature -LiteralPath '{path}').Status",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() or "Error"
    except Exception:
        return "Error"


def _update_decision(status: str, require_signed: bool):
    """Decide whether to launch the downloaded bundle given its signature.

    Returns (should_launch: bool, error_message: str | None).
    """
    if status == "Valid":
        return True, None
    if status == "NotSigned":
        if require_signed:
            return False, "Update is not signed; refusing to install."
        return True, None  # transition period: allow with a warning logged
    return False, f"Update signature check failed: {status}"


def _is_bundle_url(url) -> bool:
    """True if ``url`` is a Setup .exe bundle, not the release HTML page.

    Guards against the GitHub ``html_url`` fallback: without this, a release
    with no matching installer asset would make the updater download an HTML
    page and try to execute it.
    """
    return isinstance(url, str) and url.lower().endswith(".exe")


def _looks_like_pe(head: bytes) -> bool:
    """True if the bytes start with the 'MZ' signature of a Windows executable.

    A truncated download or an HTML error page will not start with 'MZ', so
    this rejects corrupt downloads before they are launched.
    """
    return head[:2] == b"MZ"


def _build_update_helper_script(
    app_pid: int, installer: str, app_exe: str
) -> str:
    """Return a PowerShell script that performs the in-place upgrade handoff.

    The running app cannot upgrade its own files while it holds them, and the
    Burn bundle does not relaunch the app. So the app spawns this detached
    helper and then exits; the helper (1) waits for the app to fully exit to
    avoid a FilesInUse conflict — and force-kills it if it doesn't exit in
    time, since a wedged GUI thread (e.g. a synchronous SDK/USB call blocking
    the event loop) can otherwise leave the app alive and stall the whole
    upgrade, (2) runs the bundle non-interactively (one UAC elevation, no
    bootstrapper UI), then (3) relaunches the app.
    """
    return (
        "# OpenWater in-app update helper (auto-generated; do not edit)\n"
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"# 1. Wait for the running app (PID {app_pid}) to exit so the\n"
        "#    installer can replace its files without a FilesInUse conflict.\n"
        "#    If it doesn't exit in time (the app may not self-exit if the\n"
        "#    GUI thread is wedged), force-kill it - installing over a live app fails the\n"
        "#    in-place file swap.\n"
        "$deadline = (Get-Date).AddSeconds(10)\n"
        f"while (Get-Process -Id {app_pid} -ErrorAction SilentlyContinue) {{\n"
        "    if ((Get-Date) -gt $deadline) {\n"
        f"        Stop-Process -Id {app_pid} -Force -ErrorAction SilentlyContinue\n"
        "        Start-Sleep -Milliseconds 500\n"
        "        break\n"
        "    }\n"
        "    Start-Sleep -Milliseconds 250\n"
        "}\n"
        "# 2. Run the upgrade non-interactively (one UAC elevation).\n"
        f"Start-Process -FilePath \"{installer}\" "
        "-ArgumentList '/passive','/norestart' -Wait\n"
        "# 3. Relaunch the (now-updated) app.\n"
        f"Start-Process -FilePath \"{app_exe}\"\n"
    )


# Camera-mask → human config name, mirroring ScanSettingsModal's
# pattern table. Unmapped masks render as hex; -1 (unknown, e.g. a
# clinical-mode scan whose meta lacks sdk_flags) renders as an em dash.
_CONFIG_NAMES = {
    0x00: "None", 0x5A: "Near", 0x66: "Middle", 0xC3: "Far",
    0x99: "Outer", 0x0F: "Left", 0xF0: "Right", 0x42: "Third Row",
    0xFF: "All",
}


def _config_name(mask) -> str:
    if mask is None or mask < 0:
        return "—"
    m = int(mask) & 0xFF
    return _CONFIG_NAMES.get(m, f"0x{m:02X}")


def _sdk_flags_from_session(session) -> Optional[dict]:
    """Pull the ``sdk_flags`` block out of a ScanDatabase session dict's
    ``session_meta`` (the camera config / clinical-mode the scan was RUN with,
    written by the SDK's ScanDBSink). Returns None when absent or unparseable.

    ScanDatabase already json-decodes session_meta into a dict, but tolerate a
    raw JSON string too — the same defensive parse the History list uses
    (_session_to_row). Used to lay a replayed scan's plot grid out from its
    recorded config rather than only the cameras that logged data (#175)."""
    if not isinstance(session, dict):
        return None
    meta = session.get("session_meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            return None
    if not isinstance(meta, dict):
        return None
    flags = meta.get("sdk_flags")
    return flags if isinstance(flags, dict) else None


logger = logging.getLogger("openmotion.bloodflow-app.connector")

# Define system states
DISCONNECTED = 0
SENSOR_CONNECTED = 1
CONSOLE_CONNECTED = 2
READY = 3
RUNNING = 4

# Issue #119: a single ``safety_known=False`` poll can be a transient I2C
# miss during a USB disconnect cascade rather than a real safety-chip
# fault. Require a streak before firing the persistent toast. Telemetry
# polls at ~1 Hz, so 3 polls ≈ 3 s — well under the time a user takes to
# reach Check, preserving the #107 contract (latched chip fault must
# surface before the laser can be fired).
SAFETY_UNKNOWN_STREAK_THRESHOLD = 3


def _safety_unknown_streak_decision(snap, prev_streak, threshold=SAFETY_UNKNOWN_STREAK_THRESHOLD):
    """Update the safety-unknown streak counter and decide whether to fire.

    Returns ``(new_streak, should_fire)``.

    - ``safety_known=False`` (chip unresponsive on this poll): increment
      the streak and report ``should_fire=True`` once it reaches the
      threshold. The connector's outer ``if not self._safetyFailure``
      guard prevents the toast from re-firing on every subsequent poll.
    - ``safety_known=True`` (or missing, for backward compat with legacy
      SDK snapshots): reset the streak; the caller handles the
      ``safety_ok`` branch separately.
    """
    if not getattr(snap, "safety_known", True):
        new_streak = prev_streak + 1
        return new_streak, new_streak >= threshold
    return 0, False


def _rearm_dropped_camera(
    side: str,
    cam_id: int,
    dropped: set,
) -> str | None:
    """Un-mark a camera the dropout watchdog flagged 'Connection Lost',
    now that a fresh frame has arrived (issue #85 follow-up).

    Dropout marking used to be permanent for the scan and later samples
    were suppressed, so any transient stall killed the camera's display
    for the rest of the scan. Frames resuming is proof the camera is back
    — re-arm the watchdog and resume display. The watchdog will re-fire
    (with a fresh toast) if the camera goes silent again.

    Returns the recovery message to log when ``(side, cam_id)`` was
    marked, else None. Mutates ``dropped``.
    """
    key = (side, int(cam_id))
    if key not in dropped:
        return None
    dropped.discard(key)
    return (
        f"Camera {side.upper()} {int(cam_id) + 1} resumed sending frames "
        f"after being marked Connection Lost — re-arming the dropout "
        f"watchdog and resuming display."
    )


def _scan_data_stall_decision(
    now_mono: float,
    trigger_on_mono: float | None,
    last_seen: dict,
    timeout_sec: float,
) -> float | None:
    """Decide whether data acquisition has completely stopped (issue #248).

    ``last_seen`` is the dropout watchdog's per-camera arrival map
    (``_camera_last_seen``) — its newest value is the last time a frame
    arrived from ANY camera. ``trigger_on_mono`` (the current trigger-ON
    edge) is the floor for the silence measurement: it covers the case
    where no camera ever delivered a frame, and restarts the clock if the
    trigger re-opens after a pause.

    Returns the stall duration in seconds when EVERY camera has been
    silent for longer than ``timeout_sec``, else None. A non-positive
    ``timeout_sec`` disables the check; a None ``trigger_on_mono`` means
    the trigger clock isn't open (no ON edge yet), so there's no baseline
    to measure silence against.
    """
    if timeout_sec <= 0 or trigger_on_mono is None:
        return None
    newest = max(last_seen.values(), default=trigger_on_mono)
    stalled = now_mono - max(newest, trigger_on_mono)
    return stalled if stalled > timeout_sec else None


_SIDE_NAMES = ("left", "right")


class _LivePlotSink:
    """Subscribes to the 'live' pipeline channel and feeds per-frame samples
    into the LiveScanSource backing the PlotViewer, for each active camera.

    The 'live' channel carries a FrameBatch after BfiBvi. Each batch may
    contain multiple frames and both sides; we iterate frame × side × cam_id,
    feed the camera-liveness bookkeeping, and append to the source.

    Camera liveness is keyed to FRAME ARRIVAL, not to whether a frame
    yielded a plottable sample. A covered / off-target camera streams
    light-typed frames whose BFI/BVI are NaN (the SDK dark stage suppresses
    realtime emission for unlit frames); that camera is connected and
    healthy. Keying the heartbeat on finite BFI — the old behavior —
    misdiagnosed covered sensors as camera dropouts.

    The 'live_side' channel carries one SideAverageSample per capture per side
    (from the SDK's LiveSideAverageStage, clinical mode) — the realtime per-side
    average, appended under cam_id=-1 for the clinical-mode display.
    """

    channels = {"live", "live_side"}

    # A camera must present this many consecutive frames of the opposite
    # low-light state before we log the transition — one operator-facing
    # line per genuine cover/uncover, not 40 Hz of boundary flapping.
    _LOW_LIGHT_DEBOUNCE_FRAMES = 80  # ~2 s at 40 fps

    def __init__(self, connector: "MotionConnector", plot_t0: float,
                 live_source: "LiveScanSource",
                 nan_gap_tracker: "NanGapTracker | None" = None):
        self._connector = connector
        self._plot_t0 = plot_t0
        self._live_source = live_source
        self._temp_alerted: dict[tuple[str, int], bool] = {}
        # Records every arriving frame so the scan-complete handler can
        # report sustained DELIVERY gaps in the notes footer. Optional so
        # the sink works standalone (tests, future callers).
        self._nan_gap_tracker = nan_gap_tracker
        # Per-camera low-light state (from the SDK's batch.low_light_rt):
        # True = camera currently sees no meaningful light (covered / off
        # target). Debounced transitions are logged so an empty plot has an
        # explanation in the capture log.
        self._low_light_state: dict[tuple[str, int], bool] = {}
        self._low_light_streak: dict[tuple[str, int], int] = {}

    def on_scan_start(self, meta) -> None:
        self._temp_alerted.clear()

    def consume(self, channel: str, payload) -> None:
        if channel == "live_side":
            self._consume_side_avg(payload)
            return
        if channel != "live":
            return
        batch = payload
        if batch.bfi_live is None:
            return

        n = batch.bfi_live.shape[0]
        connector = self._connector
        threshold = connector._camera_temp_alert_threshold_c
        now_mono = time.monotonic()

        low_light_rt = getattr(batch, "low_light_rt", None)

        for i in range(n):
            ft = str(batch.frame_type[i]) if batch.frame_type is not None else "light"
            # Stale frames are leftover garbage (e.g. an unflushed histogram
            # buffer) — not evidence of a live camera, never displayed.
            if ft == "stale":
                continue
            is_dark = (ft == "dark")
            ts = float(batch.timestamp_s[i])
            abs_frame_id = int(batch.abs_frame_ids[i]) if batch.abs_frame_ids is not None else i
            plot_ts = ts

            side_ids = getattr(batch, "side_ids", None)
            cam_ids = getattr(batch, "cam_ids", None)
            row_addressed = side_ids is not None and cam_ids is not None
            if row_addressed:
                side_idx = int(side_ids[i])
                cam_id = int(cam_ids[i])
                if side_idx < 0 or side_idx >= len(_SIDE_NAMES) or cam_id < 0 or cam_id >= 8:
                    continue
                side_cam_iter = [(side_idx, _SIDE_NAMES[side_idx], cam_id)]
            else:
                side_cam_iter = [
                    (side_idx, side, cam_id)
                    for side_idx, side in enumerate(_SIDE_NAMES)
                    for cam_id in range(8)
                ]

            for side_idx, side, cam_id in side_cam_iter:
                bfi = float(batch.bfi_live[i, side_idx, cam_id])
                bvi = float(batch.bvi_live[i, side_idx, cam_id])
                temp_c = float(batch.temperature_c[i, side_idx, cam_id])

                _key = (side, cam_id)

                # ── Liveness: frame ARRIVAL, not plottability ─────────────
                # An unlit (covered / off-target) camera streams frames whose
                # BFI/BVI are NaN — it is connected and must keep feeding the
                # heartbeat and the delivery-gap tracker. Only the
                # row-addressed path can key on arrival: legacy batches
                # without side_ids/cam_ids fan every row out to all 16
                # cameras, where a finite BFI is the only evidence this
                # camera actually produced the row.
                row_alive = row_addressed or (
                    math.isfinite(bfi) and math.isfinite(bvi))
                if row_alive:
                    connector._camera_last_seen[_key] = now_mono
                    recovery_msg = _rearm_dropped_camera(
                        side, cam_id, connector._camera_dropped)
                    if recovery_msg is not None:
                        logger.warning(recovery_msg)
                        connector._on_camera_dropout_recovered(side, cam_id)
                    # Delivery gaps: every arriving frame counts. "Data
                    # gaps" in the notes footer means frames stopped
                    # arriving — a covered sensor is not a data gap.
                    if self._nan_gap_tracker is not None:
                        self._nan_gap_tracker.record(_key, plot_ts)

                # Warmup frames prove liveness but carry no display signal.
                if ft == "warmup":
                    continue

                # Low-light bookkeeping (SDK ≥ low_light_rt): debounced
                # transition log so an empty plot has an explanation.
                if low_light_rt is not None and not is_dark:
                    self._note_low_light(
                        _key, bool(low_light_rt[i, side_idx, cam_id]))

                # Temperature cache + alert (light frames only — scheduled
                # dark frames have no meaningful camera temperature reading).
                # Deliberately before the NaN gate: a covered camera still
                # reports a real chip temperature and can still overheat.
                if not is_dark and math.isfinite(temp_c):
                    connector._camera_last_temp[_key] = temp_c
                    if temp_c >= threshold and _key not in self._temp_alerted:
                        self._temp_alerted[_key] = True
                        msg = (
                            f"ALERT: Camera {cam_id + 1} ({side}) "
                            f"temperature {temp_c:.1f}°C >= {threshold:.0f}°C threshold."
                        )
                        connector.captureLog.emit(msg)
                        logger.warning(msg)

                # Skip NaN samples for the plot — Qt would otherwise render
                # them as a spike from baseline to wherever NaN lands in the
                # y-mapping. Common causes: warmup before the first dark
                # observation, and unlit frames (the dark stage suppresses
                # realtime emission — see low_light_rt above).
                if not (math.isfinite(bfi) and math.isfinite(bvi)):
                    continue

                mean_for_source: Optional[float] = None
                contrast_for_source: Optional[float] = None
                temp_for_source: Optional[float] = None
                if not is_dark:
                    # Record the realtime dark-corrected mean (mean_dc_rt) and
                    # shot-noise-corrected contrast (contrast_sn_rt). The
                    # live display is realtime-only by design; the accurate
                    # corrected record lands in the scan DB via the SDK's
                    # ScanDBSink and is what replay shows. Skip NaN samples —
                    # early light frames before the first dark observation
                    # have NaN mean_dc_rt and shouldn't poison the plot.
                    if batch.mean_dc_rt is not None:
                        mean_val = float(batch.mean_dc_rt[i, side_idx, cam_id])
                        if math.isfinite(mean_val):
                            mean_for_source = mean_val
                    if batch.contrast_sn_rt is not None:
                        contrast_val = float(batch.contrast_sn_rt[i, side_idx, cam_id])
                        if math.isfinite(contrast_val):
                            contrast_for_source = contrast_val
                    # Camera temperature — light frames only (dark-frame
                    # readings are meaningless for display, same rule as the
                    # temperature alert above). Feeds the per-cell dev-mode
                    # readout (issue #165).
                    if math.isfinite(temp_c):
                        temp_for_source = temp_c

                # mean/contrast/temp already filtered for is_dark /
                # non-finite above; None here means "metric not available
                # for this sample".
                self._live_source.append_uncorrected(
                    side=side,
                    cam_id=cam_id,
                    frame_id=abs_frame_id,
                    t=plot_ts,
                    bfi=bfi,
                    bvi=bvi,
                    mean=mean_for_source,
                    contrast=contrast_for_source,
                    temp=temp_for_source,
                )

    def _note_low_light(self, key: tuple[str, int], is_low: bool) -> None:
        """Track a camera's low-light state (SDK batch.low_light_rt) and log
        debounced transitions. A camera flipping state must hold the new
        state for _LOW_LIGHT_DEBOUNCE_FRAMES consecutive frames before the
        transition is accepted — one capture-log line per genuine
        cover/uncover event, not 40 Hz of threshold flapping."""
        current = self._low_light_state.get(key, False)
        if is_low == current:
            self._low_light_streak[key] = 0
            return
        streak = self._low_light_streak.get(key, 0) + 1
        if streak < self._LOW_LIGHT_DEBOUNCE_FRAMES:
            self._low_light_streak[key] = streak
            return
        self._low_light_state[key] = is_low
        self._low_light_streak[key] = 0
        side, cam_id = key
        if is_low:
            msg = (
                f"Camera {side.upper()} {cam_id + 1} low light — frames "
                f"arriving but no meaningful signal (sensor covered or "
                f"off target?). Plot paused for this camera."
            )
        else:
            msg = f"Camera {side.upper()} {cam_id + 1} signal restored."
        logger.info(msg)
        self._connector.captureLog.emit(msg)

    def _consume_side_avg(self, sample) -> None:
        """Append one clinical-mode per-side average (SideAverageSample from the
        SDK's LiveSideAverageStage) under cam_id=-1. The stage already emits one
        true spatial average per capture per side at ~40 Hz, so there's no dedup
        or skipping here — we just store what it emits."""
        side_idx = int(getattr(sample, "side", -1))
        if not (0 <= side_idx < len(_SIDE_NAMES)):
            return
        bfi = float(getattr(sample, "bfi", float("nan")))
        bvi = float(getattr(sample, "bvi", float("nan")))
        t = float(getattr(sample, "t", 0.0))
        # Arrival-based, same as the per-camera path: the stage emitted a
        # sample for this capture, so the pipeline is delivering — a NaN
        # average (all cameras unlit, e.g. covered sensors) is not a
        # delivery gap.
        if self._nan_gap_tracker is not None:
            self._nan_gap_tracker.record((_SIDE_NAMES[side_idx], -1), t)
        self._live_source.append_uncorrected(
            side=_SIDE_NAMES[side_idx],
            cam_id=-1,
            frame_id=int(getattr(sample, "frame_id", -1)),
            t=t,
            bfi=bfi,
            bvi=bvi,
        )

    def on_complete(self) -> None:
        pass


class _TriggerStateSink:
    """Listens for TriggerStateEvent on the diagnostics channel and mirrors
    the laser-trigger state into the connector so:

      * ``_trigger_state`` ("ON" / "OFF") drives the QML ``triggerState``
        property — which gates the scanTimer's `running:` binding on
        BloodFlow.qml, the per-scan camera-dropout watchdog, and any
        other QML element keyed on trigger status during a scan.
      * ``_trigger_on_mono`` / ``_trigger_cumulative_s`` give
        ``_scan_elapsed_str`` and the scan-notes "duration" line a real
        trigger-ON measurement rather than wall-clock.

    Without this sink, scan-time start_trigger() goes straight to the
    firmware via ScanWorkflow without touching the connector's
    ``_trigger_state``, so the timer never ticks.
    """

    channels: set = frozenset({"diagnostics"})

    def __init__(self, connector: "MotionConnector"):
        self._connector = connector

    def on_scan_start(self, meta) -> None:
        # Reset is already done in startCapture before the sink list is
        # constructed; nothing to do here.
        pass

    def consume(self, channel: str, payload) -> None:
        if channel != "diagnostics":
            return
        # Lazy-import the event type so this module doesn't fail if the
        # SDK version pre-dates TriggerStateEvent.
        try:
            from omotion.pipeline.batch import TriggerStateEvent
        except ImportError:
            return
        if not isinstance(payload, TriggerStateEvent):
            return
        c = self._connector
        if payload.state == "ON":
            c._trigger_state = "ON"
            c._trigger_clock_open(payload.timestamp_s)
            c.triggerStateChanged.emit()
        elif payload.state == "OFF":
            c._trigger_state = "OFF"
            c._trigger_clock_close(payload.timestamp_s)
            c.triggerStateChanged.emit()

    def on_complete(self) -> None:
        pass


class _CompletionSink:
    """Fires the connector's post-scan UI cleanup when the pipeline's
    ScanRunner completes.  Replaces the legacy on_complete_fn callback.

    Wired by startCapture; the scan-done logic runs from the sink's
    on_complete() method.
    """

    channels: set = frozenset()  # no data channels — lifecycle only

    def __init__(self, connector: "MotionConnector", on_complete_cb):
        self._connector = connector
        self._on_complete_cb = on_complete_cb
        self._meta = None

    def on_scan_start(self, meta) -> None:
        self._meta = meta

    def consume(self, channel: str, payload) -> None:
        pass  # no data channels

    def on_complete(self) -> None:
        try:
            self._on_complete_cb(self._meta)
        except Exception:
            logger.exception("_CompletionSink.on_complete callback raised")


# ── Scan-outcome classification (interrupted / empty scans) ────────────────
# Kept Qt-free so it stays unit-testable (tests/test_scan_outcome.py). An
# "interval" is a dark-bounded segment the dark-correction stage emits on the
# pipeline's "final" channel; ScanDBSink persists those frames. The final,
# still-open interval is always discarded (no terminal dark closes it), so an
# unclean shutdown loses its tail — and a scan that ends before *any* interval
# closes persists nothing. classify_scan_outcome turns the two facts the sink
# observes (frames persisted, terminal-dark missing) into a user-facing
# outcome — no wall-clock thresholds.


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


class _ScanOutcomeSink:
    """Pipeline sink that tallies the two signals classify_scan_outcome needs.

    Duck-typed like the other sinks above (channels / on_scan_start / consume /
    on_complete). Holds no connector reference, so the completion handler reads
    .final_frames / .terminal_dark_missing directly off the instance.

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
            except ImportError:
                return
            if isinstance(payload, TerminalDarkResult) and not payload.found:
                self.terminal_dark_missing = True

    def on_complete(self) -> None:
        pass


class MotionConnector(QObject):
    # Ensure signals are correctly defined
    signalConnected = pyqtSignal(str, str)  # (descriptor, port)
    signalDisconnected = pyqtSignal(str, str)  # (descriptor, port)

    connectionStatusChanged = pyqtSignal()  # 🔹 New signal for connection updates
    # Notify for ``sensorInitBusy`` (issue #303): a connect-time sensor init
    # (debug flags, camera power/ID cache, info reads) is scheduled or
    # running. The drain-side emit fires on the init worker thread; Qt
    # auto-queues the cross-thread delivery to QML/main-thread consumers.
    sensorInitBusyChanged = pyqtSignal()
    stateChanged = pyqtSignal()  # Signal to notify QML of state changes
    laserStateChanged = pyqtSignal()  # Signal to notify QML of laser state changes
    safetyFailureStateChanged = pyqtSignal()  # Signal to notify QML of safety
    safetyTripDuringCaptureRequested = pyqtSignal()  # Emitted when safety trips while scan running (main-thread slot shows message & schedules cancel)
    scanWorkerFailedDuringCapture = pyqtSignal(str)  # Emitted from the SDK scan worker thread when it aborts after start_scan returned True (main-thread slot raises E-301 & tears down capture). Issue #213.
    triggerStateChanged = pyqtSignal()  # Signal to notify QML of trigger state changes
    directoryChanged = pyqtSignal()  # Signal to notify QML of directory changes
    userLabelChanged = pyqtSignal()  # Signal to notify QML of user label changes
    sensorDeviceInfoReceived = pyqtSignal(str, str)  # (fw_version, device_id)
    consoleDeviceInfoReceived = pyqtSignal(str, str)  # (fw_version, device_id)
    temperatureSensorUpdated = pyqtSignal(float)  # Temperature data
    accelerometerSensorUpdated = pyqtSignal(float, float, float)  # (x, y, z)
    gyroscopeSensorUpdated = pyqtSignal(float, float, float)  # (x, y, z)
    rgbStateReceived = pyqtSignal(int, str)  # (state, state_text)
    errorOccurred = pyqtSignal(str)
    # Critical/showstopper conditions surfaced to the user as a blocking,
    # dismissible modal with a stable error code (see error_codes.py).
    # Payload: (code, title, message, suggestedAction, detail).
    criticalErrorRaised = pyqtSignal(str, str, str, str, str)
    # Private worker→main marshalling: _raise_critical may fire from USB I/O
    # or scanner worker threads, but the console error-LED blink QTimer lives
    # on the GUI thread that owns it (issue #257). Wired in connect_signals.
    _errorLedBlinkRequested = pyqtSignal()
    notificationRequested = pyqtSignal('QVariant')  # toast notification payload dict
    notificationDismissByIdRequested = pyqtSignal(int)   # dismiss the toast with this id
    notificationDismissByTagRequested = pyqtSignal(str)  # dismiss the toast with this tag
    notificationDismissAllRequested = pyqtSignal()       # dismiss every active toast

    # Real-time plot viewer source — see data_sources.py.
    currentScanSourceChanged = pyqtSignal()
    liveSourceAvailableChanged = pyqtSignal()
    # History → "View in plot": fires on the GUI thread when an async
    # loadPastScan completes. (session_label, ok). HistoryModal uses it
    # to clear its busy overlay and close only on success (issue #152).
    pastScanLoadFinished = pyqtSignal(str, bool)
    # History → "Export CSV" / "Export to folder": fire on the GUI thread
    # when the async export workers finish. (ok, path) for the single-scan
    # export; (exported, skipped, folder) for the batch. HistoryModal shows
    # the summary toast from these instead of a synchronous return value —
    # a big batch used to freeze the GUI for its whole materialize loop.
    scanCsvExportFinished = pyqtSignal(bool, str)
    scansExportFinished = pyqtSignal(int, int, str)
    # Private worker→main marshalling for loadPastScan results:
    # (seq, session_label, session_id, buffers-or-None, source_tag,
    #  recorded_flags-or-None, display_meta-or-None). recorded_flags is the
    # scan's stored session_meta.sdk_flags so the PastScanSource lays its grid
    # out from the config the scan was RUN with, not just the cameras that
    # logged data (issue #175 reopen). display_meta is {userLabel, dateTime}
    # for the viewer's "Viewing" badge (issue #245), derived from the same
    # session row the History list shows.
    _pastScanBuffersReady = pyqtSignal(
        int, str, int, object, str, object, object)
    # Worker->main: interrupted/empty-scan outcome toast (message, severity).
    # Emitted from _on_pipeline_complete (pipeline worker thread); delivered
    # on the GUI thread via the auto-queued connection wired in connect_signals.
    _scanOutcomeWarningSignal = pyqtSignal(str, str)

    configProgress = pyqtSignal(int)
    configLog = pyqtSignal(str)
    configFinished = pyqtSignal(bool, str)

    # capture signals
    captureProgress = pyqtSignal(int)  # 0..100
    captureLog = pyqtSignal(str)  # log lines
    captureFinished = pyqtSignal(bool, str, str, str)  # ok, error, leftPath, rightPath

    # Calibration procedure signals
    calibrationStateChanged = pyqtSignal()  # any of running/passed/failed/aborted/idle
    _calibrationCompleteSignal = pyqtSignal(object)  # private worker→main marshalling
    testScanStateChanged = pyqtSignal()                # any of running/done/aborted/failed/idle
    _testScanCompleteSignal = pyqtSignal(object)       # private worker→main marshalling
    scanNotesChanged = pyqtSignal()
    # Fires once at the end of _on_complete (after the duration line has
    # been appended to _scan_notes and notes.txt has been written) for any
    # scan that finished normally or was canceled by the user. The UI uses
    # this to auto-open the notes modal at the only moment when scanNotes
    # is guaranteed to reflect the just-completed scan. Not emitted on
    # hard errors — those keep the scan dialog visible with the error.
    scanNotesReady = pyqtSignal()

    # Contact-quality quick-check signals.
    contactQualityCheckStarted = pyqtSignal(int)  # expected duration in seconds
    contactQualityCheckFinished = pyqtSignal(bool, str, 'QVariantList')  # ok, error, warnings
    contactQualityWarning = pyqtSignal(str, str, str, float)  # camera, typeKey, typeText, value
    # Mid-scan CQ state change (active=True on activation, False on clear).
    # BloodFlow.qml's ``onContactQualityIssueStateChanged`` consumes the
    # ``False`` edge to clear an entry from the live modal.
    contactQualityIssueStateChanged = pyqtSignal(str, str, str, float, bool)
    contactQualityScanInProgress = pyqtSignal(bool)
    cameraDropoutDetected = pyqtSignal(str, int, str)  # side ("left"/"right"), cam_id (0-7), elapsed HH:MM:SS
    # Frames resumed for a camera previously flagged Connection Lost — the
    # watchdog was re-armed and display resumed. Mirror of cameraDropoutDetected.
    cameraDropoutRecovered = pyqtSignal(str, int, str)  # side, cam_id (0-7), elapsed HH:MM:SS

    # post-processing signals
    postProgress = pyqtSignal(int)
    postLog = pyqtSignal(str)
    postFinished = pyqtSignal(bool, str, str, str)  # ok, err, leftCsv, rightCsv

    pduMonChanged = pyqtSignal()

    tecStatusChanged = pyqtSignal()
    tecDacChanged = pyqtSignal()
    appConfigChanged = pyqtSignal()
    consoleFanChanged = pyqtSignal()
    # Per-device firmware versions, refreshed on every (dis)connect by
    # _log_device_stats. Surfaced to Settings → System Information.
    firmwareVersionsChanged = pyqtSignal()
    # Firmware autoupdate (engineeringMode only)
    firmwareUpdateInfoChanged = pyqtSignal()                # notify for the properties below
    firmwareUpdateAvailable = pyqtSignal(str, str, str)     # deviceKey, current, latest
    firmwareUpdateProgress = pyqtSignal(str, str, int, str) # deviceKey, stage, percent(-1=indeterminate), msg
    firmwareUpdateFinished = pyqtSignal(str, bool, str)     # deviceKey, ok, msg

    # App update signals
    updateAvailable = pyqtSignal(str, str)   # (latest_version, download_url)
    updateNotAvailable = pyqtSignal()
    updateCheckFailed = pyqtSignal(str)      # error message
    updateProgress = pyqtSignal(str)         # human-readable progress status

    def __init__(
        self,
        interface: MotionInterface,
        app_config=None,
        baseline_config=None,
        data_dir=None,
        config_dir="config",
        parent=None,
        log_level=logging.INFO,
        app_version="",
        log_path="",
    ):
        super().__init__(parent)
        cfg = app_config or {}

        # Store the full config dict — exposed to QML as appConfig property
        self._app_config = dict(cfg)
        # Shipped baseline (defaults + read-only bundled config); runtime
        # changes are persisted as a diff against this (see _save_app_config).
        self._baseline_config = dict(baseline_config or {})

        # Bug-report context (see sendBugReport). app_version + log_path come
        # from main.py; support_email / bug_report_smtp from app config.
        self._app_version = app_version
        self._log_path = log_path
        self._support_email = cfg.get("support_email", "support@openwater.health")
        self._bug_report_smtp = cfg.get("bug_report_smtp")

        # Connection watchdog (E-104/E-106): one-shot check armed at startup
        # that flags expected devices that never enumerated. 0 disables it.
        self._connection_timeout_sec = float(cfg.get("connectionTimeoutSec", 30))
        self._require_console = bool(cfg.get("requireConsole", True))
        self._min_sensors = int(cfg.get("minSensors", 1))

        self._interface = interface
        self._scan_workflow = self._interface.scan_workflow

        # Audit log constructed early — before connect_signals() — so the
        # connect/disconnect handler and other instrumentation can call
        # self._audit.log(...) safely. No-op if there's no scan_db_path.
        # The system_startup / system_info events are logged later, once
        # self._directory is resolved.
        from audit_log import AuditLog
        self._audit = AuditLog(getattr(self._interface, "scan_db_path", None))

        # Unpack operational settings from config
        self._force_laser_fail            = bool(cfg.get("forceLaserFail", False))
        self._camera_temp_alert_threshold_c = float(cfg.get("cameraTempAlertThresholdC", 105.0))
        self._camera_dropout_threshold_sec = float(cfg.get("cameraDropoutThresholdSec", 2.0))
        # Whole-scan data-stall watchdog (issue #248): if NO selected camera
        # delivers a frame for this long while the trigger is ON, the scan is
        # aborted with E-303 instead of running to completion on air. Must be
        # comfortably above cameraDropoutThresholdSec (per-camera toast) and
        # the sensor warmup window. <= 0 disables the abort.
        self._scan_data_stall_timeout_sec = float(cfg.get("scanDataStallTimeoutSec", 3.0))

        # Camera dropout watchdog state — reset at start of each scan.
        # _camera_last_seen is refreshed on every FRAME ARRIVAL for the
        # camera (see _LivePlotSink.consume), so membership in
        # _camera_dropped means "frames stopped arriving" — a camera
        # streaming unlit frames (covered sensor) is NOT a dropout.
        # Dropped cameras re-arm automatically when frames resume
        # (_rearm_dropped_camera), so the set holds currently-silent
        # cameras, not a permanent per-scan record.
        self._camera_last_seen: dict[tuple[str, int], float] = {}
        self._camera_last_temp: dict[tuple[str, int], float] = {}
        self._camera_dropped: set[tuple[str, int]] = set()
        # One-shot guard for the all-camera stall abort (issue #248) —
        # set when E-303 fires, cleared at the next startCapture.
        self._scan_stall_abort_fired = False

        # NaN-gap tracker — replaced with a fresh instance at each scan
        # start. Kept on the instance for debugging/introspection only;
        # the scan path (sink + completion closure in startCapture) uses
        # a closure-local binding of the same object.
        self._nan_gap_tracker = NanGapTracker()

        # 1 Hz watchdog timer — started/stopped around the scan lifecycle.
        self._dropout_timer = QTimer(self)
        self._dropout_timer.setInterval(1000)
        self._dropout_timer.timeout.connect(self._on_dropout_check)

        # Console error-LED blink (issue #257): while the critical-error
        # modal is up, blink the console front-panel LED blue instead of
        # leaving it solid green. Started via _errorLedBlinkRequested
        # (queued from worker threads); stopped and restored to idle green
        # by criticalErrorsDismissed() when the modal queue empties.
        self._error_led_timer = QTimer(self)
        self._error_led_timer.setInterval(_ERROR_LED_BLINK_MS)
        self._error_led_timer.timeout.connect(self._on_error_led_tick)
        self._error_led_on = False  # True while the blue half-period is lit
        self._plot_t0: float = 0.0  # set at scan start; scan-start monotonic anchor

        # Trigger-ON clock (issue #201) — the single scan-time source of
        # truth for the notes duration line, the header elapsed counter and
        # the dropout log timestamps. Managed exclusively through the
        # _trigger_clock_reset/_open/_close helpers; read via
        # _trigger_elapsed_s / scanElapsedSec / scanElapsedStr.
        self._trigger_cumulative_s: float = 0.0
        self._trigger_on_mono: float | None = None
        # Scan-relative timestamp (from the SDK's TriggerStateEvent) of the
        # current ON edge. Preferred over _trigger_on_mono for the final
        # duration measurement because it is immune to host-side event-queue
        # latency — see _TriggerStateSink (issue #201).
        self._trigger_on_ts: float | None = None
        self._sensor_debug_logging        = bool(cfg.get("sensorDebugLogging", False))
        self._camera_fake_data            = bool(cfg.get("cameraFakeData", False))
        self._histo_throttle              = bool(cfg.get("histoThrottle", False))
        self._histo_cmp                   = bool(cfg.get("histoCmp", False))
        self._histo_stall_test           = bool(cfg.get("debugHistoStallTest", False))
        self._send_data_defer             = bool(cfg.get("deferHistoSend", False))
        self._comm_verbose                = bool(cfg.get("commVerbose", False))
        self._verbose_command_handling    = bool(cfg.get("verboseCommandHandling", False))
        # Console USB-printf mirror (DEBUG_FLAG_USB_PRINTF). Separate from the
        # sensor debug flags above — applied to self._interface.console, not the
        # left/right sensors. See setConsoleDebugLogging.
        self._console_debug_logging       = bool(cfg.get("consoleDebugLogging", False))
        # Root directory: caller-supplied (from main.py) wins, else
        # dataDirectory from app config, else the resolved default (see
        # app_paths.writable_root). Scan files, calibrations,
        # debug-bundles, and scans.db all live under self._data_root
        # (self._directory/data — see that property).
        resolved_dir = (
            data_dir
            or cfg.get("dataDirectory")
            or str(app_paths.writable_root(bool(cfg.get("portableMode", False))))
        )
        self._power_off_unused_cameras    = bool(cfg.get("powerOffUnusedCameras", False))
        # Raw CSV is an engineering feature (Settings → Engineering → "Save raw
        # CSV"); default False so a config missing the key fails closed for
        # clinical use (#43).
        self._write_raw_csv               = bool(cfg.get("writeRawCsv", False))
        raw_csv                           = cfg.get("rawCsvDurationSec")
        self._raw_csv_duration_sec        = float(raw_csv) if raw_csv is not None else None
        self._uncorrected_only            = bool(cfg.get("uncorrectedOnly", False))

        # Initialize CSV output directory to user's home directory
        self._csv_output_directory = os.path.expanduser("~")

        # Check if console and sensor are connected
        console_connected, left_sensor_connected, right_sensor_connected = (
            self._interface.is_device_connected()
        )

        self._leftSensorConnected = left_sensor_connected
        self._rightSensorConnected = right_sensor_connected
        self._consoleConnected = console_connected
        self._config_running = False
        # Per-side count of connect-time sensor inits scheduled or running
        # (issue #303). Guarded by _sensor_init_lock: +1 on the GUI thread
        # when _schedule_sensor_init arms the init, -1 on the init worker
        # thread when it drains (success OR failure). Non-zero blocks
        # pipeline starts via _ensure_idle / sensorInitBusy.
        self._sensor_init_pending: dict[str, int] = {"left": 0, "right": 0}
        self._sensor_init_lock = threading.Lock()
        self._laserOn = False
        self._safetyFailure = False
        self._safety_unknown_streak = 0  # see SAFETY_UNKNOWN_STREAK_THRESHOLD
        self._running = False
        self._trigger_state = "OFF"
        self._state = DISCONNECTED
        self._last_fan_status: dict[str, bool | None] = {"left": None, "right": None}
        # Console fan on/off cache. Seeded True because the connector
        # forces set_fan_speed(100) at console-connect time (see the
        # connect handler below). Surfaced to QML as ``consoleFanOn`` and
        # toggled via ``setConsoleFan``.
        self._console_fan_on: bool = True
        # Per-device firmware version strings (e.g. "v1.2.3"), populated on
        # connect by _log_device_stats and cleared on disconnect. Surfaced
        # to QML as console/left/rightSensorFirmwareVersion for the Settings
        # → System Information card.
        self._firmware_versions: dict[str, str] = {
            "console": "", "left": "", "right": "",
        }
        # Firmware autoupdate state (engineeringMode only).
        self._firmware_latest: dict[str, str] = {"console": "", "left": "", "right": ""}
        self._firmware_update_available: dict[str, bool] = {
            "console": False, "left": False, "right": False,
        }
        self._firmware_latest_by_kind: dict[str, str] = {}   # "console"/"sensor" -> tag
        self._firmware_checking_kinds: set = set()           # in-flight network checks
        self._firmware_check_lock = threading.Lock()         # guards check-then-add on _firmware_checking_kinds
        self._firmware_check_generation: int = 0
        self._firmware_update_in_progress: str | None = None # deviceKey being flashed
        # Track console connection time for safety grace period (issue #107 follow-up)
        self._console_connected_at: float | None = None
        # Real-time plot viewer source — assigned at scan start by startCapture.
        self._current_scan_source: ScanDataSource | None = None
        # The most-recent LiveScanSource is kept alive even after the user
        # navigates to a past scan, so "Back to live" can reassign without
        # reconstructing. None when no scan has run this session.
        self._live_scan_source: LiveScanSource | None = None
        # Monotonic token for async past-scan loads — a result whose seq
        # doesn't match the latest request is stale and dropped.
        self._past_scan_load_seq = 0

        self._tec_voltage_default = load_tec_params(config_dir)
        self._console_mutex = QRecursiveMutex()

        ft_mean     = cfg.get("ft_min_mean_per_camera")
        ft_contrast = cfg.get("ft_min_contrast_per_camera")
        self._ft_min_mean_per_camera     = list(ft_mean)     if isinstance(ft_mean,     (list, tuple)) else None
        self._ft_min_contrast_per_camera = list(ft_contrast) if isinstance(ft_contrast, (list, tuple)) else None

        ft_bfi      = cfg.get("ft_min_bfi_per_camera")
        ft_bfi_max  = cfg.get("ft_max_bfi_per_camera")
        ft_bvi      = cfg.get("ft_min_bvi_per_camera")
        ft_bvi_max  = cfg.get("ft_max_bvi_per_camera")
        self._ft_min_bfi_per_camera = list(ft_bfi)     if isinstance(ft_bfi,     (list, tuple)) else None
        self._ft_max_bfi_per_camera = list(ft_bfi_max) if isinstance(ft_bfi_max, (list, tuple)) else None
        self._ft_min_bvi_per_camera = list(ft_bvi)     if isinstance(ft_bvi,     (list, tuple)) else None
        self._ft_max_bvi_per_camera = list(ft_bvi_max) if isinstance(ft_bvi_max, (list, tuple)) else None
        # #122: per-camera max dark-frame mean — gates FT calibration on
        # ambient light leaking into the validation scan's dark frames.
        ft_dark_max = cfg.get("ft_max_dark_per_camera")
        self._ft_max_dark_per_camera = (
            list(ft_dark_max) if isinstance(ft_dark_max, (list, tuple)) else None
        )
        self._max_calibration_time_sec     = int(cfg.get("max_calibration_time_sec", 600))
        self._calibration_scan_duration_sec = int(cfg.get("calibration_scan_duration_sec", 5))
        self._calibration_scan_delay_sec    = int(cfg.get("calibration_scan_delay_sec", 1))
        self._test_scan_duration_sec = int(
            cfg.get("test_scan_duration_sec", 5)
        )
        self._calibration_status = ""  # "", "running", "passed", "failed", "aborted"
        self._calibration_failure_reason = ""  # populated only on FAIL in dev mode
        self._calibration_target = None  # last runCalibration() target
        self._test_scan_status = ""              # "", "running", "done", "aborted", "failed"
        self._test_scan_failure_reason = ""
        self._test_scan_rows: list[dict] = []

        self._post_thread = None
        self._post_cancel = threading.Event()

        self._capture_thread = None
        self._capture_stop = threading.Event()
        self._capture_running = False
        self._cq_quick_running = False
        # Monotonic id assigned to each notify() call. itertools.count is a
        # single atomic step under the GIL — notify() is reachable from
        # several worker threads (export/bundle/update workers), where a
        # read-modify-write `+= 1` could hand out duplicate ids.
        self._notification_ids = itertools.count(1)
        self._safety_cancel_scheduled = False  # True after scheduling cancel-due-to-safety; cleared when capture ends
        self._capture_left_path = ""
        self._capture_right_path = ""
        self._scan_notes = ""
        # session_label of the just-completed scan's DB session — notes edits
        # persist there (sessions.session_notes); empty until a scan completes.
        self._scan_notes_session_label = ""
        self.connect_signals()

        self._tec_voltage = 0.0
        self._tec_temp = 0.0
        self._tec_monV = 0.0
        self._tec_monC = 0.0
        self._tec_good = False

        self._tec_dac = 0.0

        self._pdu_raws = [0] * 16
        self._pdu_vals = [0.0] * 16

        # Start times for the scan start/end log banners (monotonic, or
        # None when the flow has never run).
        self._calibration_t0 = None
        self._test_scan_t0 = None
        self._cq_t0 = None

        os.makedirs(resolved_dir, exist_ok=True)
        self._directory = resolved_dir

        # ── Audit log startup events (the AuditLog itself was constructed
        #    earlier, before connect_signals). Logged here now that
        #    self._directory is resolved.
        from audit_log import gather_host_info
        try:
            from version import get_version as _get_app_version
            _app_version = _get_app_version()
        except Exception:
            _app_version = ""
        try:
            _sdk_version = self._interface.get_sdk_version()
            _sdk_version = (
                _sdk_version if isinstance(_sdk_version, str) else ""
            )
        except Exception:
            _sdk_version = ""
        self._audit.log("system_startup", {
            "app_version": _app_version,
            "sdk_version": _sdk_version,
            "data_dir": self._directory,
        })
        self._audit.log("system_info", gather_host_info())
        logger.info(f"[Connector] Directory initialized to: {self._directory}")

        self._user_label = self.generate_user_label()
        logger.info(f"[Connector] Generated user label: {self._user_label}")

        # Note: synthetic startup connect events for already-attached
        # devices are no longer needed. The new SDK lifecycle is:
        #   1. main.py constructs MotionInterface
        #   2. main.py constructs MotionConnector (this); we subscribe
        #      to handle.signal_state_changed in connect_signals()
        #   3. main.py calls motion_interface.start(), which discovers
        #      devices and drives state-machine transitions, firing the
        #      signals we already subscribed to.
        # So every real connection arrives via _on_handle_state_changed.

        self._interface.console.telemetry.add_listener(self._on_telemetry_update)

        # Arm the startup connection watchdog (E-104/E-106). The timer starts
        # once the Qt event loop runs — by which point motion_interface.start()
        # has had its window to enumerate already-attached devices.
        self._arm_connection_watchdog()

    def set_ft_thresholds(
        self,
        min_mean_per_camera=None,
        min_contrast_per_camera=None,
    ):
        """Set FT thresholds per camera (index 0-7). None or list of up to 8 numbers."""
        self._ft_min_mean_per_camera = (
            min_mean_per_camera
            if isinstance(min_mean_per_camera, (list, tuple))
            else None
        )
        self._ft_min_contrast_per_camera = (
            min_contrast_per_camera
            if isinstance(min_contrast_per_camera, (list, tuple))
            else None
        )

    def _compute_sensor_debug_flags(self) -> int:
        """Compute sensor debug flag bitfield from current config booleans."""
        flags = 0
        if self._sensor_debug_logging:
            flags |= DEBUG_FLAG_USB_PRINTF
        if self._camera_fake_data:
            flags |= DEBUG_FLAG_FAKE_DATA
        if self._histo_throttle:
            flags |= DEBUG_FLAG_HISTO_THROTTLE
        if self._comm_verbose:
            flags |= DEBUG_FLAG_COMM_VERBOSE
        if self._verbose_command_handling:
            flags |= DEBUG_FLAG_CMD_VERBOSE
        if self._histo_cmp:
            flags |= DEBUG_FLAG_HISTO_CMP
        if self._send_data_defer:
            flags |= DEBUG_FLAG_SEND_DEFER
        if self._histo_stall_test:
            flags |= DEBUG_FLAG_HISTO_STALL
        return flags

    def _apply_sensor_debug_flags(self) -> None:
        """Re-push the recomputed debug-flag bitmask to every connected sensor.

        Unlike ``_run_sensor_init``, this writes even when ``flags == 0`` so
        that turning the last flag off actually clears it on the firmware.
        Used by the live Settings → Engineering toggles (``setSensorDebugFlag``)
        so no reconnect/restart is needed.
        """
        flags = self._compute_sensor_debug_flags()
        for side in ("left", "right"):
            try:
                sensor = (
                    getattr(self._interface, side, None)
                    if self._interface else None
                )
            except Exception:
                sensor = None
            if sensor is None or not sensor.is_connected():
                continue
            logger.info("Re-applying debug flags 0x%x on %s sensor", flags, side)
            if not sensor.set_debug_flags(flags):
                logger.warning("Failed to set debug flags on %s sensor", side)

    def _sensor_init_note(self, side: str, delta: int) -> None:
        """Adjust the per-side init-in-flight counter (issue #303).

        +1 when a connect-time init is scheduled (GUI thread), -1 when the
        worker drains — success or failure alike, so a failed init can't
        leak the busy state. Emits sensorInitBusyChanged only on the
        busy/idle edge; the drain-side emission happens on the init worker
        thread and Qt auto-queues it to main-thread/QML consumers.
        """
        with self._sensor_init_lock:
            before = any(v > 0 for v in self._sensor_init_pending.values())
            new = self._sensor_init_pending.get(side, 0) + delta
            self._sensor_init_pending[side] = max(0, new)
            after = any(v > 0 for v in self._sensor_init_pending.values())
        if before != after:
            self.sensorInitBusyChanged.emit()

    @pyqtProperty(bool, notify=sensorInitBusyChanged)
    def sensorInitBusy(self) -> bool:
        """True while any sensor's connect-time init is scheduled/running
        (issue #303). QML holds Start/Check disabled on this; _ensure_idle
        refuses pipeline starts with it up. Re-engages on every sensor
        (re)connect because _schedule_sensor_init re-runs then."""
        with self._sensor_init_lock:
            return any(v > 0 for v in self._sensor_init_pending.values())

    def _schedule_sensor_init(self, side: str):
        """Delay initial sensor commands to allow USB settle, then run
        them on a daemon worker thread. The init sequence is a multi-
        command USB conversation with a built-in 0.5 s camera-power
        settle sleep — on the GUI thread it froze the app for >0.5 s on
        every sensor (re)connect."""
        # Raise the init-in-flight gate immediately at schedule time
        # (issue #303) — a Start clicked during the 1 s settle delay or the
        # init sequence itself collides with the connect-time USB
        # conversation and can wedge a camera until DUT power-cycle.
        self._sensor_init_note(side, +1)
        QTimer.singleShot(
            1000, lambda: self._start_sensor_init_worker(side)
        )

    def _start_sensor_init_worker(self, side: str) -> None:
        """Spawn the init worker. Worker-safety: the SDK serializes
        per-device command I/O (CommInterface._io_lock/_send_lock), every
        signal emitted from the sequence queues to the main thread, and
        _raise_critical is worker-safe by contract — same pattern as the
        CQ-check and past-scan-load workers."""
        threading.Thread(
            target=self._run_sensor_init, args=(side,),
            name=f"sensor-init-{side}", daemon=True,
        ).start()

    def _run_sensor_init(self, side: str):
        """Run the connect-time init sequence for one sensor. Called on
        a sensor-init worker thread (see _start_sensor_init_worker);
        unit tests call it synchronously. Never raises — an uncaught
        exception on a plain worker thread would vanish to stderr."""
        try:
            self._run_sensor_init_impl(side)
        except Exception:
            logger.exception("sensor init failed for %s sensor", side)
        finally:
            # Drop the init-in-flight gate on success AND failure — a
            # leaked busy state would lock Start/Check forever (issue #303).
            self._sensor_init_note(side, -1)

    def _run_sensor_init_impl(self, side: str):
        if side == "left" and not self._leftSensorConnected:
            return
        if side == "right" and not self._rightSensorConnected:
            return

        # Apply sensor debug flags (USB has had time to settle after connection)
        flags = self._compute_sensor_debug_flags()
        try:
            sensor = getattr(self._interface, side, None) if self._interface else None
        except Exception:
            sensor = None
        if flags != 0 and sensor is not None and sensor.is_connected():
            logger.info(
                "Setting debug flags 0x%x on %s sensor "
                "(debug_logging=%s, fake_data=%s, histoThrottle=%s, histoCmp=%s, "
                "deferHistoSend=%s, commVerbose=%s, verboseCommand=%s)",
                flags,
                side,
                self._sensor_debug_logging,
                self._camera_fake_data,
                getattr(self, "_histo_throttle", False),
                getattr(self, "_histo_cmp", False),
                getattr(self, "_send_data_defer", False),
                getattr(self, "_comm_verbose", False),
                getattr(self, "_verbose_command_handling", False),
            )
            if not sensor.set_debug_flags(flags):
                logger.warning("Failed to set debug flags on %s sensor", side)
        elif flags != 0:
            logger.info(
                "Skipping debug flag set on %s sensor (flags=0x%x, sensor_present=%s, connected=%s)",
                side,
                flags,
                sensor is not None,
                getattr(sensor, "is_connected", lambda: False)() if sensor else False,
            )

        self.getFanControlStatus(side)

        # Surface the firmware's boot-time I2C self-check (E-101/E-102) — the
        # canonical "I2C check at the beginning". Best-effort: never blocks
        # init, just raises the modal if the sensor reports trouble.
        self._check_sensor_i2c_health(side)

        # Power on all cameras, fill the ID cache (serial numbers, connection info), then power off
        try:
            sensor = getattr(self._interface, side, None) if self._interface else None
            if sensor is not None and sensor.is_connected():
                enable_power = getattr(sensor, "enable_camera_power", None)
                disable_power = getattr(sensor, "disable_camera_power", None)
                refresh_cache = getattr(sensor, "refresh_id_cache", None)
                if enable_power and disable_power and refresh_cache:
                    if enable_power(0xFF):
                        time.sleep(0.5)  # settle time
                        refresh_cache()
                        if self._power_off_unused_cameras:
                            disable_power(0xFF)
                            time.sleep(0.05)
                    else:
                        logger.warning(
                            "Could not power on cameras on %s sensor for ID cache fill",
                            side,
                        )
                        self._raise_critical("E-105", detail=f"{side} sensor")
                        refresh_cache()  # try anyway in case some cameras are already on
                elif refresh_cache:
                    refresh_cache()  # fallback: fill cache without power cycle (may get zeros for off cameras)
        except Exception as e:
            logger.debug("Could not refresh sensor ID cache for %s: %s", side, e)

        # Log the sensor's hardware identity (firmware, hw_id, serial, and
        # all 8 camera UIDs) once per sensor connect, after the cache fill.
        # The SDK's log_sensor_info emits the guarded LEFT/RIGHT block; this
        # mirrors log_console_info on console connect and log_system_info at
        # startup — the three "log the configuration" calls.
        self._interface.log_sensor_info(side)

        self.connectionStatusChanged.emit()

    def _check_sensor_i2c_health(self, side: str) -> None:
        """Raise E-101/E-102 if a sensor's boot-time I2C self-check is bad.

        Reads the firmware's cached snapshot (no disruptive rescan). Never
        raises out — it's a best-effort surfacing layer over the SDK's
        ``MotionSensor.i2c_health``.
        """
        try:
            sensor = getattr(self._interface, side, None) if self._interface else None
            if sensor is None or not sensor.is_connected():
                return
            health = getattr(sensor, "i2c_health", None)
            if health is None:
                self._raise_critical("E-102", detail=f"{side} sensor")
            elif not health.get("all_present", False):
                missing = self._i2c_missing_devices(health)
                self._raise_critical("E-101", detail=f"{side} sensor: {missing}")
        except Exception:
            logger.exception("I2C health check failed for %s sensor", side)

    def _arm_connection_watchdog(self) -> None:
        """Schedule the one-shot startup connection check (E-104/E-106)."""
        if self._connection_timeout_sec > 0:
            QTimer.singleShot(
                int(self._connection_timeout_sec * 1000),
                self._check_connection_watchdog,
            )

    def _check_connection_watchdog(self) -> None:
        """Warn about devices that never connected within the startup timeout.

        One-shot: reads the current cached connection state. A missing console
        (E-104) and/or too few sensors (E-106) are non-blocking — they surface
        as a single yellow warning toast (bottom-right), not the critical
        modal, since the fix is usually just "plug it in". If *both* are
        missing the toast collapses to a plain "System not found". Disconnects
        that happen *after* startup are handled by the connection-status UI.
        """
        try:
            console_missing = self._require_console and not self._consoleConnected
            n_sensors = (int(bool(self._leftSensorConnected))
                         + int(bool(self._rightSensorConnected)))
            sensors_missing = n_sensors < self._min_sensors
            if not console_missing and not sensors_missing:
                return

            if console_missing and sensors_missing:
                logger.warning(
                    "Connection watchdog: console and sensors missing "
                    "(E-104/E-106: %d of %d sensors)", n_sensors, self._min_sensors)
                msg = ("System not found. Check that the console and sensor "
                       "are connected and powered on.")
            elif console_missing:
                logger.warning("Connection watchdog: console not detected (E-104)")
                msg = ("Console not detected. Check the console USB cable and "
                       "power, then reconnect.")
            else:
                logger.warning(
                    "Connection watchdog: sensor not detected "
                    "(E-106: %d of %d)", n_sensors, self._min_sensors)
                msg = ("Sensor not detected. Check the sensor USB cable and "
                       "power, then reconnect.")
            # Sticky, single (tagged) yellow toast — the user must act, but it
            # never blocks the UI like the critical modal.
            self.notify(msg, "warning", duration_ms=0, dismissible=True,
                        tag="connection-watchdog")
        except Exception:
            logger.exception("connection watchdog check failed")

    @staticmethod
    def _i2c_missing_devices(health: dict) -> str:
        """Human-readable summary of which I2C devices didn't respond."""
        parts = []
        if not health.get("mux", True):
            parts.append("mux")
        if not health.get("imu", True):
            parts.append("imu")
        cams = [i for i, ok in enumerate(health.get("cameras", [])) if not ok]
        if cams:
            parts.append("cameras " + ",".join(str(i) for i in cams))
        fpgas = [i for i, ok in enumerate(health.get("fpgas", [])) if not ok]
        if fpgas:
            parts.append("fpgas " + ",".join(str(i) for i in fpgas))
        return "; ".join(parts) or "unknown device"

    # --- GETTERS/SETTERS FOR Qt PROPERTIES ---
    def getUserLabel(self) -> str:
        return self._user_label

    def setUserLabel(self, value: str):
        if not value:
            return
        # normalize to "ow" + alphanumerics (uppercase)
        if value.startswith("ow"):
            rest = value[2:]
        else:
            rest = value
        rest = "".join(ch for ch in rest.upper() if ch.isalnum())
        new_val = "ow" + rest
        if new_val != self._user_label:
            self._user_label = new_val
            self.userLabelChanged.emit()

    userLabel = pyqtProperty(
        str, fget=getUserLabel, fset=setUserLabel, notify=userLabelChanged
    )

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def leftSensorConnected(self):
        """Expose Sensor connection status to QML."""
        return self._leftSensorConnected

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def rightSensorConnected(self):
        """Expose Sensor connection status to QML."""
        return self._rightSensorConnected

    @pyqtProperty(bool, notify=connectionStatusChanged)
    def consoleConnected(self):
        """Expose Console connection status to QML."""
        return self._consoleConnected

    @pyqtProperty(str, notify=firmwareVersionsChanged)
    def consoleFirmwareVersion(self) -> str:
        """Console firmware version cached at connect time (empty when
        unknown / disconnected). Shown in Settings → System Information."""
        return self._firmware_versions["console"]

    @pyqtProperty(str, notify=firmwareVersionsChanged)
    def leftSensorFirmwareVersion(self) -> str:
        """Left sensor firmware version; see consoleFirmwareVersion."""
        return self._firmware_versions["left"]

    @pyqtProperty(str, notify=firmwareVersionsChanged)
    def rightSensorFirmwareVersion(self) -> str:
        """Right sensor firmware version; see consoleFirmwareVersion."""
        return self._firmware_versions["right"]

    @pyqtProperty(bool, notify=firmwareUpdateInfoChanged)
    def anyFirmwareUpdateAvailable(self) -> bool:
        return any(self._firmware_update_available.values())

    @pyqtProperty(bool, notify=firmwareUpdateInfoChanged)
    def consoleFirmwareUpdateAvailable(self) -> bool:
        return self._firmware_update_available["console"]

    @pyqtProperty(bool, notify=firmwareUpdateInfoChanged)
    def leftSensorFirmwareUpdateAvailable(self) -> bool:
        return self._firmware_update_available["left"]

    @pyqtProperty(bool, notify=firmwareUpdateInfoChanged)
    def rightSensorFirmwareUpdateAvailable(self) -> bool:
        return self._firmware_update_available["right"]

    @pyqtProperty(str, notify=firmwareUpdateInfoChanged)
    def consoleFirmwareLatest(self) -> str:
        return self._firmware_latest["console"]

    @pyqtProperty(str, notify=firmwareUpdateInfoChanged)
    def leftSensorFirmwareLatest(self) -> str:
        return self._firmware_latest["left"]

    @pyqtProperty(str, notify=firmwareUpdateInfoChanged)
    def rightSensorFirmwareLatest(self) -> str:
        return self._firmware_latest["right"]

    @pyqtProperty(bool, notify=consoleFanChanged)
    def consoleFanOn(self) -> bool:
        """Cached last-set console-fan state (True=on). Reflects the
        connect-time 100% default and any setConsoleFan call. Read by the
        Engineering settings switch when the Settings modal opens."""
        return self._console_fan_on

    @pyqtSlot(bool, result=bool)
    def setConsoleFan(self, on: bool) -> bool:
        """Drive the console fan to 100% (on) or 0% (off).

        Updates the cached state + notifies QML on success. Guarded and
        wrapped like the other console slots so a mid-flight disconnect
        can't raise out of the Qt slot and kill the process.
        """
        if not self._consoleConnected:
            logger.error("Console not connected — cannot set console fan")
            return False
        try:
            speed = 100 if on else 0
            # set_fan_speed returns the duty cycle that was set (0..100),
            # or -1 on OW_ERROR. Check against the -1 sentinel — NOT
            # truthiness — because turning the fan off returns 0, which
            # is falsy but successful.
            if self._interface.console.set_fan_speed(fan_speed=speed) != -1:
                self._console_fan_on = bool(on)
                self.consoleFanChanged.emit()
                logger.info("Console fan set to %s", "ON" if on else "OFF")
                return True
            logger.error("Failed to set console fan")
            return False
        except Exception as e:  # noqa: BLE001 — slot must not raise
            logger.error("Error setting console fan: %s", e)
            return False

    @pyqtSlot(bool)
    def setConsoleDebugLogging(self, on: bool) -> None:
        """Toggle the console USB-printf debug log (DEBUG_FLAG_USB_PRINTF).

        Persists ``consoleDebugLogging`` and, when the console is connected,
        pushes the bit live via ``console.enable_usb_printf`` (no restart) —
        the console analogue of the sensor ``setSensorDebugFlag`` toggles. The
        firmware flag is RAM-only, so it is also re-applied on every console
        connect (see the connect handler). When no console is connected the
        value is still persisted and applies on the next connect. Guarded like
        ``setConsoleFan`` so a mid-flight disconnect can't raise out of the
        slot.
        """
        on = bool(on)
        old = self._app_config.get("consoleDebugLogging")
        self._console_debug_logging = on
        self._app_config["consoleDebugLogging"] = on
        self._save_app_config()
        self.appConfigChanged.emit()
        if old != on:
            self._audit.log("settings_changed",
                            {"changes": {"consoleDebugLogging":
                                         {"old": old, "new": on}}})
        if not self._consoleConnected:
            return
        try:
            logger.info("Setting console debug logging %s",
                        "ON" if on else "OFF")
            if not self._interface.console.enable_usb_printf(on):
                logger.warning("Failed to set console debug logging")
        except Exception as e:  # noqa: BLE001 — slot must not raise
            logger.error("Error setting console debug logging: %s", e)

    @pyqtProperty(bool, notify=laserStateChanged)
    def laserOn(self):
        """Expose the laser-on state to QML."""
        return self._laserOn

    @pyqtProperty(bool, notify=safetyFailureStateChanged)
    def safetyFailure(self):
        """Expose the laser-safety-failure state to QML."""
        return self._safetyFailure

    @safetyFailure.setter
    def safetyFailure(self, value: bool):
        if self._safetyFailure != value:
            self._safetyFailure = value
            self.safetyFailureStateChanged.emit()
            # Clear the persistent laser-safety toast on recovery. The
            # toast is *fired* by ``_fire_safety_notification`` in the
            # call site (readSafetyStatus) where the decoded fault
            # detail is available — we only handle dismissal here so
            # we don't double-fire without context.
            if not value:
                self.dismissNotification("laser_safety")

    def _fire_safety_notification(self, fault_detail: str = ""):
        """Fire the persistent laser-safety toast.

        ``fault_detail`` is appended to the toast text only when
        ``appConfig.engineeringMode`` is enabled, so end users see a
        friendly message and engineers see which fault bits tripped.
        Tagged ``laser_safety`` so re-fires replace rather than stack.
        """
        msg = (
            "Laser safety warning detected. Please restart your "
            "console. If this error persists, please contact support."
        )
        if fault_detail and self._app_config.get("engineeringMode", False):
            msg += f"\n[dev] {fault_detail}"
        self.notify(
            msg,
            type_="error",
            duration_ms=0,
            dismissible=False,
            tag="laser_safety",
        )

    @pyqtProperty(int, notify=stateChanged)
    def state(self):
        """Expose state as a QML property."""
        return self._state

    @pyqtProperty(str, notify=triggerStateChanged)
    def triggerState(self):
        return self._trigger_state

    # --- Calibration procedure properties (consumed by SettingsModal.qml) ---
    @pyqtProperty(bool, notify=calibrationStateChanged)
    def calibrationRunning(self) -> bool:
        return self._calibration_status == "running"

    @pyqtProperty(str, notify=calibrationStateChanged)
    def calibrationStatus(self) -> str:
        return self._calibration_status

    @pyqtProperty(str, notify=calibrationStateChanged)
    def calibrationFailureReason(self) -> str:
        return self._calibration_failure_reason

    @pyqtProperty(int, notify=calibrationStateChanged)
    def maxCalibrationTimeSec(self) -> int:
        return self._max_calibration_time_sec

    @pyqtProperty(bool, notify=testScanStateChanged)
    def testScanRunning(self) -> bool:
        return self._test_scan_status == "running"

    @pyqtProperty(str, notify=testScanStateChanged)
    def testScanStatus(self) -> str:
        return self._test_scan_status

    @pyqtProperty(str, notify=testScanStateChanged)
    def testScanFailureReason(self) -> str:
        return self._test_scan_failure_reason

    @pyqtProperty('QVariantList', notify=testScanStateChanged)
    def testScanRows(self) -> list:
        return self._test_scan_rows

    # --- DEVICE CONNECTION / DISCONNECTION / STATE MANAGEMENT METHODS ---
    def _on_handle_state_changed(self, handle, old, new, reason):
        """Single state-change handler wired to console/left/right.

        Replaces the old signal_connect/signal_disconnect pair. The handle
        argument is the stable MotionConsole/MotionSensor instance; we
        switch on handle.name. ``new`` is a ConnectionState enum.
        """
        try:
            self._on_handle_state_changed_impl(handle, old, new, reason)
        except Exception as e:
            # Top-level safety net: this slot is invoked from the SDK
            # connection-monitor thread via Qt signals. Any uncaught
            # exception here propagates as "Unhandled Python exception"
            # at the Qt boundary and terminates the bloodflow process.
            # Log loudly and swallow — the worst case is a stale UI
            # state until the next connection event re-fires the slot.
            logger.exception(
                "_on_handle_state_changed crashed for handle=%s "
                "old=%s new=%s reason=%s — swallowing to keep app alive",
                getattr(handle, "name", "?"), old, new, reason,
            )

    def _on_handle_state_changed_impl(self, handle, old, new, reason):
        from omotion import ConnectionState

        is_now_connected = (new == ConnectionState.CONNECTED)
        is_now_lost = (new == ConnectionState.DISCONNECTED)
        name = handle.name

        # A device we are deliberately flashing drops into DFU and re-enumerates
        # as a different USB device; that disconnect is expected. Suppress it
        # entirely (no state mutation, no UI thrash) so the connector's
        # connected-flags + _state stay consistent until the post-flash
        # power-cycle reconnect re-runs normal connect handling.
        if is_now_lost and self._firmware_update_in_progress == name:
            logger.info("Ignoring expected DFU disconnect for %s during "
                        "firmware update", name)
            return

        if name == "console":
            self._consoleConnected = is_now_connected
            if is_now_connected:
                # Record connection time for safety grace period
                self._console_connected_at = time.monotonic()
                # Race-guard: by the time this slot fires we observe
                # CONNECTED, but the console can disconnect again before
                # any of these calls return — particularly during the
                # post-power-cycle reconnect storm where a single
                # PermissionError on the COM port immediately drives
                # CONNECTED -> DISCONNECTING. Without the try/except,
                # tec_voltage / set_fan_speed raise
                # "ValueError: Motion Console not connected" out of the
                # Qt slot, which propagates as an unhandled Python
                # exception and terminates the app process.
                try:
                    self._interface.log_console_info()
                    if self._interface.console.tec_voltage(self._tec_voltage_default):
                        logger.info(f"Console TEC voltage set to {self._tec_voltage_default}V")
                    else:
                        logger.error(
                            f"Failed to set console TEC voltage to {self._tec_voltage_default}V"
                        )
                    if self._interface.console.set_fan_speed(fan_speed=100):
                        logger.info("Console fan speed set to 100%")
                        self._console_fan_on = True
                        self.consoleFanChanged.emit()
                    else:
                        logger.error("Failed to set console fan speed")
                    # Re-apply the console debug-log flag — it is RAM-only on
                    # the MCU, so it resets across reconnects/power-cycles.
                    # Default-off needs no action (firmware default is off).
                    if self._console_debug_logging:
                        # Optional debug-logging re-apply must never abort the
                        # safety-critical laser-power application that follows.
                        # Guard it independently (e.g. an omotion build lacking
                        # console.enable_usb_printf would otherwise raise
                        # AttributeError and skip set_laser_power_from_config,
                        # leaving the laser dark — its FPGA drive registers are
                        # volatile and reloaded only here).
                        try:
                            if self._interface.console.enable_usb_printf(True):
                                logger.info("Console debug logging re-enabled")
                            else:
                                logger.error("Failed to re-enable console debug logging")
                        except Exception as e:  # noqa: BLE001
                            logger.warning(
                                "Console debug logging re-apply failed; "
                                "continuing connect-time setup: %s", e
                            )
                    # Apply laser-power params once per console connect —
                    # the FPGA registers are volatile across power cycles,
                    # so every (re)connect needs them. Scans no longer
                    # re-apply per run (was SetTriggerLaserTask + the
                    # issue-#108 cold-start guards).
                    if self.set_laser_power_from_config(self._interface):
                        logger.info("Laser power params applied from config")
                    else:
                        logger.error("Failed to apply laser power params from config")
                        self._raise_critical(
                            "E-103", detail="laser power params not applied")
                    # Push the configured TEC over-temp trip (tecTripTempC, °C)
                    # into the console user config. Read-modify-write that
                    # preserves calibration + OPT/EE keys; non-fatal on a bad
                    # value or write failure (device keeps its existing trip).
                    self.apply_tec_trip_from_config(self._interface)
                except Exception as e:
                    logger.warning(
                        f"Console connect-time setup interrupted "
                        f"(probably mid-flight disconnect): {e}"
                    )
            elif is_now_lost:
                # Clear connection timestamp on disconnect
                self._console_connected_at = None
                # A dead console can't be driving the laser trigger, and it
                # can't deliver the SDK's trigger-OFF event either (its
                # stop_trigger raises during teardown). Close the trigger
                # clock at detection time so the notes duration line and the
                # header counter stop here instead of counting several more
                # seconds of scan teardown (issue #201).
                if self._trigger_state == "ON" or self._trigger_on_mono is not None:
                    logger.warning(
                        "Console lost while trigger ON — closing the trigger "
                        "clock at disconnect detection (%s)", reason,
                    )
                    self._trigger_clock_close()
                    self._trigger_state = "OFF"
                    self.triggerStateChanged.emit()
        elif name == "left":
            if is_now_connected:
                self._leftSensorConnected = True
                self._schedule_sensor_init("left")
            elif is_now_lost:
                self._leftSensorConnected = False
                self._last_fan_status["left"] = None
                try:
                    if getattr(self._interface.left, "clear_id_cache", None):
                        self._interface.left.clear_id_cache()
                except Exception:
                    logger.debug("clear_id_cache failed for left", exc_info=True)
        elif name == "right":
            if is_now_connected:
                self._rightSensorConnected = True
                self._schedule_sensor_init("right")
            elif is_now_lost:
                self._rightSensorConnected = False
                self._last_fan_status["right"] = None
                try:
                    if getattr(self._interface.right, "clear_id_cache", None):
                        self._interface.right.clear_id_cache()
                except Exception:
                    logger.debug("clear_id_cache failed for right", exc_info=True)

        if is_now_connected:
            logger.info("Handle %s -> CONNECTED (%s)", name, reason)
            self.signalConnected.emit(name, "")
            self._audit.log("device_connected",
                            {"device": name, "reason": str(reason)})
            self._log_device_stats(name)
        elif is_now_lost:
            logger.info(
                "Handle %s -> DISCONNECTED (%s) and state is %s",
                name, reason, self._state,
            )
            self.signalDisconnected.emit(name, "")
            self._audit.log("device_disconnected",
                            {"device": name, "reason": str(reason)})
            # Drop the cached firmware version so System Information no
            # longer reports a stale value for the unplugged device.
            if name in self._firmware_versions and self._firmware_versions[name]:
                self._firmware_versions[name] = ""
                self.firmwareVersionsChanged.emit()
            if self._firmware_update_available.get(name):
                self._firmware_update_available[name] = False
                self._firmware_latest[name] = ""
                self.firmwareUpdateInfoChanged.emit()
            # Abort an in-flight FPGA flash / sensor-configure pipeline.
            # The SDK does not subscribe to disconnect events for the
            # configure-cameras flow (only start_scan does), so without
            # this hand-off the QML flashTask waits on its 4-min watchdog
            # and the Start button stays stuck on "Stop" through reconnect.
            if self._config_running:
                logger.warning(
                    "Aborting in-flight camera configuration: %s disconnected",
                    name,
                )
                try:
                    self._interface.cancel_configure_camera_sensors()
                except Exception as e:
                    logger.debug("cancel_configure_camera_sensors raised: %s", e)
                self._config_running = False
                self.configFinished.emit(
                    False, f"Device disconnected during sensor configuration ({name})"
                )
        # CONNECTING / DISCONNECTING are intermediate; UI doesn't need to
        # fire a connect/disconnect signal for those, and emitting
        # connectionStatusChanged on every intermediate transition would
        # cause QML observers to re-poll device state 4× per disconnect
        # cycle (once for each of CONNECTED→DISCONNECTING→DISCONNECTED→
        # CONNECTING→CONNECTED).
        if is_now_connected or is_now_lost:
            self.connectionStatusChanged.emit()
            self.update_state()

    def _log_device_stats(self, name: str) -> None:
        """Best-effort audit of a device's hardware + firmware IDs on
        connect. Missing values are logged as empty strings."""
        handle = getattr(self._interface, name, None)
        hwid = ""
        fw = ""
        try:
            if handle is not None and hasattr(handle, "get_hardware_id"):
                hwid = str(handle.get_hardware_id() or "")
        except Exception:
            logger.debug("device_stats: %s hardware-id lookup failed",
                         name, exc_info=True)
        try:
            if handle is not None and hasattr(handle, "get_version"):
                fw = str(handle.get_version() or "")
        except Exception:
            logger.debug("device_stats: %s firmware-version lookup failed",
                         name, exc_info=True)
        self._audit.log("device_stats", {
            "device": name, "hardware_id": hwid, "firmware_version": fw,
        })
        # Cache for the Settings → System Information card. Fired from the
        # SDK connection-monitor thread; the bound QML rows update via the
        # queued firmwareVersionsChanged emit.
        if name in self._firmware_versions:
            self._firmware_versions[name] = fw
            self.firmwareVersionsChanged.emit()
        self._maybe_check_firmware_update(name)

    @staticmethod
    def _kind_for_device(name: str) -> FirmwareKind:
        return FirmwareKind.CONSOLE if name == "console" else FirmwareKind.SENSOR

    @staticmethod
    def _devices_for_kind(kind: FirmwareKind) -> list[str]:
        return ["console"] if kind == FirmwareKind.CONSOLE else ["left", "right"]

    def _maybe_check_firmware_update(self, name: str) -> None:
        """If engineeringMode, ensure this device's firmware-update availability
        is computed — reusing a cached 'latest' for the kind, or kicking one
        background GitHub check per kind per session."""
        if not self._app_config.get("engineeringMode", False):
            return
        if name not in self._firmware_versions or not self._firmware_versions[name]:
            return
        kind = self._kind_for_device(name)
        if self._firmware_latest_by_kind.get(kind.value):
            self._recompute_firmware_update(name)   # latest already known; no network
            return
        with self._firmware_check_lock:
            if kind in self._firmware_checking_kinds:
                return                              # a check is already in flight
            self._firmware_checking_kinds.add(kind)
        threading.Thread(
            target=self._firmware_check_worker,
            args=(kind, self._firmware_check_generation),
            daemon=True,
        ).start()

    def _firmware_check_worker(self, kind: FirmwareKind, generation: int | None = None) -> None:
        try:
            beta = self._app_config.get("downloadBetaFirmware", False)
            info = check_latest(kind, include_prerelease=beta)
        except Exception:                           # defensive; check_latest is fail-soft
            info = None
        finally:
            self._firmware_checking_kinds.discard(kind)
        if info is None:
            return                                  # leave kind unchecked so it retries
        # A beta toggle (or other refresh) bumps the generation; a worker that
        # started under an older generation captured a now-stale beta flag, so
        # discard its result rather than clobber the fresh one.
        if generation is not None and generation != self._firmware_check_generation:
            return
        self._firmware_latest_by_kind[kind.value] = info.tag
        for dev in self._devices_for_kind(kind):
            self._recompute_firmware_update(dev)

    def _recompute_firmware_update(self, name: str) -> None:
        if not self._firmware_versions.get(name):
            return  # device not connected / version unknown — nothing to recompute
        kind = self._kind_for_device(name)
        installed = self._firmware_versions.get(name, "")
        latest = self._firmware_latest_by_kind.get(kind.value, "")
        beta = self._app_config.get("downloadBetaFirmware", False)
        avail = (bool(installed) and bool(latest)
                 and is_update_available(installed, latest, prerelease=beta))
        self._firmware_latest[name] = latest
        self._firmware_update_available[name] = avail
        self.firmwareUpdateInfoChanged.emit()
        if avail:
            logger.info("Firmware update available for %s: %s -> %s",
                        name, installed, latest)
            self.firmwareUpdateAvailable.emit(name, installed, latest)

    def _refresh_firmware_update_check(self) -> None:
        """Invalidate the firmware-latest caches and re-run detection for every
        connected device — called when downloadBetaFirmware toggles so the
        banner/Settings card reflect the new release pool immediately."""
        self._firmware_latest_by_kind.clear()
        self._firmware_checking_kinds.clear()
        self._firmware_check_generation += 1
        for name in ("console", "left", "right"):
            self._firmware_latest[name] = ""
            self._firmware_update_available[name] = False
        self.firmwareUpdateInfoChanged.emit()
        for name in ("console", "left", "right"):
            if self._firmware_versions.get(name):
                self._maybe_check_firmware_update(name)

    @pyqtSlot(str, result=bool)
    def startFirmwareUpdate(self, device_key: str) -> bool:
        """Download the latest firmware for device_key and flash it over DFU.
        engineeringMode-only; refused during a scan or while another update runs."""
        if device_key not in ("console", "left", "right"):
            return False
        if not self._app_config.get("engineeringMode", False):
            return False
        if self._state == RUNNING or self._running:
            self.firmwareUpdateFinished.emit(
                device_key, False, "Cannot update firmware during a scan.")
            return False
        if self._firmware_update_in_progress is not None:
            self.firmwareUpdateFinished.emit(
                device_key, False, "Another firmware update is in progress.")
            return False
        if not self._firmware_update_available.get(device_key, False):
            self.firmwareUpdateFinished.emit(
                device_key, False, "No update available for this device.")
            return False
        self._firmware_update_in_progress = device_key
        threading.Thread(
            target=self._firmware_update_worker, args=(device_key,), daemon=True
        ).start()
        return True

    def _firmware_update_worker(self, device_key: str) -> None:
        import tempfile
        from omotion.firmware_update import FirmwareUpdater, download_firmware

        ok, msg = False, ""
        try:
            kind = self._kind_for_device(device_key)
            self.firmwareUpdateProgress.emit(
                device_key, "check", -1, "Checking latest release…")
            beta = self._app_config.get("downloadBetaFirmware", False)
            info = check_latest(kind, include_prerelease=beta)
            if info is None:
                raise RuntimeError("Could not reach GitHub to fetch firmware.")
            self.firmwareUpdateProgress.emit(
                device_key, "download", -1, f"Downloading {info.tag}…")
            dest = Path(tempfile.gettempdir()) / "om-firmware"
            bin_path = download_firmware(info, dest)

            handle = getattr(self._interface, device_key, None)
            if handle is None:
                raise RuntimeError("Device handle unavailable.")

            def _prog(p) -> None:
                pct = p.percent if p.percent is not None else -1
                # Emit a clean per-phase label — do NOT forward dfu-util's raw
                # output line, which embeds an ASCII progress bar ("[===   ]")
                # whose trailing text shifts as it fills and jitters in the UI.
                label = {"erase": "Erasing", "download": "Writing"}.get(
                    p.phase, str(p.phase).capitalize())
                self.firmwareUpdateProgress.emit(device_key, p.phase, pct, label)

            self.firmwareUpdateProgress.emit(
                device_key, "flash", -1, "Entering DFU and flashing…")
            result = FirmwareUpdater().update(handle, bin_path, progress_cb=_prog)
            ok = bool(result.success)
            # Phase 0 finding: dfu-util's leave does NOT reset the STM32 — the
            # device hangs non-enumerating until a manual power-cycle, then
            # reconnects (~20-30 s) and Task 7's connect-time check clears the
            # banner. So success means "written; now power-cycle", not "done".
            msg = ("Firmware written. Power-cycle the device now — it will "
                   "reconnect with the new version shortly.") if ok else \
                  "dfu-util reported a failure."
        except Exception as e:                       # noqa: BLE001 - reported to UI
            logger.exception("Firmware update failed for %s", device_key)
            ok, msg = False, str(e)
        finally:
            self._firmware_update_in_progress = None
            # Force re-detection so the banner/card clear once the new version
            # is read back after the device re-enumerates.
            self._firmware_latest_by_kind.pop(
                self._kind_for_device(device_key).value, None)
            self.firmwareUpdateFinished.emit(device_key, ok, msg)

    def update_state(self):
        """Update system state based on connection and configuration."""
        if not self._consoleConnected and (
            (not self._leftSensorConnected) or (not self._rightSensorConnected)
        ):
            self._state = DISCONNECTED
        elif self._leftSensorConnected and not self._consoleConnected:
            self._state = SENSOR_CONNECTED
        elif self._consoleConnected and not self._leftSensorConnected:
            self._state = CONSOLE_CONNECTED
        elif self._consoleConnected and self._leftSensorConnected:
            self._state = READY
        elif self._consoleConnected and self._leftSensorConnected and self._running:
            self._state = RUNNING
        self.stateChanged.emit()  # Notify QML of state update
        logger.debug(f"Updated state: {self._state}")

    def _on_telemetry_update(self, snap) -> None:
        if not snap.read_ok:
            logger.warning("Telemetry poll error: %s", snap.error)
            return

        try:
            self.tec_status(snap)
            logger.debug(
                "TEC Status – temp: %.2f set: %.2f tec_c: %.3f tec_v: %.3f good: %s",
                self._tec_voltage, self._tec_temp,
                snap.tec_curr_raw, snap.tec_volt_raw, snap.tec_good,
            )
        except Exception as exc:
            logger.error("_on_telemetry_update TEC error: %s", exc)

        try:
            self.pdu_mon(snap)
            if snap.pdu_volts:
                logger.debug(
                    "PDU MON ADC0 vals: %s",
                    " ".join(f"{(v / SCALE_V):.3f}" for v in snap.pdu_volts[:8]),
                )
                adc1_scaled = [
                    (v / SCALE_V) if idx == 6 else (v / SCALE_I)
                    for idx, v in enumerate(snap.pdu_volts[8:])
                ]
                logger.debug(
                    "PDU MON ADC1 vals: %s",
                    " ".join(f"{v:.3f}" for v in adc1_scaled),
                )
        except Exception as exc:
            logger.error("_on_telemetry_update PDU error: %s", exc)

        try:
            self.readSafetyStatus(snap)
        except Exception as exc:
            logger.error("_on_telemetry_update safety error: %s", exc)

        try:
            logger.debug(
                "Analog Values – TCM: %d, TCL: %d, PDC: %.3f",
                snap.tcm, snap.tcl, snap.pdc,
            )
        except Exception as exc:
            logger.error("_on_telemetry_update analog error: %s", exc)

    @pyqtSlot(str)
    def handleUpdateCapStatus(self, status_msg: str):
        logger.debug(f"Console status update: {status_msg}")

    @pyqtSlot()
    def stopCapture(self):
        """Stop capture (Cancel button or app close). Ceases scan, disables cameras, waits for worker."""
        if self._capture_running:
            self.captureLog.emit("Stop requested.")

        self._dropout_timer.stop()
        self._capture_stop.set()
        try:
            if self._interface:
                self._interface.cancel_scan()
            self._trigger_state = "OFF"
            self.triggerStateChanged.emit()
        except Exception as e:
            logger.warning("Error stopping trigger: %s", e)

        self._capture_thread = None

    @pyqtSlot()
    def shutdown(self):
        """Shutdown connector. Stops any capture in flight; main.py's
        handle_exit stops the MotionInterface (monitoring + device
        teardown) right after this returns."""
        logger.info("Shutting down MotionConnector...")
        self.stopCapture()
        try:
            self._audit.log("system_shutdown", {"clean": True})
            self._audit.close()
        except Exception:
            logger.warning("audit shutdown log failed", exc_info=True)
        logger.info("MotionConnector shutdown complete.")

    # --- SCAN MANAGEMENT METHODS ---
    # Suffix patterns that distinguish the corrected (canonical) CSV
    # from per-scan auxiliary CSVs (raw histo, telemetry). Issue #44:
    # the canonical file dropped its ``_corrected`` suffix so the
    # previous ``*_corrected.csv`` glob no longer matches new scans.
    # Discovery now globs all CSVs and excludes anything matching one
    # of these aux patterns. Order matters: longer / more-specific
    # patterns first so an aux file isn't mistaken for canonical.
    _AUX_CSV_RE = re.compile(
        r"_("
        r"telemetry"                                  # _telemetry.csv
        r"|(?:left|right)_mask[0-9A-Fa-f]+(?:_raw)?"  # raw histo, new + legacy
        r")$"
    )

    @pyqtSlot(result=list)
    def get_scan_list(self):
        """Return sorted list of scan IDs from BOTH the corrected CSVs on disk
        and the scan database's sessions.

        The scan DB is the system of record: clinical-mode scans (and any scan
        with writeCorrectedCsv off) write no corrected CSV, so they exist only
        as DB sessions. A session_label has the same shape as the CSV-derived
        scan id (``YYYYMMDD_HHMMSS_userLabel``), so the two sources merge by id.

        CSV filename formats supported (legacy / corrected-CSV scans):
          New (post-#44): {YYYYMMDD_HHMMSS}_{sessionId}.csv
          Mid:            {YYYYMMDD_HHMMSS}_{sessionId}_corrected.csv
          Legacy:         scan_{sessionId}_{YYYYMMDD_HHMMSS}_corrected.csv
        """
        seen: set[str] = set()
        ids: list[str] = []

        base_path = Path(self._data_root)
        if base_path.exists():
            for f in base_path.glob("*.csv"):
                if not f.is_file():
                    continue
                stem = f.stem
                # Skip per-scan auxiliary files (raw histo, telemetry).
                if self._AUX_CSV_RE.search(stem):
                    continue
                # Mid format: strip the ``_corrected`` suffix to get the
                # canonical scan id.
                if stem.endswith("_corrected"):
                    stem = stem[:-10]
                # Legacy format: ``scan_{sessionId}_{ts}`` — strip the
                # ``scan_`` prefix.
                if stem.startswith("scan_"):
                    stem = stem[5:]
                if stem in seen:
                    continue
                seen.add(stem)
                ids.append(stem)

        # DB-backed scans (no corrected CSV on disk). Best-effort: a missing or
        # unreadable DB just leaves the CSV-derived list.
        db_path = getattr(self._interface, "scan_db_path", None)
        if db_path:
            try:
                from omotion.ScanDatabase import ScanDatabase
                db = ScanDatabase(db_path)
                try:
                    for session in db.iter_sessions():
                        label = (session.get("session_label") or "").strip()
                        if label and label not in seen:
                            seen.add(label)
                            ids.append(label)
                finally:
                    db.close()
            except Exception:
                logger.warning("get_scan_list: could not read scan DB sessions",
                               exc_info=True)

        def ts_key(s):
            # New / mid format starts with YYYYMMDD (8 digits)
            if re.match(r'^\d{8}_\d{6}', s):
                return s[:15]       # YYYYMMDD_HHMMSS
            # Legacy format: sessionId_YYYYMMDD_HHMMSS
            parts = s.split("_", 1)
            return parts[1] if len(parts) == 2 else s

        return sorted(ids, key=ts_key, reverse=True)

    @pyqtSlot(str, result=QVariant)
    def get_scan_details(self, scan_id: str, session_id: int = -1):
        """
        scan_id is either:
          New / mid format: 'YYYYMMDD_HHMMSS_userLabel'
          Legacy format:    'userLabel_YYYYMMDD_HHMMSS'

        session_id, when >= 0, is the unique DB session id: the DB-side
        fields (notes, row-count) are resolved by it instead of by
        label — a by-label lookup returns the newest session with that
        label, so duplicate labels silently read a different session's
        notes (issue #254). File resolution always keys off scan_id;
        the CSVs are named by label on disk.

        For each format we try to resolve the canonical CSV across
        all naming generations (#44):
          New:    {scan_id}.csv
          Mid:    {scan_id}_corrected.csv
          Legacy: scan_{scan_id}_corrected.csv
        And the raw histo CSVs across two generations:
          New:    {scan_id}_(left|right)_mask*_raw.csv
          Legacy: {scan_id}_(left|right)_mask*.csv
        """
        base = Path(self._data_root)

        # Detect format by checking if it starts with a date
        if re.match(r'^\d{8}_\d{6}_', scan_id):
            # New / mid: YYYYMMDD_HHMMSS_userLabel
            parts = scan_id.split("_", 2)
            ts = parts[0] + "_" + parts[1]
            subject = parts[2] if len(parts) > 2 else ""
            notes_path = base / f"{scan_id}_notes.txt"
            left      = (next(base.glob(f"{scan_id}_left_mask*_raw.csv"), None)
                         or next(base.glob(f"{scan_id}_left_mask*.csv"), None))
            right     = (next(base.glob(f"{scan_id}_right_mask*_raw.csv"), None)
                         or next(base.glob(f"{scan_id}_right_mask*.csv"), None))
            # Prefer new naming (no suffix); fall back to mid format
            # (_corrected). Filter out files that match the raw
            # mask pattern so they aren't picked up as the canonical
            # CSV.
            corrected = next(
                (p for p in base.glob(f"{scan_id}.csv") if p.is_file()),
                None,
            )
            if corrected is None:
                corrected = next(base.glob(f"{scan_id}_corrected.csv"), None)
        else:
            # Legacy: userLabel_YYYYMMDD_HHMMSS
            parts = scan_id.split("_", 1)
            subject = parts[0]
            ts = parts[1] if len(parts) > 1 else ""
            notes_path = base / f"scan_{scan_id}_notes.txt"
            left      = next(base.glob(f"scan_{scan_id}_left_mask*.csv"), None)
            right     = next(base.glob(f"scan_{scan_id}_right_mask*.csv"), None)
            corrected = next(base.glob(f"scan_{scan_id}_corrected.csv"), None)

        left_mask = ""
        right_mask = ""
        if left:
            m = re.search(r"_mask([0-9A-Fa-f]+)(?:_raw)?\.csv$", left.name)
            if m:
                left_mask = m.group(1)
        if right:
            m = re.search(r"_mask([0-9A-Fa-f]+)(?:_raw)?\.csv$", right.name)
            if m:
                right_mask = m.group(1)

        # Notes live in the scan DB (sessions.session_notes). Scans from
        # before the DB migration only have a *_notes.txt on disk — fall
        # back to that.
        notes = ""
        has_db_rows = False
        db_path = getattr(self._interface, "scan_db_path", None)
        if db_path:
            try:
                from omotion.ScanDatabase import ScanDatabase
                db = ScanDatabase(db_path)
                try:
                    if session_id >= 0:
                        session = db.get_session(int(session_id))
                    else:
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
            except OSError:
                # No legacy *_notes.txt fallback for this scan — expected
                # for anything recorded since notes moved into scans.db.
                logger.debug("no legacy notes file at %s", notes_path)

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

    @staticmethod
    def _friendly_ts(ts: str) -> str:
        """'YYYYMMDD_HHMMSS' -> 'YYYY-MM-DD HH:MM:SS'; pass through."""
        if not ts or len(ts) != 15:
            return ts or "-"
        return (f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} "
                f"{ts[9:11]}:{ts[11:13]}:{ts[13:15]}")

    def _session_to_row(self, s: dict) -> dict:
        """Flatten one ScanDatabase session dict into the QVariantMap the
        History table consumes. Masks/operator come from session_meta
        (written by ScanDBSink); duration from session_start/end."""
        label = (s.get("session_label") or "").strip()
        meta = s.get("session_meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        flags = meta.get("sdk_flags") or {}

        left_mask = flags.get("left_camera_mask", -1)
        right_mask = flags.get("right_camera_mask", -1)
        if left_mask is None:
            left_mask = -1
        if right_mask is None:
            right_mask = -1

        user_label = meta.get("subject_id") or ""
        timestamp = ""
        m = re.match(r"^(\d{8}_\d{6})_?(.*)$", label)
        if m:
            timestamp = m.group(1)
            if not user_label:
                user_label = m.group(2)

        start = s.get("session_start")
        end = s.get("session_end")
        if start is not None and end is not None:
            duration = float(end) - float(start)
        else:
            duration = -1.0

        return {
            "sessionId": int(s.get("id")),
            "label": label,
            "userLabel": user_label,
            "operator": meta.get("operator") or "",
            "timestamp": timestamp or label,
            "dateTime": self._friendly_ts(timestamp),
            "durationSec": duration,
            "leftMask": int(left_mask),
            "rightMask": int(right_mask),
            "configL": _config_name(left_mask),
            "configR": _config_name(right_mask),
            "clinicalMode": bool(flags.get("reduced_mode", False)),
            "notes": s.get("session_notes") or "",
            "interrupted": end is None,
        }

    @pyqtSlot(result="QVariantList")
    def get_scan_sessions(self):
        """Return one row per scan-DB session, newest first, for the
        History table. DB-only — does not list CSV-derived scans.
        Best-effort: a missing/unreadable DB yields []."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return []
        rows = []
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                for s in db.iter_sessions():
                    # Per-row guard: one malformed session must not blank the
                    # whole History view — skip it and keep the rest.
                    try:
                        rows.append(self._session_to_row(s))
                    except Exception:
                        logger.warning(
                            "get_scan_sessions: skipping malformed session %s",
                            s.get("id"), exc_info=True)
            finally:
                db.close()
        except Exception:
            logger.warning("get_scan_sessions: could not read scan DB",
                           exc_info=True)
            return []
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows

    @pyqtSlot(int, result="QVariantMap")
    def get_session_stats(self, session_id: int):
        """Lazily fetch heavier per-scan stats (row count) when a History
        row is focused, so the list query itself stays cheap."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return {"sampleCount": 0}
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                # Raw COUNT via the connection — ScanDatabase exposes no
                # public row-count API; get_scan_details reaches for
                # _connection() the same way for its EXISTS probe.
                row = next(
                    db._connection().execute(
                        "SELECT COUNT(*) FROM session_data"
                        " WHERE session_id = ?",
                        (int(session_id),),
                    ),
                    None,
                )
                return {"sampleCount": int(row[0]) if row else 0}
            finally:
                db.close()
        except Exception:
            logger.warning(
                "get_session_stats failed for %s", session_id,
                exc_info=True)
            return {"sampleCount": 0}

    @pyqtSlot("QVariantList", result=int)
    def deleteScans(self, session_ids):
        """Delete the given scan-DB sessions (CASCADE removes their
        session_data). Returns the count actually deleted. The engineering-
        password gate is enforced in QML before this is called."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            return 0
        deleted = 0
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                for sid in session_ids:
                    try:
                        if db.delete_session(int(sid)):
                            deleted += 1
                            logger.info(
                                "deleteScans: removed session %s", sid)
                    except Exception:
                        logger.warning(
                            "deleteScans: failed to delete %s", sid,
                            exc_info=True)
            finally:
                db.close()
        except Exception:
            logger.warning(
                "deleteScans: could not open scan DB", exc_info=True)
        # Tolerate non-int ids the same way the delete loop above does —
        # this comprehension runs outside AuditLog.log's fail-soft wrapper.
        _ids = []
        for s in session_ids:
            try:
                _ids.append(int(s))
            except (TypeError, ValueError):
                pass
        self._audit.log("scan_deleted", {
            "session_ids": _ids,
            "count": deleted,
        })
        return deleted

    # ── Audit log (QML-facing) ───────────────────────────────────────────
    @pyqtSlot(result="QVariantList")
    @pyqtSlot(int, result="QVariantList")
    def auditLogEntries(self, limit: int = 500):
        """Audit-log rows (newest first) for the Logs modal. Pure read —
        does not itself log, so refreshing never double-logs."""
        try:
            return self._audit.query(int(limit))
        except Exception:
            logger.warning("auditLogEntries failed", exc_info=True)
            return []

    @pyqtSlot()
    def recordAuditLogViewed(self):
        """Record that the audit log was opened. Called once from
        LogsModal.open()."""
        try:
            n = self._audit.count()
        except Exception:
            n = 0
        self._audit.log("audit_log_viewed", {"entry_count": n})

    @pyqtSlot(str, result=str)
    def exportAuditLogCsv(self, dest_path: str) -> str:
        """Export the full audit log to CSV. Accepts a plain path or a
        file:// URL. Records an ``audit_log_exported`` event. Returns the
        written path, or '' on failure."""
        if not dest_path:
            return ""
        path = dest_path.replace("file:///", "").replace("file://", "")
        try:
            n = self._audit.export_csv(path)
            self._audit.log(
                "audit_log_exported", {"dest": path, "row_count": n}
            )
            logger.info("exportAuditLogCsv: wrote %d rows -> %s", n, path)
            return path
        except Exception:
            logger.exception("exportAuditLogCsv failed for %r", path)
            self.errorOccurred.emit("Audit log export failed.")
            return ""

    @pyqtSlot()
    def prepareDebugLogBundle(self) -> None:
        """Zip the last 48h of app logs (+ config + system info) into
        data/debug-bundles/, reveal it in the file explorer, and toast
        the support address — on a worker thread. Zipping 48 h of logs
        is multi-second and used to freeze the GUI for the whole build.

        Fire-and-forget: Settings never used the old return value. The
        immediate toast gives feedback while the worker runs; the
        success toast (same tag) replaces it. notify/errorOccurred emit
        queued signals, so the worker path is safe."""
        self.notify("Preparing debug logs…", type_="info",
                    tag="debug-bundle")
        threading.Thread(
            target=self._prepare_debug_bundle_sync,
            name="debug-bundle", daemon=True,
        ).start()

    def _prepare_debug_bundle_sync(self) -> str:
        """Build + reveal + audit + toast the debug bundle. Returns the
        zip path, or '' on failure. Worker body of prepareDebugLogBundle;
        unit tests call it synchronously."""
        try:
            from debug_bundle import build_debug_bundle, WINDOW_HOURS
            try:
                from version import get_version as _gv
                app_version = _gv()
            except Exception:
                app_version = ""
            try:
                sdk_version = self._interface.get_sdk_version()
                sdk_version = (
                    sdk_version if isinstance(sdk_version, str) else ""
                )
            except Exception:
                sdk_version = ""
            dest_dir = os.path.join(self._data_root, "debug-bundles")
            meta = build_debug_bundle(
                self._directory,
                dest_dir,
                time.time(),
                config_path=resource_path("config", "app_config.json"),
                extra_info={
                    "app_version": app_version,
                    "sdk_version": sdk_version,
                },
            )
        except Exception:
            logger.exception("prepareDebugLogBundle: failed to build bundle")
            self.errorOccurred.emit("Could not create the debug log bundle.")
            self.dismissNotification("debug-bundle")
            return ""

        path = meta["path"]
        self._reveal_in_explorer(path)
        from audit_log import EV_DEBUG_BUNDLE_CREATED
        self._audit.log(EV_DEBUG_BUNDLE_CREATED, {
            "dest": path,
            "file_count": meta["file_count"],
            "log_count": meta["log_count"],
            "bytes": meta["bytes"],
            "window_hours": WINDOW_HOURS,
        })
        self.notify(
            "Debug logs saved to " + path
            + ". Please email this file to support@openwater.cc.",
            type_="success", duration_ms=0, dismissible=True,
            tag="debug-bundle",
        )
        return path

    def _reveal_in_explorer(self, path: str) -> None:
        """Best-effort: open the OS file browser with the file selected.
        Never raises — a failed reveal must not lose the bundle."""
        try:
            import subprocess
            if sys.platform.startswith("win"):
                subprocess.Popen(
                    ["explorer", "/select,", os.path.normpath(path)]
                )
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            logger.warning(
                "could not reveal %s in file explorer", path, exc_info=True
            )

    @pyqtProperty(str, notify=directoryChanged)
    def directory(self):
        return self._directory

    @directory.setter
    def directory(self, path):
        # Normalize incoming QML "file:///" path
        if path.startswith("file:///"):
            path = path[8:] if path[9] != ":" else path[8:]
        self._directory = path
        self._app_config["dataDirectory"] = path
        self._save_app_config()
        logger.debug(f"[Connector] Directory set to: {self._directory}")
        self.directoryChanged.emit()
        self.appConfigChanged.emit()

    @property
    def _data_root(self) -> str:
        """Where scan files, calibrations, debug-bundles, and
        the scan DB live: self._directory/data. A sibling of the app-wide
        logs/ folder (main.py's concern only — the connector never touches
        it)."""
        return os.path.join(self._directory, app_paths.DATA_DIRNAME)

    # ── App config — generic read/write API ──────────────────────────────────

    @pyqtProperty('QVariantMap', notify=appConfigChanged)
    def appConfig(self):
        return self._app_config

    def _save_app_config(self):
        """Persist runtime config changes as a diff vs the shipped baseline.

        Writes only changed keys to the writable app_config.local.json under
        %PROGRAMDATA%, never the read-only bundled config in Program Files.
        """
        config_store.save_overrides(self._app_config, self._baseline_config)

    @pyqtSlot(str, result=bool)
    def checkEngineeringPassword(self, pw: str) -> bool:
        """Return True if ``pw`` matches the engineering-mode password.

        Comparison lives in Python so the literal is not present in
        shipped QML source. QML calls this from the unlock modal and,
        on True, sets engineeringMode via setConfig.
        """
        ok = engineering_password_matches(pw)
        if not ok:
            logger.info("[Connector] Engineering unlock attempt failed")
        return ok

    @pyqtSlot(str, 'QVariant')
    def setConfig(self, key: str, value):
        """Update a single config key, persist to disk, and notify QML."""
        old = self._app_config.get(key)
        self._app_config[key] = value
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] Config set: {key} = {value!r}")
        if old != value:
            self._audit.log("settings_changed",
                            {"changes": {key: {"old": old, "new": value}}})
        if key == "downloadBetaFirmware" and old != value:
            self._refresh_firmware_update_check()

    @pyqtSlot('QVariantMap')
    def saveConfigs(self, configs: dict):
        """Update multiple config keys at once, persist to disk, and notify QML."""
        changes = {}
        for k, v in configs.items():
            old = self._app_config.get(k)
            if old != v:
                changes[k] = {"old": old, "new": v}
        self._app_config.update(configs)
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] Config saved: {sorted(configs.keys())}")
        if changes:
            self._audit.log("settings_changed", {"changes": changes})
        if "downloadBetaFirmware" in changes:
            self._refresh_firmware_update_check()

    # Sensor debug-flag config keys surfaced as live Settings → Engineering
    # toggles, mapped to the runtime cache attribute that
    # _compute_sensor_debug_flags() reads.
    _SENSOR_DEBUG_FLAG_ATTRS = {
        "histoCmp": "_histo_cmp",
        "sensorDebugLogging": "_sensor_debug_logging",
    }

    @pyqtSlot(str, bool)
    def setSensorDebugFlag(self, key: str, enabled: bool) -> None:
        """Toggle a sensor debug-flag config key, persist it, and re-push the
        recomputed debug-flag bitmask to every connected sensor immediately.

        Mirrors the live ``setConsoleFan`` engineering toggle — no app restart
        or reconnect needed. Unknown keys are ignored. When no sensor is
        connected the value is still persisted and applies on the next
        connect via ``_run_sensor_init``.
        """
        attr = self._SENSOR_DEBUG_FLAG_ATTRS.get(key)
        if attr is None:
            logger.warning("setSensorDebugFlag: unknown key %r", key)
            return
        enabled = bool(enabled)
        old = self._app_config.get(key)
        setattr(self, attr, enabled)
        self._app_config[key] = enabled
        self._save_app_config()
        self.appConfigChanged.emit()
        if old != enabled:
            self._audit.log("settings_changed",
                            {"changes": {key: {"old": old, "new": enabled}}})
        self._apply_sensor_debug_flags()

    @pyqtSlot(bool)
    def setWriteRawCsv(self, enabled: bool) -> None:
        """Update writeRawCsv in both the runtime cache and persisted config."""
        self._write_raw_csv = bool(enabled)
        self._app_config["writeRawCsv"] = self._write_raw_csv
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] writeRawCsv set to {self._write_raw_csv}")

    @pyqtSlot('QVariant')
    def setRawCsvDurationSec(self, value) -> None:
        """Update rawCsvDurationSec in both the runtime cache and persisted config.

        Pass ``None`` / ``null`` / empty string to disable the limit (full scan duration).
        """
        if value is None or str(value).strip() in ("", "null", "undefined"):
            self._raw_csv_duration_sec = None
        else:
            try:
                self._raw_csv_duration_sec = float(value)
            except (TypeError, ValueError):
                self._raw_csv_duration_sec = None
        self._app_config["rawCsvDurationSec"] = self._raw_csv_duration_sec
        self._save_app_config()
        self.appConfigChanged.emit()
        logger.debug(f"[Connector] rawCsvDurationSec set to {self._raw_csv_duration_sec}")

    @pyqtProperty(str, notify=scanNotesChanged)  # <-- add notify
    def scanNotes(self):
        return self._scan_notes

    @scanNotes.setter
    def scanNotes(self, value: str):
        value = value or ""
        if value != self._scan_notes:
            self._scan_notes = value
            self.scanNotesChanged.emit()
        # Always persist when a scan's DB session exists, even if the
        # in-memory value didn't change (covers the first save after capture).
        if self._scan_notes_session_label:
            self._persist_scan_notes(self._scan_notes_session_label)

    def _persist_scan_notes(self, session_label: str) -> bool:
        """Write the in-memory notes buffer to sessions.session_notes of the
        scan DB session whose session_label matches. Returns True on success.
        ScanDBSink (critical) creates the session at scan start, so every scan
        that ran has a row to update."""
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path or not session_label:
            return False
        try:
            from omotion.ScanDatabase import ScanDatabase
            db = ScanDatabase(db_path)
            try:
                session = db.get_session_by_label(session_label)
                if session is None:
                    logger.error(
                        "Scan notes: no DB session with label %r", session_label
                    )
                    return False
                db.update_session(session["id"],
                                  session_notes=self._scan_notes.strip())
                logger.info("Scan notes saved to DB session %r",
                            session_label)
                return True
            finally:
                db.close()
        except Exception:
            logger.exception(
                "Failed to save scan notes to DB (label %r)", session_label
            )
            return False

    @pyqtProperty(QObject, notify=currentScanSourceChanged)
    def currentScanSource(self) -> ScanDataSource | None:
        """Current ScanDataSource bound to the PlotViewer. Usually the
        live source during a scan; can be a PastScanSource while the
        user is reviewing history via loadPastScan."""
        return self._current_scan_source

    @pyqtProperty(bool, notify=liveSourceAvailableChanged)
    def liveSourceAvailable(self) -> bool:
        """True when a LiveScanSource is held (i.e. at least one scan
        has run this session). PlotToolbar uses this to decide whether
        the "Back to live" button should switch sources back to live."""
        return self._live_scan_source is not None

    def _retire_scan_source(self, source: ScanDataSource | None) -> None:
        """Tear down a source QML can no longer reach: stop its flush
        timer, close any scan-DB handle, and queue C++ deletion. Sources
        are parented to the connector, so without this every superseded
        source (one per scan / History view) stays alive for the app's
        lifetime — up to ~46 MB of buffers plus a running 10 Hz timer
        each. deleteLater (not a direct delete) so any in-flight QML
        paint against the old source finishes before the object dies."""
        if source is None:
            return
        try:
            source.release()
        except Exception:
            logger.warning("scan source release failed", exc_info=True)
        source.deleteLater()

    def _set_current_scan_source(self, source: ScanDataSource | None) -> None:
        """Replace the active source. Dedupes identical-instance assignments
        so the notify signal only fires on real transitions. The previous
        source is retired unless it is the held live source, which must
        survive History navigation for "Back to live"."""
        if source is self._current_scan_source:
            return
        prev = self._current_scan_source
        self._current_scan_source = source
        self.currentScanSourceChanged.emit()
        if prev is not None and prev is not self._live_scan_source:
            self._retire_scan_source(prev)

    @pyqtSlot()
    def showLiveSource(self) -> None:
        """Switch the viewer back to the held live source, if any.
        No-op when no LiveScanSource has been created this session."""
        if self._live_scan_source is None:
            return
        self._set_current_scan_source(self._live_scan_source)
        logger.info("[Plot] viewer switched back to live source")

    @pyqtSlot(int, str)
    def loadPastScan(self, session_id: int, session_label: str) -> None:
        """Open the saved scan with the given unique DB session id and
        display it in the PlotViewer. The held live source is left
        intact so a subsequent showLiveSource() can return to it.

        session_label (the YYYYMMDD_HHMMSS_userLabel string used
        elsewhere in the UI) still names the scan's CSVs on disk and the
        log/signal payloads, but the DB session is resolved by id — a
        by-label lookup returns the newest session with that label, so
        duplicate labels silently loaded a different scan than the one
        selected in History (issue #254, same fix as the exports).

        Asynchronous load (issue #152): the bulk SQLite walk / CSV parse
        is multi-second for long scans (a ~70-min scan measured ~8.3M
        samples ≈ 20 s) and used to freeze the GUI thread. The heavy
        load now runs on a daemon worker thread; the finished buffers
        are marshalled back to the GUI thread via _pastScanBuffersReady,
        where the PastScanSource QObject is constructed and bound to the
        viewer. pastScanLoadFinished(label, ok) fires on completion
        either way so the History modal can clear its busy state."""
        if session_id < 0 or not session_label:
            logger.warning(
                "loadPastScan: invalid session_id %r / label %r",
                session_id, session_label,
            )
            self.pastScanLoadFinished.emit(session_label or "", False)
            return
        db_path = getattr(self._interface, "scan_db_path", None)
        if not db_path:
            logger.warning(
                "loadPastScan: scan_db_path unavailable on interface"
            )
            self.pastScanLoadFinished.emit(session_label, False)
            return
        self._audit.log("scan_viewed", {"label": session_label,
                                        "session_id": int(session_id)})
        # Per-cam corrected CSV ({scan_id}.csv, 82-col wide format)
        # is the only source of per-cam BFI/BVI/mean/contrast for
        # past replay — the DB's session_data only holds side-
        # aggregated corrected-frame placeholders. CsvSink writes
        # this CSV unconditionally for every scan, so it's reliably
        # available. Resolving the path is a cheap directory glob —
        # fine on the GUI thread (the modal already does it per
        # selection); the heavy parse happens on the worker.
        details = self.get_scan_details(
            session_label, session_id=int(session_id)) or {}
        corrected_csv = details.get("correctedPath") or None
        self._past_scan_load_seq += 1
        seq = self._past_scan_load_seq
        threading.Thread(
            target=self._load_past_scan_worker,
            args=(seq, int(session_id), session_label, str(db_path),
                  corrected_csv),
            name=f"past-scan-load-{seq}",
            daemon=True,
        ).start()

    def _load_past_scan_worker(
        self,
        seq: int,
        session_id: int,
        session_label: str,
        db_path: str,
        corrected_csv: Optional[str],
    ) -> None:
        """Worker thread: bulk-load a past scan's samples (the heavy,
        Qt-free part of loadPastScan). Opens its own ScanDatabase handle;
        results cross back to the GUI thread via _pastScanBuffersReady
        (cross-thread signal → queued delivery)."""
        try:
            from omotion.ScanDatabase import ScanDatabase
            from data_sources import load_past_scan_buffers
            db = ScanDatabase(db_path)
            try:
                session = db.get_session(session_id)
                if not session:
                    logger.warning(
                        "loadPastScan: no session found for id %d "
                        "(label %r)",
                        session_id, session_label,
                    )
                    self._pastScanBuffersReady.emit(
                        seq, session_label, -1, None, "", None, None
                    )
                    return
                # The camera config the scan was RUN with lives in
                # session_meta.sdk_flags (written by ScanDBSink). Thread it to
                # the GUI thread so PastScanSource lays its grid out from the
                # recorded config, not just the cameras that logged data
                # (issue #175 reopen). Same parse the History list uses.
                recorded_flags = _sdk_flags_from_session(session)
                # The viewer's "Viewing" badge (issue #245) names the scan with
                # the user label + date/time. Derive them from the SAME row the
                # History list renders (_session_to_row prefers subject_id over
                # the label suffix) so the badge can't disagree with the row the
                # user clicked. Best-effort: a parse failure just hides the
                # badge — it must never block the load.
                display_meta = None
                try:
                    row = self._session_to_row(session)
                    display_meta = {
                        "userLabel": row["userLabel"],
                        "dateTime": row["dateTime"],
                    }
                except Exception:
                    logger.warning(
                        "loadPastScan: could not derive badge meta for %r",
                        session_label, exc_info=True,
                    )
                buffers, _ = load_past_scan_buffers(
                    db, session_id, corrected_csv
                )
            finally:
                db.close()
            # Detect which source actually populated per-cam BFI: if
            # any (side, cam_id != -1, bfi) buffer exists, the DB was
            # sufficient and the CSV fallback was skipped.
            db_had_per_cam = any(k[1] >= 0 and k[2] == "bfi" for k in buffers)
            source_tag = (
                "db" if db_had_per_cam
                else ("csv" if corrected_csv else "db-only-sentinel")
            )
            self._pastScanBuffersReady.emit(
                seq, session_label, session_id, buffers, source_tag,
                recorded_flags, display_meta,
            )
        except Exception:
            logger.exception("loadPastScan failed for label %r", session_label)
            self._pastScanBuffersReady.emit(
                seq, session_label, -1, None, "", None, None
            )

    @pyqtSlot(str, str)
    def _on_scan_outcome_warning(self, message: str, severity: str) -> None:
        """GUI thread: surface an interrupted/empty-scan toast. Errors are
        sticky (duration_ms=0) so a 'not saved' scan can't be missed."""
        duration = 0 if severity == "error" else 8000
        self.notify(message, severity, duration_ms=duration, tag="scan-outcome")

    def _on_past_scan_buffers_ready(
        self,
        seq: int,
        session_label: str,
        session_id: int,
        buffers,
        source_tag: str,
        recorded_flags=None,
        display_meta=None,
    ) -> None:
        """GUI thread: bind a worker-loaded past scan to the viewer.
        Results from a superseded request (user clicked another scan
        while this one was loading) are dropped."""
        if seq != self._past_scan_load_seq:
            logger.info(
                "loadPastScan: dropping stale result for %r "
                "(seq %d, latest %d)",
                session_label, seq, self._past_scan_load_seq,
            )
            return
        if buffers is None:
            self.errorOccurred.emit(
                f"Could not load scan '{session_label}' for plotting.\n"
                "See the app log for details."
            )
            self.pastScanLoadFinished.emit(session_label, False)
            return
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
        try:
            dm = display_meta or {}
            past = PastScanSource(
                scan_db=None,
                session_id=session_id,
                parent=self,
                preloaded_buffers=buffers,
                recorded_flags=recorded_flags,
                user_label=dm.get("userLabel", ""),
                date_time=dm.get("dateTime", ""),
            )
            self._set_current_scan_source(past)
            # Diagnostic — left in for now so we can spot regressions
            # in past-scan loading. Cheap (one log line per click).
            n_buffers = len(past.buffers)
            n_samples = sum(b.n for b in past.buffers.values())
            sample_keys = sorted(past.buffers.keys())[:8]
            # Grid masks are what the viewer actually lays out from; log them
            # next to the data shape so a config/data mismatch (issue #175) is
            # diagnosable from one line. flags=no means an older scan with no
            # stored sdk_flags (masks derived from data instead). -1 prints as
            # "unknown" (viewer falls back to the live selection).
            def _mstr(v):
                return f"0x{v & 0xFF:02X}" if v >= 0 else "unknown"
            logger.info(
                "[Plot] loaded past scan %r (session_id=%d) source=%s: "
                "buffers=%d samples=%d liveEdge=%.3f gridMasks=L%s/R%s "
                "clinical=%d flags=%s sample_keys=%s",
                session_label, session_id, source_tag,
                n_buffers, n_samples, past.liveEdge,
                _mstr(past.leftMask), _mstr(past.rightMask),
                past.clinicalMode,
                "yes" if recorded_flags else "no",
                sample_keys,
            )
            self.pastScanLoadFinished.emit(session_label, True)
        except Exception:
            logger.exception("loadPastScan failed for label %r", session_label)
            self.pastScanLoadFinished.emit(session_label, False)

    @pyqtSlot(int, str)
    def exportScanCsv(self, session_id: int, output_path: str) -> None:
        """Export a scan's session_data to a corrected-format CSV, on a
        worker thread. Called from the History modal's "Export CSV"
        button after the user picks a save path; the result arrives via
        scanCsvExportFinished(ok, path) — materializing a long scan is
        multi-second and used to freeze the GUI for the duration.
        Resolves the scan by its unique DB id — a by-label lookup can
        silently pick a different session when labels collide (#254)."""
        if session_id < 0 or not output_path:
            self.scanCsvExportFinished.emit(False, output_path or "")
            return
        threading.Thread(
            target=self._export_scan_csv_worker,
            args=(int(session_id), output_path),
            name="scan-csv-export", daemon=True,
        ).start()

    def _export_scan_csv_worker(self, session_id: int,
                                output_path: str) -> None:
        ok = self._export_scan_csv_sync(session_id, output_path)
        self.scanCsvExportFinished.emit(ok, output_path)

    def _export_scan_csv_sync(self, session_id: int,
                              output_path: str) -> bool:
        """Worker body of exportScanCsv; unit tests call it directly."""
        try:
            from omotion.ScanDatabase import ScanDatabase
            from omotion.SessionPlayback import materialize_corrected_csv

            db_path = getattr(self._interface, "scan_db_path", None)
            if not db_path:
                self.errorOccurred.emit("No scan database available.")
                return False
            with ScanDatabase(db_path) as db:
                session = db.get_session(int(session_id))
            if not session:
                self.errorOccurred.emit(
                    f"No database session found for id {session_id}."
                )
                return False
            materialize_corrected_csv(
                str(db_path), int(session_id), output_path,
                include_quality=True,
            )
            logger.info(
                "exportScanCsv: exported %r (sid=%d) → %s",
                session.get("session_label"), session_id, output_path,
            )
            return True
        except Exception as exc:
            logger.exception("exportScanCsv failed for sid %s", session_id)
            self.errorOccurred.emit(f"Export failed:\n{exc}")
            return False

    @pyqtSlot('QVariantList', str)
    def exportScansToFolder(self, session_ids, folder) -> None:
        """Export several scans, one CSV each, into ``folder`` — on a
        worker thread. Completion arrives via
        scansExportFinished(exported, skipped, folder); a big batch used
        to freeze the GUI for its whole materialize loop. Takes each
        label from the id-resolved DB session (ids are unique; labels
        can collide — #254). (Callers pre-filter interrupted scans,
        which can't be materialized.)"""
        session_ids = [int(x) for x in (session_ids or [])]
        if not session_ids or not folder:
            self.scansExportFinished.emit(0, 0, str(folder or ""))
            return
        self.notify(f"Exporting {len(session_ids)} scan(s)…", type_="info",
                    tag="scan-export")
        threading.Thread(
            target=self._export_scans_worker, args=(session_ids, str(folder)),
            name="scan-batch-export", daemon=True,
        ).start()

    def _export_scans_worker(self, session_ids, folder) -> None:
        result = self._export_scans_sync(session_ids, folder)
        self.scansExportFinished.emit(
            result["exported"], result["skipped"], folder)

    def _export_scans_sync(self, session_ids, folder) -> dict:
        """Worker body of exportScansToFolder. Writes
        ``<folder>/<label>_export.csv`` for every session id; opens the
        scan DB once for the whole batch. Missing or failing scans are
        counted as skipped and never raise. Returns
        ``{"exported": int, "skipped": int}``. Unit tests call it
        directly."""
        result = {"exported": 0, "skipped": 0}
        if not session_ids or not folder:
            return result
        try:
            from omotion.ScanDatabase import ScanDatabase
            from omotion.SessionPlayback import materialize_corrected_csv

            db_path = getattr(self._interface, "scan_db_path", None)
            if not db_path:
                self.errorOccurred.emit("No scan database available.")
                return result
            with ScanDatabase(db_path) as db:
                for sid in session_ids:
                    try:
                        sid = int(sid)
                        session = db.get_session(sid)
                        if not session:
                            result["skipped"] += 1
                            continue
                        label = session.get("session_label") or f"scan_{sid}"
                        out_path = os.path.join(
                            folder, f"{label}_export.csv")
                        materialize_corrected_csv(
                            str(db_path), sid, out_path,
                            include_quality=True,
                        )
                        result["exported"] += 1
                        logger.info(
                            "exportScansToFolder: exported %r (sid=%d) -> %s",
                            label, sid, out_path,
                        )
                    except Exception:
                        logger.exception(
                            "exportScansToFolder: failed for %r", sid)
                        result["skipped"] += 1
            return result
        except Exception as exc:
            logger.exception("exportScansToFolder failed")
            self.errorOccurred.emit(f"Export failed:\n{exc}")
            return result

    @pyqtSlot(str, result=int)
    @pyqtSlot(str, str, result=int)
    @pyqtSlot(str, str, int, result=int)
    @pyqtSlot(str, str, int, bool, result=int)
    @pyqtSlot(str, str, int, bool, str, result=int)
    def notify(self, text: str, type_: str = "info", duration_ms: int = 4000,
               dismissible: bool = True, tag: str = "") -> int:
        """Fire a toast notification. Reachable from QML as MotionInterface.notify(...)
        and from any Python code holding the connector instance.

        Args:
            text: message shown in the toast
            type_: one of "info", "success", "warning", "error"
            duration_ms: auto-dismiss after N ms; 0 = sticky until user dismisses
            dismissible: whether to show the ✕ close button
            tag: optional stable identifier. If non-empty, calling notify with the
                same tag again replaces the existing toast (no duplicate stacking),
                and dismissNotification(tag) can later target it.

        Returns:
            The integer id assigned to this notification. Pass to
            dismissNotification(id) to dismiss it later.
        """
        if type_ not in ("info", "success", "warning", "error"):
            logger.warning(f"notify: unknown type '{type_}', falling back to 'info'")
            type_ = "info"
        nid = next(self._notification_ids)
        self.notificationRequested.emit({
            "id": nid,
            "tag": str(tag),
            "text": text,
            "type": type_,
            "durationMs": int(duration_ms),
            "dismissible": bool(dismissible),
        })
        return nid

    @pyqtSlot(int)
    @pyqtSlot(str)
    def dismissNotification(self, value):
        """Dismiss a single notification by id (int) or tag (str). Animated.

        Safe to call with an id/tag that no longer exists — it's a no-op.
        """
        if isinstance(value, bool):
            # bool is a subclass of int in Python; reject explicitly to avoid
            # surprising callers who pass True/False expecting different semantics.
            logger.warning("dismissNotification: bool is not a valid id/tag")
            return
        if isinstance(value, int):
            self.notificationDismissByIdRequested.emit(value)
        else:
            self.notificationDismissByTagRequested.emit(str(value))

    @pyqtSlot()
    def dismissAllNotifications(self):
        """Dismiss every active toast. Animated."""
        self.notificationDismissAllRequested.emit()

    def generate_user_label(self) -> str:
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"ow{suffix}"

    # --- CONSOLE COMMUNICATION METHODS ---
    @pyqtSlot()
    def queryConsoleInfo(self):
        """Fetch and emit device information."""
        try:
            fw_version = self._interface.console.get_version()
            logger.info(f"Version: {fw_version}")
            hw_id = self._interface.console.get_hardware_id()
            device_id = base58.b58encode(bytes.fromhex(hw_id)).decode()
            self.consoleDeviceInfoReceived.emit(fw_version, device_id)
            logger.info(
                f"Console Device Info - Firmware: {fw_version}, Device ID: {device_id}"
            )
        except Exception as e:
            logger.error(f"Error querying device info: {e}")

    @pyqtSlot(str, int, int, int, bool, result=bool)
    def startCapture(
        self,
        subject_id: str,
        duration_sec: int,
        left_camera_mask: int,
        right_camera_mask: int,
        disable_laser: bool,
    ) -> bool:
        """Start capture asynchronously; returns True if kicked off."""
        logger.info(
            f"startCapture(subject_id={subject_id}, dur={duration_sec}s, "
            f"left_mask=0x{left_camera_mask:02X}, right_mask=0x{right_camera_mask:02X}, "
            f"disable_laser={disable_laser})"
        )

        if duration_sec <= 0:
            logger.warning("duration_sec was %s, clamping to 3600", duration_sec)
            duration_sec = 3600

        if self._capture_running or self._capture_thread is not None:
            self.captureLog.emit("Capture already running.")
            return False

        if self._safetyFailure:
            self.captureLog.emit(
                "Scan cannot start: laser safety system is tripped. Clear the safety interlock first."
            )
            return False

        err = self._ensure_idle()
        if err is not None:
            logger.warning("startCapture refused: %s", err)
            self.captureLog.emit(err)
            return False

        try:
            os.makedirs(self._data_root, exist_ok=True)
        except Exception as e:
            self.captureLog.emit(f"Failed to create data dir: {e}")
            return False

        self._capture_stop = threading.Event()
        # Each new scan starts with a fresh notes buffer
        self._scan_notes = ""
        self._scan_notes_session_label = ""
        self.scanNotesChanged.emit()
        self._capture_running = True
        self._capture_start_time = time.time()
        self._audit.log("scan_started", {
            "label": subject_id,
            "left_mask": int(left_camera_mask),
            "right_mask": int(right_camera_mask),
        })
        # Per-scan monotonic zero for plot timestamps. sample.timestamp_s comes
        # from each sensor's firmware clock, which resets on sensor reboot — so
        # after a mid-scan unplug/replug, the two sides' clocks diverge and the
        # QML plot's shared `latestTimestamp` prunes the lagging side to empty.
        plot_t0 = time.monotonic()
        self._plot_t0 = plot_t0  # scan-start anchor (NOT the sample t axis —
        # samples carry SDK-normalized timestamps, t=0 at the first frame,
        # which arrives seconds after this line; see issue #284)
        # Real-time plot viewer: construct a fresh LiveScanSource for this scan
        # and install it on the connector. Phase 1 has no QML consumer yet —
        # the sinks (added in subsequent tasks) accumulate samples here in
        # parallel with the legacy Qt signal emissions.
        # Pass the scan DB path so the live source can lazily load older
        # samples from the DB tail when the user pans before the in-memory
        # ring-trim window (>30 min into a long scan). None-safe: if the
        # interface has no scan_db_path the source stays in-memory-only.
        # In-memory cache size before ring-trim → DB tail. Default 30 min
        # (1800 s × captureRateHz). Shrink liveCacheMaxSeconds in app_config
        # to exercise the DB lazy-load quickly (e.g. 60 → eviction after 1 min).
        _live_cache_sec = self._app_config.get("liveCacheMaxSeconds", 1800)
        _capture_hz = float(self._app_config.get("captureRateHz", 40))
        _live_cache_samples = max(2, int(float(_live_cache_sec) * _capture_hz))
        live_source = LiveScanSource(
            plot_t0=plot_t0,
            parent=self,
            scan_db_path=getattr(self._interface, "scan_db_path", None),
            cache_max_samples=_live_cache_samples,
        )
        # Track the live source separately so the user can navigate
        # to a past scan and return; emit so QML rebinds the
        # PlotToolbar's "Back to live" visibility. The previous scan's
        # live source is retired below — release() is idempotent, so it
        # doesn't matter if _set_current_scan_source already retired it
        # as the outgoing current source.
        prev_live = self._live_scan_source
        self._live_scan_source = live_source
        if prev_live is None:
            self.liveSourceAvailableChanged.emit()
        self._set_current_scan_source(live_source)
        self._retire_scan_source(prev_live)
        self._capture_left_path = ""
        self._capture_right_path = ""

        # Camera dropout watchdog state — fresh per scan.
        self._camera_last_seen = {}
        self._camera_last_temp = {}
        self._camera_dropped = set()
        self._scan_stall_abort_fired = False
        self._dropout_timer.start()

        # NaN-gap tracker — fresh per scan (same lifecycle as the watchdog).
        # Bound to a local so the completion closure and the sink provably
        # share THIS scan's tracker even if the attribute is reset elsewhere.
        nan_gap_tracker = NanGapTracker()
        self._nan_gap_tracker = nan_gap_tracker

        # Interrupted-scan outcome tracker — bound to a local so the
        # completion closure and the sink list provably share THIS scan's
        # instance (same pattern as nan_gap_tracker above).
        outcome_sink = _ScanOutcomeSink()

        # Reset the trigger-ON clock so _scan_elapsed_str starts from zero.
        self._trigger_clock_reset()

        # _CompletionSink calls this from its on_complete() method once the
        # ScanRunner finishes.
        def _on_pipeline_complete(meta):
            """Fires from _CompletionSink.on_complete() at the end of the scan."""
            # Determine whether the user requested a stop (cancellation).
            canceled = self._capture_stop.is_set()

            if canceled:
                self.captureLog.emit("Scan stopped.")
            else:
                self.captureLog.emit("Capture session complete.")

            # Trigger-ON duration sourced from _TriggerStateSink so notes
            # report the actual laser-on time, not wall-clock (which includes
            # pre-scan setup + post-scan USB drain). Falls back to wall-clock
            # if the sink never saw a TriggerStateEvent (e.g. cancel before
            # trigger fired).
            if self._trigger_on_mono is not None:
                # No OFF event and no disconnect-close reached us before
                # completion — close here as a last resort, knowing the
                # measurement now includes scan-teardown time.
                logger.warning(
                    "Scan completed with the trigger clock still open "
                    "(no TriggerStateEvent OFF received); duration may "
                    "include teardown time."
                )
                self._trigger_clock_close()
            trigger_elapsed = self._trigger_cumulative_s
            if trigger_elapsed > 0:
                elapsed = trigger_elapsed
            else:
                elapsed = time.time() - self._capture_start_time
            hours = int(elapsed // 3600)
            minutes = int((elapsed % 3600) // 60)
            seconds = int(elapsed % 60)
            duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            status = "stopped" if canceled else "completed"
            logger.info(
                "=== Full scan ended: %s — duration %s ===", status, duration_str
            )
            duration_line = f"\n---\nScan {status} — duration: {duration_str}"
            self._scan_notes = (self._scan_notes.strip() + duration_line)
            # Data-gap footer: aggregate ranges where any camera went >1 s
            # without a finite BFI/BVI sample (sustained NaN-fill/dropout).
            # Empty string when the scan was clean. Fail-soft like the rest
            # of this handler — a tracker bug must never block notes
            # persistence.
            try:
                gap_line = gap_note_line(nan_gap_tracker)
                if gap_line:
                    logger.warning("Scan data gaps detected: %s", gap_line.strip())
                    self._scan_notes += gap_line
            except Exception:
                logger.exception("NaN-gap footer computation failed")
            self.scanNotesChanged.emit()

            # Persist notes to the scan DB session ScanDBSink created at
            # scan start (label = scan_id_subject_id). Later edits via the
            # Notes modal update the same row through the scanNotes setter.
            scan_id = getattr(meta, "scan_id", "") if meta else ""
            session_label = f"{scan_id}_{subject_id}" if scan_id else ""
            self._scan_notes_session_label = session_label
            self._persist_scan_notes(session_label)

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

            # New pipeline writes CSVs directly; no .raw→.csv post-processing
            # needed.  Pass empty paths to captureFinished so startPostProcess
            # is a no-op (QML still proceeds to the next scan step).
            self._capture_left_path = ""
            self._capture_right_path = ""
            self._capture_running = False
            self._safety_cancel_scheduled = False
            self._capture_thread = None
            try:
                _outcome_kind = outcome.kind
            except Exception:
                _outcome_kind = None
            try:
                _dur = (time.time() - self._capture_start_time
                        if self._capture_start_time else None)
            except Exception:
                _dur = None
            self._audit.log("scan_ended", {
                "label": subject_id,
                "session_label": session_label or None,
                "duration_s": round(_dur, 1) if _dur is not None else None,
                "outcome": _outcome_kind,
            })
            self.captureFinished.emit(True, "", "", "")
            self.scanNotesReady.emit()

        # Issue #43: telemetry and raw CSVs are engineering diagnostics —
        # clinical users must not get them. Default False (fail closed).
        engineering_mode = self._app_config.get("engineeringMode", False)

        req = ScanRequest(
            subject_id=subject_id,
            duration_sec=duration_sec,
            left_camera_mask=left_camera_mask,
            right_camera_mask=right_camera_mask,
            disable_laser=disable_laser,
            reduced_mode=self._app_config.get("clinicalMode", False),
            # Corrected CSV is opt-in now that per-cam BFI/BVI lands in
            # the scan DB (the new viewer + past replay read from there).
            # Default False to skip the redundant {scan_id}.csv; flip
            # writeCorrectedCsv:true in app_config to keep exporting it.
            # The SDK still forces it on if no DB is configured.
            write_corrected_csv=self._app_config.get("writeCorrectedCsv", False),
            # Issue #43 (regression): the SDK defaults write_telemetry_csv
            # to True, so the per-scan {scan_id}_{subject}_telemetry.csv
            # must be explicitly gated on engineeringMode here. The original
            # gate was dropped in the sink-refactor follow-up (93c2feb).
            write_telemetry_csv=engineering_mode,
            # Raw CSV duration forwarded to the pipeline's Tee("raw") gate
            # via raw_save_max_duration_s. None means unbounded (write entire
            # scan); 0 omits raw tee entirely. The writeRawCsv toggle lives
            # in the engineering-only Settings card, so its persisted value is
            # additionally gated on engineeringMode (#43) — flipping engineering
            # mode off must stop raw output even if the toggle was left on.
            raw_save_max_duration_s=(
                self._raw_csv_duration_sec
                if (engineering_mode and self._write_raw_csv) else 0
            ),
            sinks=[
                _LivePlotSink(connector=self, plot_t0=plot_t0, live_source=live_source,
                              nan_gap_tracker=nan_gap_tracker),
                _TriggerStateSink(connector=self),
                outcome_sink,
                _CompletionSink(connector=self, on_complete_cb=_on_pipeline_complete),
            ],
            # Async backstop (#213): if the worker aborts after start_scan
            # returned True (e.g. a critical sink dies because the scan DB
            # became unwritable past the preflight), it can't ride the return
            # value. Fires on the worker thread, so just hop to the main thread
            # via a queued signal — see _on_scan_worker_failed.
            on_error=lambda exc: self.scanWorkerFailedDuringCapture.emit(str(exc)),
        )

        started = self._interface.start_scan(req)
        if started:
            logger.info(
                "=== Full scan started: subject=%s duration=%ss "
                "left=0x%02X right=0x%02X laser=%s ===",
                subject_id, duration_sec, left_camera_mask, right_camera_mask,
                "off" if disable_laser else "on",
            )
            # Bind the live source's DB tail to THIS scan's session row by its
            # exact label (set synchronously inside start_scan), so a later
            # pan-into-past resolves the right session instead of guessing the
            # newest one.
            label = getattr(self._scan_workflow, "current_scan_label", None)
            if label:
                live_source.set_scan_label(label)
        if not started:
            self._capture_running = False
            # Unbind the viewer. The orphaned LiveScanSource itself stays
            # held as _live_scan_source (it is NOT freed by dropping the
            # current-source reference — it's parented to the connector)
            # until the next startCapture retires it.
            self._set_current_scan_source(None)
            # start_scan refuses for two reasons: a prior worker still alive,
            # or a pre-flight failure (e.g. the scan DB couldn't be opened, in
            # which case the scan is aborted before the laser fires so its data
            # isn't silently lost). last_scan_error carries the specific reason
            # when it's the latter.
            reason = getattr(self._scan_workflow, "last_scan_error", None)
            # Log at WARNING so this is visible in the run log file —
            # captureLog signal goes through QML console.log which is
            # filtered out by default.
            if reason:
                logger.warning("startCapture aborted: %s", reason)
                self.captureLog.emit(f"Scan aborted: {reason}")
                self.errorOccurred.emit(reason)
                self._raise_critical("E-301", detail=reason)
            else:
                logger.warning(
                    "startCapture aborted: SDK refused to spawn a new scan "
                    "(usually a previous worker thread that didn't exit cleanly)."
                )
                self.captureLog.emit("Capture already running.")
                self._raise_critical("E-302")
        return bool(started)

    def _on_safety_trip_during_capture(self):
        """Called on main thread when safety tripped while scan was running: show message and cancel scan in 5 s."""
        if not self._capture_running or self._safety_cancel_scheduled:
            return
        self._safety_cancel_scheduled = True
        self.captureLog.emit(
            "Laser safety system tripped. Scan will be cancelled in 5 seconds."
        )
        self._raise_critical("E-202")
        QTimer.singleShot(5000, self.stopCapture)

    @pyqtSlot(str)
    def _on_scan_worker_failed(self, detail: str):
        """Main-thread handler for an async scan-worker abort (#213).

        The SDK worker invokes ScanRequest.on_error after start_scan already
        returned True — typically a critical sink (ScanDBSink) dying because
        the scan DB became unwritable in the race window past the synchronous
        preflight. Surface E-301 and tear the capture down so the app doesn't
        sit 'capturing' forever with a dead worker.
        """
        if not self._capture_running:
            return  # already torn down (e.g. user cancelled first)
        logger.warning("scan worker aborted mid-capture: %s", detail)
        self.captureLog.emit(f"Scan aborted: {detail}")
        self._raise_critical("E-301", detail=detail)
        # Tear down: stop the dropout watchdog and cancel the (dead) scan, then
        # clear running state and unblock QML, mirroring the completion path.
        self.stopCapture()
        self._capture_running = False
        self._safety_cancel_scheduled = False
        self._set_current_scan_source(None)
        self.captureFinished.emit(False, detail, "", "")

    @pyqtSlot(result=QVariant)
    def tec_status(self, snap=None):
        if snap is None:
            snap = self._interface.console.telemetry.get_snapshot()
        if snap is None or not snap.read_ok:
            return False

        from omotion.console_telemetry_conversions import (
            tec_thermistor_voltage_to_celsius,
            tec_current_to_amps,
            tec_voltage_to_volts,
        )

        self._tec_voltage = round(
            tec_thermistor_voltage_to_celsius(snap.tec_v_raw), 2
        )
        self._tec_temp = round(
            tec_thermistor_voltage_to_celsius(snap.tec_set_raw), 2
        )
        self._tec_monC = round(tec_current_to_amps(snap.tec_curr_raw), 3)
        self._tec_monV = round(tec_voltage_to_volts(snap.tec_volt_raw), 3)
        self._tec_good = bool(snap.tec_good)

        self.tecStatusChanged.emit()
        return True

    @pyqtSlot(result=QVariant)
    def pdu_mon(self, snap=None):
        if snap is None:
            snap = self._interface.console.telemetry.get_snapshot()
        if snap is None or not snap.read_ok or not snap.pdu_raws:
            return {"ok": False, "error": "no data"}

        self._pdu_raws = list(snap.pdu_raws)
        self._pdu_vals = list(snap.pdu_volts)
        self.pduMonChanged.emit()

        return {
            "ok": True,
            "adc0": {"raws": self._pdu_raws[:8], "vals": self._pdu_vals[:8]},
            "adc1": {"raws": self._pdu_raws[8:], "vals": self._pdu_vals[8:]},
        }

    @pyqtSlot()
    def readSafetyStatus(self, snap=None):
        if snap is None:
            snap = self._interface.console.telemetry.get_snapshot()
        if snap is None:
            logger.warning("readSafetyStatus: no telemetry snapshot yet")
            return
        try:
            # If the safety interlock chip didn't respond on this poll,
            # ``safety_ok`` is the dataclass default ``True`` — not a
            # verified clear signal. Earlier #107 fix tripped on the
            # first unresponsive poll, but #119 showed that a single
            # missed I2C read can be a transient during a USB
            # disconnect cascade rather than a real chip fault. Require
            # a streak (~3 s at 1 Hz) before firing; still well under
            # the time a user takes to reach Check.
            #
            # **GRACE PERIOD**: During the first 5 seconds after console
            # connection (power-on or reconnect), allow transient
            # unresponsiveness without triggering a safety failure. This
            # prevents spurious warnings during rapid power cycles where
            # the safety chip may not respond immediately while hardware
            # initializes. After the grace period, any unresponsiveness
            # is treated as a persistent fault.
            #
            # Backward compat: snapshots from older SDK builds without
            # ``safety_known`` keep the existing (less safe) behavior
            # of trusting the default ``True``.
            self._safety_unknown_streak, should_fire_unknown = (
                _safety_unknown_streak_decision(snap, self._safety_unknown_streak)
            )
            if not getattr(snap, "safety_known", True):
                # Grace period: suppress all trips during first 5 s after connect
                if self._console_connected_at is not None:
                    time_since_connect = time.monotonic() - self._console_connected_at
                    if time_since_connect < 5.0:
                        logger.debug(
                            f"readSafetyStatus: safety chip unresponsive "
                            f"{time_since_connect:.1f}s after connect (within grace period)"
                        )
                        return
                # After grace period: require streak before firing
                if not should_fire_unknown:
                    return  # transient miss; wait for streak to confirm
                if not self._safetyFailure:
                    fault_detail = (
                        "safety interlock chip unresponsive — cannot "
                        "verify laser safety state"
                    )
                    logger.error(f"Laser safety failure: {fault_detail}")
                    self.safetyFailure = True
                    self._fire_safety_notification(fault_detail)
                    self._raise_critical("E-201", detail=fault_detail)
                    self.stopTrigger()
                    self._laserOn = False
                    self.laserStateChanged.emit()
                    if self._capture_running and not self._safety_cancel_scheduled:
                        self.safetyTripDuringCaptureRequested.emit()
                return
            if snap.safety_ok:
                if self._safetyFailure:
                    self.safetyFailure = False
            else:
                if not self._safetyFailure:
                    # Decode which safety bits tripped so the engineering-
                    # mode toast (and the log) can name them. The SDK
                    # owns the bit→label mapping in ConsoleTelemetry.
                    from omotion.ConsoleTelemetry import _decode_safety_faults
                    se_faults = _decode_safety_faults(snap.safety_se)
                    so_faults = _decode_safety_faults(snap.safety_so)
                    fault_detail = (
                        f"safety_se=0x{snap.safety_se:02X} "
                        f"({', '.join(se_faults) or 'no faults'}); "
                        f"safety_so=0x{snap.safety_so:02X} "
                        f"({', '.join(so_faults) or 'no faults'})"
                    )
                    logger.error(f"Laser safety failure: {fault_detail}")
                    self.safetyFailure = True
                    self._fire_safety_notification(fault_detail)
                    self.stopTrigger()
                    # laserStateChanged is the notify signal for the
                    # zero-arg laserOn pyqtProperty — set the underlying
                    # state and emit() without args so QML re-reads the
                    # property. Previous form passed `False` and PyQt
                    # raised "signal has 0 arguments but 1 provided",
                    # which surfaced via the dev-mode safety toast.
                    self._laserOn = False
                    self.laserStateChanged.emit()
                    if self._capture_running and not self._safety_cancel_scheduled:
                        self.safetyTripDuringCaptureRequested.emit()
        except Exception as e:
            logger.error(f"readSafetyStatus failed: {e}")
            self.safetyFailure = True
            self._fire_safety_notification(f"telemetry exception: {e}")
            if self._capture_running and not self._safety_cancel_scheduled:
                self.safetyTripDuringCaptureRequested.emit()

    @pyqtSlot(str, int, int, int, int, int, result=QVariant)
    def i2cReadBytes(
        self,
        target: str,
        mux_idx: int,
        channel: int,
        i2c_addr: int,
        offset: int,
        data_len: int,
    ):
        """Send i2c read to device"""
        try:
            if target == "console":
                fpga_data, fpga_data_len = (
                    self._interface.console.read_i2c_packet(
                        mux_index=mux_idx,
                        channel=channel,
                        device_addr=i2c_addr,
                        reg_addr=offset,
                        read_len=data_len,
                    )
                )
                if fpga_data is None or fpga_data_len == 0:
                    logger.error("readI2CBytes failed (I2C read error)")
                    return []
                else:
                    return list(fpga_data[:fpga_data_len])

            elif target in ("left", "right"):
                logger.error("I2C Read Not Implemented")
                return []
        except Exception as e:
            logger.error(f"Error sending i2c read command: {e}")
            return []

    @pyqtSlot(int)
    def setRGBState(self, state):
        """Set the RGB state using integer values."""
        try:
            valid_states = [0, 1, 2, 3]
            if state not in valid_states:
                logger.error(f"Invalid RGB state value: {state}")
                return
            if self._interface.console.set_rgb_led(state) == state:
                logger.info(f"RGB state set to: {state}")
            else:
                logger.error(f"Failed to set RGB state to: {state}")
        except Exception as e:
            logger.error(f"Error setting RGB state: {e}")

    @pyqtSlot()
    def queryRGBState(self):
        """Fetch and emit RGB state."""
        try:
            state = self._interface.console.get_rgb_led()
            state_text = {0: "Off", 1: "IND1", 2: "IND2", 3: "IND3"}.get(
                state, "Unknown"
            )

            logger.info(f"RGB State: {state_text}")
            self.rgbStateReceived.emit(state, state_text)  # Emit both values
        except Exception as e:
            logger.error(f"Error querying RGB state: {e}")

    @pyqtSlot(result=QVariant)
    def queryTriggerConfig(self):
        trigger_setting = self._interface.console.get_trigger_json()
        if trigger_setting:
            if isinstance(trigger_setting, str):
                updateTrigger = json.loads(trigger_setting)
            else:
                updateTrigger = trigger_setting
            if updateTrigger["TriggerStatus"] == 2:
                self._trigger_state = "ON"
                self.triggerStateChanged.emit()
                return trigger_setting or {}

        self._trigger_state = "OFF"
        self.triggerStateChanged.emit()

        return trigger_setting or {}

    @pyqtSlot(str, result=bool)
    def setTrigger(self, triggerjson):  # Lock auto-released at function exit
        try:
            json_trigger_data = json.loads(triggerjson)

            # Resolve to (interface default ⊕ override). QML callers
            # only need to specify the fields they want to change
            # (typically just ``TriggerStatus``); absent fields fall
            # through to the SDK's resolved default rather than being
            # sent as missing keys.
            json_trigger_data = self._interface.resolve_trigger_config(
                json_trigger_data
            )

            trigger_setting = self._interface.console.set_trigger_json(
                data=json_trigger_data
            )
            if trigger_setting:
                logger.info(f"Trigger Setting: {trigger_setting}")
                return True
            else:
                logger.error("Failed to set trigger setting.")
                return False

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON data: {e}")
            return False

        except AttributeError as e:
            logger.error(f"Invalid interface or method: {e}")
            return False

        except Exception as e:
            logger.error(f"Unexpected error while setting trigger: {e}")
            return False

    @pyqtSlot(result=bool)
    def startTrigger(self):
        success = self._interface.console.start_trigger()
        if success:
            self._trigger_state = "ON"
            self.triggerStateChanged.emit()
            logger.info("Trigger started successfully.")
        return success

    @pyqtSlot()
    def stopTrigger(self):
        self._interface.console.stop_trigger()
        self._trigger_state = "OFF"
        self.triggerStateChanged.emit()
        logger.info("Trigger stopped.")

    @pyqtSlot(result=int)
    def getFsyncCount(self):
        """Get the Fsync count from the console."""
        try:
            fsync_count = self._interface.console.get_fsync_pulsecount()
            logger.info(f"Fsync Count: {fsync_count}")
            return fsync_count
        except Exception as e:
            logger.error(f"Error getting Fsync count: {e}")
            return -1

    @pyqtSlot(result=int)
    def getLsyncCount(self):
        """Get the Lsync count from the console."""
        try:
            lsync_count = self._interface.console.get_lsync_pulsecount()
            logger.debug(f"Lsync Count: {lsync_count}")
            return lsync_count
        except Exception as e:
            logger.error(f"Error getting Lsync count: {e}")
            return -1

    def set_laser_power_from_config(self, interface):
        # Laser-power config now lives in the SDK (omotion.laser): it bundles
        # the FPGA register map + the laser param set and writes them over I2C.
        # force_fault honors the forceLaserFail app flag (loads the
        # safety-trip param set to exercise the interlock).
        return interface.apply_laser_power(force_fault=self._force_laser_fail)

    def apply_tec_trip_from_config(self, interface) -> TecTripOutcome:
        """Push the configured TEC over-temp trip (tecTripTempC, °C) into the
        console user config. Read-modify-write touching only the TEC_TRIP key
        (calibration + OPT_*/EE_* preserved), serialized under _console_mutex.
        Today this connect-time push is the only holder of that mutex (so it is
        not yet load-bearing); a future concurrent console-config writer must
        take the same mutex for the read+write pair to stay atomic.

        Non-fatal: a bad config value or a device read/write failure is logged
        at ERROR and the device keeps its existing trip — never blocks scanning
        and never writes 0 (which would disable the firmware over-temp trip)."""
        temp_c = self._app_config.get("tecTripTempC", 40)
        self._console_mutex.lock()
        try:
            outcome = ensure_tec_trip(interface.console, temp_c)
        finally:
            self._console_mutex.unlock()

        if outcome is TecTripOutcome.WROTE:
            logger.info(f"Console TEC_TRIP set to {temp_c} °C")
        elif outcome is TecTripOutcome.UNCHANGED:
            logger.info(f"Console TEC_TRIP already {temp_c} °C, skipping write")
        elif outcome is TecTripOutcome.SKIPPED_INVALID:
            logger.error(
                f"Invalid tecTripTempC={temp_c!r}; leaving console over-temp "
                f"trip untouched (expected a number in "
                f"{TEC_TRIP_MIN_C}-{TEC_TRIP_MAX_C} °C)"
            )
        else:  # TecTripOutcome.FAILED
            logger.error(f"Failed to apply TEC_TRIP={temp_c} °C to console")
        return outcome

    # ------------------------------------------------------------------
    # Contact-quality quick-check
    # ------------------------------------------------------------------
    @staticmethod
    def _camera_label(side: str, cam_id: int) -> str:
        prefix = "L" if side == "left" else "R"
        return f"{prefix}{int(cam_id) + 1}"

    @staticmethod
    def _warning_text(type_key: str) -> str:
        return {
            "ambient_light": "Ambient light detected",
            "poor_contact": "Poor sensor contact",
        }.get(type_key, type_key)

    @staticmethod
    def _threshold_for(thresholds, cam_id: int, default_value: float) -> float:
        if isinstance(thresholds, (list, tuple)) and 0 <= int(cam_id) < len(thresholds):
            try:
                return float(thresholds[int(cam_id)])
            except Exception:
                return float(default_value)
        return float(default_value)

    def _ensure_idle(self) -> str | None:
        """Gate for pipeline-starting slots (capture / configure / check)."""
        # Issue #303: the post-connect sensor init (debug flags, camera
        # power masks, info reads) runs async for a few seconds after the
        # handles report READY. A capture/CQ/configure started inside that
        # window collides with the in-flight init — "Failed to program
        # FPGA" on both sensors, camera wedged until power-cycle. The UI
        # start gate polls isPipelineIdle and defers past this.
        if self.sensorInitBusy:
            return ("Sensors are still initializing — wait a few seconds "
                    "and try again")
        if self._cq_quick_running:
            return "Contact-quality check already in progress"
        if self._capture_running or self._capture_thread is not None:
            return "Scan already running"
        if self._config_running:
            return "Camera configuration already in progress"
        workflow = getattr(self, "_scan_workflow", None)
        if workflow is not None and getattr(workflow, "running", False):
            return "Scan already running"
        if workflow is not None and getattr(workflow, "config_running", False):
            return "Camera configuration already in progress"
        return None

    @pyqtSlot(result=bool)
    def isPipelineIdle(self) -> bool:
        """QML probe: True when a pipeline-starting slot called right now
        would not be refused by _ensure_idle. The pre-scan CQ check's SDK
        worker keeps unwinding for ~2 s after results are displayed, so a
        fast Start Scan click used to be refused synchronously and silently
        — BloodFlow's scan-start gate polls this instead of firing blind."""
        return self._ensure_idle() is None

    @pyqtSlot(result=bool)
    def isConfigInFlight(self) -> bool:
        """QML probe: True while a camera-sensor configuration (FPGA
        flash) is in flight or still draining after a cancel. Unlike the
        other _ensure_idle busy states (a few seconds at most), a config
        holds the pipeline for ~50 s — BloodFlow's start gate (issue #283)
        stretches its wait deadline to flash timescale while this is True
        instead of erroring out at the short deadline."""
        if self._config_running:
            return True
        workflow = getattr(self, "_scan_workflow", None)
        return bool(workflow is not None and getattr(workflow, "config_running", False))

    # ──────────────────────────────────────────────────────────────────
    @pyqtSlot(int, int)
    def runContactQualityCheck(self, left_camera_mask: int, right_camera_mask: int):
        """Run the contact-quality check via the SDK's ContactQualityWorkflow.

        Delegates to interface.contact_quality_workflow.check(), which runs
        a short scan internally and returns a ContactQualityResult with
        per-camera BFI statistics. The SDK call is synchronous/blocking, so
        we run it in a background thread and marshal results back via a
        private signal.

        left_camera_mask / right_camera_mask: the *configured scan* mask
        (bloodFlow.leftMask/rightMask — cameras the user actually selected,
        with any camera set to "None" cleared). Only these cameras are
        evaluated; a camera excluded from the scan must not be required to
        pass contact quality. Still AND-gated on sensor-connected state
        below, matching the pre-existing safety check.
        """
        err = self._ensure_idle()
        if err is not None:
            self.contactQualityCheckFinished.emit(False, err, [])
            return

        cfg = self._app_config or {}
        duration_s = float(cfg.get("cq_check_duration_sec", 1.0))
        dark_thresholds = list(
            cfg.get("cq_dark_threshold_per_camera") or [_CQ_DEFAULT_DARK_THRESHOLD_DN] * 8
        )
        light_thresholds = list(
            cfg.get("cq_light_threshold_per_camera") or [_CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8
        )
        rolling_window = int(cfg.get("cq_rolling_avg_window", _CQ_DEFAULT_ROLLING_WINDOW))

        if not (self._leftSensorConnected or self._rightSensorConnected):
            self.contactQualityCheckFinished.emit(False, "No sensors connected", [])
            return

        self._cq_quick_running = True
        self.contactQualityScanInProgress.emit(True)
        self.contactQualityCheckStarted.emit(int(round(duration_s + 3)))

        left_mask = int(left_camera_mask) if self._leftSensorConnected else 0x00
        right_mask = int(right_camera_mask) if self._rightSensorConnected else 0x00

        self._cq_t0 = time.monotonic()
        logger.info(
            "=== Contact-quality check started: duration=%.1fs "
            "left=0x%02X right=0x%02X ===",
            duration_s, left_mask, right_mask,
        )

        def _worker():
            try:
                result = self._interface.contact_quality_workflow.check(
                    duration_sec=duration_s,
                    rolling_window=rolling_window,
                    dark_threshold_per_camera=dark_thresholds,
                    light_threshold_per_camera=light_thresholds,
                    left_camera_mask=left_mask,
                    right_camera_mask=right_mask,
                )
                self._cq_result_signal.emit(result)
            except Exception as exc:
                logger.exception("CQ workflow check raised: %s", exc)
                self._cq_result_signal.emit(None)

        t = threading.Thread(target=_worker, daemon=True, name="CQWorkflow-check")
        t.start()

    # Private signal used to marshal ContactQualityResult from the CQ worker
    # thread back to the main Qt thread (emitted by the _worker closure in
    # runContactQualityCheck, consumed by _on_cq_result_ready).
    _cq_result_signal = pyqtSignal(object)

    @pyqtSlot(object)
    def _on_cq_result_ready(self, result):
        """Main-thread slot: convert ContactQualityResult → UI warning list and
        emit contactQualityCheckFinished.  Connected to _cq_result_signal in
        __init__ (via connect_signals).
        """
        cfg = self._app_config or {}
        dark_thresholds = list(
            cfg.get("cq_dark_threshold_per_camera") or [_CQ_DEFAULT_DARK_THRESHOLD_DN] * 8
        )
        light_thresholds = list(
            cfg.get("cq_light_threshold_per_camera") or [_CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8
        )

        self._cq_quick_running = False
        self.contactQualityScanInProgress.emit(False)

        elapsed_s = (
            time.monotonic() - self._cq_t0 if self._cq_t0 is not None else 0.0
        )
        if result is None:
            logger.info(
                "=== Contact-quality check ended: failed after %.1fs ===",
                elapsed_s,
            )
            self.contactQualityCheckFinished.emit(False, "CQ check failed", [])
            return
        logger.info(
            "=== Contact-quality check ended: completed after %.1fs ===",
            elapsed_s,
        )

        # Convert CamCQResult.reason → (typeKey, warning dict) that the QML
        # ContactQualityModal expects.
        warnings_by_key: dict[tuple[str, str], dict] = {}
        table_rows: list[dict] = []

        # Iterate over active cameras in mask order for consistent logging.
        for side_idx, (side, mask) in enumerate((("left", 0xFF), ("right", 0xFF))):
            for cam_id in range(8):
                cam_res = result.per_camera.get((side, cam_id))
                if cam_res is None:
                    continue  # camera not evaluated (outside mask)
                camera = self._camera_label(side, cam_id)
                dark_threshold = (
                    dark_thresholds[cam_id] if cam_id < len(dark_thresholds)
                    else _CQ_DEFAULT_DARK_THRESHOLD_DN
                )
                light_threshold = (
                    light_thresholds[cam_id] if cam_id < len(light_thresholds)
                    else _CQ_DEFAULT_LIGHT_THRESHOLD_DN
                )
                light_avg_dn = cam_res.light_avg_dn
                dark_max_dn = cam_res.dark_max_dn
                reason = cam_res.reason
                warn_tags = []

                if reason == "ambient_light":
                    warnings_by_key[(camera, "ambient_light")] = {
                        "camera": camera,
                        "typeKey": "ambient_light",
                        "typeText": self._warning_text("ambient_light"),
                        "value": float(dark_max_dn) if dark_max_dn == dark_max_dn else 0.0,
                    }
                    warn_tags.append("ambient_light")
                elif reason in ("poor_contact", "no_signal"):
                    warnings_by_key[(camera, "poor_contact")] = {
                        "camera": camera,
                        "typeKey": "poor_contact",
                        "typeText": self._warning_text("poor_contact"),
                        "value": float(light_avg_dn) if light_avg_dn == light_avg_dn else 0.0,
                    }
                    warn_tags.append("poor_contact")

                table_rows.append({
                    "camera": camera,
                    "light_avg_dn": f"{light_avg_dn:.2f}" if light_avg_dn == light_avg_dn else "n/a",
                    "dark_max_dn":  f"{dark_max_dn:.2f}"  if dark_max_dn  == dark_max_dn  else "n/a",
                    "dark_threshold": dark_threshold,
                    "light_threshold": light_threshold,
                    "reason": reason,
                    "warnings": ",".join(warn_tags) if warn_tags else "-",
                })

        logger.info("CQ Final Compare (DN, pedestal-subtracted; rolling-avg light, max dark):")
        logger.info(
            "| Camera | LightAvg | DarkMax | DarkThr | LightThr | Reason         | Warnings |"
        )
        logger.info(
            "|--------|----------|---------|---------|----------|----------------|----------|"
        )
        for row in table_rows:
            logger.info(
                "| %-6s | %8s | %7s | %7.2f | %8.2f | %-14s | %-8s |",
                row["camera"],
                row["light_avg_dn"],
                row["dark_max_dn"],
                row["dark_threshold"],
                row["light_threshold"],
                row["reason"],
                row["warnings"],
            )

        warning_list = list(warnings_by_key.values())
        ok = result.passed
        err_msg = "" if ok else "Contact quality check failed"
        self.contactQualityCheckFinished.emit(ok, err_msg, warning_list)

    @pyqtSlot()
    def _on_dropout_check(self):
        """1 Hz watchdog: emit cameraDropoutDetected for any camera silent > threshold."""
        if not self._capture_running:
            return
        # Also bail when the trigger is OFF. _capture_running only
        # flips false in _on_complete after the post-scan cleanup
        # finishes, so between trigger-stop and that flip there's a
        # 2+ s window where samples have legitimately stopped arriving
        # but the watchdog still considers the cameras 'live'. Without
        # this gate, every natural scan end fires a spurious 'Camera
        # X connection lost at HH:MM:SS' toast on every active cam.
        if self._trigger_state != "ON":
            return
        now = time.monotonic()
        threshold = self._camera_dropout_threshold_sec
        for key, last_t in list(self._camera_last_seen.items()):
            if key in self._camera_dropped:
                continue
            if now - last_t > threshold:
                side, cam_id = key
                temp = self._camera_last_temp.get(key, float("nan"))
                elapsed_str = self._scan_elapsed_str()
                msg = (
                    f"[{elapsed_str}] Camera {side.upper()} {cam_id + 1} dropout detected "
                    f"(no frames for >{threshold:.0f} s). Last temperature: {temp:.1f}°C"
                )
                logger.warning(msg)
                self.notify(
                    f"Camera {side.upper()} {cam_id + 1} connection lost at {elapsed_str}"
                    f" — last temp {temp:.1f}°C",
                    type_="warning",
                    duration_ms=30000,
                    tag=f"dropout_{side}_{cam_id}",
                )
                self._camera_dropped.add(key)
                self.cameraDropoutDetected.emit(side, cam_id, elapsed_str)
                # Also feed the LiveScanSource so the PlotViewer renders a
                # dropout bar. The marker must live on the SAME time axis
                # as the plotted samples — SDK-normalized timestamps, t=0
                # at the scan's FIRST FRAME. (`now - self._plot_t0` counted
                # from startCapture instead, which runs seconds of sensor
                # setup before the first frame, so the marker landed past
                # the live edge — clipped invisible while following live —
                # and at a wrong x afterwards; issue #284.) Anchor at the
                # camera's own newest sample: exactly where its trace
                # stops. NaN (no plottable samples ever, e.g. a covered
                # camera) means there is no trace to mark — skip the bar,
                # the toast/signal above still surface the dropout.
                src = self._current_scan_source
                if src is not None and getattr(src, "live", False):
                    drop_t = src.last_sample_t(side, cam_id)
                    if math.isfinite(drop_t):
                        src.mark_dropped(side=side, cam_id=cam_id, t=drop_t)

        # ── All-camera stall (issue #248) ─────────────────────────────
        # Per-camera dropouts above are informational (fail-soft: the
        # scan keeps running on the remaining cameras). Total data loss
        # is NOT fail-soft: if no camera has delivered a frame for
        # scanDataStallTimeoutSec the scan is dead air — abort it and
        # tell the user instead of counting down to a hollow "complete".
        if not self._scan_stall_abort_fired:
            stalled_s = _scan_data_stall_decision(
                now,
                self._trigger_on_mono,
                self._camera_last_seen,
                self._scan_data_stall_timeout_sec,
            )
            if stalled_s is not None:
                self._abort_scan_data_stall(stalled_s)

    def _abort_scan_data_stall(self, stalled_s: float) -> None:
        """Abort the running scan because data acquisition has completely
        stopped (issue #248). Runs on the main thread from the 1 Hz
        dropout watchdog. Mirrors the safety-trip path: capture-log line,
        critical-error modal (E-303), then stopCapture() — which cancels
        the SDK scan; the pipeline's completion sink then finalizes the
        data files and emits captureFinished, returning the QML scan flow
        (and the state machine) to idle exactly like a user Stop.
        """
        self._scan_stall_abort_fired = True
        elapsed_str = self._scan_elapsed_str()
        msg = (
            f"[{elapsed_str}] No data from any camera for "
            f">{self._scan_data_stall_timeout_sec:.0f} s — data acquisition "
            f"has stopped. Stopping the scan; data captured before the loss "
            f"is saved."
        )
        logger.error(msg)
        self.captureLog.emit(msg)
        self._raise_critical(
            "E-303",
            detail=(
                f"no camera frames for {stalled_s:.0f} s "
                f"(scan elapsed {elapsed_str})"
            ),
        )
        self.stopCapture()

    def _on_camera_dropout_recovered(self, side: str, cam_id: int) -> None:
        """Frames resumed for a camera the watchdog had flagged Connection
        Lost — surface the recovery. Called from _LivePlotSink.consume on
        the pipeline runner thread right after _rearm_dropped_camera
        un-marked the key (signal emits and notify() are thread-safe: both
        go through queued signal delivery).

        Reuses the dropout toast's tag so the "connection lost" toast is
        replaced in place rather than stacking a second one.
        """
        elapsed_str = self._scan_elapsed_str()
        self.notify(
            f"Camera {side.upper()} {cam_id + 1} reconnected at {elapsed_str}",
            type_="success",
            duration_ms=8000,
            tag=f"dropout_{side}_{cam_id}",
        )
        self.captureLog.emit(
            f"[{elapsed_str}] Camera {side.upper()} {cam_id + 1} frames "
            f"resumed — dropout watchdog re-armed."
        )
        self.cameraDropoutRecovered.emit(side, cam_id, elapsed_str)

    # --- TRIGGER-ON CLOCK (issue #201) ---
    # Single scan-time clock backing the notes duration line, the header
    # elapsed counter (scanElapsedSec) and dropout log timestamps. Opened /
    # closed by _TriggerStateSink from SDK TriggerStateEvents; also closed
    # by the console-disconnect handler because a dead console can't deliver
    # its OFF event (stop_trigger raises before _emit_trigger_event runs).

    def _trigger_clock_reset(self) -> None:
        """Zero the clock. Called once per scan from startCapture."""
        self._trigger_cumulative_s = 0.0
        self._trigger_on_mono = None
        self._trigger_on_ts = None

    def _trigger_clock_open(self, ts_s: float | None) -> None:
        """Start an ON interval. No-op if one is already open."""
        if self._trigger_on_mono is None:
            self._trigger_on_mono = time.monotonic()
            self._trigger_on_ts = ts_s

    def _trigger_clock_close(self, ts_s: float | None = None) -> None:
        """Bank the open ON interval into the cumulative total.

        Prefers the SDK-stamped, scan-relative timestamp pair: the OFF event
        can sit queued behind histogram batches in the runner's diagnostics
        channel and be consumed ~1 s after it was emitted, which inflates a
        receive-time measurement (a 16:00 scan shows as 16:01 — issue #201).
        Falls back to host receive-time when the timestamps are unusable
        (the SDK's t0-None path emits 0.0 for both edges, giving a
        non-positive delta) or when closing without an event (console lost).
        No-op when the clock is already closed, so a late OFF event after a
        disconnect-close can't double-count.
        """
        if self._trigger_on_mono is None:
            return
        ts_delta = (
            ts_s - self._trigger_on_ts
            if ts_s is not None and self._trigger_on_ts is not None
            else 0.0
        )
        if ts_delta > 0:
            self._trigger_cumulative_s += ts_delta
        else:
            self._trigger_cumulative_s += time.monotonic() - self._trigger_on_mono
        self._trigger_on_mono = None
        self._trigger_on_ts = None

    def _trigger_elapsed_s(self) -> float:
        """Trigger-ON seconds so far this scan, including any open interval."""
        elapsed = self._trigger_cumulative_s
        if self._trigger_on_mono is not None:
            elapsed += time.monotonic() - self._trigger_on_mono
        return elapsed

    @pyqtSlot(result=int)
    def scanElapsedSec(self) -> int:
        """QML-facing accessor for trigger-ON elapsed whole seconds.

        The header counter polls this once a second instead of counting its
        own Timer ticks — a QML Timer fires late under GUI load and a
        tick-counting clock drifts behind real time, disagreeing with the
        notes duration line written from this same clock.
        """
        return int(self._trigger_elapsed_s())

    @pyqtSlot(result=str)
    def scanElapsedStr(self) -> str:
        """QML-facing accessor for trigger-ON elapsed time (HH:MM:SS)."""
        return self._scan_elapsed_str()

    def _scan_elapsed_str(self) -> str:
        """Return current scan elapsed trigger-ON time as HH:MM:SS."""
        total_s = int(self._trigger_elapsed_s())
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    # --- SENSOR COMMUNICATION METHODS ---
    @pyqtSlot(int, int, result=bool)
    def startConfigureCameraSensors(
        self, left_camera_mask: int, right_camera_mask: int
    ) -> bool:
        err = self._ensure_idle()
        if err is not None:
            logger.warning("startConfigureCameraSensors refused: %s", err)
            self.configFinished.emit(False, err)
            return False
        self._config_running = True
        req = ConfigureRequest(
            left_camera_mask=left_camera_mask,
            right_camera_mask=right_camera_mask,
            power_off_unused_cameras=bool(self._power_off_unused_cameras),
        )
        started = self._interface.start_configure_camera_sensors(
            req,
            on_progress_fn=lambda pct: self.configProgress.emit(int(pct)),
            on_log_fn=lambda msg: self.configLog.emit(msg),
            on_complete_fn=self._on_config_finished,
        )
        if not started:
            self._config_running = False
            self.configFinished.emit(False, "Configuration could not start")
        return bool(started)

    @pyqtSlot()
    def cancelConfigureCameraSensors(self):
        if self._config_running:
            self._interface.cancel_configure_camera_sensors()

    def _on_config_finished(self, result):
        # If a disconnect already finalized this run via _on_handle_state_changed,
        # the SDK's late callback would otherwise re-emit configFinished and
        # re-fire the QML flashTask handler. Guard so we emit exactly once.
        if not self._config_running:
            return
        self._config_running = False
        self.configFinished.emit(bool(result.ok), result.error or "")

    @pyqtSlot(str)
    def querySensorAccelerometer(self, target: str):
        """Fetch and emit Accelerometer data. ``target`` is "left" or "right"."""
        try:
            if target not in ("left", "right"):
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            # Check if sensor is connected
            if (target == "left" and not self._leftSensorConnected) or (
                target == "right" and not self._rightSensorConnected
            ):
                logger.error(f"{target.capitalize()} sensor not connected")
                return

            sensor = getattr(self._interface, target)
            if sensor is None:
                logger.error(f"{target.capitalize()} sensor object is None")
                return
            accel = sensor.imu_get_accelerometer()
            logger.info(f"Accel (raw): X={accel[0]}, Y={accel[1]}, Z={accel[2]}")
            self.accelerometerSensorUpdated.emit(accel[0], accel[1], accel[2])
        except Exception as e:
            logger.error(f"Error querying Accelerometer data: {e}")

    @pyqtSlot(str)
    def querySensorGyroscope(self, target: str):
        """Fetch and emit Gyroscope data. ``target`` is "left" or "right"."""
        try:
            if target not in ("left", "right"):
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            gyro = getattr(self._interface, target).imu_get_gyroscope()
            logger.info(f"Gyro  (raw): X={gyro[0]}, Y={gyro[1]}, Z={gyro[2]}")
            self.gyroscopeSensorUpdated.emit(gyro[0], gyro[1], gyro[2])
        except Exception as e:
            logger.error(f"Error querying Gyroscope data: {e}")

    @pyqtSlot(str)
    def softResetSensor(self, target: str):
        """Reset a device. ``target`` is "console", "left", or "right"."""
        try:
            if target == "console":
                if self._interface.console.soft_reset():
                    logger.info("Software Reset Sent")
                else:
                    logger.error("Failed to send Software Reset")
            elif target in ("left", "right"):
                if getattr(self._interface, target).soft_reset():
                    logger.info("Software Reset Sent")
                else:
                    logger.error("Failed to send Software Reset")
        except Exception as e:
            logger.error(f"Error Sending Software Reset: {e}")

    @pyqtSlot(str)
    def querySensorTemperature(self, target: str):
        """Fetch and emit Temperature data. ``target`` is "left" or "right"."""
        try:
            if target not in ("left", "right"):
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            if (target == "left" and not self._leftSensorConnected) or (
                target == "right" and not self._rightSensorConnected
            ):
                logger.error(f"{target.capitalize()} sensor not connected")
                return

            sensor = getattr(self._interface, target)
            if sensor is None:
                logger.error(f"{target.capitalize()} sensor object is None")
                return

            imu_temp = sensor.imu_get_temperature()
            logger.info(f"Temperature Data - IMU Temp: {imu_temp}")
            self.temperatureSensorUpdated.emit(imu_temp)
        except Exception as e:
            logger.error(f"Error querying Temperature data: {e}")

    @pyqtSlot(str)
    def querySensorInfo(self, target: str):
        """Fetch and emit device information. ``target`` is "left" or "right"."""
        try:
            if target not in ("left", "right"):
                logger.error(f"Invalid target for sensor info query: {target}")
                return

            if (target == "left" and not self._leftSensorConnected) or (
                target == "right" and not self._rightSensorConnected
            ):
                logger.error(f"{target.capitalize()} sensor not connected")
                return

            sensor = getattr(self._interface, target)
            if sensor is None:
                logger.error(f"{target.capitalize()} sensor object is None")
                return

            fw_version = sensor.get_version()
            logger.info(f"Version: {fw_version}")
            hw_id = sensor.get_hardware_id()
            device_id = base58.b58encode(bytes.fromhex(hw_id)).decode()
            self.sensorDeviceInfoReceived.emit(fw_version, device_id)
            logger.info(
                f"Sensor Device Info - Firmware: {fw_version}, Device ID: {device_id}"
            )
        except Exception as e:
            logger.error(f"Error querying device info: {e}")

    # Fan control methods
    @pyqtSlot(str, bool, result=bool)
    def setFanControl(self, sensor_side: str, fan_on: bool) -> bool:
        """
        Set fan control for the specified sensor.

        Args:
            sensor_side (str): "left" or "right"
            fan_on (bool): True to turn fan ON, False to turn fan OFF

        Returns:
            bool: True if command was sent successfully, False otherwise
        """
        try:
            if sensor_side.lower() == "left":
                if not self._leftSensorConnected:
                    logger.error("Left sensor not connected")
                    return False
                result = self._interface.left.set_fan_control(fan_on)
            elif sensor_side.lower() == "right":
                if not self._rightSensorConnected:
                    logger.error("Right sensor not connected")
                    return False
                result = self._interface.right.set_fan_control(fan_on)
            else:
                logger.error(f"Invalid sensor side: {sensor_side}")
                return False

            if result:
                logger.info(
                    f"Fan control set to {'ON' if fan_on else 'OFF'} for {sensor_side} sensor"
                )
            else:
                logger.error(f"Failed to set fan control for {sensor_side} sensor")

            return result

        except Exception as e:
            logger.error(f"Error setting fan control: {e}")
            return False

    @pyqtSlot(str, result=bool)
    def getFanControlStatus(self, sensor_side: str) -> bool:
        """
        Get fan control status for the specified sensor.

        Args:
            sensor_side (str): "left" or "right"

        Returns:
            bool: True if fan is ON, False if fan is OFF
        """
        side = sensor_side.lower()
        if side not in ("left", "right"):
            logger.error(f"Invalid sensor side: {sensor_side}")
            return False

        sensor = self._interface.left if side == "left" else self._interface.right
        if not sensor.is_connected():
            # Polled while disconnected/disconnecting — silent, expected.
            return False

        try:
            status = sensor.get_fan_control_status()
            if status != self._last_fan_status.get(side):
                self._last_fan_status[side] = status
                logger.info(
                    f"Fan status for {sensor_side} sensor: {'ON' if status else 'OFF'}"
                )
            return status
        except Exception as e:
            # Disconnect raced with the in-flight call. Log at DEBUG;
            # the SDK's state machine already logs the disconnect at INFO.
            logger.debug(f"Fan status read on {side} failed during disconnect: {e}")
            return False

    # --- POST-PROCESSING METHODS ---
    @pyqtSlot(str, str, result=bool)
    def startPostProcess(self, left_raw: str, right_raw: str) -> bool:
        """
        Convert left/right .raw to .csv in-place (same directory).
        Returns False if a post job is already running.
        """
        if self._post_thread is not None:
            self.postLog.emit("Post-process already running.")
            return False

        left_raw = left_raw or ""
        right_raw = right_raw or ""
        self._post_cancel = threading.Event()

        def _worker():
            ok = True
            err = ""
            left_csv = ""
            right_csv = ""

            try:
                def _to_csv_path(p):
                    base, ext = os.path.splitext(p)
                    return base + ".csv" if base else ""

                # Process LEFT
                if left_raw and os.path.isfile(left_raw):
                    self.postLog.emit(f"Processing LEFT: {os.path.basename(left_raw)}")
                    self.postProgress.emit(5)
                    left_csv = _to_csv_path(left_raw)
                    process_bin_file(left_raw, left_csv)
                    self.postLog.emit(f"LEFT → {os.path.basename(left_csv)}")
                    self.postProgress.emit(50)
                else:
                    if left_raw:
                        self.postLog.emit(f"LEFT missing: {left_raw}")
                    self.postProgress.emit(50)

                # Cancel check between files
                if self._post_cancel.is_set():
                    ok = False
                    err = "Canceled"
                    return

                # Process RIGHT
                if right_raw and os.path.isfile(right_raw):
                    self.postLog.emit(
                        f"Processing RIGHT: {os.path.basename(right_raw)}"
                    )
                    self.postProgress.emit(55)
                    right_csv = _to_csv_path(right_raw)
                    process_bin_file(right_raw, right_csv)
                    self.postLog.emit(f"RIGHT → {os.path.basename(right_csv)}")
                    self.postProgress.emit(95)
                else:
                    if right_raw:
                        self.postLog.emit(f"RIGHT missing: {right_raw}")
                    self.postProgress.emit(95)

                self.postProgress.emit(100)

            except Exception as e:
                ok = False
                err = str(e)
                self.postLog.emit(f"Post-process error: {err}")
            finally:
                # clear thread handle before emitting
                self._post_thread = None
                self.postFinished.emit(ok, err, left_csv or "", right_csv or "")
                logger.info(
                    f"Post-process finished: ok={ok}, err={err}, left_csv={left_csv}, right_csv={right_csv}"
                )

        self._post_thread = threading.Thread(target=_worker, daemon=True)
        self._post_thread.start()
        return True

    @pyqtSlot()
    def cancelPostProcess(self):
        """Request cancel; takes effect between files."""
        if self._post_thread is None:
            return
        self.postLog.emit("Cancel requested.")
        self._post_cancel.set()

    # --- ERROR HANDLING METHODS / MISCELLANEOUS METHODS ---
    @pyqtSlot(str)
    def emitError(self, msg):
        self.errorOccurred.emit(msg)

    @pyqtSlot(result=str)
    def get_sdk_version(self):
        return self._interface.get_sdk_version()

    @pyqtSlot(str)
    def copyToClipboard(self, text: str) -> None:
        """Push a string to the system clipboard via Qt — used by the
        Test Results window's Copy button. Centralised here so QML
        doesn't need a direct dependency on PyQt6.QtGui."""
        from PyQt6.QtGui import QGuiApplication
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text)

    # --- CRITICAL ERROR SURFACING -----------------------------------------
    def _raise_critical(self, code: str, detail: str = "") -> None:
        """Surface a showstopper to the user via the critical-error modal.

        Looks ``code`` up in :mod:`error_codes`, logs it, and emits
        ``criticalErrorRaised``. Safe to call from worker threads — the QML
        side connects with a queued connection. Never raises.

        Also starts the console error-LED blink (issue #257) so the hardware
        mirrors the on-screen error state until the modal is dismissed.
        """
        try:
            err = error_codes.lookup(code)
            logger.error("CRITICAL %s — %s%s", code, err.title,
                         f" ({detail})" if detail else "")
            self.criticalErrorRaised.emit(
                code, err.title, err.message, err.suggested_action, detail)
            self._errorLedBlinkRequested.emit()
        except Exception:
            logger.exception("Failed to raise critical error %s", code)

    # --- Console error-LED blink (issue #257) -----------------------------
    # While the critical-error modal is showing, the console front-panel LED
    # blinks blue (mirroring the firmware Error_Handler's 500 ms blink)
    # instead of staying solid green. There is no firmware "blink" command —
    # OW_CTRL_SET_IND only sets solid states — so the app toggles blue/off on
    # a GUI-thread QTimer and restores idle green on dismissal.

    def _start_error_led_blink(self) -> None:
        """Main-thread slot: begin blinking the console LED blue.

        Idempotent — additional critical errors queued while the modal is
        already up keep the one running timer.
        """
        if self._error_led_timer.isActive():
            return
        self._error_led_on = False
        self._error_led_timer.start()
        self._on_error_led_tick()  # first (blue) edge now, not after 500 ms

    def _on_error_led_tick(self) -> None:
        """Toggle the console LED between blue and off; stop on failure."""
        self._error_led_on = not self._error_led_on
        state = _RGB_LED_BLUE if self._error_led_on else _RGB_LED_OFF
        if not self._set_console_led(state):
            # Console unreachable (e.g. disconnected while the modal is up)
            # — stop retrying rather than spamming failing UART writes.
            self._error_led_timer.stop()
            self._error_led_on = False

    def _set_console_led(self, state: int) -> bool:
        """Best-effort console RGB LED write. True on success; never raises."""
        try:
            return self._interface.console.set_rgb_led(state) == state
        except Exception:
            logger.debug("console LED write failed (state %d)", state,
                         exc_info=True)
            return False

    @pyqtSlot()
    def criticalErrorsDismissed(self):
        """Called from CriticalErrorModal when the last queued error is
        dismissed: stop the error blink and restore the idle green LED
        (issue #257). No-op if the blink never started (e.g. the console
        was never reachable)."""
        if not self._error_led_timer.isActive() and not self._error_led_on:
            return
        self._error_led_timer.stop()
        self._error_led_on = False
        self._set_console_led(_RGB_LED_GREEN)

    def _device_info_str(self) -> str:
        """Best-effort one-line device identity for bug reports."""
        try:
            console, left, right = self._interface.is_device_connected()
            return f"console={'yes' if console else 'no'} " \
                   f"left={'yes' if left else 'no'} " \
                   f"right={'yes' if right else 'no'}"
        except Exception:
            return "unknown"

    @pyqtSlot(str)
    def sendBugReport(self, code: str) -> None:
        """Send the current session log + error context to Openwater support.

        If a complete ``bug_report_smtp`` block is configured the email is sent
        directly (with the log attached) on a background thread. Otherwise we
        fall back to opening the user's mail client and revealing the log file
        so they can attach it manually.
        """
        err = error_codes.lookup(code)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = f"Open-Motion Bug Report — {code} {err.title}"
        body = bug_report.build_report_text(
            code=code, title=err.title, message=err.message,
            timestamp=timestamp, app_version=self._app_version or "unknown",
            device_info=self._device_info_str(),
            log_path=self._log_path or "(no log file)",
        )
        if bug_report.smtp_config_complete(self._bug_report_smtp):
            self._send_bug_report_smtp(subject, body)
        else:
            self._send_bug_report_fallback(subject, body)

    def _send_bug_report_smtp(self, subject: str, body: str) -> None:
        """Send the report via SMTP on a daemon thread; toast the result."""
        def _worker():
            try:
                bug_report.send_via_smtp(
                    self._bug_report_smtp, to_addr=self._support_email,
                    subject=subject, body=body, log_path=self._log_path or None,
                )
                self.notify("Bug report sent to Openwater.", "success")
            except Exception as exc:
                logger.exception("SMTP bug report failed")
                self.notify(f"Could not send bug report: {exc}", "error")
                # Fall back so the user can still get the report out.
                self._send_bug_report_fallback(subject, body)
        threading.Thread(target=_worker, daemon=True).start()

    def _send_bug_report_fallback(self, subject: str, body: str) -> None:
        """Open the mail client, copy the report, and reveal the log file."""
        import webbrowser
        self.copyToClipboard(body)
        if self._log_path:
            bug_report.reveal_in_file_manager(self._log_path)
        try:
            webbrowser.open(bug_report.build_mailto_url(
                self._support_email, subject=subject, body=body))
        except Exception:
            logger.exception("Could not open mail client for bug report")
        self.notify(
            "Bug report copied to clipboard. Attach the highlighted log "
            "file and send the email to Openwater.", "info", 8000, True)

    def connect_signals(self):
        """Subscribe to per-handle state changes on the SDK interface."""
        for handle in (
            self._interface.console,
            self._interface.left,
            self._interface.right,
        ):
            handle.signal_state_changed.connect(self._on_handle_state_changed)
        self.safetyTripDuringCaptureRequested.connect(
            self._on_safety_trip_during_capture
        )
        # Worker → Qt main thread for an async scan-worker abort (#213).
        # Default (auto) connection like safetyTripDuringCaptureRequested above:
        # resolves to a queued connection for the cross-thread emit, so the
        # slot's QML-touching teardown runs on the main thread.
        self.scanWorkerFailedDuringCapture.connect(
            self._on_scan_worker_failed
        )
        # Worker → Qt main thread: start the console error-LED blink for a
        # critical error (issue #257). The QTimer must be driven from the
        # GUI thread that owns it.
        self._errorLedBlinkRequested.connect(self._start_error_led_blink)
        # Worker → Qt main thread for the calibration completion callback.
        self._calibrationCompleteSignal.connect(self._on_calibration_complete)
        self._testScanCompleteSignal.connect(self._on_test_scan_complete)
        # Worker → Qt main thread for the CQ workflow result.
        self._cq_result_signal.connect(self._on_cq_result_ready)
        # Worker → Qt main thread for async past-scan loads (issue #152).
        self._pastScanBuffersReady.connect(self._on_past_scan_buffers_ready)
        # Auto-queued: emitted on the pipeline worker thread, runs the toast
        # on the GUI thread (same marshalling pattern as _pastScanBuffersReady).
        self._scanOutcomeWarningSignal.connect(self._on_scan_outcome_warning)

    @pyqtSlot()
    @pyqtSlot(str)
    def runCalibration(self, target: str = "both"):
        """Kick off the SDK calibration procedure. Idempotent if already
        running. Marshals the worker-thread completion back onto the Qt
        event loop via _calibrationCompleteSignal.

        ``target`` selects which side(s) to calibrate: ``"left"``,
        ``"right"``, or ``"both"`` (default). Issue #117 — test stations
        with only one static phantom need to calibrate one side at a time.
        Camera mask is still ``0xFF`` per side (every camera on the chosen
        sensor); the app config's leftMask/rightMask still don't apply.
        """
        from omotion import CalibrationRequest, CalibrationThresholds

        if self._calibration_status == "running":
            return

        if not self._consoleConnected:
            self.captureLog.emit("⚠️ Cannot calibrate: console not connected.")
            return

        target = (target or "both").lower().strip()
        if target not in ("left", "right", "both"):
            self.captureLog.emit(
                f"⚠️ Cannot calibrate: invalid target '{target}'."
            )
            return

        want_left  = target in ("left", "both")
        want_right = target in ("right", "both")
        left_mask  = 0xFF if (want_left  and self._leftSensorConnected)  else 0x00
        right_mask = 0xFF if (want_right and self._rightSensorConnected) else 0x00
        if (left_mask | right_mask) == 0:
            if target == "left" and not self._leftSensorConnected:
                self.captureLog.emit("⚠️ Cannot calibrate: left sensor not connected.")
            elif target == "right" and not self._rightSensorConnected:
                self.captureLog.emit("⚠️ Cannot calibrate: right sensor not connected.")
            else:
                self.captureLog.emit("⚠️ Cannot calibrate: no sensors connected.")
            return

        thresholds = CalibrationThresholds(
            min_mean_per_camera=list(self._ft_min_mean_per_camera or [0.0]*8),
            min_contrast_per_camera=list(self._ft_min_contrast_per_camera or [0.0]*8),
            min_bfi_per_camera=list(self._ft_min_bfi_per_camera or [0.0]*8),
            min_bvi_per_camera=list(self._ft_min_bvi_per_camera or [0.0]*8),
            max_bfi_per_camera=(
                list(self._ft_max_bfi_per_camera)
                if self._ft_max_bfi_per_camera is not None else None
            ),
            max_bvi_per_camera=(
                list(self._ft_max_bvi_per_camera)
                if self._ft_max_bvi_per_camera is not None else None
            ),
            max_dark_per_camera=(
                list(self._ft_max_dark_per_camera)
                if self._ft_max_dark_per_camera is not None else None
            ),
        )
        output_dir = os.path.join(self._data_root, "calibrations")
        os.makedirs(output_dir, exist_ok=True)
        # CalibrationWorkflow resolves the trigger config to the
        # interface's default (SDK ⊕ app override at construction)
        # when the request doesn't override — matches what the QML
        # scan / CQ flows do via SetTriggerTask. Pass None so
        # the workflow always sees the canonical config.
        req = CalibrationRequest(
            operator_id="bloodflow-app",
            output_dir=output_dir,
            left_camera_mask=left_mask,
            right_camera_mask=right_mask,
            thresholds=thresholds,
            duration_sec=self._calibration_scan_duration_sec,
            scan_delay_sec=self._calibration_scan_delay_sec,
            max_duration_sec=self._max_calibration_time_sec,
        )

        self._calibration_status = "running"
        self._calibration_target = target
        self.calibrationStateChanged.emit()
        self.captureLog.emit("Calibration: starting…")
        self._calibration_t0 = time.monotonic()
        self._audit.log("calibration_started", {"target": target})
        logger.info(
            "=== Calibration started: target=%s left=0x%02X right=0x%02X "
            "scan_duration=%ss ===",
            target, left_mask, right_mask, self._calibration_scan_duration_sec,
        )

        started = self._interface.start_calibration(
            req,
            on_log_fn=lambda msg: self.captureLog.emit(msg),
            on_complete_fn=self._calibrationCompleteSignal.emit,
        )
        if not started:
            self._calibration_status = ""
            self.calibrationStateChanged.emit()
            self.captureLog.emit("⚠️ Calibration failed to start.")

    @pyqtSlot()
    @pyqtSlot(str)
    def runTestScan(self, target: str = "both"):
        """Run just the calibration scan (phase 1) as a Test diagnostic.
        Mirrors runCalibration but does NOT write calibration to the
        console EEPROM and does NOT run a validation scan. Idempotent
        if a calibration or test scan is already in flight.

        ``target`` selects which side(s) to test: ``"left"``,
        ``"right"``, or ``"both"`` (default). Issue #117 — test stations
        with only one static phantom need to test one side at a time.
        """
        from omotion import CalibrationRequest, CalibrationThresholds

        # Mutual exclusion with the Calibrate flow.
        if self._test_scan_status == "running":
            return
        if self._calibration_status == "running":
            self.captureLog.emit(
                "⚠️ Cannot run Test scan: calibration in progress."
            )
            return

        if not self._consoleConnected:
            self.captureLog.emit(
                "⚠️ Cannot run Test scan: console not connected."
            )
            return

        target = (target or "both").lower().strip()
        if target not in ("left", "right", "both"):
            self.captureLog.emit(
                f"⚠️ Cannot run Test scan: invalid target '{target}'."
            )
            return

        want_left  = target in ("left", "both")
        want_right = target in ("right", "both")
        left_mask  = 0xFF if (want_left  and self._leftSensorConnected)  else 0x00
        right_mask = 0xFF if (want_right and self._rightSensorConnected) else 0x00
        if (left_mask | right_mask) == 0:
            if target == "left" and not self._leftSensorConnected:
                self.captureLog.emit("⚠️ Cannot run Test scan: left sensor not connected.")
            elif target == "right" and not self._rightSensorConnected:
                self.captureLog.emit("⚠️ Cannot run Test scan: right sensor not connected.")
            else:
                self.captureLog.emit("⚠️ Cannot run Test scan: no sensors connected.")
            return

        thresholds = CalibrationThresholds(
            min_mean_per_camera=list(self._ft_min_mean_per_camera or [0.0]*8),
            min_contrast_per_camera=list(self._ft_min_contrast_per_camera or [0.0]*8),
            min_bfi_per_camera=list(self._ft_min_bfi_per_camera or [0.0]*8),
            min_bvi_per_camera=list(self._ft_min_bvi_per_camera or [0.0]*8),
            max_bfi_per_camera=(
                list(self._ft_max_bfi_per_camera)
                if self._ft_max_bfi_per_camera is not None else None
            ),
            max_bvi_per_camera=(
                list(self._ft_max_bvi_per_camera)
                if self._ft_max_bvi_per_camera is not None else None
            ),
            max_dark_per_camera=(
                list(self._ft_max_dark_per_camera)
                if self._ft_max_dark_per_camera is not None else None
            ),
        )
        output_dir = os.path.join(self._data_root, "calibrations")
        os.makedirs(output_dir, exist_ok=True)
        req = CalibrationRequest(
            operator_id="bloodflow-app",
            output_dir=output_dir,
            left_camera_mask=left_mask,
            right_camera_mask=right_mask,
            thresholds=thresholds,
            duration_sec=self._test_scan_duration_sec,   # OQ8: Test uses shorter duration (default 5s), NOT _calibration_scan_duration_sec (15s)
            scan_delay_sec=self._calibration_scan_delay_sec,
            max_duration_sec=self._max_calibration_time_sec,
        )

        self._test_scan_status = "running"
        self._test_scan_rows = []
        self._test_scan_failure_reason = ""
        self.testScanStateChanged.emit()
        self.captureLog.emit("Test scan: starting…")
        self._test_scan_t0 = time.monotonic()
        logger.info(
            "=== Test scan started: target=%s left=0x%02X right=0x%02X "
            "duration=%ss ===",
            target, left_mask, right_mask, self._test_scan_duration_sec,
        )

        started = self._interface.start_test_scan(
            req,
            on_log_fn=lambda msg: self.captureLog.emit(msg),
            on_complete_fn=self._testScanCompleteSignal.emit,
        )
        if not started:
            self._test_scan_status = ""
            self.testScanStateChanged.emit()
            self.captureLog.emit("⚠️ Test scan failed to start.")

    @pyqtSlot(object)
    def _on_test_scan_complete(self, result):
        """Runs on the Qt main thread (queued from the SDK worker via
        _testScanCompleteSignal). Translates a TestScanResult into the
        QML-friendly _test_scan_rows model and updates _test_scan_status.
        """
        self._test_scan_failure_reason = ""
        if result.canceled:
            self._test_scan_status = "aborted"
            self.captureLog.emit(
                f"⚠️ Test scan aborted: {result.error or 'canceled'}"
            )
        elif not result.ok:
            self._test_scan_status = "aborted"
            self.captureLog.emit(
                f"⚠️ Test scan aborted: {result.error or 'unknown error'}"
            )
        elif result.passed:
            self._test_scan_status = "done"
            self.captureLog.emit(
                f"✅ Test scan: PASS  (CSV: {result.csv_path})"
            )
        else:
            self._test_scan_status = "failed"
            if self._app_config.get("engineeringMode", False):
                tests = (("mean", "mean_test"), ("contrast", "contrast_test"),
                         ("ambient", "dark_test"))
                breakdown = "; ".join(
                    f"{'L' if r.side == 'left' else 'R'}{r.cam_id + 1}:"
                    f"{','.join(n for n, a in tests if getattr(r, a) == 'FAIL')}"
                    for r in result.rows
                    if any(getattr(r, a) == "FAIL" for _, a in tests)
                )
                if any(r.dark_test == "FAIL" for r in result.rows):
                    breakdown = f"too much ambient light — {breakdown}"
                self._test_scan_failure_reason = breakdown
            self.captureLog.emit(
                f"❌ Test scan: FAIL  (CSV: {result.csv_path})"
            )

        elapsed_s = (
            time.monotonic() - self._test_scan_t0
            if self._test_scan_t0 is not None
            else 0.0
        )
        logger.info(
            "=== Test scan ended: %s after %.1fs ===",
            self._test_scan_status, elapsed_s,
        )

        # Build the QML-friendly row dicts.
        self._test_scan_rows = [
            {
                "side": r.side,
                "cam": r.cam_id + 1,
                "light_mean": r.mean,
                "min_mean": (
                    self._ft_min_mean_per_camera[r.cam_id]
                    if self._ft_min_mean_per_camera
                    and r.cam_id < len(self._ft_min_mean_per_camera)
                    else None
                ),
                "mean_pf": r.mean_test,
                "dark_mean": r.dark,
                "max_dark": (
                    self._ft_max_dark_per_camera[r.cam_id]
                    if self._ft_max_dark_per_camera
                    and r.cam_id < len(self._ft_max_dark_per_camera)
                    else None
                ),
                "dark_pf": r.dark_test,
                "contrast": r.avg_contrast,
                "min_contrast": (
                    self._ft_min_contrast_per_camera[r.cam_id]
                    if self._ft_min_contrast_per_camera
                    and r.cam_id < len(self._ft_min_contrast_per_camera)
                    else None
                ),
                "contrast_pf": r.contrast_test,
                "overall": (
                    "PASS"
                    if r.mean_test == "PASS"
                    and r.contrast_test == "PASS"
                    and r.dark_test != "FAIL"
                    else "FAIL"
                ),
            }
            for r in result.rows
        ]
        self.testScanStateChanged.emit()

    @pyqtSlot(object)
    def _on_calibration_complete(self, result):
        """Runs on the Qt main thread (queued from worker via signal)."""
        self._calibration_failure_reason = ""  # reset each run
        if result.canceled:
            self._calibration_status = "aborted"
            self.captureLog.emit(
                f"⚠️ Calibration aborted: {result.error or 'canceled'}"
            )
        elif not result.ok:
            self._calibration_status = "aborted"
            self.captureLog.emit(
                f"⚠️ Calibration aborted: {result.error or 'unknown error'}"
            )
        elif result.passed:
            self._calibration_status = "passed"
            self.captureLog.emit(
                f"✅ Calibration: PASS  (CSV: {result.csv_path})"
            )
        else:
            self._calibration_status = "failed"
            if self._app_config.get("engineeringMode", False):
                tests = (("mean", "mean_test"), ("contrast", "contrast_test"),
                         ("bfi", "bfi_test"), ("bvi", "bvi_test"),
                         ("ambient", "dark_test"))
                breakdown = "; ".join(
                    f"{'L' if r.side == 'left' else 'R'}{r.cam_id + 1}:"
                    f"{','.join(n for n, a in tests if getattr(r, a) == 'FAIL')}"
                    for r in result.rows
                    if any(getattr(r, a) == "FAIL" for _, a in tests)
                )
                # #122: dev-mode message must explicitly call out ambient-
                # light failures so operators don't misread an "ambient"
                # tag in the breakdown as a generic test name.
                if any(r.dark_test == "FAIL" for r in result.rows):
                    breakdown = f"too much ambient light — {breakdown}"
                self._calibration_failure_reason = breakdown
            self.captureLog.emit(
                f"❌ Calibration: FAIL  (CSV: {result.csv_path})"
            )
        elapsed_s = (
            time.monotonic() - self._calibration_t0
            if self._calibration_t0 is not None
            else 0.0
        )
        logger.info(
            "=== Calibration ended: %s after %.1fs ===",
            self._calibration_status, elapsed_s,
        )
        self._audit.log("calibration_ended", {
            "target": self._calibration_target,
            "outcome": self._calibration_status,
            "reason": self._calibration_failure_reason or None,
        })
        self.calibrationStateChanged.emit()

    @property
    def interface(self):
        return self._interface

    # ── App update checking ─────────────────────────────────────────────

    _GITHUB_REPO = "OpenwaterHealth/openmotion-bloodflow-app"

    @pyqtSlot()
    def checkForUpdates(self):
        """Check GitHub releases for a newer version (runs in a background thread)."""
        t = threading.Thread(target=self._check_for_updates_worker, daemon=True)
        t.start()

    def _check_for_updates_worker(self):
        import urllib.request
        from version import get_version

        # ``updateRepo`` points the check at a staging/mirror repo; the broader
        # ``updateApiUrl`` fully overrides the releases-latest endpoint (used by
        # the local fake-releases server for offline end-to-end update testing).
        # Both absent => the production GitHub repo.
        repo = self._app_config.get("updateRepo") or self._GITHUB_REPO
        api_url = self._app_config.get("updateApiUrl") or (
            f"https://api.github.com/repos/{repo}/releases/latest"
        )
        try:
            req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            remote_tag = data.get("tag_name", "").lstrip("v")
            if not remote_tag:
                self.updateCheckFailed.emit("Could not determine latest release tag.")
                return

            # Find the Setup bundle matching this build's variant.
            # clinicalMode True = clinical build; False = Research/full build.
            is_research = not bool(self._app_config.get("clinicalMode", False))
            download_url = _select_update_asset(data.get("assets", []), is_research)

            local_version = get_version()
            # Strip local metadata for comparison (e.g. "+3.gabc1234.dirty")
            local_base = local_version.split("+")[0]

            if not self._version_newer(remote_tag, local_base):
                logger.info(f"App is up to date ({local_base} >= {remote_tag})")
                self.updateNotAvailable.emit()
            elif not _is_bundle_url(download_url):
                # Newer release exists but has no matching installer asset
                # (e.g. assets still uploading) -- don't offer an in-place
                # update we can't actually perform.
                logger.warning(
                    "Update %s available but no installer asset found",
                    remote_tag,
                )
                self.updateNotAvailable.emit()
            else:
                logger.info(
                    "Update available: %s (current: %s)",
                    remote_tag, local_base,
                )
                self.updateAvailable.emit(remote_tag, download_url)

        except Exception as e:
            logger.warning(f"Update check failed: {e}")
            self.updateCheckFailed.emit(str(e))

    @staticmethod
    def _version_newer(remote: str, local: str) -> bool:
        """Return True if remote version is strictly newer than local.

        Handles versions like '0.4.3', 'pre-0.4.3', '1.0-pre3', and
        setuptools_scm-style dev strings like '1.2.0-dev.1-0-gabc1234-dirty'.
        Compares the leading dotted-numeric core; any suffix after it
        marks a pre-release, which counts as older than the same base
        version. (The old int() parse raised on dev suffixes and fell
        back to [0], making every remote release look newer.)
        """
        def parse(v):
            is_pre = v.startswith("pre-")
            base = v[4:] if is_pre else v
            m = re.match(r"(\d+(?:\.\d+)*)(.*)", base)
            if not m:
                return [0], is_pre
            parts = [int(x) for x in m.group(1).split(".")]
            if m.group(2):
                is_pre = True
            return parts, is_pre

        r_parts, r_pre = parse(remote)
        l_parts, l_pre = parse(local)

        if r_parts != l_parts:
            return r_parts > l_parts
        # Same base version: non-pre > pre
        if l_pre and not r_pre:
            return True
        return False

    @pyqtSlot(str)
    def openDownloadUrl(self, url: str):
        """Open the download URL in the system browser."""
        import webbrowser
        webbrowser.open(url)

    @pyqtSlot(str)
    def applyUpdate(self, download_url: str):
        """Download the update bundle, verify it, and launch the upgrade.

        Guarded against re-entry so repeated clicks can't spawn racing
        download threads against the same file.
        """
        if getattr(self, "_update_in_progress", False):
            logger.info("Update already in progress; ignoring repeat request")
            return
        self._update_in_progress = True
        t = threading.Thread(
            target=self._apply_update_worker, args=(download_url,), daemon=True
        )
        t.start()

    def _apply_update_worker(self, download_url: str):
        import urllib.request
        import subprocess

        try:
            if not _is_bundle_url(download_url):
                self.updateCheckFailed.emit("No installer for this update.")
                return

            portable = bool(self._app_config.get("portableMode", False))
            updates_dir = app_paths.writable_root(portable) / app_paths.DATA_DIRNAME / "updates"
            updates_dir.mkdir(parents=True, exist_ok=True)
            # Clear stale downloads so old installers don't accumulate.
            for stale in updates_dir.glob("*"):
                try:
                    stale.unlink()
                except OSError:
                    pass

            dest = updates_dir / download_url.rsplit("/", 1)[-1]
            self.updateProgress.emit("Downloading update…")
            logger.info("Downloading update %s -> %s", download_url, dest)
            urllib.request.urlretrieve(download_url, str(dest))

            # Reject truncated / non-executable downloads before launching.
            with open(dest, "rb") as f:
                head = f.read(2)
            if not _looks_like_pe(head):
                self.updateCheckFailed.emit(
                    "Downloaded update is not a valid installer."
                )
                return

            status = _authenticode_status(str(dest))
            should_launch, error = _update_decision(
                status, _REQUIRE_SIGNED_UPDATES
            )
            if not should_launch:
                self.updateCheckFailed.emit(error)
                return
            if status == "NotSigned":
                logger.warning(
                    "Update bundle not signed (transition); proceeding"
                )

            # The app cannot replace its own running files, and the Burn
            # bundle does not relaunch the app. So write a detached helper
            # that waits for us to exit, runs the bundle silently, then
            # relaunches the (now-updated) app. Then quit.
            helper = updates_dir / "update_helper.ps1"
            helper.write_text(
                _build_update_helper_script(
                    os.getpid(), str(dest), sys.executable
                ),
                encoding="utf-8",
            )
            self.updateProgress.emit("Installing update…")
            logger.info("Spawning update helper; quitting for upgrade")
            # NB: DETACHED_PROCESS leaves powershell with no console and no std
            # handles, so it dies instantly WITHOUT running the script (verified
            # in isolation — the helper never executed, which is why earlier
            # upgrades silently never installed). CREATE_NO_WINDOW gives it a
            # hidden console and DEVNULL std handles, so the detached helper
            # actually runs.
            subprocess.Popen(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(helper),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # Ask Qt to shut down gracefully (aboutToQuit -> handle_exit stops
            # the hardware monitor, releases the USB transport, flushes the
            # audit log). This runs on a worker thread, so DO NOT call
            # QCoreApplication.quit() directly: a cross-thread quit() can
            # block inside the C++ call while HOLDING the GIL, which freezes
            # the whole process — every thread, including any hard-exit
            # backstop, starves (observed under the old qasync event loop via
            # py-spy; the queued post is the safe pattern under the plain Qt
            # loop too). Post the quit to the main thread instead;
            # QueuedConnection just enqueues an event and returns immediately,
            # so this worker never blocks. If graceful
            # teardown stalls or the quit is ignored, the detached helper
            # force-kills us after its grace window and the upgrade proceeds
            # regardless — so we don't rely on the app exiting itself.
            QMetaObject.invokeMethod(
                QCoreApplication.instance(),
                "quit",
                Qt.ConnectionType.QueuedConnection,
            )
        except Exception as e:
            logger.error("applyUpdate failed: %s", e)
            self.updateCheckFailed.emit(str(e))
        finally:
            # Cleared so a failed attempt can be retried (on success the app
            # is already quitting).
            self._update_in_progress = False
