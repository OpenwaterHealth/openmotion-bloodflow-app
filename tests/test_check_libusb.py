"""The Windows build ships no libusb older than 1.0.30 (#669).

``scripts/check_libusb.py`` reads each libusb-1.0.dll's PE version and fails
the build on an old one. Both build paths call it on their exact payload,
and both keep the libusb1 wheel's own DLL out. These tests cover the check
itself and that wiring; the builds only run on a build machine.
"""

import ast
import os
import re
import struct
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_libusb  # noqa: E402


def _fake_pe(path: Path, version: tuple[int, int, int, int] | None) -> Path:
    """A file holding just a VS_FIXEDFILEINFO, enough for pe_file_version."""
    body = b"MZ" + b"\0" * 64
    if version is not None:
        major, minor, micro, nano = version
        body += struct.pack(
            "<IIII", 0xFEEF04BD, 0x00010000,
            (major << 16) | minor, (micro << 16) | nano,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body + b"\0" * 32)
    return path


def test_pe_file_version_reads_the_fixed_file_info(tmp_path):
    dll = _fake_pe(tmp_path / "libusb-1.0.dll", (1, 0, 30, 12037))
    assert check_libusb.pe_file_version(dll) == (1, 0, 30, 12037)
    assert check_libusb.pe_file_version(_fake_pe(tmp_path / "x.dll", None)) is None


def test_check_passes_1_0_30_and_fails_anything_older(tmp_path):
    new = _fake_pe(tmp_path / "new.dll", (1, 0, 30, 12037))
    old = _fake_pe(tmp_path / "old.dll", (1, 0, 29, 11953))
    unversioned = _fake_pe(tmp_path / "bare.dll", None)

    assert check_libusb.check([(r"_vendor\libusb\windows\x64\libusb-1.0.dll", new)]) == []
    errors = check_libusb.check([
        (r"_vendor\libusb\windows\x64\libusb-1.0.dll", new),
        (r"usb1\libusb-1.0.dll", old),
        ("omotion/dfu-util/win64/LIBUSB-1.0.DLL", unversioned),
    ])
    assert len(errors) == 2
    assert "usb1" in errors[0] and "1.0.29.11953" in errors[0]
    assert "no version resource" in errors[1]


def test_check_ignores_other_files_but_requires_one_libusb(tmp_path):
    other = _fake_pe(tmp_path / "other.dll", (1, 0, 0, 0))
    assert check_libusb.check([("Qt6Core.dll", other)]) == [
        "no libusb-1.0.dll in the payload"
    ]


def test_main_walks_a_payload_directory(tmp_path, capsys):
    _fake_pe(tmp_path / "_vendor/libusb/windows/x64/libusb-1.0.dll", (1, 0, 30, 0))
    assert check_libusb.main([str(tmp_path)]) == 0
    _fake_pe(tmp_path / "usb1/libusb-1.0.dll", (1, 0, 28, 0))
    assert check_libusb.main([str(tmp_path)]) == 1
    assert "::error ::usb1/libusb-1.0.dll is libusb 1.0.28.0" in capsys.readouterr().out
    assert check_libusb.main([str(tmp_path / "missing")]) == 2


@pytest.mark.parametrize(
    ("dylib", "expected"),
    [
        ("/opt/homebrew/Cellar/libusb/1.0.30/lib/libusb-1.0.0.dylib", (1, 0, 30)),
        ("/usr/local/Cellar/libusb/1.0.29_1/lib/libusb-1.0.0.dylib", (1, 0, 29)),
        ("/opt/local/lib/libusb-1.0.0.dylib", None),
    ],
)
def test_homebrew_libusb_version_comes_from_the_cellar_path(dylib, expected):
    assert check_libusb.homebrew_libusb_version(dylib) == expected


def test_macos_spec_refuses_an_old_homebrew_libusb():
    """build_macos.sh writes the macOS spec from a heredoc; the tracked copy
    must match it (tests/test_pyinstaller_spec.py), so checking the script is
    enough."""
    src = (REPO_ROOT / "build_macos.sh").read_text(encoding="utf-8")
    guard = src.index("_libusb_ver = homebrew_libusb_version(_libusb)")
    assert "if _libusb_ver is None or _libusb_ver < MIN_VERSION:" in src
    assert guard < src.index('binaries.append((_libusb, "."))')


def _spec_foreign_libusb(om_dir: str):
    """The spec's own _foreign_libusb, bound to a given SDK package dir."""
    src = (REPO_ROOT / "openwater.spec").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_foreign_libusb"
    )
    ns = {
        "os": os,
        "_norm": lambda p: p.replace("/", os.sep).replace("\\", os.sep),
        "_OM_DIR": os.path.normcase(os.path.abspath(om_dir)) + os.sep,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "openwater.spec", "exec"), ns)
    return ns["_foreign_libusb"]


def test_pyinstaller_spec_keeps_only_the_sdk_libusb(tmp_path):
    """Seen in a real build: the wheel's usb1 copy and a System32 copy the
    dfu-util.exe dependency scan picked up both reached the Analysis."""
    sdk = tmp_path / "site-packages" / "omotion"
    foreign = _spec_foreign_libusb(str(sdk))
    assert not foreign((r"_vendor\libusb\windows\x64\libusb-1.0.dll",
                        str(sdk / "_vendor/libusb/windows/x64/libusb-1.0.dll"), "BINARY"))
    assert not foreign((r"omotion\dfu-util\win64\libusb-1.0.dll",
                        str(sdk / "dfu-util/win64/libusb-1.0.dll"), "BINARY"))
    assert foreign((r"usb1\libusb-1.0.dll",
                    str(tmp_path / "site-packages/usb1/libusb-1.0.dll"), "BINARY"))
    assert foreign(("libusb-1.0.dll", r"C:\WINDOWS\system32\libusb-1.0.dll", "BINARY"))
    assert not foreign(("Qt6Core.dll", r"C:\elsewhere\Qt6Core.dll", "BINARY"))


def test_pyinstaller_spec_filters_then_checks_the_payload():
    src = (REPO_ROOT / "openwater.spec").read_text(encoding="utf-8")
    drop = src.index("a.binaries = [e for e in a.binaries if not _foreign_libusb(e)]")
    run = src.index("_check_libusb((e[0], e[1]) for e in a.binaries + a.datas)")
    # The check must see the filtered list, and both must happen before the
    # payload is packed.
    assert src.index("a = Analysis(") < drop < run < src.index("exe_gui = EXE(")


def test_nuitka_build_drops_the_wheel_dll_and_checks_main_dist():
    src = (REPO_ROOT / "scripts" / "build_nuitka.ps1").read_text(encoding="utf-8-sig")
    assert '"--noinclude-dlls=usb1/libusb*",' in src
    check = re.search(
        r'Invoke-AppPython .*"scripts\\check_libusb\.py", \(Join-Path \$workPath "main\.dist"\)\)\s*\n'
        r'if \(\$LASTEXITCODE -ne 0\) \{ throw',
        src,
    )
    assert check, "build_nuitka.ps1 must fail the build on the libusb check"
    # main.dist is deleted by the cleanup; the check has to run first.
    assert check.start() < src.index('foreach ($d in @("main.dist"')
