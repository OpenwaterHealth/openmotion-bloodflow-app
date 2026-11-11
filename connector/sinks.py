"""Pipeline sinks the scan attaches to the SDK runner, and the Qt-free
scan-outcome classification they feed."""
import math
import time
from typing import Any, Dict, NamedTuple, Optional

from omotion.pipeline.batch import TerminalDarkResult, TriggerStateEvent

from connector._common import SIDES, logger

def _rearm_dropped_camera(side: str, cam_id: int, dropped: set) -> str | None:
    """Un-mark a camera the dropout watchdog flagged 'Connection Lost', now
    that a fresh frame has arrived (issue #85 follow-up).

    Frames resuming is proof the camera is back: re-arm the watchdog and
    resume display, instead of suppressing the camera for the rest of the
    scan after any transient stall. The watchdog re-fires (with a fresh
    toast) if the camera goes silent again.

    Returns the recovery message to log when ``(side, cam_id)`` was marked,
    else None. Mutates ``dropped``.
    """
    key = (side, int(cam_id))
    if key not in dropped:
        return None
    dropped.discard(key)
    return (f"Camera {side.upper()} {int(cam_id) + 1} resumed sending frames after being "
            f"marked Connection Lost — re-arming the dropout watchdog and resuming display.")


def _finite_or_none(array, index) -> Optional[float]:
    """``array[index]`` as a float, or None when the array is absent or the value non-finite."""
    if array is None:
        return None
    value = float(array[index])
    return value if math.isfinite(value) else None


class _Sink:
    """The three hooks the SDK's ScanRunner calls on every sink, as no-ops."""

    def on_scan_start(self, meta) -> None:
        pass

    def consume(self, channel: str, payload) -> None:
        pass

    def on_complete(self) -> None:
        pass


