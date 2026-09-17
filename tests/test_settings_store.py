"""Unit tests for the scans.db settings table (#546).

Plaintext database here (the encryption policy is off in unit tests); on a
clinical build the same code runs through the SDK's db_open against the
SQLCipher file, which is what makes the store tamper-evident.
"""
import sqlite3

import pytest

from utils.settings_store import SettingsStore

pytestmark = pytest.mark.unit


def test_round_trip_save_load_delete(tmp_path):
    store = SettingsStore(tmp_path / "scans.db")
    assert store.enabled
    assert store.load() == {}
    assert store.save({"bfiMax": 5.0, "altCameraGains": [1, 2], "darkMode": False})
    assert store.load() == {"bfiMax": 5.0, "altCameraGains": [1, 2], "darkMode": False}
    assert store.save({"bfiMax": 6.0})                      # upsert
    assert store.load()["bfiMax"] == 6.0
    assert store.delete(["bfiMax", "neverThere"])
    assert store.load() == {"altCameraGains": [1, 2], "darkMode": False}
    store.close()
    assert not store.enabled


def test_values_survive_reopen(tmp_path):
    path = tmp_path / "scans.db"
    SettingsStore(path).save({"plotWindowSec": 30})
    assert SettingsStore(path).load() == {"plotWindowSec": 30}


def test_table_coexists_with_other_tables(tmp_path):
    """Same file as the scan data and the audit ``logs`` table."""
    path = tmp_path / "scans.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, event_type TEXT)")
        conn.execute("INSERT INTO logs (event_type) VALUES ('system_startup')")
    store = SettingsStore(path)
    store.save({"darkMode": False})
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM logs").fetchone()[0] == 1
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    assert rows == [("darkMode", "false")]


def test_disabled_store_is_a_noop():
    store = SettingsStore(None)
    assert not store.enabled
    assert store.load() == {}
    assert store.save({"bfiMax": 1.0}) is False
    assert store.delete(["bfiMax"]) is False


def test_unserializable_value_loses_only_that_write(tmp_path):
    store = SettingsStore(tmp_path / "scans.db")
    circular = {}
    circular["self"] = circular            # json.dumps raises even with default=str
    assert store.save({"bfiMax": 1.0, "bad": circular}) is False   # not every row written
    loaded = store.load()
    assert loaded == {"bfiMax": 1.0}       # the bad row is lost, the good one lands
    # an odd-but-serializable object is stringified (default=str), never a crash
    assert store.save({"obj": object()}) is True
    assert store.load()["obj"].startswith("<object")


def test_undecodable_row_is_skipped(tmp_path):
    path = tmp_path / "scans.db"
    store = SettingsStore(path)
    store.save({"bfiMax": 1.0})
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO settings VALUES ('broken', '{not json', 0)")
    assert SettingsStore(path).load() == {"bfiMax": 1.0}


def test_unopenable_path_degrades_to_session_only(tmp_path):
    store = SettingsStore(tmp_path / "no" / "such" / "dir" / "scans.db")
    assert not store.enabled
    assert store.load() == {}
