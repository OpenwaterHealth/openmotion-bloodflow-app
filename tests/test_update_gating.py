"""Unified update gating (#386): _beta_enabled, _select_release, app beta path,
and the refresh/withdraw hook that ties engineering mode to the beta channel."""
import json as _json
import urllib.request
from unittest.mock import MagicMock

import pytest

import motion_connector
import version as version_mod
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit


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


@pytest.mark.parametrize("clinical,eng,beta,expected", [
    (False, True,  True,  True),    # research + eng + toggle -> beta
    (False, True,  False, False),   # toggle off
    (False, False, True,  False),   # eng off -> no beta even if toggle on
    (True,  True,  True,  False),   # clinical never opts in
])
def test_beta_enabled_matrix(tmp_path, clinical, eng, beta, expected):
    c = _connector(tmp_path, clinicalMode=clinical,
                   engineeringMode=eng, downloadBetaUpdates=beta)
    assert c._beta_enabled() is expected


# ── Refresh / withdraw on engineering-mode or beta-toggle change ──────────

def test_eng_mode_change_refreshes_both_updaters_in_research(tmp_path):
    c = _connector(tmp_path, clinicalMode=False, engineeringMode=False,
                   downloadBetaUpdates=False)
    fw, app = [], []
    c._refresh_firmware_update_check = lambda: fw.append(1)
    c.checkForUpdates = lambda: app.append(1)
    c.setConfig("engineeringMode", True)
    assert fw == [1], "firmware detection must re-run on engineering-mode change"
    assert app == [1], "app updater must re-check on engineering-mode change"


def test_eng_mode_change_makes_no_network_call_in_clinical(tmp_path):
    c = _connector(tmp_path, clinicalMode=True, engineeringMode=False,
                   downloadBetaUpdates=False)
    fw, app = [], []
    c._refresh_firmware_update_check = lambda: fw.append(1)
    c.checkForUpdates = lambda: app.append(1)
    c.setConfig("engineeringMode", True)
    assert fw == [1], "the refresh hook must fire on engineering-mode change"
    assert app == [], "clinical build must not make an outbound app-update call"


# ── _select_release: newest non-draft from a GitHub /releases list ────────

_STABLE = {"tag_name": "1.5.0", "draft": False, "prerelease": False, "assets": []}
_RC = {"tag_name": "1.6.0-rc.1", "draft": False, "prerelease": True, "assets": []}
_DRAFT = {"tag_name": "1.7.0", "draft": True, "prerelease": False, "assets": []}


def test_select_release_beta_takes_newest_non_draft():
    from motion_connector import _select_release
    # GitHub lists newest-first; the draft is skipped, the rc is taken.
    assert _select_release([_DRAFT, _RC, _STABLE], include_prerelease=True) is _RC


def test_select_release_stable_skips_prerelease():
    from motion_connector import _select_release
    assert _select_release([_DRAFT, _RC, _STABLE], include_prerelease=False) is _STABLE


def test_select_release_empty_is_none():
    from motion_connector import _select_release
    assert _select_release([], include_prerelease=True) is None
    assert _select_release([_DRAFT], include_prerelease=True) is None


# ── App self-updater beta channel (conditional /releases endpoint) ────────

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return _json.dumps(self._p).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_http(monkeypatch, payload, capture):
    def fake_urlopen(req, timeout=0):
        capture["url"] = req.full_url
        return _FakeResp(payload)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(version_mod, "get_version", lambda: "1.5.0")


def test_app_beta_offers_newest_prerelease(tmp_path, monkeypatch):
    releases = [
        {"tag_name": "1.6.0-rc.1", "draft": False, "prerelease": True,
         "assets": [{"name": "Open-Motion-Research-Setup-1.6.0-rc.1.exe",
                     "browser_download_url": "https://x/rc.exe"}]},
        {"tag_name": "1.5.0", "draft": False, "prerelease": False, "assets": []},
    ]
    cap = {}
    _patch_http(monkeypatch, releases, cap)
    c = _connector(tmp_path, clinicalMode=False, engineeringMode=True,
                   downloadBetaUpdates=True)
    out = []
    c.updateAvailable.connect(lambda v, u: out.append((v, u)))
    c._check_for_updates_worker()
    assert cap["url"].endswith("/releases")          # list endpoint, not /latest
    assert out == [("1.6.0-rc.1", "https://x/rc.exe")]


