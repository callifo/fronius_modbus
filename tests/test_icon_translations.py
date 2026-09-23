"""Icons live in icons.json, keyed by the entities' translation keys.

Quality scale icon-translations. An icon set in code beside the file would
win over it silently, and an icons.json entry for a key no entity carries
is dead weight nobody notices, so both directions are checked.
"""

import json
import pathlib

from custom_components.fronius_modbus import entities

from .test_entity_table import _everything_present

ICONS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "fronius_modbus"
    / "icons.json"
)


def _tables() -> dict[str, list]:
    runtime = _everything_present()
    return {
        "sensor": entities.sensor_descriptions(runtime),
        "number": entities.number_descriptions(runtime),
        "select": entities.select_descriptions(runtime),
        "switch": entities.switch_descriptions(runtime),
        "button": entities.button_descriptions(runtime),
    }


def test_no_entity_sets_its_icon_in_code():
    in_code = [
        (platform, description.key)
        for platform, descriptions in _tables().items()
        for description in descriptions
        if description.icon
    ]
    assert in_code == []


def test_every_icon_names_a_translation_key_an_entity_carries():
    icons = json.loads(ICONS.read_text(encoding="utf-8"))["entity"]
    tables = _tables()
    orphans = [
        (platform, key)
        for platform, keys in icons.items()
        for key in keys
        if key not in {d.translation_key for d in tables.get(platform, [])}
    ]
    assert icons
    assert orphans == []


def test_every_icon_has_a_default():
    icons = json.loads(ICONS.read_text(encoding="utf-8"))["entity"]
    without = [
        (platform, key)
        for platform, keys in icons.items()
        for key, value in keys.items()
        if not value.get("default", "").startswith("mdi:")
    ]
    assert without == []
