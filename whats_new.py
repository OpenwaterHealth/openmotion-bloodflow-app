"""'What's new' notes shown after a new release is installed (#597).

The notes ship inside the build as ``resources/whats_new.md``, one
``## X.Y.Z`` section per release, newest first. Bundled rather than fetched
so a Clinical build (no updater, maybe no network) behaves exactly like a
Research one.

The last version whose notes the operator dismissed is the STATE key
``whatsNewSeenVersion`` in the scans.db settings table. At launch the notes
for every release after that one, up to and including the running build,
are pending; the modal shows them once and records the running version when
it is dismissed.

Versions compare on their ``X.Y.Z`` base only: ``1.6.0-rc.1``,
``1.6.0-dev.3`` and ``1.6.0+4.gabc1234`` all read the ``1.6.0`` section,
so a tester moving rc.1 -> rc.2 -> 1.6.0 sees the notes once.

A ``- [research]`` bullet is dropped from a Clinical build and shown
without the tag in a Research one.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("openmotion.bloodflow-app.whats-new")

SEEN_KEY = "whatsNewSeenVersion"

Version = Tuple[int, int, int]

_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?")
_HEADING_RE = re.compile(r"^##\s+v?(\d+\.\d+(?:\.\d+)?)\b.*$")
_RESEARCH_TAG_RE = re.compile(r"^(\s*[-*]\s+)\[research\]\s*", re.IGNORECASE)


def base_version(version: Optional[str]) -> Optional[Version]:
    """``(major, minor, patch)`` of a version string, or None if it has none."""
    if not version:
        return None
    m = _VERSION_RE.match(str(version))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def parse_sections(text: str) -> List[Tuple[Version, str, str]]:
    """``[(version, heading_line, body)]`` for every ``## X.Y.Z`` section.

    Anything above the first release heading (the file's own title and
    authoring notes) is ignored.
    """
    sections: List[Tuple[Version, str, str]] = []
    current: Optional[Tuple[Version, str]] = None
    body: List[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if current is not None:
                sections.append((current[0], current[1], "\n".join(body).strip()))
            current = (base_version(m.group(1)), line.lstrip("#").strip())
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        sections.append((current[0], current[1], "\n".join(body).strip()))
    return sections


def _filter_variant(body: str, clinical: bool) -> str:
    out = []
    for line in body.splitlines():
        m = _RESEARCH_TAG_RE.match(line)
        if m:
            if clinical:
                continue
            line = m.group(1) + line[m.end():]
        out.append(line)
    return "\n".join(out).strip()


def notes_between(
    text: str,
    current: Optional[str],
    last_seen: Optional[str],
    clinical: bool,
) -> str:
    """Markdown for every section after ``last_seen`` up to ``current``.

    ``last_seen=None`` means the operator has never dismissed any notes
    (an install from before this feature): only the running release's
    section is shown, not the whole history. Empty string when the running
    release has no section of its own, so a build whose notes were never
    written shows nothing rather than an older release's news.
    """
    cur = base_version(current)
    if cur is None:
        return ""
    seen = base_version(last_seen)
    picked = []
    has_current = False
    for version, heading, body in parse_sections(text):
        if version > cur:
            continue
        if seen is None:
            if version != cur:
                continue
        elif version <= seen:
            continue
        body = _filter_variant(body, clinical)
        if not body:
            continue
        has_current = has_current or version == cur
        picked.append(f"## {heading}\n\n{body}")
    if not has_current:
        return ""
    return "\n\n".join(picked)


def notes_path() -> Path:
    from utils.resource_path import resource_path

    return resource_path("resources", "whats_new.md")


def load_notes_text(path: Optional[Path] = None) -> str:
    """The bundled notes file, or "" (logged) when it is missing/unreadable."""
    path = path or notes_path()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        logger.warning("What's new: notes file unavailable at %s", path)
        return ""
