"""The battery of a Symo Hybrid: one DC input for both directions."""

from modbus_connection import IllegalDataAddressError, ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection
import pytest

from custom_components.fronius_modbus.datamanager import DeviceFamily
from custom_components.fronius_modbus.fronius_modbus_api.device import FroniusInverter

from .conftest import INVERTER_UNIT_ID, METER_UNIT_ID, load_fixture
from .test_datamanager_entry import (
    answer_family,
    make_datamanager_entry,
    registered_keys,
)
from .test_e2e import make_entry, setup_entry, state_of

# The chain of issue 110 ends here; a hybrid goes on with model 124.
END_OF_CHAIN = 40303
STORAGE_HEADER = [124, 24]
# The storage model of a Symo Hybrid 5.0 as evcc issue 19078 lists it.
STORAGE_BODY = [11520, 100, 100, 0, 0xFFFF, 0, 3170, 0xFFFF, 0xFFFF, 3, 10000, 10000]
STORAGE_BODY += [0xFFFF, 0xFFFF, 0xFFFF, 1, 0, 0, 0x8000, 0xFFFE, 0xFFFE]
STORAGE_BODY += [0x8000, 0x8000, 0xFFFE]
STORAGE_STATUS_ADDRESS = END_OF_CHAIN + 2 + 9
MODEL_ADDRESS = 40020
PV_POWER_ADDRESS = 40274
BATTERY_CURRENT_ADDRESS = 40292
OFF, EMPTY, DISCHARGING, CHARGING, FULL, HOLDING, TESTING = range(1, 8)


def hybrid_words(status: int) -> dict[int, int]:
    """The registers of a hybrid: 100 W from the roof, 304.46 W on the battery."""
    words = dict(load_fixture("symo_datamanager.json")[INVERTER_UNIT_ID]["holding"])
    body = [*STORAGE_BODY[:9], status, *STORAGE_BODY[10:]]
    chain = [*STORAGE_HEADER, *body, 0xFFFF, 0]
    words |= {END_OF_CHAIN + offset: word for offset, word in enumerate(chain)}
    model = b"Symo Hybrid 5.0-3-S".ljust(32, b"\0")
    for offset in range(16):
        word = int.from_bytes(model[2 * offset : 2 * offset + 2], "big")
        words[MODEL_ADDRESS + offset] = word
    # Current, voltage, power and no energy counter, for each of the two inputs.
    words |= {PV_POWER_ADDRESS + o: w for o, w in enumerate([10000, 0, 0])}
    battery = [65, 46840, 30446, 0, 0]
    return words | {BATTERY_CURRENT_ADDRESS + o: w for o, w in enumerate(battery)}


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    """Let Home Assistant load custom_components/fronius_modbus in every test here."""
    return enable_custom_integrations


@pytest.fixture
def hybrid_entry(hass, datamanager_modbus, monkeypatch):
    """An entry on a Datamanager, not set up yet."""
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    return make_datamanager_entry(hass)


@pytest.fixture
def unit():
    unit = MockModbusConnection().for_unit(INVERTER_UNIT_ID)
    unit.load_raw({"holding": hybrid_words(DISCHARGING)})
    return unit


async def test_the_second_input_of_a_hybrid_is_its_battery(unit):
    device = FroniusInverter(unit, INVERTER_UNIT_ID, {}, datamanager=True)
    await device.async_update()

    assert device.mppt_channels.pv == (0,)
    assert device.mppt_channels.charge == device.mppt_channels.discharge == 1
    assert device.pv_power_w == 100.0


async def test_two_inputs_without_a_battery_are_both_pv():
    unit = MockModbusConnection().for_unit(INVERTER_UNIT_ID)
    unit.load_raw(load_fixture("symo_datamanager.json")[INVERTER_UNIT_ID])
    device = FroniusInverter(unit, INVERTER_UNIT_ID, {}, datamanager=True)
    await device.async_update()

    assert device.mppt_channels.pv == (0, 1)
    assert device.pv_power_w == 633.59


async def test_a_gen24_with_two_inputs_keeps_them(unit):
    device = FroniusInverter(unit, INVERTER_UNIT_ID, {})
    await device.async_update()

    assert device.mppt_channels.pv == (0, 1)
    assert device.mppt_channels.charge is None