def test_app_stable_uses_latest_endpoint(tmp_path, monkeypatch):
    latest = {"tag_name": "1.5.0", "draft": False, "prerelease": False, "assets": []}
    cap = {}
    _patch_http(monkeypatch, latest, cap)
    c = _connector(tmp_path, clinicalMode=False, engineeringMode=False,
                   downloadBetaUpdates=False)
    seen = []
    c.updateNotAvailable.connect(lambda: seen.append("uptodate"))
    c._check_for_updates_worker()
    assert cap["url"].endswith("/releases/latest")
    assert seen == ["uptodate"]                      # 1.5.0 == local 1.5.0


# ── Clinical hard-block for the APP self-updater (Python backstop) ────────

def test_app_check_no_network_in_clinical(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(1))
    # Clinical + engineering unlocked + beta on: still no outbound call.
    c = _connector(tmp_path, clinicalMode=True, engineeringMode=True,
                   downloadBetaUpdates=True)
    emitted = []
    c.updateAvailable.connect(lambda v, u: emitted.append((v, u)))
    c._check_for_updates_worker()
    assert calls == [], "clinical build must make no outbound update request"
    assert emitted == []


def test_apply_update_refused_in_clinical(tmp_path, monkeypatch):
    c = _connector(tmp_path, clinicalMode=True)
    started = []
    monkeypatch.setattr(
        motion_connector.threading, "Thread",
        lambda **k: MagicMock(start=lambda: started.append(1)))
    c.applyUpdate("https://x/Open-Motion-Research-Setup-1.6.0-rc.1.exe")
    assert started == [], "clinical build must not start an app-update install"


# ── Withdraw an offered beta when engineering mode is turned OFF ──────────

def test_eng_off_withdraws_app_offer_synchronously(tmp_path):
    c = _connector(tmp_path, clinicalMode=False, engineeringMode=True,
                   downloadBetaUpdates=True)
    c.checkForUpdates = lambda: None   # don't spawn the real re-check thread
    withdrawn = []
    c.updateNotAvailable.connect(lambda: withdrawn.append(1))
    c.setConfig("engineeringMode", False)
    assert withdrawn == [1], "app offer must be withdrawn synchronously on eng-off"


def test_eng_off_withdraws_firmware_offer(tmp_path, monkeypatch):
    seen = {}

    def fake_check(kind, *, include_prerelease=False):
        seen["beta"] = include_prerelease
        return None

    monkeypatch.setattr(motion_connector, "check_latest", fake_check)
    c = _connector(tmp_path, clinicalMode=False, engineeringMode=True,
                   downloadBetaUpdates=True)
    c.checkForUpdates = lambda: None
    c._firmware_versions["left"] = "1.8.0"
    c._firmware_latest_by_kind["sensor"] = "1.8.1-dev.5"
    c._firmware_update_available["left"] = True
    monkeypatch.setattr(
        motion_connector.threading, "Thread",
        lambda **k: MagicMock(start=lambda: k["target"](*k["args"])))
    c.setConfig("engineeringMode", False)
    assert c._firmware_update_available["left"] is False   # stale beta withdrawn
    assert seen.get("beta") is False                       # re-checked on stable


def test_saveconfigs_refreshes_both_updaters(tmp_path):
    c = _connector(tmp_path, clinicalMode=False, engineeringMode=False,
                   downloadBetaUpdates=False)
    fw, app = [], []
    c._refresh_firmware_update_check = lambda: fw.append(1)
    c.checkForUpdates = lambda: app.append(1)
    c.saveConfigs({"engineeringMode": True})
    assert fw == [1]
    assert app == [1]


def test_app_beta_override_single_object_does_not_crash(tmp_path, monkeypatch):
    # An updateApiUrl override that returns a single release object (not a list)
    # on the beta channel must be handled by shape, not crash _select_release.
    obj = {"tag_name": "1.6.0-rc.1", "draft": False, "prerelease": True,
           "assets": [{"name": "Open-Motion-Research-Setup-1.6.0-rc.1.exe",
                       "browser_download_url": "https://x/rc.exe"}]}
    cap = {}
    _patch_http(monkeypatch, obj, cap)
    c = _connector(tmp_path, clinicalMode=False, engineeringMode=True,
                   downloadBetaUpdates=True, updateApiUrl="http://localhost:9/custom")
    out = []
    c.updateAvailable.connect(lambda v, u: out.append((v, u)))
    c._check_for_updates_worker()
    assert out == [("1.6.0-rc.1", "https://x/rc.exe")]
