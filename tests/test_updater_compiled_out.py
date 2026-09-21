"""The app self-updater is compiled out of clinical builds (#543, M-02).

Before this, "no updater in clinical" was a runtime read of ``clinicalMode``
inside a connector that carried the whole updater in every variant (tracker
V-01). Now ``app_updater.py`` holds the implementation, ``motion_connector``
imports it only when the compiled ``CLINICAL_MODE`` constant is False, and
``openwater.spec`` excludes the module from a clinical bundle. The connector
keeps the slots and signals so QML is identical in both variants.
"""

import re
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import motion_connector
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]


def _connector(tmp_path, **cfg):
    iface = MagicMock()
    iface.is_device_connected.return_value = (False, False, False)
    iface.scan_workflow.running = False
    iface.scan_workflow.config_running = False
    iface.scan_db_path = str(tmp_path / "scans.db")
    iface.get_sdk_version.return_value = "9.9.9"
    return MotionConnector(
        interface=iface, app_config=cfg,
        data_dir=str(tmp_path), config_dir="config",
    )


def test_research_source_tree_binds_the_updater_module():
    """The repo constant is False (Research), so the module is bound."""
    import app_updater
    assert motion_connector.app_updater is app_updater


def test_check_and_apply_are_noops_without_the_module(tmp_path, monkeypatch):
    """What a clinical binary does: research-looking config, no module."""
    monkeypatch.setattr(motion_connector, "app_updater", None)
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(1))
    c = _connector(tmp_path, clinicalMode=False, engineeringMode=True,
                   downloadBetaUpdates=True)
    emitted = []
    c.updateAvailable.connect(lambda v, u: emitted.append(("avail", v, u)))
    c.updateNotAvailable.connect(lambda: emitted.append(("none",)))
    c.updateCheckFailed.connect(lambda e: emitted.append(("fail", e)))
    c._check_for_updates_worker()
    assert calls == [], "no outbound request without the updater module"
    assert emitted == [], "no signal either: nothing to offer, nothing failed"

    c._update_in_progress = True
    c._apply_update_worker("https://x/Open-Motion-Research-Setup-1.6.0.exe")
    assert c._update_in_progress is False, "re-entry flag must be released"
    assert emitted == []


def test_connector_import_is_gated_on_the_compiled_constant():
    src = (REPO_ROOT / "motion_connector.py").read_text(encoding="utf-8")
    assert "from config.app_config import CLINICAL_MODE as _CLINICAL_BUILD" in src
    assert re.search(
        r"if _CLINICAL_BUILD:\n    app_updater = None\nelse:\n    try:\n"
        r"        import app_updater\n", src,
    ), "app_updater must only be imported when the build is not clinical"
    # Nothing else in the connector may pull the module in unconditionally.
    assert src.count("import app_updater") == 1
    assert "from app_updater import" not in src
    # The helpers really left: a clinical connector must not carry them.
    for name in ("def _authenticode_status", "def _update_decision",
                 "def _build_update_helper_script", "_REQUIRE_SIGNED_UPDATES",
                 "urllib.request.urlretrieve"):
        assert name not in src, name


def test_spec_excludes_the_updater_from_clinical_builds():
    src = (REPO_ROOT / "openwater.spec").read_text(encoding="utf-8")
    assert re.search(r'if _is_clinical:\n    _excludes\.append\("app_updater"\)', src)
    assert "excludes=_excludes" in src


def test_app_updater_does_not_depend_on_the_connector():
    """No import cycle, and nothing that would drag the connector's hardware
    stack (the SDK) into the updater module."""
    import ast
    tree = ast.parse((REPO_ROOT / "app_updater.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "motion_connector" not in imported
    assert "omotion" not in imported
