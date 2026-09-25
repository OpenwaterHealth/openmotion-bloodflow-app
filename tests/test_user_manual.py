"""Guards for the Research user manual and its build wiring (#371).

The manual is Research-only, the committed PDF must be rebuilt with its
sources, CI renders it as a build artifact, and a production release fails
early when the manual still documents an older version. The workflows only
run on GitHub, so these tests pin their wiring on every push.
"""

import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
MANUAL_DIR = REPO_ROOT / "docs" / "user-manual"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-build.yml"
MANUAL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "user-manual.yml"

sys.path.insert(0, str(MANUAL_DIR))

import build_pdf  # noqa: E402


# ------------------------------------------------------------ research only

def test_the_only_manual_is_the_research_manual():
    sources = sorted(p.name for p in MANUAL_DIR.glob("*.md") if p.name != "README.md")
    assert sources == [build_pdf.MANUAL] == ["open-motion-research-user-manual.md"]
    pdfs = sorted(p.name for p in MANUAL_DIR.glob("*.pdf"))
    assert pdfs == [build_pdf.PDF_NAME]


def test_the_manual_does_not_cover_clinical_or_engineering_mode():
    text = (MANUAL_DIR / build_pdf.MANUAL).read_text(encoding="utf-8").lower()
    assert "clinical" not in text
    assert "engineering" not in text


def test_every_image_the_manual_references_exists():
    text = (MANUAL_DIR / build_pdf.MANUAL).read_text(encoding="utf-8")
    for name in re.findall(r"\]\((img/[^)]+)\)", text):
        assert (MANUAL_DIR / name).is_file(), name


# --------------------------------------------------------- version checking

def test_documented_version_is_read_from_the_cover():
    text = (MANUAL_DIR / build_pdf.MANUAL).read_text(encoding="utf-8")
    assert build_pdf.documented_version(text)


@pytest.mark.parametrize("tag,base", [
    ("1.6.0", "1.6.0"), ("v1.6.0", "1.6.0"), ("1.6.0-rc.2", "1.6.0"),
    ("1.6.0-dev.3", "1.6.0"), ("1.5.3-47-g7beec44", "1.5.3"),
])
def test_base_version_ignores_prerelease_and_describe_suffixes(tag, base):
    assert build_pdf.base_version(tag) == base


def test_version_mismatch_warns_or_fails_when_strict(capsys):
    text = "| **Application version** | 1.5.3 |\n"
    assert build_pdf.check_version(text, "1.5.3-rc.1", strict=True) is True
    assert build_pdf.check_version(text, "1.6.0-rc.1", strict=False) is True
    assert "WARNING" in capsys.readouterr().out
    assert build_pdf.check_version(text, "1.6.0", strict=True) is False
    assert "ERROR" in capsys.readouterr().out


def test_check_only_exits_non_zero_on_a_strict_mismatch():
    assert build_pdf.main(["--check-only", "--check-version", "999.0.0", "--strict"]) == 1
    assert build_pdf.main(["--check-only", "--check-version", "999.0.0"]) == 0


# ------------------------------------------------------------- CI wiring

def test_release_checks_the_manual_before_any_signing():
    wf = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    check_at = wf.index("- name: Check the user manual documents this release")
    assert check_at < wf.index("uses: ./.github/actions/windows-build")
    step = wf[check_at:wf.index("- name:", check_at + 10)]
    # strict (fatal) on production tags only; dev/rc tags carry a '-'
    assert 'case "$GITHUB_REF_NAME" in *-*) ;; *) strict="--strict" ;; esac' in step
    assert "--check-only --check-version" in step


def test_release_builds_uploads_and_attaches_the_manual():
    wf = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "python docs/user-manual/build_pdf.py --version \"$TAG\"" in wf
    assert "name: Open-Motion-Research-User-Manual-${{ steps.win.outputs.tag }}" in wf
    upload = wf[wf.index("- name: Upload release assets"):wf.index("  notify-clinical:")]
    assert '"build/manual/Open-Motion-Research-User-Manual-${TAG}.pdf"' in upload


def test_pr_workflow_requires_the_pdf_with_its_sources_and_uploads_it():
    wf = MANUAL_WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request:" in wf and "branches: [next, main]" in wf
    assert "open-motion-research-user-manual\\.md|build_pdf\\.py|img/.+" in wf
    assert "docs/user-manual/Open-Motion-Research-User-Manual.pdf" in wf
    assert "uses: actions/upload-artifact@v4" in wf
