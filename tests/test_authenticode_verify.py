"""Authenticode verification through WinVerifyTrust, publisher pinned (#544).

Replaces the PowerShell ``Get-AuthenticodeSignature`` call (V-10: the path
was interpolated into a command line) with a direct Win32 call, and turns
"any valid signature" (V-11) into "valid *and* signed by a pinned Openwater
certificate". The Win32 layer is exercised for real where Windows is
available; the policy layer is exercised with substituted results.
"""

import re
import shutil
import sys

import pytest

import app_updater
from app_updater import ACCEPTED_SIGNER_SHA256, SignatureInfo, _update_decision

pytestmark = pytest.mark.unit

PIN = next(iter(ACCEPTED_SIGNER_SHA256))
OPENWATER = "Open Water Internet, Inc."


def test_pin_is_a_set_of_sha256_thumbprints():
    assert ACCEPTED_SIGNER_SHA256
    for thumb in ACCEPTED_SIGNER_SHA256:
        assert re.fullmatch(r"[0-9A-F]{64}", thumb), thumb


def _fake_wintrust(monkeypatch, result):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(app_updater, "_verify_with_wintrust", lambda path: result)


def test_valid_signature_from_pinned_publisher_installs(monkeypatch):
    _fake_wintrust(monkeypatch, (0, PIN, OPENWATER))
    info = app_updater.verify_authenticode("C:/updates/Open-Motion-Research-Setup.exe")
    assert info == SignatureInfo("Valid", PIN, OPENWATER)
    assert _update_decision(info) == (True, None)


def test_valid_signature_from_another_publisher_is_refused(monkeypatch):
    other = "A" * 64
    _fake_wintrust(monkeypatch, (0, other, "Someone Else Ltd"))
    info = app_updater.verify_authenticode("C:/x.exe")
    assert info.status == "Valid"
    launch, err = _update_decision(info)
    assert launch is False
    assert "unexpected publisher" in err and "Someone Else Ltd" in err


def test_valid_signature_with_unreadable_signer_is_an_error(monkeypatch):
    _fake_wintrust(monkeypatch, (0, None, None))
    info = app_updater.verify_authenticode("C:/x.exe")
    assert info.status == "Error"
    assert _update_decision(info)[0] is False


@pytest.mark.parametrize("hresult,status", [
    (0x800B0100, "NotSigned"),
    (0x80096010, "HashMismatch"),
    (0x800B0003, "NotPE"),
    (0x800B0109, "UntrustedRoot"),
    (0x800B0101, "Expired"),
    (0x12345678, "0x12345678"),
])
def test_failed_verification_maps_the_hresult_and_refuses(monkeypatch, hresult, status):
    _fake_wintrust(monkeypatch, (hresult, None, None))
    info = app_updater.verify_authenticode("C:/x.exe")
    assert info.status == status
    launch, err = _update_decision(info)
    assert launch is False
    assert err


def test_unsigned_bundle_is_refused_and_points_at_the_releases_page(monkeypatch):
    _fake_wintrust(monkeypatch, (0x800B0100, None, None))
    launch, err = _update_decision(app_updater.verify_authenticode("C:/x.exe"))
    assert launch is False
    assert "not signed" in err.lower()
    assert "releases page" in err


def test_exception_in_the_win32_layer_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(path):
        raise OSError("wintrust.dll missing")
    monkeypatch.setattr(app_updater, "_verify_with_wintrust", boom)
    info = app_updater.verify_authenticode("C:/x.exe")
    assert info.status == "Error" and "wintrust.dll" in info.detail
    launch, err = _update_decision(info)
    assert launch is False and "could not be verified" in err


def test_non_windows_is_an_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert app_updater.verify_authenticode("/x.exe").status == "Error"


def test_no_powershell_signature_check_remains():
    """V-10: the check is a Win32 call; PowerShell appears only as the
    detached install helper, launched with -File on a path we wrote."""
    src = open(app_updater.__file__, encoding="utf-8").read()
    assert "Get-AuthenticodeSignature" not in src
    assert '"-Command"' not in src
    assert "_REQUIRE_SIGNED_UPDATES" not in src


# ── the real Win32 path ────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="WinVerifyTrust is Windows-only")
def test_real_winverifytrust_rejects_unsigned_inputs(tmp_path):
    """Exercises the ctypes bindings for real: a text file is not a PE, an
    unsigned PE has no signature, a missing file is not Valid. None may
    raise, and none may install."""
    info = app_updater.verify_authenticode(__file__)
    assert info.status in ("NotPE", "NotSigned"), info
    assert _update_decision(info)[0] is False

    fake_pe = tmp_path / "unsigned.exe"
    fake_pe.write_bytes(b"MZ" + b"\0" * 600)
    info = app_updater.verify_authenticode(str(fake_pe))
    assert info.status != "Valid" and info.status != "Error", info
    assert _update_decision(info)[0] is False

    info = app_updater.verify_authenticode(str(tmp_path / "missing.exe"))
    assert info.status != "Valid"


@pytest.mark.skipif(sys.platform != "win32", reason="WinVerifyTrust is Windows-only")
def test_real_winverifytrust_reads_the_signer_of_a_signed_binary():
    """Positive path through the chain helpers on a third-party signed
    binary when one is at hand (Git for Windows ships code-signed): the
    signer is identified, and the pin still refuses it."""
    git = shutil.which("git")
    if not git:
        pytest.skip("no git.exe on PATH")
    info = app_updater.verify_authenticode(git)
    if info.status != "Valid":
        pytest.skip(f"git.exe is not Authenticode-signed here ({info.status})")
    assert re.fullmatch(r"[0-9A-F]{64}", info.thumbprint_sha256)
    assert info.subject
    assert info.thumbprint_sha256 not in ACCEPTED_SIGNER_SHA256
    launch, err = _update_decision(info)
    assert launch is False and "unexpected publisher" in err
