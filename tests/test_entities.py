"""Entity descriptions read against a real runtime, and the total-sensor behaviour."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import logging
import time
from types import SimpleNamespace

from modbus_connection import (
    ModbusConnectionError,
    ModbusTimeoutError,
    ServerDeviceFailureError,
)
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
from custom_components.fronius_modbus.fronius_modbus_api.exceptions import (
    ControlUnavailable,
)
from custom_components.fronius_modbus.fronius_modbus_api.storage import ExtendedMode
from custom_components.fronius_modbus.froniuswebclient import (
    FroniusWebAuthError,
    FroniusWebResponseError,
    FroniusWebUnreachable,
)
from custom_components.fronius_modbus.sensor import FroniusSensor
from homeassistant.exceptions import HomeAssistantError

from .conftest import INVERTER_UNIT_ID, METER_UNIT_ID
from .test_web_control import make_control

# MinRsvPct in the captured fixture; the model-124 scale factor is -2.
SOC_MINIMUM_ADDRESS = 40350
# The model-123 header; failing it fails the controls report.
CONTROLS_HEADER_ADDRESS = 40227


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


# The window the integration opens around a web battery write.
TOLERATED_OUTAGE_SECONDS = 30


async def test_a_retained_poll_does_not_confirm_a_bad_energy_sample(
    hass, entry, runtime, inverter_unit
):
    """Audit B05: inside the tolerance window the old poll was served again as a fresh one."""
    sensor = _total_sensor(runtime, entry, hass)
    original = sensor.native_value

    _report(sensor, 120.0)
    runtime.modbus.tolerate_failures_until(time.monotonic() + TOLERATED_OUTAGE_SECONDS)
    inverter_unit.fail_requests(ModbusConnectionError())
    for _ in range(entities.TOTAL_INCREASING_RESET_POLLS):
        await runtime.modbus.async_refresh()
        assert runtime.modbus.last_update_success
        _report(sensor, 120.0)

    assert sensor.native_value == original


async def test_the_throttle_reason_needs_both_of_its_reports(
    hass, entry, connection, inverter_unit
):
    """It reads the status and the controls model, so a stale one must not answer."""
    runtime = await make_runtime(hass, entry, connection)
    description = _description(entities.sensor_descriptions(runtime), "throttle_reason")
    assert description.value_fn(runtime) == "none"

    inverter_unit.fail_read(CONTROLS_HEADER_ADDRESS, ServerDeviceFailureError())
    await runtime.modbus.async_refresh()

    assert description.value_fn(runtime) is None


async def test_the_battery_web_fields_show_once_storage_and_web_are_there(
    hass, entry, connection
):
    """The backup reserve and the SoC mode come from the web API's battery config."""
    runtime = await make_runtime(hass, entry, connection)
    assert not any(
        d.key == "backup_reserve" for d in entities.number_descriptions(runtime)
    )

    web_control = make_control(hass)
    web_control._client.battery.update(HYB_BACKUP_RESERVED=35)
    await web_control.async_refresh()
    runtime = replace(
        runtime, web_control=web_control, web=SimpleNamespace(data=web_control.data)
    )
    try:
        reserve = _description(entities.number_descriptions(runtime), "backup_reserve")
        soc_mode = _description(entities.select_descriptions(runtime), "api_soc_mode")
        assert reserve.value_fn(runtime) == 35
        assert soc_mode.value_fn(runtime) == "automatic"
        assert soc_mode.options == ["automatic", "manual"]
        await soc_mode.set_fn(runtime, 1)
        assert web_control._client.calls[-1] == ("soc_mode", "manual")
        assert not any(
            d.key == "api_soc_mode" for d in entities.sensor_descriptions(runtime)
        )
    finally:
        web_control.shutdown()


async def test_the_web_soc_limits_are_writable_in_manual_soc_mode_alone(
    hass, entry, connection
):
    """SoC Maximum used to hang on the energy-management mode, the wrong switch."""
    runtime = await make_runtime(hass, entry, connection)
    web_control = make_control(hass)
    web_control._client.battery.update(
        HYB_EM_MODE=0, BAT_M0_SOC_MODE="manual", BAT_M0_SOC_MIN=15
    )
    await web_control.async_refresh()
    runtime = replace(
        runtime, web_control=web_control, web=SimpleNamespace(data=web_control.data)
    )
    try:
        numbers = entities.number_descriptions(runtime)
        maximum = _description(numbers, "soc_maximum")
        minimum = _description(numbers, "api_soc_minimum")
        assert maximum.available_fn(runtime)
        assert minimum.available_fn(runtime)
        assert minimum.value_fn(runtime) == 15
        await minimum.set_fn(runtime, 25)
    finally:
        web_control.shutdown()
    assert web_control._client.calls[-1] == ("soc", 25, None)


