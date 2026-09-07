"""FroniusInverter: discovery over the SunSpec chain, and one poll as a report."""

from modbus_connection import (
    GatewayTargetError,
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusTimeoutError,
)
from modbus_connection.mock import MockModbusConnection
import pytest

from custom_components.fronius_modbus.fronius_modbus_api.device import (
    REPORT_INVERTER,
    REPORT_MPPT,
    REPORT_STORAGE,
    FroniusInverter,
    meter_report_name,
)
from custom_components.fronius_modbus.fronius_modbus_api.exceptions import (
    NotAFroniusInverter,
)

from .conftest import INVERTER_UNIT_ID, METER_UNIT_ID


@pytest.fixture
def device(connection):
    return FroniusInverter(
        connection.for_unit(INVERTER_UNIT_ID),
        INVERTER_UNIT_ID,
        {METER_UNIT_ID: connection.for_unit(METER_UNIT_ID)},
    )


async def test_probe_returns_the_identity(inverter_unit):
    identity = await FroniusInverter.async_probe(inverter_unit)
    assert (identity.manufacturer, identity.model, identity.version) == (
        "Fronius",
        "Symo GEN24 10.0",
        "1.38.6-1",
    )
    assert identity.address == 1


async def test_probe_rejects_a_device_without_an_inverter_model():
    unit = MockModbusConnection().for_unit(1)
    unit.load_raw({"holding": {40000: 0x5375, 40001: 0x6E53, 40002: 0xFFFF, 40003: 0}})
    with pytest.raises(NotAFroniusInverter):
        await FroniusInverter.async_probe(unit)


async def test_the_first_update_discovers_every_sub_system(device):
    report = await device.async_update()
    assert device.three_phase is True
    assert device.storage is not None and device.mppt is not None
    assert set(report.updated) == {
        "inverter",
        "nameplate",
        "settings",
        "status",
        "controls",
        "mppt",
        "storage",
        meter_report_name(METER_UNIT_ID),
    }
    assert report.failed == {}
    assert device.inverter.w == 3075.1
    assert device.meters[METER_UNIT_ID].phases == 3
    assert device.meters[METER_UNIT_ID].identity.model == "Smart Meter TS 65A-3"


async def test_a_missing_meter_is_absent_not_an_error(connection):
    absent = connection.for_unit(201)
    absent.fail_requests(GatewayTargetError())
    device = FroniusInverter(
        connection.for_unit(INVERTER_UNIT_ID),
        INVERTER_UNIT_ID,
        {METER_UNIT_ID: connection.for_unit(METER_UNIT_ID), 201: absent},
    )
    report = await device.async_update()
    assert 201 not in device.meters
    assert meter_report_name(201) not in report.updated
    assert meter_report_name(201) not in report.failed


async def test_a_meter_that_stops_answering_fails_only_its_own_report(
    device, meter_unit
):
    await device.async_update()
    meter_unit.fail_requests(ModbusTimeoutError())
    report = await device.async_update()
    assert REPORT_INVERTER in report.updated
    assert isinstance(
        report.failed[meter_report_name(METER_UNIT_ID)], ModbusTimeoutError
    )


async def test_an_inverter_without_storage_and_mppt(symo_gen24):
    connection = MockModbusConnection()
    holding = dict(symo_gen24[1]["holding"])
    holding[40253], holding[40254] = 0xFFFF, 0  # chain ends after model 123
    connection.for_unit(1).load_raw({"holding": holding})
    device = FroniusInverter(connection.for_unit(1), 1, {})
    report = await device.async_update()
    assert device.storage is None and device.mppt is None
    assert REPORT_STORAGE not in report.updated and REPORT_MPPT not in report.updated


async def test_a_dead_link_propagates(device, inverter_unit):
    inverter_unit.fail_requests(ModbusConnectionError())
    with pytest.raises(ModbusConnectionError):
        await device.async_update()
    assert device.is_set_up is False


async def test_isolation_resistance_is_reported_in_megaohm(device):
    await device.async_update()
    assert device.isolation_resistance_megaohm == 8.607


async def test_raw_diagnostics_drop_the_serial_number(device):
    await device.async_update()
    raw = await device.async_read_raw()
    assert (
        40052 not in raw[INVERTER_UNIT_ID]["holding"]
        and 40067 not in raw[INVERTER_UNIT_ID]["holding"]
    )
    assert raw[INVERTER_UNIT_ID]["holding"][40002] == 1


async def test_mppt_channels_are_classified_by_their_label(device):
    await device.async_update()
    channels = device.mppt_channels
    assert channels.pv == (0, 1)
    assert (channels.charge, channels.discharge) == (2, 3)
    assert device.pv_power_w == 3123.6


