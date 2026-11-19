"""A clinical build never runs on a plaintext scans.db (#683).

``main()`` turns the SDK's encryption policy on by passing
``require_encrypted_db=clinicalMode`` to the one ``MotionInterface`` it
constructs, after which the SDK creates scans.db encrypted and refuses to
open a plaintext one. Before that, a clinical launch checks scans.db with
``_scan_db_is_encrypted``; a plaintext file is reported as E-108 through the
critical-error modal once the window is up.

The startup tests run the real ``main()`` up to the MotionInterface line
with a stand-in interface that records what it was given, and every file
lives under tmp_path. Nothing here reaches the real Windows Credential
Manager: db_key's keystore seam is an in-memory stand-in.
"""
import hashlib
import importlib
import logging
import re
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import error_codes
from config import app_config as compiled
from utils import app_paths, config_store

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


class _Stop(Exception):
    """Raised by the MotionInterface stand-in to end the startup run."""


@pytest.fixture
def run_main(tmp_path, monkeypatch):
    """Run ``main()`` with ``argv`` until it constructs MotionInterface.

    Returns the startup events in order: ``("check", <result>)`` for each
    ``_scan_db_is_encrypted`` call and ``("MotionInterface", <kwargs>)``.

    tmp_path is requested before sys.platform is pinned (see
    test_main_config_wiring.py). win32 is the clinical platform: on darwin
    main forces Research.
    """
    main = importlib.import_module("main")
    events = []
    real_check = main._scan_db_is_encrypted

    def _check(scan_db_path):
        result = real_check(scan_db_path)
        events.append(("check", result))
        return result

    class _Interface:
        def __init__(self, **kwargs):
            events.append(("MotionInterface", kwargs))
            raise _Stop

    monkeypatch.setattr(main, "_scan_db_is_encrypted", _check)
    monkeypatch.setattr(main, "MotionInterface", _Interface)
    monkeypatch.setattr(main, "_pin_qt_environment", lambda: [])
    monkeypatch.setattr(main, "check_single_instance", lambda *a, **k: True)
    monkeypatch.setattr(app_paths, "DATA_ROOT_OVERRIDE", tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")

    # main() attaches console and file handlers to its own logger and the
    # SDK's; put both back so the log file closes and later tests see the
    # loggers as they were.
    loggers = [main.logger, logging.getLogger("openmotion.sdk")]
    saved = [(lg, list(lg.handlers), lg.level, lg.propagate) for lg in loggers]

    def _run(*argv):
        monkeypatch.setattr(sys, "argv", ["main.py", *argv])
        with pytest.raises(_Stop):
            main.main()
        return events

    yield _run
    for lg, handlers, level, propagate in saved:
        for handler in lg.handlers:
            if handler not in handlers:
                handler.close()
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


@pytest.fixture
def clinical_policy(monkeypatch):
    """The SDK's clinical encryption policy, with an in-memory keystore."""
    from omotion import db_key

    class _WinVaultStandIn:
        pass

    _WinVaultStandIn.__module__ = "keyring.backends.Windows"
    store = {}
    keyring = types.SimpleNamespace(
        get_keyring=_WinVaultStandIn,
        get_password=lambda service, entry: store.get((service, entry)),
        set_password=lambda service, entry, value: store.__setitem__((service, entry), value),
    )
    monkeypatch.setattr(db_key, "_keyring", lambda: keyring)
    monkeypatch.setattr(db_key, "_require_encryption", None)
    monkeypatch.setattr(sys, "platform", "win32")
    db_key.set_policy(require_encryption=True)
    return store


def _scan_db(tmp_path) -> Path:
    path = tmp_path / app_paths.DATA_DIRNAME / "scans.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _plaintext_scans_db(tmp_path) -> Path:
    """What a Research install or a pre-1.5.0 version leaves behind."""
    path = _scan_db(tmp_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, subject TEXT)")
        conn.execute("INSERT INTO sessions (subject) VALUES ('SUBJ-683')")
        conn.commit()
    finally:
        conn.close()
    return path


def _encrypted_looking_scans_db(tmp_path) -> Path:
    """A file without the SQLite magic, which is how SQLCipher's output
    looks on disk (its header is encrypted)."""
    path = _scan_db(tmp_path)
    path.write_bytes(bytes(range(256)) * 16)
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Startup: the check runs before MotionInterface, on clinical builds only
# --------------------------------------------------------------------------

@pytest.mark.parametrize("argv, stamped", [
    pytest.param(("--clinical",), False, id="source-run-clinical-flag"),
    pytest.param((), True, id="clinical-build-stamp"),
])
def test_clinical_startup_checks_scans_db_before_motion_interface(
    run_main, monkeypatch, tmp_path, argv, stamped,
):
    monkeypatch.setitem(compiled.APP_CONFIG, "clinicalMode", stamped)
    db = _plaintext_scans_db(tmp_path)
    before = _digest(db)

    events = run_main(*argv)

    assert [name for name, _ in events] == ["check", "MotionInterface"]
    assert events[0][1] is False                      # plaintext found
    assert events[1][1]["require_encrypted_db"] is True
    assert events[1][1]["scan_db_path"] == str(db)
    assert _digest(db) == before


@pytest.mark.parametrize("make_db", [
    pytest.param(None, id="fresh-install"),
    pytest.param(_encrypted_looking_scans_db, id="encrypted-scans-db"),
])
def test_clinical_startup_passes_the_check_without_a_plaintext_db(
    run_main, tmp_path, make_db,
):
    if make_db is not None:
        make_db(tmp_path)

    events = run_main("--clinical")

    assert events[0] == ("check", True)
    assert events[1][1]["require_encrypted_db"] is True


def test_research_startup_skips_the_check(run_main, tmp_path):
    _plaintext_scans_db(tmp_path)

    events = run_main("--research")

    assert [name for name, _ in events] == ["MotionInterface"]
    assert events[0][1]["require_encrypted_db"] is False


# --------------------------------------------------------------------------
# The check itself
# --------------------------------------------------------------------------

def test_scan_db_is_encrypted_classifies_by_file_content(tmp_path):
    main = importlib.import_module("main")
    db = _scan_db(tmp_path)

    assert main._scan_db_is_encrypted(str(db))            # missing: created encrypted later
    db.write_bytes(b"")
    assert main._scan_db_is_encrypted(str(db))            # empty: same
    db.unlink()
    _plaintext_scans_db(tmp_path)
    assert not main._scan_db_is_encrypted(str(db))


def test_scan_db_is_encrypted_accepts_what_the_sdk_writes(tmp_path, clinical_policy):
    pytest.importorskip("sqlcipher3")
    from omotion import db_open

    db = _scan_db(tmp_path)
    conn = db_open.connect(db)
    try:
        conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    main = importlib.import_module("main")
    assert main._scan_db_is_encrypted(str(db))


# --------------------------------------------------------------------------
# E-108 goes through the normal critical-error modal
# --------------------------------------------------------------------------

def test_raise_startup_error_emits_the_e108_modal(tmp_path):
    from motion_connector import MotionConnector

    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_db_path = None
    conn = MotionConnector(
        interface=iface, app_config={"engineeringMode": False},
        data_dir=str(tmp_path), config_dir="config",
    )
    received = []
    conn.criticalErrorRaised.connect(lambda *a: received.append(a))

    conn.raise_startup_error("E-108", detail="scans.db is a plaintext database.")

    entry = error_codes.lookup("E-108")
    assert received == [(
        "E-108", entry.title, entry.message, entry.suggested_action,
        "scans.db is a plaintext database.",
    )]


def test_after_e108_nothing_opens_the_plaintext_scans_db(tmp_path, clinical_policy):
    """The app stays open after E-108. Under the clinical policy every store
    it opens refuses the plaintext file, which stays byte-identical."""
    from audit_log import AuditLog
    from omotion import db_key
    from omotion.ScanDatabase import ScanDatabase
    from utils.settings_store import SettingsStore

    db = _plaintext_scans_db(tmp_path)
    before = _digest(db)

    settings = SettingsStore(db)
    assert not settings.enabled                  # preferences: session-only
    assert settings.load() == {}
    assert not AuditLog(db).enabled              # audit log refused
    with pytest.raises(db_key.EncryptionUnavailable):
        ScanDatabase(db_path=str(db))            # history, replay, export, scan pre-flight
    assert _digest(db) == before
    for sidecar in ("-wal", "-shm"):
        assert not db.with_name(db.name + sidecar).exists()


# --------------------------------------------------------------------------
# Nothing can turn it off or route around it
# --------------------------------------------------------------------------

def test_clinical_mode_cannot_change_at_runtime():
    """A runtime config write of clinicalMode is refused, and the settings
    table in scans.db can never carry it."""
    assert config_store.tier_of("clinicalMode") == compiled.CONSTANT
    assert config_store.refused_keys(["clinicalMode"]) == ["clinicalMode"]
    assert not config_store.is_persisted("clinicalMode")
    cfg = {"clinicalMode": True}
    assert config_store.apply_saved_preferences(cfg, {"clinicalMode": False}) == set()
    assert cfg["clinicalMode"] is True


# vendor/: build inputs of vendored third-party binaries (#682). Its tooling
# imports sqlcipher3 to check the rebuilt module, never opens scans.db and is
# never frozen into the app.
_SKIP_DIRS = {"build", "dist", "docs", "tests", "investigations", "sandbox", "vendor", "__pycache__"}


def _app_modules():
    for path in REPO_ROOT.rglob("*.py"):
        parts = path.relative_to(REPO_ROOT).parts
        if any(p in _SKIP_DIRS or p.startswith(".") for p in parts[:-1]):
            continue
        yield path


def test_app_code_never_opens_sqlite_around_the_sdk():
    """Every scans.db open must go through omotion.db_open (directly or via
    ScanDatabase), which is what applies the policy. A stdlib or sqlcipher3
    connect in app code would bypass it, and a second MotionInterface or a
    set_policy call would reset the process-wide policy after startup."""
    direct_open = re.compile(r"\bsqlite3\s*\.\s*connect\s*\(|\bsqlcipher3\b|\bdbapi2\b")
    policy_reset = re.compile(r"\bset_policy\s*\(|\bMotionInterface\(")
    offenders = []
    for path in _app_modules():
        rel = path.relative_to(REPO_ROOT).as_posix()
        src = path.read_text(encoding="utf-8", errors="replace")
        if direct_open.search(src):
            offenders.append(f"{rel}: opens SQLite directly")
        constructs = len(policy_reset.findall(src))
        if constructs and not (rel == "main.py" and constructs == 1):
            offenders.append(f"{rel}: sets the encryption policy")
    assert offenders == [], offenders