# Model-123 WMaxLimPct_Ena and WMaxLimPct in the captured fixture.
LIMIT_ENABLE_ADDRESS = 40236
LIMIT_PERCENT_ADDRESS = 40232
UNIMPLEMENTED_UINT16 = 0xFFFF


async def test_an_unimplemented_enable_flag_leaves_the_throttle_reason_unknown(
    hass, entry, connection, inverter_unit
):
    """Audit E02: the coordinator turned the sentinel into `== 1` -> False -> `none`."""
    inverter_unit.holding[LIMIT_ENABLE_ADDRESS] = UNIMPLEMENTED_UINT16
    runtime = await make_runtime(hass, entry, connection)
    assert runtime.device.controls.w_max_lim_ena is None
    description = _description(entities.sensor_descriptions(runtime), "throttle_reason")
    assert description.value_fn(runtime) is None


# Model-203 TotWhExp of the captured meter: two registers, 0 is SunSpec's "not accumulated".
METER_EXPORTED_ADDRESS = 40107


async def test_a_zero_accumulator_is_no_reading_not_a_reset(
    hass, entry, connection, caplog
):
    """callifo #125: after a firmware update the meter served 0 Wh for a while.

    Upstream's guard refused it on every poll and logged each time; a guard that
    adopted it after three polls would hand the energy dashboard a false reset.
    The library decodes an accumulator of 0 as None, and None is no observation.
    """
    runtime = await make_runtime(hass, entry, connection)
    description = _description(
        entities.sensor_descriptions(runtime), "meter_200_exported"
    )
    sensor = entities.FroniusTotalSensor(runtime, entry, description)
    sensor.hass = hass
    before = sensor.native_value
    assert before

    meter = connection.for_unit(METER_UNIT_ID)
    meter.holding[METER_EXPORTED_ADDRESS] = 0
    meter.holding[METER_EXPORTED_ADDRESS + 1] = 0
    with caplog.at_level(logging.WARNING):
        for _ in range(entities.TOTAL_INCREASING_RESET_POLLS + 1):
            await runtime.modbus.async_refresh()
            sensor._observe_poll()

    assert description.value_fn(runtime) is None
    assert sensor.native_value == before
    assert not [r for r in caplog.records if r.name.endswith("entities")]


async def test_an_unreachable_web_interface_is_a_translated_error():
    """Audit R24-02: FroniusWebUnreachable is an OSError, which no write mapped.

    A web control used while the inverter's web server was down ended in an
    untranslated error with a traceback.
    """

    async def unreachable():
        raise FroniusWebUnreachable("ConnectionError")

    with pytest.raises(HomeAssistantError) as raised:
        await entities.FroniusEntity.async_run_write(None, unreachable)

    assert raised.value.translation_key == "web_api_unreachable"


async def test_grid_charging_that_the_web_api_refuses_is_an_error(
    hass, entry, connection
):
    """Audit F24-02: the web failure was only a warning; the user saw success.

    The Modbus mode is already switched when the web charge flags fail, so the
    error says that the mode is set and grid charging is not.
    """
    runtime = await make_runtime(hass, entry, connection)
    web_control = make_control(hass)

    def refuse(charge_from_grid=None, charge_from_ac=None):
        raise FroniusWebResponseError("HTTP 500 on /api/config/batteries", 500)

    web_control._client.set_battery_charge_sources = refuse
    await web_control.async_refresh()
    runtime = replace(
        runtime, web_control=web_control, web=SimpleNamespace(data=web_control.data)
    )
    try:
        with pytest.raises(ControlUnavailable) as raised:
            await runtime.async_set_extended_mode(ExtendedMode.CHARGE_FROM_GRID)
    finally:
        web_control.shutdown()

    assert raised.value.key == "grid_charge_sources_failed"
    assert runtime.storage_control.extended_mode is ExtendedMode.CHARGE_FROM_GRID
