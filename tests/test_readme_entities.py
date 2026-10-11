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
# A table the marker files under one device and the platforms its rows are of;
# its first column names the entities, several in one cell separated by <br>.
# Per platform: a sensor and a control of the same name need a row each
# (reaudit G-05).
MARKED_TABLE = re.compile(r"<!-- entities: (\w+) ([\w ]+) -->\n((?:\|.*\n)+)")

type Entity = tuple[str, str, str]


def _documented(text: str) -> set[Entity]:
    """(device, platform, name) for every name and every platform of its table."""
    documented: set[Entity] = set()
    for device, platforms, table in MARKED_TABLE.findall(text):
        for row in table.splitlines()[2:]:
            for name in row.split("|")[1].split("<br>"):
                documented.update(
                    (device, platform, name.strip()) for platform in platforms.split()
                )
    return documented


def _created() -> set[Entity]:
    runtime = _everything_present()
    created: set[Entity] = set()
    for platform in PLATFORMS:
        for description in getattr(entities, f"{platform}_descriptions")(runtime):
            key = description.translation_key or description.key
            name = NAMES[platform][key]["name"].replace("{module}", "N")
            created.add((str(description.device), platform, name))
    return created


def test_the_readme_names_every_entity_on_its_device():
    documented, created = _documented(README), _created()

    missing = created - documented
    unknown = {
        (device, name)
        for device, _, name in documented
        if not any(c[0] == device and c[2] == name for c in created)
    }
    assert not missing, f"not in the README: {sorted(missing)}"
    assert not unknown, f"no such entity: {sorted(unknown)}"


def test_a_marked_table_names_each_entity_of_a_cell_for_each_platform():
    """The shape the README uses: a marker, a header, and cells joined by <br>."""
    text = (
        "<!-- entities: meter sensor -->\n"
        "| Entity | Description |\n"
        "| ------ | ----------- |\n"
        "| Power<br>Power L1 | Real power. |\n"
        "\n"
        "<!-- entities: storage number select -->\n"
        "| Entity | Description |\n"
        "| ------ | ----------- |\n"
        "| Mode | A control. |\n"
        "\n"
        "| Entity | Description |\n"
        "| Unmarked | Not counted. |\n"
    )

    assert _documented(text) == {
        ("meter", "sensor", "Power"),
        ("meter", "sensor", "Power L1"),
        ("storage", "number", "Mode"),
        ("storage", "select", "Mode"),
    }
