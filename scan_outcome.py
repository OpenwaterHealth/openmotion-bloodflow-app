# scan_outcome.py
"""Pure scan-outcome classification + the data-channel sink that feeds it.

Extracted from motion_connector.py (already ~4000 lines) so the decision
logic is unit-testable without Qt or hardware. See
docs/superpowers/plans/2026-06-15-interrupted-scan-handling.md.

An "interval" is a dark-bounded segment the dark-correction stage emits on
the pipeline's "final" channel; ScanDBSink persists those frames. The final,
still-open interval is always discarded (no terminal dark closes it), so an
unclean shutdown loses its tail — and a scan that ends before *any* interval
closes persists nothing at all. This module turns the two facts the sink can
observe (corrected frames persisted, and whether any terminal dark was
missing) into a user-facing outcome — no wall-clock thresholds.
"""

from __future__ import annotations

from typing import NamedTuple


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

    Pure Python — no Qt, no connector reference — so the completion handler
    reads .final_frames / .terminal_dark_missing directly off the instance
    after the scan.

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
            except Exception:
                return
            if isinstance(payload, TerminalDarkResult) and not payload.found:
                self.terminal_dark_missing = True

    def on_complete(self) -> None:
        pass
