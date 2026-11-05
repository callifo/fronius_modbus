"""No code uses a Home Assistant constant whose removal is announced.

Home Assistant logs such a use while the module is imported, before any test
runs, so the deprecated-call check in conftest.py does not see it.
"""

import ast
import pathlib

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "fronius_modbus"
)
# Gone in Home Assistant 2027.5; iot_class in the manifest took over.
ANNOUNCED_REMOVALS = ("CONN_CLASS_",)


def _identifier(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.alias):
        return node.name
    return ""


def _removed_names(source: str) -> set[str]:
    identifiers = {_identifier(node) for node in ast.walk(ast.parse(source))}
    return {name for name in identifiers if name.startswith(ANNOUNCED_REMOVALS)}


def test_the_scan_catches_the_line_that_warned_on_2026_11():
    assert _removed_names(
        "CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL\n"
    ) == {"CONN_CLASS_LOCAL_POLL"}
    assert _removed_names("from x import CONN_CLASS_CLOUD_POLL\n") == {
        "CONN_CLASS_CLOUD_POLL"
    }


def test_no_constant_with_an_announced_removal_is_used():
    offenders = {
        path.name: sorted(_removed_names(path.read_text(encoding="utf-8")))
        for path in PACKAGE.rglob("*.py")
    }
    assert {name: found for name, found in offenders.items() if found} == {}
