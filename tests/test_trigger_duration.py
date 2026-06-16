"""Trigger-ON duration measurement (issue #201).

The scan-notes "duration" line must reflect the laser-on time the firmware
actually ran, derived from the SDK's scan-relative TriggerStateEvent
``timestamp_s`` — NOT the host's event-receive wall-clock. The OFF event can
sit queued behind histogram batches in the runner's diagnostics channel and be
consumed ~1 s after it was emitted; measuring receive-time inflated a 960 s
(16:00) scan to 16:01.
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector, _TriggerStateSink
from omotion.pipeline.batch import TriggerStateEvent

pytestmark = pytest.mark.unit


def _connector(tmp_path):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    return MotionConnector(
        interface=iface, app_config={"developerMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )


def _reset_trigger_state(c):
    c._trigger_cumulative_s = 0.0
    c._trigger_on_mono = None
    c._trigger_on_ts = None


# NOTE: these are intentionally module-level functions, not grouped in a
# class. The conftest's pytest_runtest_setup hook xfails every test after the
# first failure *within a class*, which would mask independent failures here.


def test_cumulative_uses_event_timestamp_delta(tmp_path):
    """A 960 s laser-on interval emitted at scan-relative ts 2.0 → 962.2
    must measure as 960.2 s regardless of how long the host took to
    consume the OFF event."""
    c = _connector(tmp_path)
    _reset_trigger_state(c)
    sink = _TriggerStateSink(c)

    # ON emitted 2.0 s into the scan (post-setup), OFF at 962.2 s.
    sink.consume("diagnostics", TriggerStateEvent(state="ON", timestamp_s=2.0))
    sink.consume("diagnostics", TriggerStateEvent(state="OFF", timestamp_s=962.2))

    assert c._trigger_cumulative_s == pytest.approx(960.2, abs=1e-6)


def test_formatted_duration_floors_to_configured(tmp_path):
    """A 960 s scan that overshoots by the guard's poll granularity must
    still display 00:16:00, not 00:16:01 (issue #201)."""
    c = _connector(tmp_path)
    _reset_trigger_state(c)
    sink = _TriggerStateSink(c)

    sink.consume("diagnostics", TriggerStateEvent(state="ON", timestamp_s=0.0))
    sink.consume("diagnostics", TriggerStateEvent(state="OFF", timestamp_s=960.2))

    assert c._scan_elapsed_str() == "00:16:00"


def test_falls_back_to_monotonic_when_timestamps_unavailable(tmp_path):
    """If the SDK could not stamp the events (both ts 0.0 — the t0-None
    fallback), the measurement must not silently collapse to a negative or
    garbage value; it falls back to host receive-time."""
    c = _connector(tmp_path)
    _reset_trigger_state(c)
    sink = _TriggerStateSink(c)

    sink.consume("diagnostics", TriggerStateEvent(state="ON", timestamp_s=0.0))
    # No usable timestamp delta -> receive-time delta (≈0 in this fast test)
    # is used; it must be non-negative, never a negative/garbage value.
    sink.consume("diagnostics", TriggerStateEvent(state="OFF", timestamp_s=0.0))

    assert c._trigger_cumulative_s >= 0.0
