"""The device library must stay importable without Home Assistant, so it can be tested and reused alone."""

import ast
import pathlib

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "fronius_modbus"
    / "fronius_modbus_api"
)


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_module_of_the_library_imports_home_assistant():
    offenders = {
        path.name: sorted(
            n
            for n in _imports(path)
            if n.split(".")[0] in {"homeassistant", "voluptuous"}
        )
        for path in PACKAGE.glob("*.py")
    }
    assert {name: names for name, names in offenders.items() if names} == {}
