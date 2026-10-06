"""Engineering-mode unlock (#706). Two rules:

* A clinical build has the engineering toggle compiled out unless it is the
  service tool (``SERVICE_BUILD``): no ``engineering_unlock`` module, no
  EngineeringUnlockModal.qml, no logo double-click handler, no engineering
  password, and ``engineeringMode`` can't be turned on through any
  connector slot.
* A Research build (and the service tool) always has the toggle and keeps
  the plaintext engineering password, defined only in engineering_unlock.py.

Calibrate / Test also need engineering mode in Python, not just in the UI.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import engineering_unlock
import motion_connector
from config import app_config
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
GOOD = engineering_unlock._ENGINEERING_PASSWORD


@pytest.fixture(autouse=True)
def no_update_checks(monkeypatch):
    """Unlocking re-runs the update checks; on a Research connector that is
    a GitHub request from a worker thread. Keep these tests offline."""
    monkeypatch.setattr(MotionConnector, "_refresh_update_checks",
                        lambda self: None)


@pytest.fixture
def clinical_bundle(monkeypatch):
    """What the shipped clinical exe runs: the unlock module is absent."""
    monkeypatch.setattr(motion_connector, "engineering_unlock", None)


def _connector(tmp_path, **cfg):
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    config = {"clinicalMode": False, "engineeringMode": False}
    config.update(cfg)
    return MotionConnector(
        interface=iface, app_config=config,
        data_dir=str(tmp_path), config_dir="config",
    )


def _events(c, event_type):
    return [json.loads(e["details"] or "{}") for e in c._audit.query(limit=1000)
            if e["event_type"] == event_type]


def _engineering(c):
    return c._app_config.get("engineeringMode")


# ── Research build: the engineering password unlocks ─────────────────────

def test_research_build_offers_the_unlock(tmp_path):
    c = _connector(tmp_path)
    assert c.engineeringUnlockAvailable is True
    assert c.serviceBuild is False


def test_the_engineering_password_unlocks(tmp_path):
    c = _connector(tmp_path)
    changed = []
    c.appConfigChanged.connect(lambda: changed.append(1))

    assert c.unlockEngineeringMode(GOOD) is True

    assert _engineering(c) is True
    assert changed, "QML must see the flag change"
    assert len(_events(c, "engineering_mode_unlocked")) == 1


@pytest.mark.parametrize("pw", ["wrong", "", GOOD.lower(), GOOD + " "])
def test_a_wrong_password_is_refused_and_audited(tmp_path, pw):
    c = _connector(tmp_path)

    assert c.unlockEngineeringMode(pw) is False

    assert _engineering(c) is False
    assert len(_events(c, "engineering_unlock_failed")) == 1
    assert _events(c, "engineering_mode_unlocked") == []


@pytest.mark.parametrize("pw", [None, 123, GOOD.encode("utf-8")])
def test_password_matches_rejects_non_strings(pw):
    assert engineering_unlock.password_matches(pw) is False


def test_unlocking_refreshes_the_update_checks(tmp_path, monkeypatch):
    c = _connector(tmp_path)
    refreshed = []
    monkeypatch.setattr(c, "_refresh_update_checks", lambda: refreshed.append(1))
    c.unlockEngineeringMode(GOOD)
    assert refreshed == [1]


def test_the_lock_button_still_turns_it_off(tmp_path):
    c = _connector(tmp_path)
    c.unlockEngineeringMode(GOOD)
    c.setConfig("engineeringMode", False)
    assert _engineering(c) is False


# ── Every build: only the unlock slot turns it on ────────────────────────

@pytest.mark.parametrize("value", [True, 1, "true"])
def test_set_config_cannot_turn_engineering_mode_on(tmp_path, value):
    c = _connector(tmp_path)
    c.setConfig("engineeringMode", value)
    assert _engineering(c) is False
    refused = _events(c, "config_write_refused")
    assert refused and refused[-1]["keys"] == ["engineeringMode"]
    assert refused[-1]["source"] == "setConfig"


def test_save_configs_drops_engineering_mode_but_keeps_the_rest(tmp_path):
    c = _connector(tmp_path, writeTelemetryCsv=False)
    c.saveConfigs({"engineeringMode": True, "writeTelemetryCsv": True})
    assert _engineering(c) is False
    assert c._app_config["writeTelemetryCsv"] is True
    assert _events(c, "config_write_refused")[-1]["source"] == "saveConfigs"


# ── Clinical build: compiled out ─────────────────────────────────────────

def test_clinical_bundle_has_no_unlock(tmp_path, clinical_bundle):
    c = _connector(tmp_path, clinicalMode=True)
    assert c.engineeringUnlockAvailable is False

    assert c.unlockEngineeringMode(GOOD) is False

    assert _engineering(c) is False
    assert _events(c, "engineering_unlock_refused") == [
        {"reason": "not available in this build"}]
    assert _events(c, "engineering_unlock_failed") == []


def test_clinical_engineering_mode_stays_off_however_driven(tmp_path, clinical_bundle):
    c = _connector(tmp_path, clinicalMode=True)
    c.setConfig("engineeringMode", True)
    c.saveConfigs({"engineeringMode": True})
    c.unlockEngineeringMode(GOOD)
    c.unlockEngineeringMode("")
    assert _engineering(c) is False


def test_clinical_source_run_behaves_like_the_clinical_exe(tmp_path):
    # `python main.py --clinical`: the module is importable (the constant
    # is still the repo's False), but the runtime clinicalMode check holds.
    assert motion_connector.engineering_unlock is engineering_unlock
    c = _connector(tmp_path, clinicalMode=True)
    assert c.engineeringUnlockAvailable is False
    assert c.unlockEngineeringMode(GOOD) is False
    assert _engineering(c) is False


def test_clinical_service_build_keeps_the_unlock(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_connector, "_SERVICE_BUILD", True)
    c = _connector(tmp_path, clinicalMode=True)
    assert c.engineeringUnlockAvailable is True
    assert c.serviceBuild is True
    assert c.unlockEngineeringMode("wrong") is False
    assert c.unlockEngineeringMode(GOOD) is True
    assert _engineering(c) is True


def test_service_stamp_alone_does_not_label_a_research_build(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_connector, "_SERVICE_BUILD", True)
    c = _connector(tmp_path, clinicalMode=False)
    assert c.serviceBuild is False


# ── Calibrate / Test need engineering mode in Python ─────────────────────

@pytest.mark.parametrize("slot, action", [
    ("runCalibration", "calibrate"), ("runTestScan", "test_scan"),
])
def test_engineering_actions_refused_without_engineering_mode(tmp_path, slot, action):
    c = _connector(tmp_path)
    c._consoleConnected = True
    logs = []
    c.captureLog.connect(logs.append)

    getattr(c, slot)("both")

    assert _events(c, "engineering_action_refused") == [{"action": action}]
    assert c._calibration_status != "running"
    assert c._test_scan_status != "running"
    assert logs == [], "refused before any capture-log activity"


@pytest.mark.parametrize("slot", ["runCalibration", "runTestScan"])
def test_engineering_actions_pass_the_gate_when_unlocked(tmp_path, slot):
    c = _connector(tmp_path)
    c.unlockEngineeringMode(GOOD)
    logs = []
    c.captureLog.connect(logs.append)

    getattr(c, slot)("both")      # console not connected: the next check

    assert _events(c, "engineering_action_refused") == []
    assert any("console not connected" in line for line in logs)


# ── The password lives only in the compiled-out module ───────────────────

def _app_sources():
    skip = {"tests", ".claude", ".git", "build", "dist", "docs", "vendor"}
    for path in REPO_ROOT.rglob("*"):
        if path.suffix not in (".py", ".qml", ".spec", ".ps1"):
            continue
        if skip.intersection(path.relative_to(REPO_ROOT).parts):
            continue
        yield path


def test_the_engineering_password_is_only_in_engineering_unlock():
    literal = f'"{GOOD}"'
    for path in _app_sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in ("checkEngineeringPassword", "engineering_password_matches"):
            assert name not in text, f"{name} in {rel}"
        if rel == "engineering_unlock.py":
            continue
        assert "_ENGINEERING_PASSWORD" not in text, rel
        assert literal not in text, f"the engineering password literal is in {rel}"


def test_repo_stamps_are_the_research_defaults():
    assert app_config.CLINICAL_MODE is False
    assert app_config.SERVICE_BUILD is False
    text = (REPO_ROOT / "config" / "app_config.py").read_text(encoding="utf-8")
    assert re.search(r"^SERVICE_BUILD = False$", text, re.M)


def test_connector_import_is_gated_on_the_compiled_constants():
    src = (REPO_ROOT / "motion_connector.py").read_text(encoding="utf-8")
    assert re.search(
        r"if _CLINICAL_BUILD and not _SERVICE_BUILD:\n"
        r"    engineering_unlock = None\nelse:\n    try:\n"
        r"        import engineering_unlock\n", src,
    ), "engineering_unlock must only be imported when the build keeps it"
    assert src.count("import engineering_unlock") == 1
    assert "from engineering_unlock import" not in src
    # The connector itself never sets the flag on; only the module does.
    assert not re.search(r"""\["engineeringMode"\]\s*=\s*True""", src)


def test_pyinstaller_spec_leaves_the_unlock_out_of_a_clinical_bundle():
    spec = (REPO_ROOT / "openwater.spec").read_text(encoding="utf-8")
    assert "_has_engineering_unlock = not _is_clinical or _is_service" in spec
    assert re.search(r'if not _has_engineering_unlock:\n'
                     r'    _excludes\.append\("engineering_unlock"\)', spec)
    assert 'os.path.join("components", "EngineeringUnlockModal.qml")' in spec
    assert re.search(r"if not _has_engineering_unlock:\n"
                     r"    a\.datas = \[e for e in a\.datas if _norm\(e\[0\]\) != _UNLOCK_QML\]",
                     spec)


def test_nuitka_build_leaves_the_unlock_out_of_a_clinical_bundle():
    ps1 = (REPO_ROOT / "scripts" / "build_nuitka.ps1").read_text(encoding="utf-8")
    block = re.search(
        r'if \(\$Variant -eq "clinical" -and -not \$Service\) \{(.*?)\n\}',
        ps1, re.S)
    assert block, "clinical-without-service exclusion block missing"
    assert '"--nofollow-import-to=engineering_unlock"' in block.group(1)
    assert '"--noinclude-data-files=components/EngineeringUnlockModal.qml"' in block.group(1)
    assert "Set-BuildVariant -Clinical ($Variant -eq \"clinical\") -Service $Service.IsPresent" in ps1


def test_ci_refuses_a_service_stamp():
    action = (REPO_ROOT / ".github" / "actions" / "windows-build" / "action.yml"
              ).read_text(encoding="utf-8")
    assert "name: Refuse a service-tool stamp" in action
    assert '^SERVICE_BUILD = False[[:space:]]*$' in action
    code = "\n".join(line for line in action.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "build_nuitka.ps1" in code
    assert not re.search(r"build_nuitka\.ps1[^\n]*-Service", code), \
        "CI must never build the service tool"


def test_main_qml_loads_the_unlock_only_when_the_build_has_it():
    qml = (REPO_ROOT / "main.qml").read_text(encoding="utf-8")
    assert "EngineeringUnlockModal {" not in qml, \
        "a type reference would break a bundle without the file"
    assert "onLogoDoubleClicked" not in qml
    assert re.search(
        r"active: MotionInterface\.engineeringUnlockAvailable === true\s*\n"
        r'\s*source: active \? "components/EngineeringUnlockModal\.qml" : ""',
        qml)
    menu = (REPO_ROOT / "components" / "WindowMenu.qml").read_text(encoding="utf-8")
    assert "onDoubleClicked" not in menu
    assert "logoDoubleClicked" not in menu
