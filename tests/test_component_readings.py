"""The component readings: what the inverter's readable endpoints add beyond Modbus.

The fixture is a GEN24 with a BYD battery, read on 2026-09-24, with its serial
numbers, part serials and device ids removed. Those never leave the web client:
only the fields a sensor shows are taken.
"""

import json
import pathlib
from unittest.mock import MagicMock

import pytest

from custom_components.fronius_modbus import entities
from custom_components.fronius_modbus.component_readings import (
    COMPONENT_READINGS,
    component_value,
)
from custom_components.fronius_modbus.froniuswebclient import (
    _parse_inverter_readable,
    _parse_storage_readable,
)
from custom_components.fronius_modbus.web_control import WebData

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "components_gen24.json").read_text(
        encoding="utf-8"
    )
)
ENABLED = {
    "inverter_temperature",
    "storage_temperature",
    "module_temperature_1",
    "module_temperature_3",
    "module_temperature_4",
    "fan_1",
    "fan_2",
    "inverter_power_l1",
    "inverter_power_l2",
    "inverter_power_l3",
    "production_limit",
    "production_limit_reached",
    "battery_max_charge_power",
    "battery_max_discharge_power",
    "grid_valid",
    "power_stage_firmware_1",
    "power_stage_firmware_2",
    "storage_state_of_health",
}
DISABLED = {
    "dc_link_voltage",
    "feed_in_voltage_l1",
    "feed_in_voltage_l2",
    "feed_in_voltage_l3",
    "feed_in_voltage_l1_l2",
    "feed_in_voltage_l2_l3",
    "feed_in_voltage_l3_l1",
    "feed_in_frequency",
    "operating_time",
    "backup_time",
    "power_stage_hardware",
    "storage_cell_temperature_min",
    "storage_cell_temperature_max",
    "storage_bms_temperature",
    "storage_discharge_limit",
    "storage_power_limit",
    "storage_peak_charge_power",
    "storage_peak_discharge_power",
    "storage_rated_soc_min",
    "storage_rated_soc_max",
    "storage_voltage_min",
    "storage_voltage_max",
    "storage_modules",
    "storage_link",
}


def runtime_with(web_data: WebData | None):
    runtime = MagicMock()
    runtime.device.storage = object()
    runtime.web_control.configured = True
    runtime.web_data = web_data
    return runtime


def read_gen24() -> WebData:
    return WebData(
        inverter_readings=_parse_inverter_readable(FIXTURE["inverter"])["readings"],
        storage_readings=_parse_storage_readable(FIXTURE["storage"])["readings"],
    )


def component_sensors(runtime) -> dict:
    keys = ENABLED | DISABLED | {"module_temperature_2"}
    return {d.key: d for d in entities.sensor_descriptions(runtime) if d.key in keys}


def with_private_fields(payload: dict) -> dict:
    """The fixture had them removed; the real endpoints carry them."""
    payload = json.loads(json.dumps(payload))
    device = next(iter(payload["Body"]["Data"].values()))
    attributes = device["attributes"]
    attributes |= {
        "serial": "PRIVATE-1",
        "pmc.0": "PRIVATE-2",
        "device-id": "PRIVATE-3",
    }
    if "nameplate" in attributes:
        nameplate = json.loads(attributes["nameplate"]) | {"serial": "PRIVATE-4"}
        attributes["nameplate"] = json.dumps(nameplate)
    return payload


def test_no_serial_number_or_device_id_leaves_the_web_client():
    taken = json.dumps(
        [
            _parse_inverter_readable(with_private_fields(FIXTURE["inverter"]))[
                "readings"
            ],
            _parse_storage_readable(with_private_fields(FIXTURE["storage"]))[
                "readings"
            ],
        ]
    )

    assert "PRIVATE" not in taken


