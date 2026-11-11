"""Pipeline sinks the scan attaches to the SDK runner, and the Qt-free
scan-outcome classification they feed."""
import math
import time
from typing import Any, Dict, NamedTuple, Optional

from connector._common import SIDES as _SIDE_NAMES, logger

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

    def __init__(self, connector, plot_t0: float,
                 live_source,
                 nan_gap_tracker=None):
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
                        # Every build, clinical included (#702): the
                        # operator sees it and the audit log records it.
                        # AuditLog is thread-safe and the latch above
                        # limits this to one write per camera per scan.
                        connector._audit.log("camera_over_temperature", {
                            "side": side,
                            "camera": cam_id + 1,
                            "temp_c": round(temp_c, 1),
                            "threshold_c": threshold,
                        })
                        # captureLog only reaches the app log (its QML
                        # terminus is console.log), so the operator sees
                        # nothing on screen without this. notify() is safe
                        # from this runner thread — it emits a signal that
                        # is delivered queued onto the GUI thread, same as
                        # _on_camera_dropout_recovered below.
                        connector.notify(
                            f"Camera {side.upper()} {cam_id + 1} temperature "
                            f"{temp_c:.1f}°C — above {threshold:.0f}°C threshold. "
                            f"Check the airflow around the sensor module.",
                            type_="warning",
                            duration_ms=5000,
                            tag=f"temp_{side}_{cam_id}",
                        )

                # Non-finite BFI/BVI: row-addressed LIGHT rows are appended
                # anyway (issue #418) — an unlit (covered / off-target)
                # camera must keep advancing its buffers and the source's
                # liveEdge so the time axis scrolls through a contact loss;
                # PlotCell breaks the trace at NaN so the run renders as a
                # blank gap. Dark rows keep the finite gate (a NaN append at
                # every scheduled dark would notch the trace ~each 15 s),
                # and legacy fan-out batches keep it too — without
                # side_ids/cam_ids a finite BFI is the only evidence this
                # camera actually produced the row.
                if not (math.isfinite(bfi) and math.isfinite(bvi)):
                    if is_dark or not row_addressed:
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

    def __init__(self, connector):
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

    def __init__(self, connector, on_complete_cb):
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
SCAN_TERMINATION_OK = "ok"            # ran to its configured end
SCAN_TERMINATION_STOPPED = "stopped"  # operator stop, no fault
SCAN_TERMINATION_ABORTED = "aborted"  # a fault ended it (error_code/reason)


def audit_scan_ended_details(
    *,
    outcome: Optional[ScanOutcome],
    canceled: bool,
    abort_code: Optional[str],
    abort_reason: str = "",
) -> Dict[str, Any]:
    """Termination fields for the ``scan_ended`` audit event.

    Returns ``outcome`` (``ok`` / ``stopped`` / ``aborted``), ``data`` (the
    classify_scan_outcome kind: ``ok`` / ``partial`` / ``empty`` /
    ``skipped``, or None when classification failed), and ``error_code`` /
    ``reason`` — populated only for an abort, null otherwise, so the CSV
    export keeps one stable column set.

    - An app-side abort (``abort_code`` set) always wins: E-303 / E-304 /
      E-202 / E-301 all cancel the SDK scan, so ``canceled`` is True for
      them too and must not be read as an operator stop.
    - No abort code but the classifier flagged an unexpected end (severity
      set — an interrupted scan the SDK tore down before any app-side
      handler claimed it) → ``aborted`` with the classifier's message as the
      reason and no error code.
    - Otherwise ``stopped`` when the stop event was set, else ``ok``.
    """
    data = outcome.kind if outcome is not None else None
    if abort_code:
        return {
            "outcome": SCAN_TERMINATION_ABORTED,
            "data": data,
            "error_code": abort_code,
            "reason": abort_reason or None,
        }
    if outcome is not None and outcome.severity:
        return {
            "outcome": SCAN_TERMINATION_ABORTED,
            "data": data,
            "error_code": None,
            "reason": outcome.message or None,
        }
    return {
        "outcome": (SCAN_TERMINATION_STOPPED if canceled
                    else SCAN_TERMINATION_OK),
        "data": data,
        "error_code": None,
        "reason": None,
    }

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
