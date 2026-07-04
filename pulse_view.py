"""Pulse-view glue: the pipeline sink that feeds the pulse waveform view.

Kept out of ``motion_connector.py`` (already ~4,700 lines) — the connector just
owns an instance and forwards its output to QML via ``pulseSnapshot``.

* ``PulseSink`` — subscribes to the SDK pipeline's ``"pulse"`` channel and
  forwards each ``PulseAnalysis`` to an ``emit_fn`` callback (the connector
  wires this to the ``pulseSnapshot`` signal). Decoupled from the connector so
  it is trivially unit-testable.

The pulse view is purely scan-driven: it only ever shows data the running scan
produced (real sensor data, or the engineering demo-mode replay, both of which
flow through this same channel). There is no in-app synthetic generator.
"""

from __future__ import annotations

from typing import Callable


class PulseSink:
    """Pipeline sink for the ``"pulse"`` channel → ``emit_fn(PulseAnalysis)``."""

    channels = {"pulse"}

    def __init__(self, emit_fn: Callable[[object], None]):
        self._emit = emit_fn

    def on_scan_start(self, meta) -> None:  # pipeline lifecycle hook
        pass

    def consume(self, channel: str, payload) -> None:
        if channel == "pulse":
            self._emit(payload)

    def on_complete(self) -> None:
        pass
