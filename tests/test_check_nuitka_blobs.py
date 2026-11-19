"""A Nuitka build must not ship another build's constants or payload (#698).

With zig, Nuitka's default C23 #embed let zig's compile cache hand a build
the blob objects of another build directory that was still on disk, and the
exe shipped that build's constants and payload without any error.
scripts/build_nuitka.ps1 now selects the coff_obj resource mode and checks
the finished binaries with scripts/check_nuitka_blobs.py. These tests cover
the check and that wiring; the builds themselves only run on a build
machine.
"""

import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_nuitka_blobs  # noqa: E402

BUILD_NUITKA = (REPO_ROOT / "scripts" / "build_nuitka.ps1").read_text(encoding="utf-8-sig")


def _build(tmp_path, constants=b"C" * 5000, payload=b"P" * 90000,
           dll_holds=None, exe_holds=None, stem="main"):
    """A fake Nuitka output dir plus onefile exe. By default each binary
    holds its own build's blob; pass ``*_holds`` to put other bytes in."""
    out = tmp_path / "out"
    for sub in (f"{stem}.build/blobs", f"{stem}.onefile-build/blobs", f"{stem}.dist"):
        (out / sub).mkdir(parents=True)
    (out / f"{stem}.build/blobs/__constant.bin").write_bytes(constants)
    (out / f"{stem}.onefile-build/blobs/__payload.bin").write_bytes(payload)
    dll = constants if dll_holds is None else dll_holds
    (out / f"{stem}.dist/{stem}.dll").write_bytes(b"MZ" + os.urandom(300) + dll + b"\0" * 64)
    exe = tmp_path / "Open-Motion.exe"
    held = payload if exe_holds is None else exe_holds
    exe.write_bytes(b"MZ" + os.urandom(300) + held + b"signature")
    return out, exe


def test_a_build_that_holds_its_own_blobs_passes(tmp_path, capsys):
    out, exe = _build(tmp_path)
    assert check_nuitka_blobs.check(out, exe) == []
    said = capsys.readouterr().out
    assert "constants" in said and "payload" in said


def test_another_builds_payload_fails(tmp_path):
    out, exe = _build(tmp_path, exe_holds=b"Q" * 90000)
    errors = check_nuitka_blobs.check(out, exe)
    assert len(errors) == 1 and "onefile payload" in errors[0]


def test_another_builds_constants_fail(tmp_path):
    out, exe = _build(tmp_path, dll_holds=b"D" * 5000)
    errors = check_nuitka_blobs.check(out, exe)
    assert len(errors) == 1 and "constants" in errors[0]


def test_a_blob_that_only_starts_the_same_fails(tmp_path):
    """Two builds' blobs share long prefixes; matching the head alone is not
    enough."""
    payload = b"P" * 70000 + b"new tail"
    out, exe = _build(tmp_path, payload=payload, exe_holds=b"P" * 70000 + b"old tail")
    assert any("onefile payload" in e for e in check_nuitka_blobs.check(out, exe))


def test_a_later_occurrence_still_counts(tmp_path):
    payload = b"head" * 20000 + b"tail"
    out, exe = _build(tmp_path, payload=payload, exe_holds=b"head" * 20000 + b"junk" + payload)
    assert check_nuitka_blobs.check(out, exe) == []


def test_missing_blobs_or_binaries_fail_rather_than_pass(tmp_path):
    out, exe = _build(tmp_path)
    (out / "main.build/blobs/__constant.bin").unlink()
    exe.unlink()
    errors = check_nuitka_blobs.check(out, exe)
    assert any("no constants blob" in e for e in errors)
    assert any("no onefile exe" in e for e in errors)


def test_main_exit_codes(tmp_path, capsys):
    out, exe = _build(tmp_path, stem="probe")
    assert check_nuitka_blobs.main([str(out), str(exe), "--stem", "probe"]) == 0
    assert check_nuitka_blobs.main([str(out)]) == 2
    out2, exe2 = _build(tmp_path / "two", exe_holds=b"Q" * 90000)
    assert check_nuitka_blobs.main([str(out2), str(exe2)]) == 1
    assert "::error ::" in capsys.readouterr().out


# --- the build script -------------------------------------------------------


def test_build_nuitka_compiles_blobs_as_coff_objects():
    mode = BUILD_NUITKA.index('$env:NUITKA_RESOURCE_MODE = "coff_obj"')
    assert mode < BUILD_NUITKA.index("Invoke-AppPython -CondaEnv $CondaEnv -Arguments $args")
    # restored afterwards, so a caller's session is left as it was
    assert "$env:NUITKA_RESOURCE_MODE = $prevResourceMode" in BUILD_NUITKA


def test_build_nuitka_checks_the_finished_exe_before_cleanup():
    check = re.search(
        r'Invoke-AppPython .*"scripts\\check_nuitka_blobs\.py", \$workPath, \$target\)\s*\n'
        r'if \(\$LASTEXITCODE -ne 0\) \{ throw',
        BUILD_NUITKA,
    )
    assert check, "build_nuitka.ps1 must fail the build on the blob check"
    # after the icon step rewrote the exe, before the cleanup deletes the blobs
    assert BUILD_NUITKA.index("add_named_group_icon(") < check.start()
    assert check.start() < BUILD_NUITKA.index('foreach ($d in @("main.dist"')
