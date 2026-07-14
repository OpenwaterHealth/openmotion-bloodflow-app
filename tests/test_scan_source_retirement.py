"""Connector-level retirement of superseded scan sources (leak fix).

Every LiveScanSource / PastScanSource is parented to the connector so QML
can read it safely; without an explicit release()+deleteLater() when a
source is superseded, each one lives for the app's lifetime (~46 MB of
buffers plus a running 10 Hz flush timer and an open scan-DB handle per
scan / History view). These tests pin the retirement rules in
_set_current_scan_source.
"""

from unittest.mock import MagicMock

import pytest

from motion_connector import MotionConnector
from data_sources import LiveScanSource, PastScanSource

pytestmark = pytest.mark.unit


def _connector(tmp_path):
    iface = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = None
    return MotionConnector(
        interface=iface, app_config={"engineeringMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )


def _past(session_id=1):
    return PastScanSource(scan_db=None, session_id=session_id,
                          preloaded_buffers={})


def test_navigating_between_past_scans_retires_the_previous(tmp_path):
    c = _connector(tmp_path)
    first = _past(1)
    second = _past(2)
    c._set_current_scan_source(first)
    c._set_current_scan_source(second)
    assert not first._flush_timer.isActive()  # retired
    assert second._flush_timer.isActive()     # current stays live


def test_back_to_live_retires_past_source_but_keeps_live(tmp_path):
    c = _connector(tmp_path)
    live = LiveScanSource(plot_t0=0.0, parent=c)
    c._live_scan_source = live
    c._set_current_scan_source(live)
    past = _past()
    c._set_current_scan_source(past)
    # Navigating away from live must NOT retire it — showLiveSource()
    # ("Back to live") needs it intact.
    assert live._flush_timer.isActive()
    c._set_current_scan_source(live)
    assert not past._flush_timer.isActive()  # past retired on the way back
    assert live._flush_timer.isActive()


def test_clearing_current_source_keeps_live_for_back_to_live(tmp_path):
    c = _connector(tmp_path)
    live = LiveScanSource(plot_t0=0.0, parent=c)
    c._live_scan_source = live
    c._set_current_scan_source(live)
    c._set_current_scan_source(None)  # scan teardown path
    assert live._flush_timer.isActive()


def test_same_instance_assignment_is_a_noop(tmp_path):
    c = _connector(tmp_path)
    past = _past()
    c._set_current_scan_source(past)
    c._set_current_scan_source(past)
    assert past._flush_timer.isActive()


def test_retire_scan_source_tolerates_release_failure(tmp_path):
    c = _connector(tmp_path)
    src = _past()

    def boom():
        raise RuntimeError("release failed")

    src.release = boom
    c._retire_scan_source(src)  # must not raise; still deleteLater()s
