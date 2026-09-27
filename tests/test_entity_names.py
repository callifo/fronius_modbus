"""The names the entities show: one term per thing, in both languages.

Discussion #6 and a review of the German texts: the same limit had two names,
active power read as plain power next to a reactive power sensor, and the
charge reference read as a maximum power.
"""

import json
import pathlib

import pytest

from custom_components.fronius_modbus import entities

TRANSLATIONS = (
    pathlib.Path(__file__).parent.parent
    / "custom_components/fronius_modbus/translations"
)
NAMES = {
    language: json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )["entity"]
    for language in ("en", "de")
}
PHASES = ("l1", "l2", "l3")

EXPECTED = {
    ("sensor", "acpower", "de"): "AC-Wirkleistung",
    ("sensor", "power", "de"): "Wirkleistung",
    **{("sensor", f"power_{p}", "de"): f"Wirkleistung {p.upper()}" for p in PHASES},
    ("select", "ac_limit_enable", "de"): "AC-Leistungsbegrenzung aktivieren",
    ("sensor", "exported", "de"): "Eingespeist",
    ("sensor", "imported", "de"): "Bezogen",
    **{("sensor", f"exported_{p}", "de"): f"Eingespeist {p.upper()}" for p in PHASES},
    **{("sensor", f"imported_{p}", "de"): f"Bezogen {p.upper()}" for p in PHASES},
    ("sensor", "storage_state_of_health", "de"): "Gesundheitszustand (SoH)",
    (
        "sensor",
        "battery_max_charge_power",
        "de",
    ): "Speicher max. Ladeleistung (DC-Wandler)",
    ("sensor", "battery_max_discharge_power", "de"): (
        "Speicher max. Entladeleistung (DC-Wandler)"
    ),
    ("sensor", "feed_in_frequency", "de"): "Frequenz Einspeisepunkt",
    **{
        (
            "sensor",
            f"feed_in_voltage_{suffix}",
            "de",
        ): f"Spannung Einspeisepunkt {label}"
        for suffix, label in (
            ("l1", "L1"),
            ("l2", "L2"),
            ("l3", "L3"),
            ("l1_l2", "L1-L2"),
            ("l2_l3", "L2-L3"),
            ("l3_l1", "L3-L1"),
        )
    },
    ("sensor", "max_charge", "en"): "Charge/discharge reference power",
    ("sensor", "max_charge", "de"): "Lade-/Entlade-Referenzleistung",
}


@pytest.mark.parametrize(("platform", "key", "language"), list(EXPECTED))
def test_an_entity_shows_the_name_of_what_it_measures(platform, key, language):
    assert NAMES[language][platform][key]["name"] == EXPECTED[platform, key, language]


def test_the_ac_limit_flag_is_shown_once_by_default():
    """The throttle control sensor reads WMaxLim_Ena like "AC limit enabled"."""
    duplicate = next(
        d for d in entities._STATIC_SENSOR_DESCRIPTIONS if d.key == "WMaxLim_Ena"
    )

    assert duplicate.entity_registry_enabled_default is False


def test_the_readme_names_every_throttle_state_as_the_translations_do():
    """The state table in the README is what users compare their sensor with."""
    readme = (TRANSLATIONS.parent.parent.parent / "README.md").read_text(
        encoding="utf-8"
    )
    section = readme.split("### Throttle reason\n", 1)[1].split("\n### ", 1)[0]
    rows = {
        cells[0].strip("` "): (cells[1].strip(), cells[2].strip())
        for line in section.splitlines()
        if line.startswith("| `")
        for cells in [line.strip("|").split("|")]
    }
    states = {
        language: NAMES[language]["sensor"]["throttle_reason"]["state"]
        for language in ("en", "de")
    }

    assert set(rows) == set(states["en"]) - {"unknown"}
    for key, (english, german) in rows.items():
        assert (english, german) == (states["en"][key], states["de"][key])


def test_a_limit_in_watts_reads_as_the_export_limit_it_mostly_is():
    """ "Leistungsgrenze" read as the inverter at its own maximum, and a bare
    "Leistungsbegrenzung" as the AC limit next to it."""
    states = {
        language: NAMES[language]["sensor"]["throttle_reason"]["state"]
        for language in ("en", "de")
    }

    assert states["en"]["feed_in_limit"] == "Export limit"
    assert states["de"]["feed_in_limit"] == "Einspeisebegrenzung"


def test_the_old_limit_keys_are_history_only():
    """export_limit meant a switched-on AC limit; reusing it for the export limit
    would relabel old history and turn old automations silently."""
    options = entities.SENSOR_STATE_OPTIONS["throttle_reason"]

    assert {"export_limit", "active_power_control"} <= set(options)
    assert (
        NAMES["en"]["sensor"]["throttle_reason"]["state"]["export_limit"] == "AC limit"
    )
