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

def _spec_source(spec: str) -> str:
    return (
        (_REPO_ROOT / "openwater.spec").read_text(encoding="utf-8")
        if spec == "windows"
        else _macos_spec_source()
    )


def _collect_all_calls(source: str) -> list[ast.Call]:
    return [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "collect_all"
    ]


def _data_folders(source: str) -> set[str]:
    """Folder names the spec ships whole as data (`for folder in (...)`)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "folder"
            and isinstance(node.iter, ast.Tuple)
        ):
            names.update(
                e.value for e in node.iter.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
    return names


@pytest.mark.parametrize("spec", ["windows", "macos"])
def test_collect_all_never_ships_py_source_as_data(spec):
    """Every collect_all() must pass include_py_files=False (#557).

    PyInstaller's collect_all defaults to include_py_files=True, which copies
    every .py of the package into the bundle as a loose data file next to the
    bytecode already compiled into the PYZ. The frozen finder resolves the PYZ
    first, so the copies are never imported - they only leak the source. The
    1.5.3-dev.0 Research zip shipped 100 omotion files that way, including
    the laser_params.py / fpga_model.py modules sdk#278 had just converted
    from JSON.
    """
    calls = _collect_all_calls(_spec_source(spec))
    assert calls, "spec no longer calls collect_all at all?"
    for call in calls:
        package = call.args[0].value if call.args else "?"
        flags = {
            kw.arg: kw.value.value
            for kw in call.keywords
            if isinstance(kw.value, ast.Constant)
        }
        assert flags.get("include_py_files") is False, (
            f"collect_all({package!r}) must pass include_py_files=False"
        )


def test_windows_spec_does_not_bundle_python_packages_as_data_folders():
    """processing/ is a standalone CSV script the app never imports; bundling
    it as a data folder shipped its source as plaintext (#557). The real
    resource folders stay."""
    folders = _data_folders(
        (_REPO_ROOT / "openwater.spec").read_text(encoding="utf-8")
    )
    assert {"pages", "components", "assets"} <= folders
    assert "processing" not in folders

@pytest.mark.parametrize("spec", ["windows", "macos"])
def test_analysis_puts_the_sdk_package_on_pathex(spec):
    """The Analysis must reach omotion as a plain directory (#557).

    A PEP 660 editable SDK install (the documented dev setup) is exposed
    through an import-hook finder that PyInstaller's module analysis does not
    consult. Before #557 that went unnoticed: the loose .py copies shipped by
    collect_all() were imported at runtime instead. With those gone, the spec
    resolves omotion's parent directory and passes it as pathex; a build that
    drops back to ``pathex=[]`` would freeze an app with no SDK in it.
    """
    source = _spec_source(spec)
    assert 'find_spec("omotion")' in source
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Analysis"
        ):
            keywords = {kw.arg: kw.value for kw in node.keywords}
            assert "pathex" in keywords, "Analysis() has no pathex="
            value = keywords["pathex"]
            assert not (isinstance(value, ast.List) and not value.elts), (
                "Analysis(pathex=[]) would leave an editable SDK out of the PYZ"
            )
            assert isinstance(value, ast.Name) and value.id == "pathex"
            break
    else:
        pytest.fail("no Analysis(...) call found in the spec")
