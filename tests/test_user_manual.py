"""Guards for the Research user manual (#371).

The manual is Research-only and states the app version its content
describes; build_pdf.py checks that version against a release tag.
"""

import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
MANUAL_DIR = REPO_ROOT / "docs" / "user-manual"

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
