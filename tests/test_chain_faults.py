"""Faulty SunSpec chains and refused reads: what a poll makes of them."""

from datetime import timedelta
import logging

from modbus_connection import (
    IllegalDataAddressError,
    ServerDeviceBusyError,
    ServerDeviceFailureError,
)
from modbus_connection.mock import MockModbusUnit
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fronius_modbus.const import DOMAIN
from custom_components.fronius_modbus.coordinator import FroniusModbusCoordinator
from custom_components.fronius_modbus.fronius_modbus_api.device import FroniusInverter
from custom_components.fronius_modbus.fronius_modbus_api.storage import ExtendedMode

INVERTER_UNIT_ID, METER_UNIT_ID = 1, 200
# Addresses in the captured Symo GEN24 of tests/fixtures.
INVERTER_CHAIN_END = 40369
STORAGE_CONTROL_MODE = 40348
METER_MODEL_HEADER = 40069
# The length word of the meter unit's common model.
METER_COMMON_LENGTH = 40003
METER_CHAIN_END = METER_MODEL_HEADER + 105 + 2
REGISTER_SPACE = 0x10000


@pytest.fixture
def coordinator(hass, connection):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.0.2.1"})
    entry.add_to_hass(hass)
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


@pytest.fixture
def register_space_bound(monkeypatch):
    """The bound tmodbus, the backend Home Assistant uses, puts on every request."""
    original = MockModbusUnit.read_holding_registers

    async def bounded(self, address, count):
        if not 0 <= address < REGISTER_SPACE or address + count > REGISTER_SPACE:
            raise ValueError("Start address plus quantity must not exceed 65536.")
        return await original(self, address, count)

    monkeypatch.setattr(MockModbusUnit, "read_holding_registers", bounded)


async def test_a_mode_write_survives_polls_that_could_not_read_the_storage(
    coordinator, inverter_unit
):
    """Unread polls counted against the write, and the mode fell back after three."""
    await coordinator.async_refresh()
    storage = coordinator.storage_control
    await storage.set_mode(ExtendedMode.CHARGE_FROM_GRID)

    inverter_unit.fail_read(STORAGE_CONTROL_MODE, ServerDeviceFailureError())
    for _ in range(3):
        await coordinator.async_refresh()
        assert "storage" in coordinator.data.report.failed
    inverter_unit.fail_read(STORAGE_CONTROL_MODE, None)
    await coordinator.async_refresh()

    assert "storage" in coordinator.data.report.updated
    assert storage.extended_mode is ExtendedMode.CHARGE_FROM_GRID


async def test_a_chain_that_always_ends_in_a_refusal_warns_once(
    coordinator, inverter_unit, caplog
):
    """Every poll's rediscovery warned again, about 8,600 times a day."""
    inverter_unit.fail_read(INVERTER_CHAIN_END, IllegalDataAddressError())

    with caplog.at_level(logging.DEBUG):
        for _ in range(6):
            await coordinator.async_refresh()
            assert coordinator.last_update_success

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) <= 1, [r.getMessage() for r in warnings]


async def test_a_meter_model_in_an_incomplete_chain_is_placed(coordinator, meter_unit):
    """The meter model was read, but the refused end marker dropped the meter."""
    meter_unit.fail_read(METER_CHAIN_END, IllegalDataAddressError())

    await coordinator.async_refresh()

    assert METER_UNIT_ID in coordinator.device.meters


async def test_a_meter_that_refuses_its_probe_is_filed_with_its_refusal(
    coordinator, meter_unit
):
    """A busy meter was reported as one that does not answer."""
    meter_unit.fail_requests(ServerDeviceBusyError())

    await coordinator.async_refresh()

    assert isinstance(
        coordinator.data.report.failed["meter_200"], ServerDeviceBusyError
    )


async def test_a_model_length_past_the_register_space_fails_only_that_meter(
    coordinator, meter_unit, register_space_bound
):
    """A garbage length raised a bare ValueError that took the whole poll down."""
    meter_unit.holding[METER_MODEL_HEADER + 1] = 0xFFFE

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert "inverter" in coordinator.data.report.updated
    assert METER_UNIT_ID not in coordinator.device.meters


async def test_a_common_model_length_past_the_register_space_fails_only_that_meter(
    coordinator, meter_unit, register_space_bound
):
    """With the first model too long nothing was kept, and the bare ValueError
    went on to take the whole poll down."""
    meter_unit.holding[METER_COMMON_LENGTH] = 0xFFFE

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert "inverter" in coordinator.data.report.updated
    assert METER_UNIT_ID not in coordinator.device.meters
