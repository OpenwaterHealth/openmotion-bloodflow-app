import subprocess
import types
import pytest
import motion_connector


@pytest.mark.unit
def test_authenticode_status_parses_powershell_output(monkeypatch):
    def fake_run(*a, **k):
        return types.SimpleNamespace(stdout="Valid\n", returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert motion_connector._authenticode_status("C:/x.exe") == "Valid"


@pytest.mark.unit
def test_authenticode_status_returns_error_on_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("no powershell")
    monkeypatch.setattr(subprocess, "run", boom)
    assert motion_connector._authenticode_status("C:/x.exe") == "Error"
