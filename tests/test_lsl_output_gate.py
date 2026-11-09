"""LSL output is attached to a scan only when the Settings switch is on and
the build allows research output (same gate as the raw CSV), and a missing
liblsl never stops the scan from starting."""

from unittest.mock import patch

import pytest

import lsl_output
from lsl_output import LslStreamSink
from test_telemetry_dev_gate import _captured_request, _make_connector

pytestmark = pytest.mark.unit


def _lsl_sinks(req):
    return [s for s in req.sinks if isinstance(s, LslStreamSink)]


@pytest.mark.parametrize("cfg,attached", [
    ({"clinicalMode": False, "lslOutputEnabled": True}, True),
    ({"clinicalMode": True, "engineeringMode": True, "lslOutputEnabled": True}, True),
    ({"clinicalMode": True, "engineeringMode": False, "lslOutputEnabled": True}, False),
    ({"clinicalMode": False, "lslOutputEnabled": False}, False),
    ({"clinicalMode": False}, False),  # missing key fails closed
])
def test_lsl_sink_follows_switch_and_build_gate(tmp_path, cfg, attached):
    connector = _make_connector(tmp_path, cfg)
    with patch.object(lsl_output, "lsl_available", return_value=(True, "")):
        req = _captured_request(connector)
    assert len(_lsl_sinks(req)) == (1 if attached else 0)


def test_missing_liblsl_warns_and_still_starts_the_scan(tmp_path):
    connector = _make_connector(
        tmp_path, {"clinicalMode": False, "lslOutputEnabled": True})
    with patch.object(lsl_output, "lsl_available",
                      return_value=(False, "RuntimeError: no liblsl")), \
            patch.object(connector, "notify") as notify:
        req = _captured_request(connector)
    assert _lsl_sinks(req) == []
    notify.assert_called_once()
    assert "LSL output is unavailable" in notify.call_args.args[0]


def test_switch_slot_updates_runtime_and_persisted_config(tmp_path):
    connector = _make_connector(tmp_path, {"clinicalMode": False})
    connector.setLslOutputEnabled(True)
    assert connector._lsl_output_enabled is True
    assert connector._app_config["lslOutputEnabled"] is True
    with patch.object(lsl_output, "lsl_available", return_value=(True, "")):
        req = _captured_request(connector)
    assert len(_lsl_sinks(req)) == 1
