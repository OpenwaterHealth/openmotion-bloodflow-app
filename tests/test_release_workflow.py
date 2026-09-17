"""Guards for the release SDK pin and the per-release SBOM (#545).

rc and production builds must ship the exact openmotion-sdk release named in
``sdk-version.txt``, and every release must carry a CycloneDX SBOM generated
from the environment that was frozen. These tests pin the wiring in
``release-build.yml`` and the two helper scripts it calls; the workflow itself
only runs on GitHub, so this is the check that runs on every push.
"""

import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-build.yml"
PIN_FILE = REPO_ROOT / "sdk-version.txt"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_sdk_pin  # noqa: E402
import stamp_sbom  # noqa: E402


# --------------------------------------------------------------- the pin file
def test_sdk_pin_is_one_plain_release_version():
    pin = check_sdk_pin.read_pin(PIN_FILE)
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin)
    # One line, no pre-release suffix: rc/production builds install this from
    # PyPI, and a dev stamp there would never resolve.
    assert PIN_FILE.read_text(encoding="utf-8").strip().splitlines() == [pin]


def test_read_pin_rejects_anything_but_a_release(tmp_path):
    bad = tmp_path / "sdk-version.txt"
    for text in ("1.12.0-rc.1\n", "latest\n", "1.12\n", "1.12.0\n1.11.0\n"):
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            check_sdk_pin.read_pin(bad)


# --------------------------------------------------------- check_sdk_pin.py
def test_check_sdk_pin_enforce_fails_on_mismatch(tmp_path, monkeypatch, capsys):
    pin = tmp_path / "sdk-version.txt"
    pin.write_text("1.12.0\n", encoding="utf-8")
    monkeypatch.setattr(check_sdk_pin, "installed_version", lambda *_: "1.11.0")
    assert check_sdk_pin.main(["--enforce", "--pin-file", str(pin)]) == 1
    assert "::error" in capsys.readouterr().out
    # Without --enforce the mismatch is reported, not fatal (dev builds).
    assert check_sdk_pin.main(["--pin-file", str(pin)]) == 0
    monkeypatch.setattr(check_sdk_pin, "installed_version", lambda *_: "1.12.0")
    assert check_sdk_pin.main(["--enforce", "--pin-file", str(pin)]) == 0


# ------------------------------------------------------------ stamp_sbom.py
def _sbom(sdk_version="1.12.0"):
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {},
        "components": [
            {"type": "library", "name": "PyQt6", "version": "6.8.0"},
            {"type": "library", "name": "openmotion-sdk", "version": sdk_version},
        ],
    }


def test_stamp_sbom_sets_main_component_and_checks_sdk(tmp_path):
    path = tmp_path / "x.cdx.json"
    path.write_text(json.dumps(_sbom()), encoding="utf-8")
    rc = stamp_sbom.main([
        str(path), "--name", "Open-Motion", "--version", "1.5.3-rc.1",
        "--expect-sdk", "1.12.0",
    ])
    assert rc == 0
    out = json.loads(path.read_text(encoding="utf-8"))
    assert out["metadata"]["component"] == {
        "type": "application",
        "name": "Open-Motion",
        "version": "1.5.3-rc.1",
        "bom-ref": "Open-Motion@1.5.3-rc.1",
    }
    assert out["components"] == _sbom()["components"]


def test_stamp_sbom_fails_on_wrong_or_missing_sdk(tmp_path):
    path = tmp_path / "x.cdx.json"
    path.write_text(json.dumps(_sbom("1.11.0")), encoding="utf-8")
    assert stamp_sbom.main([
        str(path), "--name", "Open-Motion", "--version", "1.5.3",
        "--expect-sdk", "1.12.0",
    ]) == 1
    # A failed check must not rewrite the file.
    assert json.loads(path.read_text(encoding="utf-8"))["metadata"] == {}

    no_sdk = _sbom()
    no_sdk["components"].pop()
    path.write_text(json.dumps(no_sdk), encoding="utf-8")
    assert stamp_sbom.main([str(path), "--name", "Open-Motion", "--version", "1.5.3"]) == 1


# ------------------------------------------------------- release-build.yml
@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_release_builds_install_the_pinned_sdk(workflow):
    """No more 'latest' anywhere: rc/production (and main/dispatch) builds
    install openmotion-sdk==<sdk-version.txt>; only dev/next builds source-
    install @next."""
    assert "pip install --upgrade openmotion-sdk" not in workflow
    assert "releases/latest" not in workflow
    assert workflow.count('SDK_PIN=$(tr -d \'[:space:]\' < sdk-version.txt)') == 2
    assert workflow.count('pip install "openmotion-sdk==${SDK_PIN}"') == 2
    assert workflow.count("openmotion-sdk.git@next") == 2


def test_release_builds_verify_the_pin_in_both_jobs(workflow):
    assert workflow.count("python scripts/check_sdk_pin.py --enforce") == 2


def test_release_builds_generate_stamp_and_attach_the_sbom(workflow):
    assert workflow.count("cyclonedx-py environment") == 2
    assert workflow.count("python scripts/stamp_sbom.py") == 4  # dev + enforced, per job
    assert workflow.count("--expect-sdk") == 2
    # Attached to the release by both jobs and always uploaded as an artifact.
    assert workflow.count("${{ steps.sbom.outputs.SBOM_PATH }}") == 4


def test_stale_hand_written_sbom_is_gone():
    """The generated per-release SBOM replaces the 0.4.3 snapshot that
    recorded omotion as 'latest'."""
    assert not (REPO_ROOT / "sbom.cdx.json").exists()


def test_release_builds_sign_only_production_tags_or_explicit_dispatch(workflow):
    """#443: the eSigner CKA step provides CODESIGN_THUMBPRINT on production
    tags (X.Y.Z, no '-') and on a manual run with sign=true; nothing else
    signs, because every signing is metered."""
    assert "- name: Set up eSigner CKA (EV code signing)" in workflow
    assert ("if: (startsWith(github.ref, 'refs/tags/') && !contains(github.ref, '-')) "
            "|| (github.event_name == 'workflow_dispatch' && inputs.sign)") in workflow
    assert "CODESIGN_THUMBPRINT: ${{ env.CODESIGN_THUMBPRINT || secrets.CODESIGN_THUMBPRINT }}" in workflow
    assert "      sign:\n        description:" in workflow


def test_packaging_signs_each_variants_exe_before_zipping_and_harvesting():
    script = (REPO_ROOT / "scripts" / "package_artifacts.ps1").read_text(encoding="utf-8-sig")
    sign_at = script.index('installer\\sign.ps1")')
    zip_at = script.index("New-PortableZip -DistDir $distDir")
    installer_at = script.index(r'installer\build_installer.ps1")')  # the call, not the comment
    assert sign_at < zip_at < installer_at
    assert 'Join-Path $distDir "Open-Motion.exe"' in script

