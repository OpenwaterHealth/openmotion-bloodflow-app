"""
Dynamic version derived from git tags.

Tag format expected:  0.4.3  or  pre-0.4.3
git describe output:  0.4.3           (on tag)
                      0.4.3-3-gabc1234       (3 commits after tag)
                      0.4.3-3-gabc1234-dirty (uncommitted changes)

Resulting APP_VERSION:
    "0.4.3"                   â€“ exact tag
    "0.4.3+3.gabc1234"        â€“ incremental build
    "0.4.3+3.gabc1234.dirty"  â€“ dirty working tree
"""

import subprocess
import os

# Fallback used when: no git, no tags, or running from a frozen PyInstaller bundle
_FALLBACK_VERSION = "1.0-pre3-0-g2b7a8aa"


def get_version() -> str:
    """Return a PEP 440-ish version string derived from git describe."""

    # Inside a PyInstaller bundle there is no .git directory;
    # return whatever was stamped at build time.
    import sys

    if getattr(sys, "frozen", False):
        return _FALLBACK_VERSION

    repo_dir = os.path.dirname(os.path.abspath(__file__))

    try:
        raw = (
            subprocess.check_output(
                ["git", "describe", "--tags", "--dirty", "--always", "--long"],
                cwd=repo_dir,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _FALLBACK_VERSION

    # Possible outputs:
    #   v0.4.3-0-gabc1234          exact tag  (long format always has -N-gHASH)
    #   v0.4.3-3-gabc1234          3 commits past tag
    #   v0.4.3-3-gabc1234-dirty    3 commits past tag + uncommitted changes
    #   abc1234                     no tags at all
    #   abc1234-dirty               no tags + dirty

    dirty = raw.endswith("-dirty")
    if dirty:
        raw = raw[: -len("-dirty")]

    # Strip optional leading 'v' or 'pre-' prefix for the base version
    parts = raw.rsplit("-", 2)  # [tag, N, gHASH] in long format

    if len(parts) >= 3:
        tag, distance, commit = parts[-3], parts[-2], parts[-1]
        # re-join if tag itself contained dashes (e.g. "pre-0.4.3")
        tag = raw[: raw.rfind(f"-{distance}-{commit}")]
        # strip leading 'v' only; keep 'pre-' prefix for pre-release tags
        base = tag.lstrip("v")

        if distance == "0":
            # Exactly on the tag
            version = base
        else:
            version = f"{base}+{distance}.{commit}"

        if dirty:
            version += ".dirty" if "+" in version else "+dirty"
    else:
        # No recognizable tag â€” just a commit hash
        version = _FALLBACK_VERSION + f"+{raw}"
        if dirty:
            version += ".dirty"

    return version


if __name__ == "__main__":
    print(get_version())
