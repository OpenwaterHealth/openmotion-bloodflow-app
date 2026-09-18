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
# The Windows build steps live in a composite action so the private Clinical
# repository runs the very same build (#573).
WINDOWS_BUILD = REPO_ROOT / ".github" / "actions" / "windows-build" / "action.yml"
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
def release_workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def windows_build() -> str:
    return WINDOWS_BUILD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(release_workflow, windows_build) -> str:
    """Everything a release build runs: the workflow (macOS job inline) plus
    the Windows composite action it calls."""
    return release_workflow + "\n" + windows_build


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
    assert workflow.count("${{ steps.sbom.outputs.SBOM_PATH }}") == 3  # macOS x2 + the action's output
    assert workflow.count("${{ steps.win.outputs.sbom-path }}") == 2


def test_stale_hand_written_sbom_is_gone():
    """The generated per-release SBOM replaces the 0.4.3 snapshot that
    recorded omotion as 'latest'."""
    assert not (REPO_ROOT / "sbom.cdx.json").exists()


def test_release_builds_sign_rc_and_production_tags_or_explicit_dispatch(release_workflow, windows_build):
    """#443 / #573: the eSigner CKA step provides CODESIGN_THUMBPRINT on rc and
    production tags (anything but -dev.) and on a manual run with sign=true;
    dev tags and branch pushes never sign, because every signing is metered.
    The policy is the caller's; the action only obeys `sign`."""
    assert "- name: Set up eSigner CKA (EV code signing)" in windows_build
    assert "      if: inputs.sign == 'true'" in windows_build
    assert ("sign: ${{ (startsWith(github.ref, 'refs/tags/') && !contains(github.ref, '-dev.')) "
            "|| (github.event_name == 'workflow_dispatch' && inputs.sign) }}") in release_workflow
    assert "CODESIGN_THUMBPRINT: ${{ env.CODESIGN_THUMBPRINT || inputs.codesign-thumbprint }}" in windows_build
    assert "      sign:\n        description:" in release_workflow


def test_public_workflow_never_builds_or_publishes_clinical(release_workflow, windows_build):
    """#573: this repo is public, so release assets, workflow artifacts and
    logs are world-readable. The public workflow builds Research only; the
    Clinical build lives in the private repo and is only poked from here."""
    assert release_workflow.count("uses: ./.github/actions/windows-build") == 1
    assert "          variants: research\n" in release_workflow
    assert "dist/clinical" not in release_workflow
    assert "Open-Motion-Setup-" not in release_workflow       # the Clinical bundle name
    assert "path: dist/research" in release_workflow
    # dev/rc tags only, fire-and-forget: no needs:, a failed poke only warns.
    job = release_workflow[release_workflow.index("  notify-clinical:"):release_workflow.index("  build-macos:")]
    assert "if: startsWith(github.ref, 'refs/tags/') && contains(github.ref, '-')" in job
    assert "needs:" not in job
    assert "clinical-prerelease.yml" in job and "clinical-release.yml" not in job
    assert "exit 1" not in job
    # The action must work from another repository: the tag is an input.
    assert "GITHUB_REF" not in windows_build.replace("GITHUB_REF / GITHUB_SHA", "")
    assert "github.ref" not in windows_build and "github.sha" not in windows_build


def test_production_release_carries_the_installer_but_no_portable_zip(release_workflow):
    upload = release_workflow[release_workflow.index("- name: Upload release assets"):]
    upload = upload[:upload.index("  notify-clinical:")]
    assert 'if [ "$CHANNEL" != "prod" ]; then\n            assets+=("Open-Motion-Research-${TAG}.zip")' in upload
    assert "build/installer/Open-Motion-Research-Setup-*.exe" in upload


def test_packaging_signs_each_variants_exe_before_zipping_and_harvesting():
    script = (REPO_ROOT / "scripts" / "package_artifacts.ps1").read_text(encoding="utf-8-sig")
    sign_at = script.index('installer\\sign.ps1")')
    zip_at = script.index("New-PortableZip -DistDir $distDir")
    installer_at = script.index(r'installer\build_installer.ps1")')  # the call, not the comment
    assert sign_at < zip_at < installer_at
    assert 'Join-Path $distDir "Open-Motion.exe"' in script


