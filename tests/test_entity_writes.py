"""Every write action of the entity tables, and the call it must make.

Audit F10: 20 of 25 write actions ran in no test. A Conn select that took the
inverter off the grid on "enabled", or swapped Solar API switch actions, kept
the whole suite green.
"""

from unittest.mock import AsyncMock, Mock, call

import pytest

from custom_components.fronius_modbus import entities

storage = call.storage_control
controls = call.inverter_controls
web = call.web_control

# (platform, key, input, the one call it must make). A number takes a value, a
# select the option the user picks, a switch or a button the action to run.
WRITE_ACTIONS = [
    ("number", "grid_discharge_power", 2500, storage.set_grid_discharge_power_w(2500)),
    ("number", "grid_charge_power", 2500, storage.set_grid_charge_power_w(2500)),
    ("number", "discharge_limit", 2500, storage.set_discharge_limit_w(2500)),
    ("number", "charge_limit", 2500, storage.set_charge_limit_w(2500)),
    ("number", "soc_minimum", 20, call.async_set_soc_minimum(20)),
    ("number", "ac_limit_rate", 4000, controls.set_ac_limit_w(4000)),
    ("number", "power_factor", 0.95, controls.set_power_factor(0.95)),
    ("number", "api_battery_power", -3000, web.set_battery_power_w(-3000)),
    ("number", "soc_maximum", 89.6, web.set_soc_maximum(90)),
    ("number", "api_soc_minimum", 24.6, web.set_soc_minimum_manual(25)),
    ("number", "backup_reserve", 30.4, web.set_backup_reserve(30)),
    ("number", "export_soft_limit", 7000, web.set_export_soft_limit_w(7000)),
    ("select", "ext_control_mode", "charge_from_grid", call.async_set_extended_mode(4)),
    ("select", "api_soc_mode", "manual", web.set_soc_mode(manual=True)),
    ("select", "api_soc_mode", "automatic", web.set_soc_mode(manual=False)),
    ("select", "api_battery_mode", "manual", web.set_battery_mode(1)),
    ("select", "api_battery_mode", "auto", web.set_battery_mode(0)),
    ("select", "ac_limit_enable", "enabled", controls.set_ac_limit_enable(True)),
    ("select", "ac_limit_enable", "disabled", controls.set_ac_limit_enable(False)),
    (
        "select",
        "power_factor_enable",
        "enabled",
        controls.set_power_factor_enable(True),
    ),
    (
        "select",
        "power_factor_enable",
        "disabled",
        controls.set_power_factor_enable(False),
    ),
    ("select", "Conn", "enabled", controls.set_connected(True)),
    ("select", "Conn", "disabled", controls.set_connected(False)),
    (
        "switch",
        "api_charge_from_ac",
        "turn_on",
        web.set_charge_sources(charge_from_ac=True),
    ),
    (
        "switch",
        "api_charge_from_ac",
        "turn_off",
        web.set_charge_sources(charge_from_grid=False, charge_from_ac=False),
    ),
    (
        "switch",
        "api_charge_from_grid",
        "turn_on",
        web.set_charge_sources(charge_from_grid=True, charge_from_ac=True),
    ),
    (
        "switch",
        "api_charge_from_grid",
        "turn_off",
        web.set_charge_sources(charge_from_grid=False),
    ),
    ("switch", "api_solar_api_enabled", "turn_on", web.set_solar_api_enabled(True)),
    ("switch", "api_solar_api_enabled", "turn_off", web.set_solar_api_enabled(False)),
    ("button", "reset_modbus_control", "press", web.reset_modbus_control()),
]


def _every_control_runtime():
    """A runtime on which every control exists and every write is recorded."""
    return Mock(
        storage_control=AsyncMock(),
        inverter_controls=AsyncMock(),
        web_control=AsyncMock(),
        async_set_soc_minimum=AsyncMock(),
        async_set_extended_mode=AsyncMock(),
    )


def _control_descriptions(runtime):
    return {
        "number": entities.number_descriptions(runtime),
        "select": entities.select_descriptions(runtime),
        "switch": entities.switch_descriptions(runtime),
        "button": entities.button_descriptions(runtime),
    }


def test_the_write_table_names_every_control():
    every_control = {
        (platform, description.key)
        for platform, descriptions in _control_descriptions(
            _every_control_runtime()
        ).items()
        for description in descriptions
    }

    assert {(platform, key) for platform, key, *_ in WRITE_ACTIONS} == every_control


@pytest.mark.parametrize(("platform", "key", "given", "expected"), WRITE_ACTIONS)
async def test_every_control_writes_through_the_right_call(
    platform, key, given, expected
):
    runtime = _every_control_runtime()
    description = next(
        d for d in _control_descriptions(runtime)[platform] if d.key == key
    )
    runtime.reset_mock()

    if platform == "number":
        await description.set_fn(runtime, given)
    elif platform == "select":
        codes = {option: code for code, option in description.options_map.items()}
        await description.set_fn(runtime, codes[given])
    else:
        await getattr(description, given)(runtime)

    assert runtime.mock_calls == [expected]
