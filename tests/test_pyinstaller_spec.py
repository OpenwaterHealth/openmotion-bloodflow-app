import ast
import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


_REPO_ROOT = Path(__file__).resolve().parents[1]

# build_macos.sh writes openwater_macos.spec from an inline heredoc on every
# run, so the heredoc — not the tracked .spec — is the source of truth.
_MACOS_HEREDOC_RE = re.compile(
    r"cat > \"\$SPEC_FILE\" << 'SPEC_EOF'\n(.*?)\nSPEC_EOF\n", re.S
)


def _macos_spec_source() -> str:
    """The macOS spec as build_macos.sh would emit it."""
    script = (_REPO_ROOT / "build_macos.sh").read_text(encoding="utf-8")
    match = _MACOS_HEREDOC_RE.search(script)
    assert match, "could not find the spec heredoc in build_macos.sh"
    return match.group(1) + "\n"


def _bundles_sample_scan(source: str) -> bool:
    """True when `source` declares the replay sample scan as a data file.

    Structural rather than a substring match: looks for the module-level
    ``_SAMPLE_SCAN = os.path.join("resources", "sample_scan.csv")`` binding
    both specs use, so a spec that merely mentions the file in a comment
    does not pass.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "_SAMPLE_SCAN"
            for t in node.targets
        ):
            continue
        parts = [
            a.value for a in getattr(node.value, "args", [])
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
        ]
        if parts == ["resources", "sample_scan.csv"]:
            return True
    return False


def _hidden_imports_from_spec() -> set[str]:
    spec_path = _REPO_ROOT / "openwater.spec"
    tree = ast.parse(spec_path.read_text(encoding="utf-8"), filename=str(spec_path))

    hidden_imports: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AugAssign):
            continue
        if not isinstance(node.target, ast.Name) or node.target.id != "hidden":
            continue
        if not isinstance(node.op, ast.Add):
            continue
        if not isinstance(node.value, ast.List):
            continue

        for item in node.value.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                hidden_imports.add(item.value)

    return hidden_imports


def test_pyinstaller_spec_bundles_runtime_dependency_hidden_imports():
    assert {
        "requests",
        "urllib3",
        "certifi",
        "charset_normalizer",
        "idna",
        "crcmod",
        "base58",
    } <= _hidden_imports_from_spec()


@pytest.mark.parametrize("spec", ["windows", "macos"])
def test_every_spec_bundles_the_replay_sample_scan(spec):
    """Both platform specs must ship resources/sample_scan.csv (#432).

    The macOS spec listed only whole folders (pages/components/assets/models/
    config) and never picked up `resources`, so the DMG shipped without the
    sample. The no-device offer dialog does not gate on the file's existence,
    so it still appeared and then silently did nothing when clicked.
    """
    source = (
        (_REPO_ROOT / "openwater.spec").read_text(encoding="utf-8")
        if spec == "windows"
        else _macos_spec_source()
    )
    assert _bundles_sample_scan(source)


def test_tracked_macos_spec_matches_build_script():
    """The checked-in openwater_macos.spec is generated output — keep it equal
    to the heredoc that produces it.

    It is regenerated on every build, so a hand-edit to one side alone is
    silently discarded (and a stale copy misleads anyone reading it: before
    #432 the tracked file still predated the OPENMOTION_VERSION support added
    in #368).
    """
    tracked = (_REPO_ROOT / "openwater_macos.spec").read_text(encoding="utf-8")
    assert tracked == _macos_spec_source()
