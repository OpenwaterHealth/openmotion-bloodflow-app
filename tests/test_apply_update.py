import pytest
from app_updater import (
    BUNDLE_FILENAME,
    SignatureInfo,
    _build_update_helper_script,
    _is_bundle_url,
    _looks_like_pe,
    _update_decision,
    ACCEPTED_SIGNER_SHA256,
)

pytestmark = pytest.mark.unit

PIN = next(iter(ACCEPTED_SIGNER_SHA256))


def test_valid_pinned_signature_launches():
    assert _update_decision(SignatureInfo("Valid", PIN, "Open Water Internet, Inc.")) == (True, None)


def test_valid_unpinned_signature_aborts_with_publisher_in_message():
    launch, err = _update_decision(SignatureInfo("Valid", "F" * 64, "Mallory Corp"))
    assert launch is False
    assert "Mallory Corp" in err


def test_invalid_signature_aborts_with_message():
    launch, err = _update_decision(SignatureInfo("HashMismatch"))
    assert launch is False
    assert "HashMismatch" in err


def test_unsigned_is_always_rejected():
    """There is no transition path any more (#544): unsigned never installs,
    whatever channel it came from."""
    launch, err = _update_decision(SignatureInfo("NotSigned"))
    assert launch is False
    assert "not signed" in err.lower()


def test_is_bundle_url_accepts_only_exe():
    assert _is_bundle_url("https://x/Open-Motion-Setup-1.2.3.exe")
    assert _is_bundle_url("https://x/Open-Motion-Research-Setup-1.2.3.EXE")
    assert not _is_bundle_url("https://github.com/o/r/releases/tag/1.2.3")
    assert not _is_bundle_url("")
    assert not _is_bundle_url(None)


def test_looks_like_pe_rejects_non_executables():
    assert _looks_like_pe(b"MZ\x90\x00")
    assert not _looks_like_pe(b"<!DOCTYPE html>")  # HTML error page
    assert not _looks_like_pe(b"")                  # empty / truncated
    assert not _looks_like_pe(b"M")                 # 1-byte truncation


def test_download_lands_under_a_fixed_name_not_the_url_basename():
    """V-10: the asset name comes from a network response and would be
    written into the PowerShell helper; the local filename is ours."""
    assert BUNDLE_FILENAME[True] == "Open-Motion-Research-Setup.exe"
    assert BUNDLE_FILENAME[False] == "Open-Motion-Setup.exe"


def test_helper_script_waits_installs_silently_and_relaunches():
    s = _build_update_helper_script(
        1234, r"C:\Users\u\updates\Open-Motion-Research-Setup.exe",
        r"C:\Program Files\Open-Motion\Open-Motion.exe",
    )
    assert "1234" in s                              # waits for our PID
    assert "Get-Process" in s
    assert "/passive" in s                          # non-interactive install
    assert "Open-Motion-Research-Setup.exe" in s    # runs the installer
    assert "Open-Motion.exe" in s                   # relaunches the app


@pytest.mark.parametrize("bad", [
    'C:\\u\\x".exe',            # closes the PowerShell string
    "C:\\u\\x`n.exe",           # backtick escape
    "C:\\u\\$env:TEMP\\x.exe",  # variable expansion
    "C:\\u\\x\n.exe",           # newline
])
def test_helper_script_refuses_paths_that_could_escape_the_command(bad):
    with pytest.raises(ValueError):
        _build_update_helper_script(1, bad, r"C:\app\Open-Motion.exe")
    with pytest.raises(ValueError):
        _build_update_helper_script(1, r"C:\u\ok.exe", bad)
