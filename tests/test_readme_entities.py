"""The README's entity tables name every entity the integration creates, and no other.

README audit 2026-10-11: the tables listed a fraction of the sensors without
saying so, and their names had drifted from the translations.
"""

import json
import pathlib
import re

from custom_components.fronius_modbus import entities

from .test_entity_table import _everything_present

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
NAMES = json.loads(
    (ROOT / "custom_components/fronius_modbus/translations/en.json").read_text(
        encoding="utf-8"
    )
)["entity"]
PLATFORMS = ("sensor", "number", "select", "switch", "button", "event")
# A table the marker files under one device; its first column names the
# entities, several in one cell separated by <br>.
MARKED_TABLE = re.compile(r"<!-- entities: (\w+) -->\n((?:\|.*\n)+)")


def _documented(text: str) -> dict[str, set[str]]:
    documented: dict[str, set[str]] = {}
    for device, table in MARKED_TABLE.findall(text):
        for row in table.splitlines()[2:]:
            cell = row.split("|")[1]
            documented.setdefault(device, set()).update(
                name.strip() for name in cell.split("<br>")
            )
    return documented


def _created() -> dict[str, set[str]]:
    runtime = _everything_present()
    created: dict[str, set[str]] = {}
    for platform in PLATFORMS:
        for description in getattr(entities, f"{platform}_descriptions")(runtime):
            key = description.translation_key or description.key
            name = NAMES[platform][key]["name"].replace("{module}", "N")
            created.setdefault(str(description.device), set()).add(name)
    return created


def test_the_readme_names_every_entity_on_its_device():
    documented, created = _documented(README), _created()

    for device in created.keys() | documented.keys():
        missing = created.get(device, set()) - documented.get(device, set())
        unknown = documented.get(device, set()) - created.get(device, set())
        assert not missing, f"{device}: not in the README: {sorted(missing)}"
        assert not unknown, f"{device}: no such entity: {sorted(unknown)}"


def test_a_marked_table_names_each_entity_of_a_cell():
    """The shape the README uses: a marker, a header, and cells joined by <br>."""
    text = (
        "<!-- entities: meter -->\n"
        "| Entity | Description |\n"
        "| ------ | ----------- |\n"
        "| Power<br>Power L1 | Real power. |\n"
        "\n"
        "| Entity | Description |\n"
        "| Unmarked | Not counted. |\n"
    )

    assert _documented(text) == {"meter": {"Power", "Power L1"}}