def test_installer_signs_the_engine_and_the_bundle_but_not_the_app_msi():
    """#569: signings are metered, so a signed build makes exactly three per
    variant: the exe (test above), the detached Burn engine and the
    reattached bundle. The app MSI never ships on its own and Burn verifies
    it by hash, so it is deliberately left unsigned. A new sign.ps1 call in
    build_installer.ps1 has to be a decision, not a drive-by."""
    script = (REPO_ROOT / "installer" / "build_installer.ps1").read_text(encoding="utf-8-sig")
    calls = [
        line.strip() for line in script.splitlines()
        if "sign.ps1" in line and not line.lstrip().startswith("#")
    ]
    assert len(calls) == 2, calls
    assert calls[0].endswith("-Files $engine")
    assert calls[1].endswith("-Files $bundleExe")
    assert not any("$appMsi" in c for c in calls)
    # WiX order: detach, sign the engine, reattach, sign the bundle.
    detach_at = script.index("wix burn detach")
    reattach_at = script.index("wix burn reattach")
    assert detach_at < script.index("-Files $engine") < reattach_at < script.index("-Files $bundleExe")


def test_nuitka_is_the_default_compiler_with_pyinstaller_as_dispatch_fallback(workflow):
    """#548 (default since 2026-09-17): every push/tag build compiles both
    Windows variants with Nuitka; PyInstaller runs only on a manual dispatch
    that selects it. build_nuitka.ps1 lands the exe at the PyInstaller path
    so packaging is untouched, and the C-compile cache is restored between
    runs."""
    assert "options: [nuitka, pyinstaller]" in workflow and "default: nuitka" in workflow
    assert "compiler: ${{ github.event_name == 'workflow_dispatch' && inputs.compiler || 'nuitka' }}" in workflow
    assert "if: inputs.compiler != 'pyinstaller'" in workflow
    assert "if: inputs.compiler == 'pyinstaller'" in workflow
    assert "- name: Cache Nuitka's C-compile cache" in workflow
    assert "scripts/build_nuitka.ps1 -Variant" in workflow
    reqs = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "nuitka==4.2.1" in reqs and "ordered-set==" in reqs and "zstandard==" in reqs
    script = (REPO_ROOT / "scripts" / "build_nuitka.ps1").read_text(encoding="utf-8-sig")
    assert "--standalone" in script and "--onefile" in script and "--deployment" in script
    assert '"--onefile-tempdir-spec=' not in script, "a static extraction dir recreates V-07"
    assert "--include-qt-plugins=sensible,qml" in script
    assert 'Join-Path (Join-Path $DistRoot $Variant) "Open-Motion"' in script
    # clinical must not carry the self-updater (#543): Nuitka follows the
    # conditional import statically, so the build tells it not to.
    assert '"--nofollow-import-to=app_updater"' in script
    packaging = (REPO_ROOT / "scripts" / "package_artifacts.ps1").read_text(encoding="utf-8-sig")
    assert '[ValidateSet("pyinstaller", "nuitka")][string]$Compiler = "nuitka"' in packaging


def test_macos_dmg_step_mounts_only_to_style_and_retries_the_detach():
    """#553: the CI path never mounts the image (nothing to style headless),
    the local path retries a busy detach and forces it last, and hdiutil's
    stderr is no longer thrown away."""
    src = (REPO_ROOT / "build_macos.sh").read_text(encoding="utf-8")
    assert "detach_with_retry()" in src
    assert 'hdiutil detach "$mount" -force' in src
    assert "image never mounted" in src
    for cmd in ("hdiutil create", "hdiutil convert"):
        block = src[src.index(cmd):src.index(cmd) + 260]
        assert "2>&1" not in block, f"{cmd} still discards stderr"
