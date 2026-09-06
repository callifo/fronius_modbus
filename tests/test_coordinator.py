"""The Modbus coordinator turns the device's report into entity data, and fails the right way."""

import asyncio
from datetime import timedelta
import time
from unittest.mock import MagicMock

from modbus_connection import (
    ModbusConnectionError,
    ModbusTimeoutError,
    ServerDeviceFailureError,
)
from modbus_connection.model.sunspec import SunSpecMapShiftError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fronius_modbus.const import DOMAIN
from custom_components.fronius_modbus.coordinator import (
    DEFAULT_MAX_RATE_W,
    FroniusModbusCoordinator,
)
from custom_components.fronius_modbus.fronius_modbus_api.device import FroniusInverter
from custom_components.fronius_modbus.fronius_modbus_api.sunspec_models import Nameplate
from homeassistant.helpers.update_coordinator import UpdateFailed

from .conftest import INVERTER_UNIT_ID, METER_UNIT_ID


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.0.2.1"})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def coordinator(hass, entry, connection):
    device = FroniusInverter(
        connection.for_unit(INVERTER_UNIT_ID),
        INVERTER_UNIT_ID,
        {METER_UNIT_ID: connection.for_unit(METER_UNIT_ID)},
    )
    return FroniusModbusCoordinator(
        hass,
        entry,
        device,
        interval=timedelta(seconds=10),
        primary_meter_unit_id=METER_UNIT_ID,
        meter_locations={METER_UNIT_ID: 0},
    )


async def test_a_poll_yields_report_load_and_grid_status(coordinator):
    poll = await coordinator._async_update_data()
    assert "inverter" in poll.report.updated
    assert poll.load_w == 3105.1  # meter 30 W + inverter 3075.1 W
    assert poll.grid_status == "on_grid_operating"
    assert (
        coordinator.storage_control is not None
        and coordinator.inverter_controls is not None
    )


async def test_a_dead_link_is_a_failed_update(coordinator, inverter_unit):
    inverter_unit.fail_requests(ModbusConnectionError())
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_failures_are_tolerated_inside_the_write_window(
    coordinator, inverter_unit
):
    first = await coordinator._async_update_data()
    coordinator.data = first
    coordinator.tolerate_failures_until(10**9)
    inverter_unit.fail_requests(ModbusConnectionError())
    assert await coordinator._async_update_data() is first


async def test_three_timeouts_recycle_the_link(coordinator, inverter_unit, connection):
    await coordinator._async_update_data()
    inverter_unit.fail_requests(ModbusTimeoutError())
    for _ in range(3):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    assert connection.connected is False


async def test_a_shifted_map_reloads_the_entry(coordinator, hass, entry, monkeypatch):
    await coordinator._async_update_data()
    reload = MagicMock()
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", reload)

    async def shifted():
        raise SunSpecMapShiftError("moved")

    monkeypatch.setattr(coordinator.device, "async_update", shifted)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    reload.assert_called_once_with(entry.entry_id)


async def test_failures_outside_the_write_window_are_not_tolerated(
    coordinator, inverter_unit
):
    """The tolerance is bounded by the deadline, not by having data at all.

    A window that never closes would keep every entity on its last value for
    the rest of the run, with nothing saying the inverter is gone.
    """
    first = await coordinator._async_update_data()
    coordinator.data = first
    coordinator.tolerate_failures_until(0.0)
    inverter_unit.fail_requests(ModbusConnectionError())

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_a_successful_poll_ends_the_tolerance_window(coordinator, inverter_unit):
    """A window left open after the link recovered would swallow the next real outage."""
    coordinator.data = await coordinator._async_update_data()
    coordinator.tolerate_failures_until(10**9)
    await coordinator._async_update_data()

    inverter_unit.fail_requests(ModbusConnectionError())
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_the_link_is_recycled_when_the_window_expires_failing(
    coordinator, inverter_unit, connection
):
    """A socket that outlived the inverter's restart answers nothing until it is reopened."""
    coordinator.data = await coordinator._async_update_data()
    coordinator.tolerate_failures_until(time.monotonic() + 0.05)
    inverter_unit.fail_requests(ModbusConnectionError())
    assert await coordinator._async_update_data() is coordinator.data

    await asyncio.sleep(0.1)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert connection.connected is False


async def test_derived_values_need_a_fresh_inverter_reading(coordinator, inverter_unit):
    """Audit F14: a failed inverter block still produced load and grid status from stale power."""
    await coordinator.async_refresh()
    address = coordinator.device.inverter.resolved_fields["model_id"].address
    inverter_unit.fail_read(address, ServerDeviceFailureError())
    await coordinator.async_refresh()
    assert "inverter" in coordinator.data.report.failed
    assert coordinator.data.load_w is None
    assert coordinator.data.grid_status is None


async def test_the_nameplate_rates_replace_the_fallback_once_read(
    coordinator, inverter_unit, monkeypatch
):
    """Audit F09: a first poll without model 120 fixed the storage control on 11000 W."""
    original_update = Nameplate.async_update
    calls = {"failed": False}

    async def fail_first(component):
        if not calls["failed"]:
            calls["failed"] = True
            raise ServerDeviceFailureError
        await original_update(component)

    monkeypatch.setattr(Nameplate, "async_update", fail_first)
    await coordinator.async_refresh()
    assert "nameplate" in coordinator.data.report.failed
    assert coordinator.storage_control.max_charge_rate_w == DEFAULT_MAX_RATE_W
    await coordinator.async_refresh()
    assert coordinator.storage_control.max_charge_rate_w == 10240
    assert coordinator.storage_control.max_discharge_rate_w == 10240


# A register inside the model-160 block of the captured fixture: the block read fails,
# the header scan does not.
MPPT_HEADER_ADDRESS = 40260


async def test_a_sub_system_answering_for_the_first_time_reloads_the_entry(
    coordinator, inverter_unit, hass, entry, monkeypatch
):
    """Audit F03: MPPT entities built from a failed first read never appeared."""
    inverter_unit.fail_read(MPPT_HEADER_ADDRESS, ServerDeviceFailureError())
    await coordinator.async_refresh()
    assert "mppt" in coordinator.data.report.failed
    reload = MagicMock()
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", reload)
    inverter_unit.fail_read(MPPT_HEADER_ADDRESS, None)
    await coordinator.async_refresh()
    reload.assert_called_once_with(entry.entry_id)
    await coordinator.async_refresh()
    reload.assert_called_once()
