import pytest
from motion_connector import (
    _update_decision,
    _is_bundle_url,
    _looks_like_pe,
    _build_update_helper_script,
)


@pytest.mark.unit
def test_valid_signature_launches():
    assert _update_decision("Valid", require_signed=True) == (True, None)


@pytest.mark.unit
def test_invalid_signature_aborts_with_message():
    launch, err = _update_decision("HashMismatch", require_signed=False)
    assert launch is False
    assert "HashMismatch" in err


@pytest.mark.unit
def test_unsigned_allowed_when_not_required():
    assert _update_decision("NotSigned", require_signed=False) == (True, None)


@pytest.mark.unit
def test_unsigned_rejected_when_required():
    launch, err = _update_decision("NotSigned", require_signed=True)
    assert launch is False
    assert "not signed" in err.lower()


@pytest.mark.unit
def test_is_bundle_url_accepts_only_exe():
    assert _is_bundle_url("https://x/OpenWater-Setup-1.2.3.exe")
    assert _is_bundle_url("https://x/OpenWater-Setup-1.2.3_RUO.EXE")
    assert not _is_bundle_url("https://github.com/o/r/releases/tag/1.2.3")
    assert not _is_bundle_url("")
    assert not _is_bundle_url(None)


@pytest.mark.unit
def test_looks_like_pe_rejects_non_executables():
    assert _looks_like_pe(b"MZ\x90\x00")
    assert not _looks_like_pe(b"<!DOCTYPE html>")  # HTML error page
    assert not _looks_like_pe(b"")                  # empty / truncated
    assert not _looks_like_pe(b"M")                 # 1-byte truncation


@pytest.mark.unit
def test_helper_script_waits_installs_silently_and_relaunches():
    s = _build_update_helper_script(
        1234, r"C:\Users\u\updates\Openwater-Setup-1.2.3.exe",
        r"C:\Program Files\Open-Motion\Open-Motion.exe",
    )
    assert "1234" in s                              # waits for our PID
    assert "Get-Process" in s
    assert "/passive" in s                          # non-interactive install
    assert "Openwater-Setup-1.2.3.exe" in s         # runs the installer
    assert "Open-Motion.exe" in s                   # relaunches the app