class _LivePlotSink(_Sink):
    """Feeds per-frame samples from the 'live' pipeline channel into the
    LiveScanSource backing the PlotViewer, and keeps the camera-liveness
    bookkeeping the dropout watchdog reads.

    The 'live' channel carries a FrameBatch after BfiBvi; each batch may hold
    several frames and both sides.

    Camera liveness is keyed to FRAME ARRIVAL, not to whether a frame yielded
    a plottable sample. A covered / off-target camera streams light-typed
    frames whose BFI/BVI are NaN (the SDK dark stage suppresses realtime
    emission for unlit frames); that camera is connected and healthy, and
    keying the heartbeat on finite BFI misdiagnosed it as a dropout.

    The 'live_side' channel carries one SideAverageSample per capture per side
    (the SDK's LiveSideAverageStage, clinical mode), appended under cam_id=-1.
    """

    channels = {"live", "live_side"}

    # A camera must present this many consecutive frames of the opposite
    # low-light state before we log the transition: one operator-facing line
    # per genuine cover/uncover, not 40 Hz of boundary flapping.
    _LOW_LIGHT_DEBOUNCE_FRAMES = 80  # ~2 s at 40 fps

    def __init__(self, connector, live_source, nan_gap_tracker=None):
        self._connector = connector
        self._live_source = live_source
        self._temp_alerted: set[tuple[str, int]] = set()
        # Records every arriving frame so the scan-complete handler can report
        # sustained DELIVERY gaps in the notes footer. Optional so the sink
        # works standalone (tests).
        self._nan_gap_tracker = nan_gap_tracker
        # Per-camera low-light state (the SDK's batch.low_light_rt): True =
        # the camera currently sees no meaningful light (covered / off target).
        self._low_light_state: dict[tuple[str, int], bool] = {}
        self._low_light_streak: dict[tuple[str, int], int] = {}

    def on_scan_start(self, meta) -> None:
        self._temp_alerted.clear()

    def consume(self, channel: str, payload) -> None:
        if channel == "live_side":
            self._consume_side_avg(payload)
            return
        if channel != "live" or payload.bfi_live is None:
            return
        batch = payload
        now_mono = time.monotonic()
        # Hand-built batches (tests) may omit this optional FrameBatch field.
        low_light_rt = getattr(batch, "low_light_rt", None)

        for i in range(batch.bfi_live.shape[0]):
            ft = str(batch.frame_type[i]) if batch.frame_type is not None else "light"
            # Stale frames are leftover garbage (e.g. an unflushed histogram
            # buffer): not evidence of a live camera, never displayed.
            if ft == "stale":
                continue
            is_dark = ft == "dark"
            ts = float(batch.timestamp_s[i])
            frame_id = int(batch.abs_frame_ids[i]) if batch.abs_frame_ids is not None else i
            side_idx, cam_id = int(batch.side_ids[i]), int(batch.cam_ids[i])
            if not (0 <= side_idx < len(SIDES) and 0 <= cam_id < 8):
                continue
            index = (i, side_idx, cam_id)
            key = (SIDES[side_idx], cam_id)
            bfi = float(batch.bfi_live[index])
            bvi = float(batch.bvi_live[index])
            temp_c = float(batch.temperature_c[index])
            finite = math.isfinite(bfi) and math.isfinite(bvi)

            # An unlit camera streams NaN BFI/BVI but is connected: it
            # must keep feeding the heartbeat and the delivery-gap tracker.
            self._record_alive(key, now_mono, ts)
            # Warmup frames prove liveness but carry no display signal.
            if ft == "warmup":
                continue
            # Light frames only: scheduled dark frames are expected to be
            # unlit and have no meaningful temperature reading. Before the
            # NaN gate: a covered camera still reports a real chip
            # temperature and can still overheat.
            if not is_dark:
                if low_light_rt is not None:
                    self._note_low_light(key, bool(low_light_rt[index]))
                if math.isfinite(temp_c):
                    self._note_temperature(key, temp_c)

            # Non-finite BFI/BVI: LIGHT rows are appended anyway (issue
            # #418), so an unlit camera keeps advancing its
            # buffers and the source's liveEdge and the time axis scrolls
            # through a contact loss (PlotCell breaks the trace at NaN).
            # Dark rows keep the finite gate (a NaN at every scheduled
            # dark would notch the trace each ~15 s).
            if not finite and is_dark:
                continue

            # Realtime dark-corrected mean and shot-noise-corrected
            # contrast; the accurate corrected record lands in the scan DB
            # through the SDK's ScanDBSink. None = "not available for this
            # sample": dark frames, and NaN values (early light frames
            # before the first dark observation have NaN mean_dc_rt).
            # temp feeds the per-cell dev-mode readout (issue #165).
            mean = contrast = temp = None
            if not is_dark:
                mean = _finite_or_none(batch.mean_dc_rt, index)
                contrast = _finite_or_none(batch.contrast_sn_rt, index)
                temp = temp_c if math.isfinite(temp_c) else None
            self._live_source.append_uncorrected(
                side=key[0], cam_id=cam_id, frame_id=frame_id, t=ts,
                bfi=bfi, bvi=bvi, mean=mean, contrast=contrast, temp=temp)

    def _record_alive(self, key: tuple[str, int], now_mono: float, ts: float) -> None:
        """A frame arrived from this camera: feed the dropout watchdog's
        heartbeat, re-arm the camera if it was flagged dropped, and count the
        frame for the delivery-gap footer."""
        connector = self._connector
        connector._camera_last_seen[key] = now_mono
        recovery_msg = _rearm_dropped_camera(*key, connector._camera_dropped)
        if recovery_msg is not None:
            logger.warning(recovery_msg)
            connector._on_camera_dropout_recovered(*key)
        if self._nan_gap_tracker is not None:
            self._nan_gap_tracker.record(key, ts)

    def _note_temperature(self, key: tuple[str, int], temp_c: float) -> None:
        """Cache a camera's temperature and raise the over-temperature alert
        once per camera per scan."""
        connector = self._connector
        connector._camera_last_temp[key] = temp_c
        threshold = connector._camera_temp_alert_threshold_c
        if not temp_c >= threshold or key in self._temp_alerted:
            return
        self._temp_alerted.add(key)
        side, cam_id = key
        msg = (f"ALERT: Camera {cam_id + 1} ({side}) "
               f"temperature {temp_c:.1f}°C >= {threshold:.0f}°C threshold.")
        connector.captureLog.emit(msg)
        logger.warning(msg)
        # Every build, clinical included (#702): the operator sees it and the
        # audit log records it. AuditLog is thread-safe and the latch above
        # limits this to one write per camera per scan.
        connector._audit.log("camera_over_temperature", {
            "side": side, "camera": cam_id + 1, "temp_c": round(temp_c, 1),
            "threshold_c": threshold})
        # captureLog only reaches the app log (its QML terminus is
        # console.log), so the operator sees nothing on screen without the
        # toast. notify() is safe from this runner thread: it emits a signal
        # delivered queued onto the GUI thread.
        connector.notify(
            f"Camera {side.upper()} {cam_id + 1} temperature "
            f"{temp_c:.1f}°C — above {threshold:.0f}°C threshold. "
            f"Check the airflow around the sensor module.",
            type_="warning", duration_ms=5000, tag=f"temp_{side}_{cam_id}")

    def _note_low_light(self, key: tuple[str, int], is_low: bool) -> None:
        """Track a camera's low-light state and log debounced transitions (see
        _LOW_LIGHT_DEBOUNCE_FRAMES), so an empty plot has an explanation in
        the capture log."""
        if is_low == self._low_light_state.get(key, False):
            self._low_light_streak[key] = 0
            return
        streak = self._low_light_streak.get(key, 0) + 1
        if streak < self._LOW_LIGHT_DEBOUNCE_FRAMES:
            self._low_light_streak[key] = streak
            return
        self._low_light_state[key] = is_low
        self._low_light_streak[key] = 0
        side, cam_id = key
        msg = f"Camera {side.upper()} {cam_id + 1} signal restored."
        if is_low:
            msg = (f"Camera {side.upper()} {cam_id + 1} low light — frames arriving but no "
                   f"meaningful signal (sensor covered or off target?). "
                   f"Plot paused for this camera.")
        logger.info(msg)
        self._connector.captureLog.emit(msg)

    def _consume_side_avg(self, sample) -> None:
        """Append one clinical-mode per-side average (SideAverageSample) under
        cam_id=-1. The stage already emits one true spatial average per
        capture per side at ~40 Hz, so there is no dedup or skipping here."""
        side_idx = int(sample.side)
        if not 0 <= side_idx < len(SIDES):
            return
        side = SIDES[side_idx]
        t = float(sample.t)
        # Arrival-based, same as the per-camera path: a NaN average (all
        # cameras unlit, e.g. covered sensors) is not a delivery gap.
        if self._nan_gap_tracker is not None:
            self._nan_gap_tracker.record((side, -1), t)
        self._live_source.append_uncorrected(
            side=side, cam_id=-1, frame_id=int(sample.frame_id), t=t,
            bfi=float(sample.bfi), bvi=float(sample.bvi))


