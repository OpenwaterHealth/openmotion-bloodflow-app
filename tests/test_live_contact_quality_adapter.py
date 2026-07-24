"""Unit tests for the live contact-quality adapter (issue #364).

The SDK's ContactQualityMonitor reports transitions on the pipeline runner
thread; the connector's only job is to marshal them to the main thread and
translate SDK vocabulary into what ContactQualityModal expects.
"""

import pytest

pytestmark = pytest.mark.unit


def test_cq_live_debounce_frames_is_shipped_and_whitelisted(tmp_path, monkeypatch):
    """Both halves must hold or the key is silently non-persistent: it must
    ship in config/app_config.json AND appear in the in-code defaults inside
    _load_app_config(), which filters the file and the runtime overrides to
    that whitelist. A key registered in only one place looks fine until a
    user toggles it and the change evaporates."""
    import json
    from pathlib import Path

    import main as app_main
    from utils import config_store

    repo_root = Path(__file__).resolve().parents[1]
    shipped_path = repo_root / "config" / "app_config.json"
    shipped = json.loads(shipped_path.read_text(encoding="utf-8"))

    # Half 1: the key ships in the config file.
    assert shipped["cq_live_debounce_frames"] == 80

    # Half 2: it survives the whitelist filter. Pin the loader at the shipped
    # file and redirect the writable-overrides layer at tmp_path so a local
    # app_config.local.json left over from running the app can't skew this.
    real_resource_path = config_store.resource_path

    def fake_resource_path(*parts):
        if parts == ("config", "app_config.json"):
            return shipped_path
        return real_resource_path(*parts)

    monkeypatch.setattr(config_store, "resource_path", fake_resource_path)
    monkeypatch.setenv("OPENWATER_DATA_ROOT", str(tmp_path))

    assert app_main._load_app_config()["cq_live_debounce_frames"] == 80


def _connector():
    """A MotionConnector instance without running __init__ — we exercise the
    adapter methods only, and __init__ builds hardware/threads."""
    from motion_connector import MotionConnector

    return MotionConnector.__new__(MotionConnector)


def test_adapter_maps_side_and_cam_to_display_label():
    conn = _connector()
    emitted = []
    conn.contactQualityWarning = type(
        "S", (), {"emit": lambda _s, *a: emitted.append(("warn", a))}
    )()
    conn.contactQualityIssueStateChanged = type(
        "S", (), {"emit": lambda _s, *a: emitted.append(("state", a))}
    )()

    conn._on_cq_transition_main("right", 6, "poor_contact", 2.5, True)

    assert emitted == [
        ("warn", ("R7", "poor_contact", "Poor sensor contact", 2.5)),
        ("state", ("R7", "poor_contact", "Poor sensor contact", 2.5, True)),
    ]


def test_adapter_clear_edge_emits_state_only_not_a_new_warning():
    conn = _connector()
    emitted = []
    conn.contactQualityWarning = type(
        "S", (), {"emit": lambda _s, *a: emitted.append(("warn", a))}
    )()
    conn.contactQualityIssueStateChanged = type(
        "S", (), {"emit": lambda _s, *a: emitted.append(("state", a))}
    )()

    conn._on_cq_transition_main("left", 0, "ambient_light", 9.0, False)

    assert emitted == [
        ("state", ("L1", "ambient_light", "Ambient light detected", 9.0, False)),
    ]


def test_adapter_collapses_no_signal_to_poor_contact():
    """The modal knows two type keys; the preflight already collapses
    no_signal the same way, so live and preflight wording agree."""
    conn = _connector()
    emitted = []
    conn.contactQualityWarning = type(
        "S", (), {"emit": lambda _s, *a: emitted.append(a)}
    )()
    conn.contactQualityIssueStateChanged = type("S", (), {"emit": lambda _s, *a: None})()

    conn._on_cq_transition_main("left", 3, "no_signal", 0.0, True)

    assert emitted == [("L4", "poor_contact", "Poor sensor contact", 0.0)]


