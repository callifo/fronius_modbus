"""Reading the serial number an address answers with, as the address check does."""

from modbus_connection import ModbusConnectionError

from custom_components.fronius_modbus import discovery
from custom_components.fronius_modbus.fronius_modbus_api.device import FroniusInverter

from .conftest import INVERTER_UNIT_ID

HOST = "192.0.2.10"
PORT = 1502
OTHER_UNIT_ID = 7


async def test_the_serial_is_read_from_the_entry_s_unit(
    hass, mock_modbus, inverter_unit
):
    mock_modbus.add_unit(OTHER_UNIT_ID, like=INVERTER_UNIT_ID)
    mock_modbus.fail_requests(INVERTER_UNIT_ID, ModbusConnectionError("refused"))

    serial = await discovery.async_serial_at(hass, HOST, PORT, OTHER_UNIT_ID)

    identity = await FroniusInverter.async_probe(inverter_unit)
    assert serial == identity.serial != ""
    params = mock_modbus.params_seen[-1]
    assert (params.host, params.port) == (HOST, PORT)


async def test_an_address_that_does_not_answer_has_no_serial(hass, mock_modbus):
    mock_modbus.fail_requests(INVERTER_UNIT_ID, ModbusConnectionError("refused"))

    assert await discovery.async_serial_at(hass, HOST, PORT, INVERTER_UNIT_ID) is None
