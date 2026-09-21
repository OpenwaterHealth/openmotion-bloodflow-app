"""Connector-side persistence contract (#546): preferences go to the
settings store as a diff against the compiled baseline; session and
constant keys never do; a runtime write to a constant is refused and
audited."""
import pytest

from motion_connector import MotionConnector
from utils import config_store

pytestmark = pytest.mark.unit


class _Store:
    def __init__(self):
        self.saved = {}
        self.deleted = []
        self.enabled = True
        self.path = "fake"

    def save(self, values):
        self.saved.update(values)
        return True

    def delete(self, keys):
        self.deleted.extend(keys)
        return True


class _Audit:
    def __init__(self):
        self.events = []

    def log(self, event_type, details=None):
        self.events.append((event_type, details))


def _connector(store=None):
    # MotionConnector.__init__ wires hardware/telemetry, so bypass it with
    # __new__ and set only the attributes the config API reads.
    conn = MotionConnector.__new__(MotionConnector)
    conn._baseline_config = config_store.compiled_config()
    conn._app_config = config_store.compiled_config()
    conn._settings_store = store
    conn._audit = _Audit()
    return conn


def test_save_app_config_persists_only_changed_preferences_and_state():
    store = _Store()
    conn = _connector(store)
    conn._app_config["bfiMax"] = 5.0                  # preference
    conn._app_config["altCameraSettingsDirty"] = True # state
    conn._app_config["engineeringMode"] = True        # session: never persisted
    conn._app_config["tecTripTempC"] = 99             # constant: never persisted

    conn._save_app_config()

    assert store.saved == {"bfiMax": 5.0, "altCameraSettingsDirty": True}
    assert "leftMask" in store.deleted                # at compiled value → row cleared
    assert "engineeringMode" not in store.deleted
    assert "tecTripTempC" not in store.deleted


def test_save_app_config_without_store_is_a_noop():
    conn = _connector(store=None)
    conn._app_config["bfiMax"] = 5.0
    conn._save_app_config()   # no raise


def test_set_config_refuses_constants_and_audits():
    store = _Store()
    conn = _connector(store)
    conn.setConfig("tecTripTempC", 99)
    assert conn._app_config["tecTripTempC"] == 40
    assert store.saved == {}
    assert conn._audit.events == [
        ("config_write_refused", {"source": "setConfig", "keys": ["tecTripTempC"]})
    ]


def test_set_config_refuses_unknown_keys():
    conn = _connector(_Store())
    conn.setConfig("someRetiredFlag", True)
    assert "someRetiredFlag" not in conn._app_config
    assert conn._audit.events[0][0] == "config_write_refused"


def test_refused_keys_helper_matches_tiers():
    assert config_store.refused_keys(["clinicalMode", "bfiMax"]) == ["clinicalMode"]