async def test_unlabelled_channels_fall_back_to_the_last_two_for_storage(
    connection, symo_gen24
):
    holding = dict(symo_gen24[1]["holding"])
    for module in range(4):  # blank every id_str (8 words from header+11+20*module)
        for word in range(8):
            holding[40253 + 11 + 20 * module + word] = 0
    connection.for_unit(1).load_raw({"holding": holding})
    device = FroniusInverter(connection.for_unit(1), 1, {})
    await device.async_update()
    assert device.mppt_channels.pv == (0, 1)
    assert (device.mppt_channels.charge, device.mppt_channels.discharge) == (2, 3)


async def test_a_link_that_dies_after_setup_still_propagates(device, inverter_unit):
    """A dead link during the per-component poll must reach the caller too.

    The first poll fails in discovery, where the re-raise is obvious. Once the
    device is set up the same failure arrives inside the component loop, and
    swallowing it there would report a green poll over a dead connection.
    """
    await device.async_update()
    inverter_unit.fail_requests(ModbusConnectionError())
    with pytest.raises(ModbusConnectionError):
        await device.async_update()


def _relabel_module(holding: dict, module: int, label: str) -> None:
    """Overwrite one model-160 module's id_str (8 words from header+11+20*module)."""
    padded = label.ljust(16, "\0")[:16]
    for word in range(8):
        pair = padded[word * 2 : word * 2 + 2]
        holding[40253 + 11 + 20 * module + word] = (ord(pair[0]) << 8) | ord(pair[1])


async def test_a_foreign_labelled_module_is_not_a_pv_channel(connection, symo_gen24):
    """The PV channels are the ones LABELLED as strings, not "whatever is left".

    The fallback for unlabelled firmware would otherwise cover for a label
    check that matches nothing at all, and a module that is neither a string
    nor a storage path would be counted as PV.
    """
    holding = dict(symo_gen24[1]["holding"])
    _relabel_module(holding, 0, "AUX 1")
    connection.for_unit(1).load_raw({"holding": holding})
    device = FroniusInverter(connection.for_unit(1), 1, {})
    await device.async_update()

    assert device.mppt_channels.pv == (1,)


async def test_a_meter_that_does_not_answer_at_setup_is_retried_on_the_next_poll(
    connection,
):
    """Audit F03: a discovery timeout was filed as "no meter" for the life of the entry."""
    meter_unit = connection.for_unit(METER_UNIT_ID)
    meter_unit.fail_requests(ModbusTimeoutError())
    device = FroniusInverter(
        connection.for_unit(INVERTER_UNIT_ID),
        INVERTER_UNIT_ID,
        {METER_UNIT_ID: meter_unit},
    )
    report = await device.async_update()
    assert METER_UNIT_ID not in device.meters
    assert isinstance(
        report.failed[meter_report_name(METER_UNIT_ID)], ModbusTimeoutError
    )
    meter_unit.fail_requests(None)
    report = await device.async_update()
    assert METER_UNIT_ID in device.meters
    assert meter_report_name(METER_UNIT_ID) in report.updated


async def test_a_unit_that_refuses_the_marker_is_absent_for_good(connection):
    absent = connection.for_unit(201)
    absent.fail_requests(GatewayTargetError())
    device = FroniusInverter(
        connection.for_unit(INVERTER_UNIT_ID), INVERTER_UNIT_ID, {201: absent}
    )
    report = await device.async_update()
    absent.fail_requests(None)
    report = await device.async_update()
    assert 201 not in device.meters
    assert meter_report_name(201) not in report.failed


# The storage model header in the captured fixture; the chain ends right after it.
STORAGE_HEADER_ADDRESS = 40343


async def test_a_refused_model_header_keeps_the_models_found_before_it(connection):
    """Upstream #105: one refused header failed the whole chain, and setup then

    reported "cannot connect" although Modbus was answering fine.
    """
    unit = connection.for_unit(INVERTER_UNIT_ID)
    unit.fail_read(STORAGE_HEADER_ADDRESS, IllegalDataAddressError())
    identity = await FroniusInverter.async_probe(unit)
    assert identity.manufacturer == "Fronius"
    device = FroniusInverter(unit, INVERTER_UNIT_ID, {})
    report = await device.async_update()
    assert device.storage is None
    assert device.mppt is not None
    assert REPORT_INVERTER in report.updated


async def test_a_unit_that_refuses_every_read_is_still_no_meter(connection):
    """The tolerant walk must not turn an absent meter into a failed setup."""
    meter_unit = connection.for_unit(201)
    meter_unit.fail_requests(IllegalDataAddressError())
    device = FroniusInverter(
        connection.for_unit(INVERTER_UNIT_ID), INVERTER_UNIT_ID, {201: meter_unit}
    )
    report = await device.async_update()
    assert 201 not in device.meters
    assert meter_report_name(201) not in report.failed
