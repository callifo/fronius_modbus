"""The registers of a Datamanager that are not part of SunSpec."""

from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusTimeoutError,
    ServerDeviceBusyError,
)
from modbus_connection.mock import MockModbusConnection
import pytest

from custom_components.fronius_modbus.fronius_modbus_api.device import (
    REPORT_SITE,
    REPORT_STATE_CODE,
    FroniusInverter,
)

from .conftest import INVERTER_UNIT_ID, load_fixture

STATE_CODE_ADDRESS = 213
SITE_ADDRESS = 499
# 1 200 W, 5 600 Wh today, 70 000 Wh this year, 4 294 967 396 Wh in total.
SITE_WORDS = [0, 1200, 0, 0, 0, 5600, 0, 0, 1, 4464, 0, 1, 0, 100]


def fronius_words(code: int = 307) -> dict[int, int]:
    """The Fronius registers of a Datamanager, by address."""
    return {STATE_CODE_ADDRESS: code} | {
        SITE_ADDRESS + offset: word for offset, word in enumerate(SITE_WORDS)
    }


@pytest.fixture
def unit():
    """The inverter of issue 110, with the Fronius registers its dump does not hold."""
    unit = MockModbusConnection().for_unit(INVERTER_UNIT_ID)
    unit.load_raw(load_fixture("symo_datamanager.json")[INVERTER_UNIT_ID])
    unit.load_raw({"holding": fronius_words()})
    return unit


def device(unit, *, datamanager: bool = True) -> FroniusInverter:
    return FroniusInverter(unit, INVERTER_UNIT_ID, {}, datamanager=datamanager)


async def test_the_state_code_and_the_site_totals_are_read(unit):
    inverter = device(unit)
    report = await inverter.async_update()

    assert inverter.state_code.code == 307
    assert inverter.site.power == 1200
    assert inverter.site.energy_day == 5600
    assert inverter.site.energy_year == 70000
    assert inverter.site.energy_total == 4294967396
    assert {REPORT_STATE_CODE, REPORT_SITE} <= set(report.updated)


async def test_a_value_the_device_does_not_have_is_unknown(unit):
    unit.load_raw({"holding": dict.fromkeys(fronius_words(), 0xFFFF)})
    inverter = device(unit)
    await inverter.async_update()

    assert inverter.state_code.code is None
    assert inverter.site.power is None
    assert inverter.site.energy_total is None


async def test_no_other_inverter_is_asked_for_them(unit):
    inverter = device(unit, datamanager=False)
    await inverter.async_update()

    assert inverter.state_code is None
    assert inverter.site is None
    asked = {event.address for event in unit.read_events}
    assert not asked & set(fronius_words())


async def test_a_refused_block_is_absent(unit):
    for address in fronius_words():
        unit.fail_read(address, IllegalDataAddressError())
    inverter = device(unit)
    report = await inverter.async_update()

    assert inverter.state_code is None
    assert inverter.site is None
    assert report.failed == {}


async def test_one_refused_block_leaves_the_other(unit):
    unit.fail_read(STATE_CODE_ADDRESS, IllegalDataAddressError())
    inverter = device(unit)
    await inverter.async_update()

    assert inverter.state_code is None
    assert inverter.site.power == 1200


@pytest.mark.parametrize("error", [ModbusTimeoutError, ServerDeviceBusyError])
async def test_a_silent_block_is_kept_and_read_again(unit, error):
    unit.fail_read(SITE_ADDRESS, error())
    inverter = device(unit)
    report = await inverter.async_update()

    assert inverter.site is not None
    assert set(report.failed) == {REPORT_SITE}
    assert inverter.inverter.w is not None

    unit.fail_read(SITE_ADDRESS, None)
    report = await inverter.async_update()

    assert report.failed == {}
    assert inverter.site.power == 1200


async def test_a_lost_connection_fails_the_poll(unit):
    unit.fail_read(SITE_ADDRESS, ModbusConnectionError())

    with pytest.raises(ModbusConnectionError):
        await device(unit).async_update()


async def test_a_hybrid_has_no_state_code(unit):
    """The register is not supported there and would show a false code."""
    model = b"Symo Hybrid 5.0-3-S".ljust(32, b"\0")
    words = {
        40020 + offset: int.from_bytes(model[2 * offset : 2 * offset + 2], "big")
        for offset in range(16)
    }
    unit.load_raw({"holding": words})
    inverter = device(unit)
    await inverter.async_update()

    assert inverter.state_code is None
    assert inverter.site.power == 1200
