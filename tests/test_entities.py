"""Entity descriptions read against a real runtime, and the total-sensor behaviour."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from modbus_connection import ModbusTimeoutError, ServerDeviceFailureError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fronius_modbus import entities
from custom_components.fronius_modbus.const import DOMAIN
from custom_components.fronius_modbus.coordinator import (
    FroniusModbusCoordinator,
    FroniusRuntimeData,
    FroniusWebCoordinator,
)
from custom_components.fronius_modbus.fronius_modbus_api.device import FroniusInverter
from custom_components.fronius_modbus.froniuswebclient import FroniusWebAuthError
from custom_components.fronius_modbus.sensor import FroniusSensor

from .conftest import INVERTER_UNIT_ID, METER_UNIT_ID
from .test_web_control import make_control

# MinRsvPct in the captured fixture; the model-124 scale factor is -2.
SOC_MINIMUM_ADDRESS = 40350


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.0.2.1"})
    entry.add_to_hass(hass)
    return entry


async def make_runtime(
    hass, entry, connection, meter_unit_ids=(METER_UNIT_ID,)
) -> FroniusRuntimeData:
    """A FroniusRuntimeData built on a fully-refreshed Modbus coordinator."""
    device = FroniusInverter(
        connection.for_unit(INVERTER_UNIT_ID),
        INVERTER_UNIT_ID,
        {unit_id: connection.for_unit(unit_id) for unit_id in meter_unit_ids},
    )
    coordinator = FroniusModbusCoordinator(
        hass,
        entry,
        device,
        interval=timedelta(seconds=10),
        primary_meter_unit_id=METER_UNIT_ID,
        meter_locations={METER_UNIT_ID: 0},
    )
    await coordinator.async_refresh()
    return FroniusRuntimeData(
        device=device,
        modbus=coordinator,
        web=None,
        web_control=None,
        meter_locations={METER_UNIT_ID: 0},
        primary_meter_unit_id=METER_UNIT_ID,
    )


@pytest.fixture
async def runtime(hass, entry, connection):
    return await make_runtime(hass, entry, connection)


def _description(descriptions, key):
    return next(d for d in descriptions if d.key == key)


def _report(sensor, value) -> None:
    """Deliver one poll reporting `value`, leaving the shared table alone.

    The description is swapped on the sensor rather than mutated: the table's
    instances are shared across every test in the session. The poll is what
    the coordinator would deliver; reading the property alone judges nothing.
    """
    sensor.entity_description = replace(
        sensor.entity_description, value_fn=lambda r: value
    )
    sensor._observe_poll()


def _total_sensor(runtime, entry, hass):
    description = _description(entities.sensor_descriptions(runtime), "acenergy")
    sensor = entities.FroniusTotalSensor(runtime, entry, description)
    sensor.hass = hass
    sensor._observe_poll()
    return sensor


async def test_sensor_descriptions_carry_the_polled_values(runtime):
    descriptions = entities.sensor_descriptions(runtime)
    assert _description(descriptions, "acpower").value_fn(runtime) == 3075.1
    assert _description(descriptions, "meter_200_power").value_fn(runtime) == 30.0
    assert (
        _description(descriptions, "mppt_module_0_dc_power").value_fn(runtime) == 2908.2
    )
    assert not any(d.key.startswith("mppt_module_2_") for d in descriptions)


async def test_a_meter_entity_is_unavailable_after_its_meter_fails(
    hass, entry, runtime, connection
):
    description = _description(entities.sensor_descriptions(runtime), "meter_200_power")
    entity = entities.FroniusEntity(runtime, entry, description)
    entity.hass = hass
    assert entity.available is True

    meter_unit = connection.for_unit(METER_UNIT_ID)
    meter_unit.fail_requests(ModbusTimeoutError())
    await runtime.modbus.async_refresh()

    assert entity.available is False


async def test_a_total_sensor_keeps_its_value_when_a_poll_reports_none(
    hass, entry, runtime
):
    sensor = _total_sensor(runtime, entry, hass)

    assert sensor.native_value == 33187794.59

    # A poll that reports no value, the same way a real Modbus timeout on "wh"
    # alone would. The description is swapped on the sensor rather than mutated:
    # the table's instances are shared across every test in the session.
    _report(sensor, None)
    assert sensor.native_value == 33187794.59


async def test_a_total_sensor_ignores_a_lower_value(hass, entry, runtime):
    sensor = _total_sensor(runtime, entry, hass)

    assert sensor.native_value == 33187794.59

    _report(sensor, 33187794.58)
    assert sensor.native_value == 33187794.59


async def test_a_total_sensor_ignores_an_implausible_jump(hass, entry, runtime):
    sensor = _total_sensor(runtime, entry, hass)

    assert sensor.native_value == 33187794.59

    # A literal jump, not the module's own limit: reading the constant back
    # would make the test agree with whatever the module says.
    too_high = 33187794.59 + 200_000
    _report(sensor, too_high)
    assert sensor.native_value == 33187794.59


async def test_a_total_sensor_accepts_a_plausible_higher_value(hass, entry, runtime):
    sensor = _total_sensor(runtime, entry, hass)

    assert sensor.native_value == 33187794.59

    higher = 33187794.59 + entities.TOTAL_INCREASING_MAX_STEP_WH - 1
    _report(sensor, higher)
    assert sensor.native_value == higher


async def test_an_entity_without_placeholders_does_not_crash_on_its_name(
    hass, entry, runtime
):
    """A description with no translation_placeholders must not touch the attribute HA owns."""
    description = _description(entities.sensor_descriptions(runtime), "acpower")
    assert description.translation_placeholders is None

    entity = FroniusSensor.create(runtime, entry, description)
    entity.hass = hass

    assert isinstance(entity.translation_placeholders, dict)
    assert entity.has_entity_name is True


async def test_an_invalid_soc_minimum_writes_nothing(hass, entry, connection):
    """Modbus and the web API must not end up disagreeing about the reserve."""
    runtime = await make_runtime(hass, entry, connection)
    web_control = make_control(hass)
    web_control._client.battery.update(
        HYB_EM_MODE=1, BAT_M0_SOC_MODE="manual", BAT_M0_SOC_MAX=20
    )
    await web_control.async_refresh()
    runtime = replace(runtime, web_control=web_control)

    writes = []
    connection.for_unit(INVERTER_UNIT_ID).on_write(writes.append)
    try:
        with pytest.raises(ValueError):
            await runtime.async_set_soc_minimum(50)
    finally:
        web_control.shutdown()

    assert writes == []


async def test_a_concurrent_maximum_cannot_slip_between_check_and_write(
    hass, entry, connection
):
    """Audit A05: the check ran outside the web lock, so Modbus took a refused minimum."""
    runtime = await make_runtime(hass, entry, connection)
    web_control = make_control(hass)
    web_control._client.battery.update(HYB_EM_MODE=1, BAT_M0_SOC_MODE="manual")
    await web_control.async_refresh()
    runtime = replace(runtime, web_control=web_control)
    storage = runtime.storage_control
    entered = asyncio.Event()
    resume = asyncio.Event()
    write_minimum = storage.set_minimum_reserve

    async def paused_write(percent):
        entered.set()
        await resume.wait()
        await write_minimum(percent)

    storage.set_minimum_reserve = paused_write
    try:
        minimum = asyncio.create_task(runtime.async_set_soc_minimum(50))
        await entered.wait()
        maximum = asyncio.create_task(web_control.set_soc_maximum(20))
        await asyncio.sleep(0)
        resume.set()
        await minimum
        with pytest.raises(ValueError):
            await maximum
    finally:
        web_control.shutdown()

    # Read the register itself, not the cached decode: the point is what the
    # device ended up with.
    assert connection.for_unit(INVERTER_UNIT_ID).holding[SOC_MINIMUM_ADDRESS] == 5000
    assert web_control.data.soc_min == 50


async def test_a_total_sensor_follows_a_genuine_counter_reset(hass, entry, runtime):
    """A replaced meter really does restart at zero; refusing that forever freezes the sensor."""
    sensor = _total_sensor(runtime, entry, hass)
    assert sensor.native_value == 33187794.59

    for _ in range(entities.TOTAL_INCREASING_RESET_POLLS - 1):
        _report(sensor, 120.0)
        assert sensor.native_value == 33187794.59
    _report(sensor, 120.0)
    assert sensor.native_value == 120.0


async def test_a_total_sensor_reset_needs_consecutive_lower_polls(hass, entry, runtime):
    """A missing poll between lower readings must not count toward a counter reset."""
    sensor = _total_sensor(runtime, entry, hass)
    assert sensor.native_value == 33187794.59

    _report(sensor, 120.0)
    assert sensor.native_value == 33187794.59
    _report(sensor, None)
    assert sensor.native_value == 33187794.59
    _report(sensor, 120.0)
    assert sensor.native_value == 33187794.59
    _report(sensor, 120.0)
    assert sensor.native_value == 33187794.59

    # Three CONSECUTIVE lower polls, with no gap, still adopt the reset.
    other_sensor = _total_sensor(runtime, entry, hass)
    assert other_sensor.native_value == 33187794.59
    for _ in range(entities.TOTAL_INCREASING_RESET_POLLS - 1):
        _report(other_sensor, 120.0)
        assert other_sensor.native_value == 33187794.59
    _report(other_sensor, 120.0)
    assert other_sensor.native_value == 120.0


async def test_an_absent_first_meter_does_not_renumber_the_second(
    hass, entry, connection
):
    """A meter that is unplugged must not move the next one's display position.

    The position is a label the user sees on the device; deriving it from the
    meters that answered would rename "Meter 2" to "Meter 1" the moment the
    first one stops responding.
    """
    absent_unit_id = METER_UNIT_ID - 1
    runtime = await make_runtime(
        hass, entry, connection, meter_unit_ids=(absent_unit_id, METER_UNIT_ID)
    )
    assert list(runtime.device.meters) == [METER_UNIT_ID]

    info = entities.device_info(runtime, entry, "meter", METER_UNIT_ID)

    assert info["name"].endswith("Meter 2")


async def test_every_enum_sensor_value_is_one_of_its_options(runtime):
    """Audit F04: the zero-flag control state read "Normal" while the options said "normal"."""
    offenders = [
        (description.key, description.value_fn(runtime))
        for description in entities.sensor_descriptions(runtime)
        if description.options is not None
        and description.value_fn(runtime) is not None
        and description.value_fn(runtime) not in description.options
    ]
    assert offenders == []


async def test_storage_rate_numbers_follow_the_storage_report(
    runtime, entry, inverter_unit
):
    """Audit F15: the rate numbers stayed available while every storage register read failed."""
    address = runtime.device.storage.resolved_fields["model_id"].address
    inverter_unit.fail_read(address, ModbusTimeoutError())
    await runtime.modbus.async_refresh()
    description = next(
        d for d in entities.number_descriptions(runtime) if d.key == "charge_limit"
    )
    entity = entities.FroniusEntity(runtime, entry, description)
    assert "storage" in runtime.modbus.data.report.failed
    assert entity.available is False


async def test_a_total_sensor_follows_a_large_but_sustained_gap(hass, entry, runtime):
    """Audit F05: after a gap above the step limit every later reading was rejected forever."""
    sensor = _total_sensor(runtime, entry, hass)
    original = sensor.native_value
    for increment in range(1, entities.TOTAL_INCREASING_RESET_POLLS + 1):
        _report(sensor, original + entities.TOTAL_INCREASING_MAX_STEP_WH + increment)
    assert sensor.native_value > original


async def test_reading_a_total_sensor_does_not_count_as_a_poll(hass, entry, runtime):
    """Audit F06: three property reads of one bad sample were taken for three polls."""
    sensor = _total_sensor(runtime, entry, hass)
    original = sensor.native_value
    _report(sensor, 120.0)
    reads = [sensor.native_value for _ in range(entities.TOTAL_INCREASING_RESET_POLLS)]
    assert reads == [original] * entities.TOTAL_INCREASING_RESET_POLLS


async def test_web_controls_go_unavailable_when_the_customer_login_is_rejected(
    hass, entry, runtime
):
    """Audit F16: after a rejected login the switches stayed available and did nothing."""
    control = make_control(hass)
    runtime.web_control = control
    runtime.web = FroniusWebCoordinator(
        hass, entry, control, interval=timedelta(seconds=60)
    )
    try:
        await runtime.web.async_refresh()
        description = next(
            d
            for d in entities.switch_descriptions(runtime)
            if d.key == "api_solar_api_enabled"
        )
        entity = entities.FroniusEntity(runtime, entry, description)
        assert entity.available
        control._client.get_inverter_info = lambda: (_ for _ in ()).throw(
            FroniusWebAuthError("rejected")
        )
        await runtime.web.async_refresh()
        assert not control.configured
        assert not entity.available
    finally:
        control.shutdown()


# The model-103 header in the captured fixture: failing it fails the inverter report.
INVERTER_HEADER_ADDRESS = 40069


async def test_failed_polls_do_not_confirm_a_bad_energy_sample(
    hass, entry, runtime, inverter_unit
):
    """Audit A02: a failed read keeps the last decoded value, which confirmed itself."""
    sensor = _total_sensor(runtime, entry, hass)
    original = sensor.native_value

    _report(sensor, 120.0)
    inverter_unit.fail_read(INVERTER_HEADER_ADDRESS, ServerDeviceFailureError())
    for _ in range(entities.TOTAL_INCREASING_RESET_POLLS):
        await runtime.modbus.async_refresh()
        assert "inverter" in runtime.modbus.data.report.failed
        _report(sensor, 120.0)

    assert sensor.native_value == original