def test_every_sensor_on_the_gen24_shows_what_its_channel_reads():
    runtime = runtime_with(read_gen24())
    sensors = component_sensors(runtime)
    value = lambda key: sensors[key].value_fn(runtime)  # noqa: E731

    assert round(value("inverter_temperature"), 1) == 51.1
    assert value("storage_temperature") == 24.5
    assert round(value("module_temperature_1"), 1) == 44.3
    assert round(value("inverter_power_l1")) == 776
    assert value("production_limit") == 10100.0
    assert value("production_limit_reached") == "no"
    assert value("grid_valid") == "yes"
    assert value("battery_max_discharge_power") > 0
    assert value("power_stage_firmware_1") == "1.6.1-30738"
    assert value("storage_state_of_health") == 95.0
    assert value("storage_rated_soc_min") == 5
    assert value("storage_voltage_max") == 467.2
    assert value("storage_link") == "ModbusRTU rtu0:21"
    assert value("operating_time") == 81562808.0


def test_a_channel_the_device_does_not_report_makes_no_sensor():
    """This GEN24 reports power modules 1, 3 and 4; there is no module 2 to show."""
    sensors = component_sensors(runtime_with(read_gen24()))

    assert "module_temperature_2" not in sensors
    assert set(sensors) == ENABLED | DISABLED


def test_an_answer_without_a_value_keeps_its_sensor():
    """Audit R25-01: an answered read without a field retired the sensor.

    The cleanup at the next start removed 28 entities, the long-standing
    inverter temperature among them, with the owner's entity ids.
    """
    readings = read_gen24()
    readings.inverter_readings = {"MODULE_TEMPERATURE_MEAN_01_F32": 40.0}
    readings.storage_readings = {}
    runtime = runtime_with(readings)
    sensors = component_sensors(runtime)

    assert {"inverter_temperature", "fan_2", "storage_temperature"} <= set(sensors)
    assert sensors["inverter_temperature"].value_fn(runtime) is None


def test_an_answer_without_any_power_module_keeps_all_four():
    """The power modules follow the device, but an answer naming none of them is no answer."""
    readings = read_gen24()
    readings.inverter_readings = {"FANCONTROL_PERCENT_01_F32": 0.0}

    sensors = component_sensors(runtime_with(readings))

    assert {f"module_temperature_{index}" for index in (1, 2, 3, 4)} <= set(sensors)


def test_firmware_without_the_endpoints_gets_no_component_sensors():
    """Audit F24-06: a 404 made every component sensor, 19 of them enabled, stay unknown."""
    readings = WebData(inverter_endpoint_missing=True, storage_endpoint_missing=True)

    assert component_sensors(runtime_with(readings)) == {}


def test_readings_not_yet_read_keep_every_sensor():
    """Not read is not absent: the stale-entity cleanup would retire them (audit A24-01)."""
    sensors = component_sensors(runtime_with(WebData()))

    assert "module_temperature_2" in sensors


def test_the_defaults_are_the_owners_choice():
    sensors = component_sensors(runtime_with(read_gen24()))

    assert {
        k for k, d in sensors.items() if d.entity_registry_enabled_default
    } == ENABLED
    assert {k for k, d in sensors.items() if not d.entity_registry_enabled_default} == (
        DISABLED
    )


def test_the_battery_device_carries_its_firmware_and_hardware():
    runtime = runtime_with(read_gen24())
    entry = MagicMock(entry_id="01TESTENTRY")

    info = entities.device_info(runtime, entry, "storage")

    assert (info["sw_version"], info["hw_version"]) == ("3.26", "5.0")


def test_a_failed_readable_read_leaves_the_readings_unread():
    assert _parse_inverter_readable(None)["readings"] is None
    assert _parse_storage_readable(None)["readings"] is None


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (44.3, 44.3),
        (44, 44),
        ("44.3", 44.3),
        ("n/a", None),
        (True, None),
        ({"x": 1}, None),
    ],
)
def test_a_sensor_with_a_unit_shows_only_a_number(value, shown):
    """Audit R25-02: the old temperature parser took numbers only; a dict reached HA."""
    reading = next(r for r in COMPONENT_READINGS if r.key == "inverter_temperature")

    assert component_value(reading, {reading.fields[0]: value}) == shown


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (1.0, "yes"),
        (0.0, "no"),
        ("1", "yes"),
        ("0", "no"),
        ("true", "yes"),
        (False, "no"),
    ],
)
def test_a_flag_is_set_only_by_a_true_value(value, shown):
    """Audit R25-02: the text "0" read as "yes"."""
    reading = next(r for r in COMPONENT_READINGS if r.key == "grid_valid")

    assert component_value(reading, {reading.fields[0]: value}) == shown
