"""The build-variant stamp in scripts/build_common.ps1 must work on a CRLF
checkout (#548).

The GitHub Windows runner checks the repo out with autocrlf=true, so
config/app_config.py arrives with CRLF line endings there, while the local
tree is LF. A .NET multiline '$' matches only before "\n", so the original
'^CLINICAL_MODE = (True|False)$' never matched on CI and the first Nuitka
build failed in Set-BuildVariant. The PyInstaller CI path never noticed
because the workflow stamped with sed. These tests drive the real
PowerShell function against LF and CRLF copies, including the SERVICE_BUILD
stamp that keeps the engineering unlock in the clinical service tool (#706).
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_COMMON = REPO_ROOT / "scripts" / "build_common.ps1"

pytest.importorskip("sys")  # keep collection simple on every platform


def _powershell():
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if sys.platform != "win32" or not exe:
        pytest.skip("Windows PowerShell needed to exercise build_common.ps1")
    return exe


def _run_stamp(tmp_path: Path, eol: bytes, clinical: bool, service: bool = False):
    module = tmp_path / "app_config.py"
    module.write_bytes(
        b"# header" + eol
        + b"CLINICAL_MODE = False" + eol
        + b"SERVICE_BUILD = False" + eol
        + b"APP_CONFIG = {" + eol
        + b'    "updateApiUrl": None,' + eol
        + b"}" + eol
    )
    script = (
        f". '{BUILD_COMMON}'; "
        f"$orig = Set-BuildVariant -Clinical ${'true' if clinical else 'false'} "
        f"-Service ${'true' if service else 'false'} -ModulePath '{module}'; "
        f"if (-not $orig) {{ exit 3 }}"
    )
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    return proc, module.read_bytes()


def _stamp(tmp_path: Path, eol: bytes, clinical: bool, service: bool = False) -> bytes:
    proc, out = _run_stamp(tmp_path, eol, clinical, service)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return out


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_set_build_variant_stamps_clinical_on_either_line_ending(tmp_path, eol):
    out = _stamp(tmp_path, eol, clinical=True)
    assert b"CLINICAL_MODE = True" + eol in out
    assert b"CLINICAL_MODE = False" not in out
    # a plain clinical build has the engineering unlock compiled out (#706)
    assert b"SERVICE_BUILD = False" + eol in out
    # line endings preserved, nothing else touched
    assert out.count(eol) == 6 and b"\r" not in out.replace(b"\r\n", b"")


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_set_build_variant_stamps_the_clinical_service_tool(tmp_path, eol):
    # #706: -Service keeps the engineering unlock in a clinical build.
    out = _stamp(tmp_path, eol, clinical=True, service=True)
    assert b"CLINICAL_MODE = True" + eol in out
    assert b"SERVICE_BUILD = True" + eol in out
    assert out.count(eol) == 6 and b"\r" not in out.replace(b"\r\n", b"")


def test_set_build_variant_refuses_a_research_service_build(tmp_path):
    proc, out = _run_stamp(tmp_path, b"\n", clinical=False, service=True)
    assert proc.returncode != 0
    assert b"SERVICE_BUILD = False" in out and b"CLINICAL_MODE = False" in out


def test_set_build_variant_needs_the_service_stamp_line(tmp_path):
    module = tmp_path / "app_config.py"
    module.write_bytes(b"CLINICAL_MODE = False\n")
    script = (
        f". '{BUILD_COMMON}'; "
        f"Set-BuildVariant -Clinical $true -ModulePath '{module}' | Out-Null"
    )
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "SERVICE_BUILD stamp line not found" in proc.stdout + proc.stderr
    assert module.read_bytes() == b"CLINICAL_MODE = False\n"


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_set_compiled_config_value_matches_on_either_line_ending(tmp_path, eol):
    module = tmp_path / "app_config.py"
    module.write_bytes(b"CLINICAL_MODE = False" + eol + b'    "updateApiUrl": None,' + eol)
    script = (
        f". '{BUILD_COMMON}'; "
        f"$orig = Set-CompiledConfigValue -Key updateApiUrl -PythonValue \"'http://x'\" -ModulePath '{module}'; "
        f"if (-not $orig) {{ exit 3 }}"
    )
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = module.read_bytes()
    assert b'"updateApiUrl": \'http://x\',' + eol in out