async def test_a_gen24_s_labelled_channels_stay_two(symo_gen24):
    """Whatever the family question once answered."""
    unit = MockModbusConnection().for_unit(INVERTER_UNIT_ID)
    unit.load_raw(symo_gen24[INVERTER_UNIT_ID])
    device = FroniusInverter(unit, INVERTER_UNIT_ID, {}, datamanager=True)
    await device.async_update()

    assert device.mppt_channels.charge == 2
    assert device.mppt_channels.discharge == 3


def serve_hybrid(shared, status: int) -> None:
    shared._fixture[INVERTER_UNIT_ID] = {"holding": hybrid_words(status)}


FLOW_KEYS = (
    "storage_charge_power",
    "storage_charge_current",
    "storage_discharge_power",
    "storage_discharge_current",
)


@pytest.mark.e2e
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CHARGING, ("304.46", "0.65", "0.0", "0.0")),
        (DISCHARGING, ("0.0", "0.0", "304.46", "0.65")),
        (OFF, ("0.0",) * 4),
        (EMPTY, ("0.0",) * 4),
        (FULL, ("0.0",) * 4),
        (HOLDING, ("0.0",) * 4),
        (TESTING, ("unknown",) * 4),
        (0, ("unknown",) * 4),
        (99, ("unknown",) * 4),
        (0xFFFF, ("unknown",) * 4),
    ],
)
async def test_the_battery_s_power_goes_where_its_status_says(
    hass,
    datamanager_modbus,
    hybrid_entry,
    status,
    expected,
):
    """The input shows the power without a sign."""
    serve_hybrid(datamanager_modbus, status)
    entry = hybrid_entry
    await setup_entry(hass, entry)

    assert tuple(state_of(hass, entry, key) for key in FLOW_KEYS) == expected
    assert state_of(hass, entry, "storage_charge_voltage") == "468.4"
    assert state_of(hass, entry, "storage_discharge_voltage") == "468.4"
    # One counter for both directions would be neither.
    assert state_of(hass, entry, "storage_charge_lfte") == "unknown"
    assert state_of(hass, entry, "storage_discharge_lfte") == "unknown"
    assert state_of(hass, entry, "pv_power") == "100.0"
    assert state_of(hass, entry, "soc") == "31.7"
    assert not any(
        key.startswith("mppt_module_1_") for key in registered_keys(hass, entry)
    )


@pytest.mark.e2e
async def test_a_counter_the_battery_does_have_is_not_taken_for_one_direction(
    hass,
    datamanager_modbus,
    hybrid_entry,
):
    serve_hybrid(datamanager_modbus, CHARGING)
    words = datamanager_modbus._fixture[INVERTER_UNIT_ID]["holding"]
    words[BATTERY_CURRENT_ADDRESS + 4] = 5000
    entry = hybrid_entry
    await setup_entry(hass, entry)

    assert state_of(hass, entry, "storage_charge_lfte") == "unknown"


@pytest.mark.e2e
async def test_the_direction_is_unknown_while_the_status_is_not_read(
    hass,
    datamanager_modbus,
    hybrid_entry,
):
    """A direction from the last poll would give the power to the wrong side."""
    serve_hybrid(datamanager_modbus, CHARGING)
    entry = hybrid_entry
    await setup_entry(hass, entry)
    assert state_of(hass, entry, "storage_charge_power") == "304.46"

    datamanager_modbus.fail_read(
        INVERTER_UNIT_ID, END_OF_CHAIN + 2, ModbusTimeoutError()
    )
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()

    assert state_of(hass, entry, "pv_power") == "100.0"
    assert state_of(hass, entry, "storage_charge_power") == "unknown"
    assert state_of(hass, entry, "storage_discharge_power") == "unknown"


@pytest.mark.e2e
async def test_a_battery_without_any_channel_shows_no_power(hass, datamanager_modbus):
    """The same registers on a device that is no Datamanager."""
    serve_hybrid(datamanager_modbus, CHARGING)
    datamanager_modbus.fail_requests(METER_UNIT_ID, IllegalDataAddressError())
    entry = make_entry(hass)
    await setup_entry(hass, entry)

    for key in FLOW_KEYS:
        assert state_of(hass, entry, key) == "unknown"
