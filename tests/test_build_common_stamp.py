"""The build-variant stamp in scripts/build_common.ps1 must work on a CRLF
checkout (#548).

The GitHub Windows runner checks the repo out with autocrlf=true, so
config/app_config.py arrives with CRLF line endings there, while the local
tree is LF. A .NET multiline '$' matches only before "\n", so the original
'^CLINICAL_MODE = (True|False)$' never matched on CI and the first Nuitka
build failed in Set-BuildVariant. The PyInstaller CI path never noticed
because the workflow stamped with sed. These tests drive the real
PowerShell function against LF and CRLF copies.
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


def _stamp(tmp_path: Path, eol: bytes, clinical: bool) -> bytes:
    module = tmp_path / "app_config.py"
    module.write_bytes(
        b"# header" + eol
        + b"CLINICAL_MODE = False" + eol
        + b"APP_CONFIG = {" + eol
        + b'    "updateApiUrl": None,' + eol
        + b"}" + eol
    )
    script = (
        f". '{BUILD_COMMON}'; "
        f"$orig = Set-BuildVariant -Clinical ${'true' if clinical else 'false'} -ModulePath '{module}'; "
        f"if (-not $orig) {{ exit 3 }}"
    )
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return module.read_bytes()


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_set_build_variant_stamps_clinical_on_either_line_ending(tmp_path, eol):
    out = _stamp(tmp_path, eol, clinical=True)
    assert b"CLINICAL_MODE = True" + eol in out
    assert b"CLINICAL_MODE = False" not in out
    # line endings preserved, nothing else touched
    assert out.count(eol) == 5 and b"\r" not in out.replace(b"\r\n", b"")


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
