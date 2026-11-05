"""No code uses a Home Assistant constant whose removal is announced.

Home Assistant logs such a use while the module is imported, before any test
runs, so the deprecated-call check in conftest.py does not see it. The import
check below reads what Home Assistant really reports; the name scan also covers
what the Home Assistant of this test run does not report yet.
"""

import ast
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = REPO / "custom_components" / "fronius_modbus"
# Gone in Home Assistant 2027.5; iot_class in the manifest took over.
ANNOUNCED_REMOVALS = ("CONN_CLASS_",)
IMPORT_TIMEOUT_SECONDS = 120
IMPORT_EVERY_MODULE = """
import importlib, logging, pathlib, sys

root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root))
logging.basicConfig(stream=sys.stdout, format="%(message)s", level=logging.WARNING)
for path in sorted((root / "custom_components").rglob("*.py")):
    parts = path.relative_to(root).with_suffix("").parts
    importlib.import_module(".".join(part for part in parts if part != "__init__"))
"""
DEPRECATING_MODULE = """
from functools import partial

from homeassistant.helpers.deprecation import (
    DeprecatedConstant,
    check_if_deprecated_constant,
)

_DEPRECATED_OLD_NAME = DeprecatedConstant("old", "NEW_NAME", "2099.1")
__getattr__ = partial(check_if_deprecated_constant, module_globals=globals())
"""


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


def _deprecated_constant_reports(root: pathlib.Path, integration: str) -> list[str]:
    """What Home Assistant logs about ``integration`` while its modules are imported."""
    result = subprocess.run(
        [sys.executable, "-c", IMPORT_EVERY_MODULE, str(root)],
        capture_output=True,
        text=True,
        timeout=IMPORT_TIMEOUT_SECONDS,
        check=True,
    )
    return [
        line
        for line in result.stdout.splitlines()
        if "deprecated constant" in line and f"used from {integration}" in line
    ]


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


def test_the_import_check_sees_a_report_home_assistant_makes(tmp_path):
    """Goes red, not quietly blind, if Home Assistant changes how it reports."""
    integration = tmp_path / "custom_components" / "deprecating"
    integration.mkdir(parents=True)
    (integration / "old_api.py").write_text(DEPRECATING_MODULE, encoding="utf-8")
    (integration / "__init__.py").write_text(
        "from . import old_api\n\nVALUE = old_api.OLD_NAME\n", encoding="utf-8"
    )

    reports = _deprecated_constant_reports(tmp_path, "deprecating")

    assert len(reports) == 1
    assert "OLD_NAME" in reports[0]


def test_importing_the_package_reports_no_deprecated_constant():
    assert _deprecated_constant_reports(REPO, "fronius_modbus") == []
