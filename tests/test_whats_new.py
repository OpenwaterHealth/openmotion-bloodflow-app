"""'What's new' notes after a new release is installed (#597)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import whats_new
from config import app_config as compiled
from motion_connector import MotionConnector

pytestmark = pytest.mark.unit

SHIPPED_NOTES = Path(__file__).resolve().parent.parent / "resources" / "whats_new.md"

NOTES = """\
# What's new

<!-- authoring notes: ## 9.9.9 in a comment line is not a heading -->

## 1.7.0 — 2026-11-01

- Seven
- [research] Seven research

## 1.6.1

- Six-one

## 1.6.0

- [research] Research only

## 1.5.0

- Five
"""


@pytest.mark.parametrize("version, expected", [
    ("1.6.0", (1, 6, 0)),
    ("v1.6.0", (1, 6, 0)),
    ("1.6.0-rc.2", (1, 6, 0)),
    ("1.6.0-dev.3", (1, 6, 0)),
    ("1.5.3+8.g1962648.dirty", (1, 5, 3)),
    ("1.6", (1, 6, 0)),
    ("1.0-pre3-0-g2b7a8aa", (1, 0, 0)),
    ("abc1234", None),
    ("", None),
    (None, None),
])
def test_base_version(version, expected):
    assert whats_new.base_version(version) == expected


def test_parse_sections_ignores_preamble_and_keeps_heading_text():
    sections = whats_new.parse_sections(NOTES)
    assert [v for v, _, _ in sections] == [(1, 7, 0), (1, 6, 1), (1, 6, 0), (1, 5, 0)]
    assert sections[0][1] == "1.7.0 — 2026-11-01"
    assert sections[1][2] == "- Six-one"


def test_upgrade_shows_every_release_since_last_seen():
    notes = whats_new.notes_between(NOTES, "1.7.0", "1.5.0", clinical=False)
    assert "## 1.7.0" in notes and "## 1.6.1" in notes and "## 1.6.0" in notes
    assert "## 1.5.0" not in notes
    # newest first, research tag stripped in a Research build
    assert notes.index("1.7.0") < notes.index("1.6.1")
    assert "- Seven research" in notes and "[research]" not in notes


def test_clinical_drops_research_bullets_and_empty_sections():
    notes = whats_new.notes_between(NOTES, "1.7.0", "1.5.0", clinical=True)
    assert "Seven research" not in notes
    assert "## 1.6.0" not in notes   # its only bullet was research-only
    assert "## 1.6.1" in notes


def test_never_dismissed_shows_only_the_running_release():
    notes = whats_new.notes_between(NOTES, "1.6.1", None, clinical=False)
    assert notes == "## 1.6.1\n\n- Six-one"


def test_prerelease_reads_its_base_release_section_once():
    assert "## 1.7.0" in whats_new.notes_between(NOTES, "1.7.0-rc.1", "1.6.1", clinical=False)
    # rc.1 -> rc.2 -> final: already seen
    assert whats_new.notes_between(NOTES, "1.7.0-rc.2", "1.7.0-rc.1", clinical=False) == ""
    assert whats_new.notes_between(NOTES, "1.7.0", "1.7.0-rc.2", clinical=False) == ""


def test_no_section_for_running_release_shows_nothing():
    # Notes never written for 1.8.0: don't resurface 1.7.0's news.
    assert whats_new.notes_between(NOTES, "1.8.0", "1.6.1", clinical=False) == ""
    # Running release's section is research-only and this is Clinical.
    assert whats_new.notes_between(NOTES, "1.6.0", "1.5.0", clinical=True) == ""


def test_downgrade_or_same_version_shows_nothing():
    assert whats_new.notes_between(NOTES, "1.6.1", "1.7.0", clinical=False) == ""
    assert whats_new.notes_between(NOTES, "abc1234", None, clinical=False) == ""


def test_missing_notes_file_is_empty_not_fatal(tmp_path):
    assert whats_new.load_notes_text(tmp_path / "nope.md") == ""


def test_shipped_notes_file_parses_newest_first():
    sections = whats_new.parse_sections(SHIPPED_NOTES.read_text(encoding="utf-8"))
    versions = [v for v, _, _ in sections]
    assert versions, "resources/whats_new.md has no '## X.Y.Z' sections"
    assert versions == sorted(versions, reverse=True)
    assert len(set(versions)) == len(versions)
    assert all(body for _, _, body in sections)


def test_seen_version_is_a_persisted_state_key():
    assert whats_new.SEEN_KEY in compiled.STATE_KEYS
    assert compiled.APP_CONFIG[whats_new.SEEN_KEY] is None


# --- connector slots -------------------------------------------------------

def _connector(monkeypatch, *, version, seen, clinical=False):
    monkeypatch.setattr(whats_new, "load_notes_text", lambda path=None: NOTES)
    c = MotionConnector.__new__(MotionConnector)
    c._app_version = version
    c._app_config = {"clinicalMode": clinical, whats_new.SEEN_KEY: seen}
    c._save_app_config = MagicMock()
    return c


def test_pending_then_mark_seen_records_running_version(monkeypatch):
    c = _connector(monkeypatch, version="1.7.0", seen="1.6.1")
    assert "## 1.7.0" in c.pendingWhatsNew()
    c.markWhatsNewSeen()
    assert c._app_config[whats_new.SEEN_KEY] == "1.7.0"
    c._save_app_config.assert_called_once()
    assert c.pendingWhatsNew() == ""
    c.markWhatsNewSeen()               # already recorded → no second write
    c._save_app_config.assert_called_once()


def test_current_whats_new_ignores_seen_state(monkeypatch):
    c = _connector(monkeypatch, version="1.7.0-rc.1", seen="1.7.0-rc.1")
    assert c.pendingWhatsNew() == ""
    assert c.currentWhatsNew().startswith("## 1.7.0")
    assert "1.6.1" not in c.currentWhatsNew()