def test_marshal_forwards_transition_with_coerced_types():
    """The runner-thread half must hop to the main thread and nothing else.
    It is the production entry point, and its broad except would otherwise
    hide exactly the silent failure this feature exists to fix (#364)."""
    conn = _connector()
    emitted = []
    conn._cqTransitionSignal = type(
        "S", (), {"emit": lambda _s, *a: emitted.append(a)}
    )()

    conn._on_cq_transition("left", 0, "poor_contact", 1.0, True)

    assert emitted == [("left", 0, "poor_contact", 1.0, True)]
    side, cam_id, reason, value, active = emitted[0]
    assert isinstance(cam_id, int) and isinstance(value, float)
    assert isinstance(active, bool) and isinstance(side, str)


def _make_scan_connector(tmp_path, app_config):
    """Full MotionConnector with a mocked hardware interface — same pattern
    as test_scan_mask_disconnected_gate.py / test_telemetry_dev_gate.py.
    Needed here (rather than the no-init _connector() helper above) because
    these tests exercise startCapture end-to-end and inspect the
    ScanRequest.sinks list it builds, not just the adapter methods."""
    from unittest.mock import MagicMock

    from motion_connector import MotionConnector

    iface = MagicMock()
    iface.console = MagicMock()
    iface.left = MagicMock()
    iface.right = MagicMock()
    iface.is_device_connected.return_value = (True, True, True)
    iface.scan_workflow = MagicMock()
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.start_scan.return_value = True
    # LiveScanSource treats scan_db_path as an optional path string; a bare
    # MagicMock attribute would leak into os.path calls.
    iface.scan_db_path = None

    c = MotionConnector(
        interface=iface,
        app_config=app_config,
        data_dir=str(tmp_path),
        config_dir="config",
    )
    c._consoleConnected = True
    c._leftSensorConnected = True
    c._rightSensorConnected = True
    return c


def _captured_scan_request(connector):
    ok = connector.startCapture("subj", 5, 0x66, 0x66, False)
    assert ok is True
    connector._interface.start_scan.assert_called_once()
    return connector._interface.start_scan.call_args.args[0]


def test_startcapture_attaches_exactly_one_contact_quality_monitor_from_config(tmp_path):
    """The regression this whole feature exists to fix (#364): the sink
    must actually reach ScanRequest.sinks, and it must be built from
    config rather than silently from the hardcoded defaults — using
    distinguishable values proves the construction actually read
    self._app_config rather than happening to pass with defaults that
    look plausible either way."""
    from omotion.contact_quality import ContactQualityMonitor

    dark = [1.1, 2.2, 3.3, 4.4, 5.5, 6.6, 7.7, 8.8]
    light = [10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8]
    connector = _make_scan_connector(tmp_path, {
        "cq_dark_threshold_per_camera": dark,
        "cq_light_threshold_per_camera": light,
        "cq_rolling_avg_window": 7,
        "cq_live_debounce_frames": 33,
    })

    req = _captured_scan_request(connector)

    cq_sinks = [s for s in req.sinks if isinstance(s, ContactQualityMonitor)]
    assert len(cq_sinks) == 1, (
        f"expected exactly one ContactQualityMonitor in sinks, found "
        f"{len(cq_sinks)} among {[type(s).__name__ for s in req.sinks]}"
    )
    sink = cq_sinks[0]
    assert sink._thresholds.dark == tuple(dark)
    assert sink._thresholds.light == tuple(light)
    assert sink._window_size == 7
    assert sink._light_debounce == 33
    # Plain bound methods aren't cached — connector._on_cq_transition
    # fetched here and the one startCapture fetched when constructing the
    # sink are two distinct MethodType objects (`is` is False even when
    # correct), but MethodType.__eq__ compares __func__ and __self__, which
    # is exactly "same underlying function bound to the same instance".
    assert sink._on_transition == connector._on_cq_transition
    assert sink._on_transition.__func__ is connector._on_cq_transition.__func__
    assert sink._on_transition.__self__ is connector


