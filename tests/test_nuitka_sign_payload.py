"""The Nuitka user plugin that signs the app's own binaries inside the onefile
payload (#579): Defender's ML model quarantined the unsigned ``main.dll`` that
the signed 1.5.3-rc.1 exe extracted to %TEMP%. The plugin only runs inside a
real Nuitka build on the signing runner, so its decisions are pinned here."""

import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
pytest.importorskip("nuitka", reason="the plugin subclasses Nuitka's plugin base")
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import nuitka_sign_payload as plugin_module  # noqa: E402


def _dist(tmp_path):
    dist = tmp_path / "main.dist"
    (dist / "PyQt6").mkdir(parents=True)
    for name in ("main.exe", "main.dll", "python312.dll", "Qt6Core.dll", "PyQt6/QtCore.pyd"):
        (dist / name).write_bytes(b"MZ")
    return dist


def _plugin(monkeypatch):
    plugin = plugin_module.NuitkaPluginSignPayload()
    said = []
    monkeypatch.setattr(plugin, "info", said.append, raising=False)

    def sysexit(message):
        raise SystemExit(message)

    monkeypatch.setattr(plugin, "sysexit", sysexit, raising=False)
    return plugin, said


def test_only_the_binaries_nuitka_compiled_from_the_app_are_selected(tmp_path):
    dist = _dist(tmp_path)
    found = plugin_module.own_binaries(str(dist), str(dist / "main.exe"))
    assert [Path(f).name for f in found] == ["main.dll", "main.exe"]


def test_without_a_thumbprint_nothing_is_signed(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    plugin, said = _plugin(monkeypatch)
    monkeypatch.delenv("CODESIGN_THUMBPRINT", raising=False)
    monkeypatch.setattr(plugin_module.subprocess, "run", lambda *a, **k: pytest.fail("signed"))
    plugin.onStandaloneDistributionFinished(str(dist))
    plugin.onStandaloneBinary(str(dist / "main.exe"))
    assert "payload left unsigned" in said[0]


def test_with_a_thumbprint_each_own_binary_goes_through_sign_ps1(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    plugin, _said = _plugin(monkeypatch)
    monkeypatch.setenv("CODESIGN_THUMBPRINT", "AB" * 20)
    calls = []

    def run(cmd, check):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(plugin_module.subprocess, "run", run)
    plugin.onStandaloneDistributionFinished(str(dist))
    plugin.onStandaloneBinary(str(dist / "main.exe"))
    # one sign.ps1 call per file: `powershell -File` cannot take an array
    assert [Path(c[-1]).name for c in calls] == ["main.dll", "main.exe"]
    for cmd in calls:
        assert cmd[:2] == ["powershell", "-NoProfile"]
        assert Path(cmd[cmd.index("-File") + 1]) == REPO_ROOT / "installer" / "sign.ps1"


def test_a_failed_signing_fails_the_build(tmp_path, monkeypatch):
    dist = _dist(tmp_path)
    plugin, _said = _plugin(monkeypatch)
    monkeypatch.setenv("CODESIGN_THUMBPRINT", "AB" * 20)
    monkeypatch.setattr(plugin_module.subprocess, "run",
                        lambda cmd, check: types.SimpleNamespace(returncode=1))
    plugin.onStandaloneDistributionFinished(str(dist))
    with pytest.raises(SystemExit, match="sign.ps1 failed"):
        plugin.onStandaloneBinary(str(dist / "main.exe"))


def test_an_empty_dist_is_an_error_not_a_silent_unsigned_build(tmp_path, monkeypatch):
    plugin, _said = _plugin(monkeypatch)
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match="no app binaries"):
        plugin.onStandaloneBinary(str(tmp_path / "empty" / "main.exe"))


def test_the_build_wires_the_plugin_and_loads_the_cert_first():
    script = (REPO_ROOT / "scripts" / "build_nuitka.ps1").read_text(encoding="utf-8-sig")
    assert r'"--user-plugin=scripts\nuitka_sign_payload.py"' in script
    action = (REPO_ROOT / ".github" / "actions" / "windows-build" / "action.yml").read_text(encoding="utf-8")
    cert = action.index("- name: Set up eSigner CKA (EV code signing)")
    build = action.index("- name: Build with Nuitka (one build per variant)")
    assert cert < build, "the payload is signed during the Nuitka run, so the cert must be loaded before it"
    build_step = action[build:action.index("- name: Install WiX v5")]
    assert "CODESIGN_THUMBPRINT: ${{ env.CODESIGN_THUMBPRINT || inputs.codesign-thumbprint }}" in build_step
