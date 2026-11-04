"""Every platform builds its entities from the one shared table.

`entities.py` is the single place that decides which description becomes
which entity, on which device, with which translation key. A platform that
assembled its own list beside the table would drift from it silently: the
entities still appear, they just stop being the ones the table (and the
migration guard next to it) knows about.

Two properties per platform, both cheap and both invisible when broken:

  1. It imports its descriptions from `.entities` - the shared table, not a
     local list.
  2. It calls `async_add_entities` exactly ONCE. The sibling integration this
     suite comes from registered a Modbus list and a second, optional source
     in two calls; an error in the optional half silently dropped every entity
     of the first.

Driven off PLATFORMS rather than a list written out here, so a platform added
later is covered without anyone remembering to come back.
"""

import ast
import pathlib

from custom_components.fronius_modbus import PLATFORMS

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "fronius_modbus"
)

THE_SHARED_TABLE = ".entities"
THE_REGISTRATION = "async_add_entities"


def _platform_sources() -> dict:
    sources = {}
    for platform in PLATFORMS:
        source_file = PACKAGE / f"{platform}.py"
        assert source_file.exists(), (
            f"{platform} is in PLATFORMS but {source_file.name} does not exist - "
            "either the platform moved or this scan has gone blind"
        )
        sources[source_file.name] = source_file.read_text(encoding="utf-8")
    return sources


def _descriptions_imported_from_the_table(source: str) -> set:
    """The `*_descriptions` factories a module imports from `.entities`.

    An import from anywhere else does not count: the point is that the table
    is the source, not that a name spelled like one exists.
    """
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if f".{node.module}" != THE_SHARED_TABLE:
                continue
            imported |= {
                alias.name
                for alias in node.names
                if alias.name.endswith("_descriptions")
            }
    return imported


def _registration_calls(source: str) -> int:
    """How often a module hands entities to Home Assistant.

    Counted as CALLS, not as occurrences of the name: the parameter of
    `async_setup_entry` is the same word, so a substring count reads 2 for a
    module that registers nothing at all.
    """
    return sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == THE_REGISTRATION
        for node in ast.walk(ast.parse(source))
    )


def test_the_scan_is_not_satisfied_by_the_signature():
    """Proof-of-red for both readers: the shapes they exist to reject."""
    signature_only = (
        "from .entities import FroniusEntity\n\n\n"
        "async def async_setup_entry(hass, entry, async_add_entities):\n"
        "    pass\n"
    )
    assert _descriptions_imported_from_the_table(signature_only) == set()
    assert _registration_calls(signature_only) == 0

    elsewhere = "from .local_table import sensor_descriptions\n"
    assert _descriptions_imported_from_the_table(elsewhere) == set()

    real = (
        "from .entities import FroniusEntity, sensor_descriptions\n"
        "async_add_entities(sensor_descriptions(runtime))\n"
    )
    assert _descriptions_imported_from_the_table(real) == {"sensor_descriptions"}
    assert _registration_calls(real) == 1


def test_every_platform_takes_its_descriptions_from_the_shared_table():
    missing = [
        name
        for name, source in _platform_sources().items()
        if not _descriptions_imported_from_the_table(source)
    ]

    assert not missing, (
        f"{missing} build(s) entities from something other than the shared "
        "table in entities.py, so the decision which entity exists lives in "
        "two places."
    )


def test_every_platform_registers_its_entities_in_one_call():
    wrong = {
        name: _registration_calls(source)
        for name, source in _platform_sources().items()
        if _registration_calls(source) != 1
    }

    assert not wrong, (
        f"platform(s) not registering exactly once (calls): {wrong}. A second "
        "call is a second list that can fail on its own and take the first "
        "one's entities with it."
    )


def test_every_platform_in_the_list_is_a_module():
    """The scan above reads PLATFORMS; a platform named there without a module
    would fail at Home Assistant's forward-setup, not here, and this says so
    first."""
    assert PLATFORMS, "no platforms at all - the scan proves nothing"
    assert set(_platform_sources()) == {f"{platform}.py" for platform in PLATFORMS}
