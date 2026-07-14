import ast
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


def _hidden_imports_from_spec() -> set[str]:
    spec_path = Path(__file__).resolve().parents[1] / "openwater.spec"
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