def test_startcapture_survives_null_light_debounce_config(tmp_path):
    """A key present in config with a JSON null value yields None from
    .get(), and int(None) raises TypeError — which would otherwise crash
    startCapture on the primary clinical scan path, contradicting the
    sink's own "must never abort a clinical scan in progress" contract.
    Must fall back to the shipped default instead of raising."""
    from omotion.contact_quality import ContactQualityMonitor

    connector = _make_scan_connector(tmp_path, {"cq_live_debounce_frames": None})

    req = _captured_scan_request(connector)

    cq_sinks = [s for s in req.sinks if isinstance(s, ContactQualityMonitor)]
    assert len(cq_sinks) == 1
    assert cq_sinks[0]._light_debounce == 80


def test_startcapture_survives_null_rolling_window_config(tmp_path):
    """Same null-config hazard as the debounce case above, for the
    sibling cq_rolling_avg_window key that feeds the same construction."""
    from motion_connector import _CQ_DEFAULT_ROLLING_WINDOW
    from omotion.contact_quality import ContactQualityMonitor

    connector = _make_scan_connector(tmp_path, {"cq_rolling_avg_window": None})

    req = _captured_scan_request(connector)

    cq_sinks = [s for s in req.sinks if isinstance(s, ContactQualityMonitor)]
    assert len(cq_sinks) == 1
    assert cq_sinks[0]._window_size == _CQ_DEFAULT_ROLLING_WINDOW


def test_startcapture_survives_malformed_threshold_array_element(tmp_path):
    """A null/non-numeric element inside either threshold array reaches
    float() inside CQThresholds.from_sequences and must degrade the whole
    construction to the shipped defaults rather than raise. Only the dark
    array is malformed here; the light key is left unset (its own default)
    to confirm the fallback is the documented combined one — both arrays
    go to defaults together, not just the offending one — so this is not
    mistaken for a partial fix if that ever changes."""
    from motion_connector import (
        _CQ_DEFAULT_DARK_THRESHOLD_DN,
        _CQ_DEFAULT_LIGHT_THRESHOLD_DN,
    )
    from omotion.contact_quality import ContactQualityMonitor

    connector = _make_scan_connector(tmp_path, {
        "cq_dark_threshold_per_camera": [3.0, None, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0],
    })

    req = _captured_scan_request(connector)

    cq_sinks = [s for s in req.sinks if isinstance(s, ContactQualityMonitor)]
    assert len(cq_sinks) == 1
    assert cq_sinks[0]._thresholds.dark == tuple([_CQ_DEFAULT_DARK_THRESHOLD_DN] * 8)
    assert cq_sinks[0]._thresholds.light == tuple([_CQ_DEFAULT_LIGHT_THRESHOLD_DN] * 8)


def test_marshal_coerces_numpy_scalar_types_to_plain_python():
    """The test above passes trivially even without the str/int/float/bool()
    casts in _on_cq_transition, because plain Python inputs already are what
    they claim to be. numpy scalar types are NOT subclasses of the builtins
    (numpy.int64 is not an int, numpy.bool_ is not a bool — confirmed with
    numpy 2.2.5), so feeding them in is what actually exercises the
    coercion. Realistic per the SDK's numpy-backed pipeline (e.g. a bare
    array index or a boolean array comparison reaching on_transition)."""
    import numpy as np

    conn = _connector()
    emitted = []
    conn._cqTransitionSignal = type(
        "S", (), {"emit": lambda _s, *a: emitted.append(a)}
    )()

    conn._on_cq_transition(
        "right", np.int64(2), "poor_contact", np.float64(3.5), np.bool_(True)
    )

    side, cam_id, reason, value, active = emitted[0]
    assert type(cam_id) is int and cam_id == 2
    assert type(value) is float and value == 3.5
    assert type(active) is bool and active is True
    assert type(side) is str and type(reason) is str
