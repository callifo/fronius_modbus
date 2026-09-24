"""Every entity key 0.3 registered is still produced, and every description has a translation."""

import json
import pathlib
import re
from unittest.mock import MagicMock

from custom_components.fronius_modbus import entities
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

TESTS = pathlib.Path(__file__).resolve().parent
PACKAGE = TESTS.parents[0] / "custom_components" / "fronius_modbus"
LEGACY = json.loads((TESTS / "legacy_unique_ids.json").read_text(encoding="utf-8"))


def _everything_present():
    """A runtime where every optional part exists, so every description is emitted."""
    runtime = MagicMock()
    runtime.device.three_phase = True
    runtime.device.storage = object()
    runtime.device.mppt = object()
    runtime.device.mppt_channels.pv = (0,)
    runtime.device.meters = {200: MagicMock(phases=3)}
    runtime.primary_meter_unit_id = 200
    runtime.web_control.configured = True
    runtime.web_control.technician_configured = True
    # Unread component readings keep every component sensor in the table.
    runtime.web_data = None
    return runtime


def _keys(descriptions):
    return {
        d.key.replace("meter_200_", "meter_{unit}_").replace(
            "mppt_module_0_", "mppt_module_{module}_"
        )
        for d in descriptions
    }


def test_every_legacy_key_is_still_registered():
    runtime = _everything_present()
    produced = {
        "sensor": _keys(entities.sensor_descriptions(runtime)),
        "number": _keys(entities.number_descriptions(runtime)),
        "select": _keys(entities.select_descriptions(runtime)),
        "switch": _keys(entities.switch_descriptions(runtime)),
        "button": _keys(entities.button_descriptions(runtime)),
    }
    for platform, legacy_keys in LEGACY.items():
        if platform == "statistics":
            continue
        assert set(legacy_keys) <= produced[platform], (
            f"{platform}: missing {set(legacy_keys) - produced[platform]}"
        )


def test_every_translation_key_has_a_name_in_every_language():
    runtime = _everything_present()
    for language in ("en", "de"):
        translations = json.loads(
            (PACKAGE / "translations" / f"{language}.json").read_text(encoding="utf-8")
        )["entity"]
        for platform, factory in (
            ("sensor", entities.sensor_descriptions),
            ("number", entities.number_descriptions),
            ("select", entities.select_descriptions),
            ("switch", entities.switch_descriptions),
            ("button", entities.button_descriptions),
        ):
            for description in factory(runtime):
                assert "name" in translations[platform].get(
                    description.translation_key, {}
                ), (language, platform, description.translation_key)


def test_every_sensor_that_carried_statistics_still_has_a_state_class():
    """Discussion #6: dropping a state class hands every user a statistics repair.

    The 0.3 line recorded long-term statistics for these; a sensor that loses its
    state class makes Home Assistant offer to delete that history.
    """
    runtime = _everything_present()
    by_key = {
        d.key.replace("meter_200_", "meter_{unit}_").replace(
            "mppt_module_0_", "mppt_module_{module}_"
        ): d
        for d in entities.sensor_descriptions(runtime)
    }
    without = [key for key in LEGACY["statistics"] if by_key[key].state_class is None]
    assert without == []


def test_no_energy_sensor_uses_the_measurement_state_class():
    runtime = _everything_present()
    for description in entities.sensor_descriptions(runtime):
        if description.device_class == SensorDeviceClass.ENERGY:
            assert description.state_class != SensorStateClass.MEASUREMENT, (
                description.key
            )


def test_every_translation_key_is_one_hassfest_accepts():
    """hassfest rejects keys outside [a-z0-9-_]+; the 0.3 data keys such as AphA failed it."""
    runtime = _everything_present()
    pattern = re.compile(r"^[a-z0-9][a-z0-9-_]*[a-z0-9]$|^[a-z0-9]$")
    offenders = [
        description.translation_key
        for factory in (
            entities.sensor_descriptions,
            entities.number_descriptions,
            entities.select_descriptions,
            entities.switch_descriptions,
            entities.button_descriptions,
        )
        for description in factory(runtime)
        if not pattern.match(description.translation_key)
    ]
    assert offenders == []


def test_every_state_translation_key_is_one_hassfest_accepts():
    """hassfest validates the state keys too; the 0.3 states ("Auto", "On grid operating") failed it."""
    pattern = re.compile(r"^[a-z0-9][a-z0-9-_]*[a-z0-9]$|^[a-z0-9]$")
    offenders = []
    for language in ("en", "de"):
        translations = json.loads(
            (PACKAGE / "translations" / f"{language}.json").read_text(encoding="utf-8")
        )["entity"]
        for platform, entries in translations.items():
            for translation_key, entry in entries.items():
                offenders.extend(
                    (language, platform, translation_key, state)
                    for state in entry.get("state", {})
                    if not pattern.match(state)
                )
    assert offenders == []