class _TriggerStateSink(_Sink):
    """Mirrors the laser-trigger state (TriggerStateEvent on the diagnostics
    channel) into the connector.

    Scan-time start_trigger() goes straight to the firmware via ScanWorkflow,
    so without this sink nothing would update:

      * ``_trigger_state`` ("ON" / "OFF"), the QML ``triggerState`` property,
        which gates BloodFlow.qml's scanTimer and the dropout watchdog;
      * the trigger-ON clock behind ``_scan_elapsed_str`` and the scan-notes
        duration line (a real trigger-ON measurement, not wall-clock).
    """

    channels: set = frozenset({"diagnostics"})

    def __init__(self, connector):
        self._connector = connector

    def consume(self, channel: str, payload) -> None:
        if channel != "diagnostics" or not isinstance(payload, TriggerStateEvent):
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


class _CompletionSink(_Sink):
    """Runs the connector's end-of-scan handler when the pipeline's ScanRunner
    completes: ``on_complete_cb(meta)`` with the scan's ScanMetadata."""

    channels: set = frozenset()  # no data channels: lifecycle only

    def __init__(self, on_complete_cb):
        self._on_complete_cb = on_complete_cb
        self._meta = None

    def on_scan_start(self, meta) -> None:
        self._meta = meta

    def on_complete(self) -> None:
        try:
            self._on_complete_cb(self._meta)
        except Exception:
            logger.exception("_CompletionSink.on_complete callback raised")


class ScanOutcome(NamedTuple):
    kind: str       # "ok" | "partial" | "empty" | "skipped"
    severity: str   # "warning" | "error"  ("" when no alert)
    message: str    # "" when no alert should be shown


def classify_scan_outcome(*, final_frames: int, terminal_dark_missing: bool,
                          canceled: bool, disable_laser: bool) -> ScanOutcome:
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
    if disable_laser or (final_frames <= 0 and canceled):
        return ScanOutcome("skipped", "", "")
    if final_frames <= 0:
        return ScanOutcome(
            "empty", "error",
            "Scan ended unexpectedly and no data was recorded (the device "
            "may have disconnected mid-scan). This scan was not saved.")
    if terminal_dark_missing and not canceled:
        return ScanOutcome(
            "partial", "warning",
            "Scan ended unexpectedly — partial data was saved. The final "
            "segment could not be dark-corrected and was discarded.")
    return ScanOutcome("ok", "", "")


SCAN_TERMINATION_OK = "ok"            # ran to its configured end
SCAN_TERMINATION_STOPPED = "stopped"  # operator stop, no fault
SCAN_TERMINATION_ABORTED = "aborted"  # a fault ended it (error_code/reason)


def audit_scan_ended_details(*, outcome: Optional[ScanOutcome], canceled: bool,
                             abort_code: Optional[str], abort_reason: str = "") -> Dict[str, Any]:
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
    termination, reason = SCAN_TERMINATION_ABORTED, ""
    if abort_code:
        reason = abort_reason
    elif outcome is not None and outcome.severity:
        reason = outcome.message
    else:
        termination = SCAN_TERMINATION_STOPPED if canceled else SCAN_TERMINATION_OK
    return {
        "outcome": termination,
        "data": outcome.kind if outcome is not None else None,
        "error_code": abort_code or None,
        "reason": reason or None,
    }


class _ScanOutcomeSink(_Sink):
    """Tallies the two signals classify_scan_outcome needs; the completion
    handler reads .final_frames / .terminal_dark_missing off the instance.

      "final"       — corrected intervals (EnrichedCorrectedInterval). Summing
                      their .frames counts = corrected frames persisted by
                      ScanDBSink (same channel, same payloads).
      "diagnostics" — integrity events; a TerminalDarkResult with found=False
                      means a camera's terminal dark was missing/contaminated.
    """

    channels = frozenset({"final", "diagnostics"})

    def __init__(self) -> None:
        self.on_scan_start(None)

    def on_scan_start(self, meta) -> None:
        self.final_frames = 0
        self.terminal_dark_missing = False

    def consume(self, channel: str, payload) -> None:
        if channel == "final":
            frames = getattr(payload, "frames", None)
            if frames:
                self.final_frames += len(frames)
        elif channel == "diagnostics":
            if isinstance(payload, TerminalDarkResult) and not payload.found:
                self.terminal_dark_missing = True
