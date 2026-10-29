"""The grid meter's power on its own short interval, for a battery control that follows it."""

from modbus_connection import ModbusTimeoutError
import pytest

from custom_components.fronius_modbus.coordinator import DeviceUnreachable
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from .conftest import METER_UNIT_ID
from .test_e2e import make_entry, setup_entry, state_of

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]

POWER_KEY = f"meter_{METER_UNIT_ID}_power"


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


def _power_address(entry) -> int:
    meter = entry.runtime_data.device.meters[METER_UNIT_ID].meter
    return meter.resolved_fields["w"].address


async def _poll_fast(hass: HomeAssistant, entry) -> None:
    await entry.runtime_data.meter_power.async_refresh()
    await hass.async_block_till_done()


async def test_without_the_option_nothing_polls_faster(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)

    assert entry.runtime_data.meter_power is None


async def test_the_option_polls_the_grid_meter_on_its_interval(hass, mock_modbus):
    entry = make_entry(hass, meter_power_interval=1)
    await setup_entry(hass, entry)

    assert entry.runtime_data.meter_power.update_interval.total_seconds() == 1


async def test_the_power_sensor_follows_the_fast_poll(hass, mock_modbus):
    entry = make_entry(hass, meter_power_interval=1)
    await setup_entry(hass, entry)
    assert state_of(hass, entry, POWER_KEY) == "30.0"

    holding = mock_modbus.unit(METER_UNIT_ID).holding
    holding[_power_address(entry)] *= 2
    await _poll_fast(hass, entry)

    assert state_of(hass, entry, POWER_KEY) == "60.0"


async def test_the_fast_poll_reads_the_power_and_its_scale_factor_only(
    hass, mock_modbus
):
    entry = make_entry(hass, meter_power_interval=1)
    await setup_entry(hass, entry)
    unit = mock_modbus.unit(METER_UNIT_ID)
    power = _power_address(entry)
    header = entry.runtime_data.device.meters[METER_UNIT_ID].model.address
    unit.read_events.clear()

    await _poll_fast(hass, entry)

    # The model header, checked on every read, then W, WphA-C and W_SF.
    assert [(event.address, event.count) for event in unit.read_events] == [
        (header, 2),
        (power, 5),
    ]


async def test_a_failed_fast_read_falls_back_to_the_full_poll(
    hass, mock_modbus, monkeypatch
):
    entry = make_entry(hass, meter_power_interval=1)
    await setup_entry(hass, entry)
    holding = mock_modbus.unit(METER_UNIT_ID).holding
    power = _power_address(entry)
    raw = holding[power]
    holding[power] = raw * 2
    await _poll_fast(hass, entry)
    assert state_of(hass, entry, POWER_KEY) == "60.0"

    # The full poll reads the same blocks, so the failure is the fast reader's own.
    async def _no_answer(**_kwargs):
        raise ModbusTimeoutError

    meter_power = entry.runtime_data.meter_power
    monkeypatch.setattr(meter_power._power, "async_update", _no_answer)
    await _poll_fast(hass, entry)
    assert not entry.runtime_data.meter_power.last_update_success
    holding[power] = raw * 3
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()

    assert state_of(hass, entry, POWER_KEY) == "90.0"


async def test_the_other_meter_sensors_stay_on_the_full_poll(hass, mock_modbus):
    entry = make_entry(hass, meter_power_interval=1)
    await setup_entry(hass, entry)
    before = state_of(hass, entry, f"meter_{METER_UNIT_ID}_line_frequency")
    holding = mock_modbus.unit(METER_UNIT_ID).holding
    frequency = (
        entry.runtime_data.device.meters[METER_UNIT_ID]
        .meter.resolved_fields["hz"]
        .address
    )
    holding[frequency] += 1

    await _poll_fast(hass, entry)

    assert state_of(hass, entry, f"meter_{METER_UNIT_ID}_line_frequency") == before


async def test_a_meter_that_does_not_answer_is_an_expected_outage(hass, mock_modbus):
    entry = make_entry(hass, meter_power_interval=1)
    await setup_entry(hass, entry)
    mock_modbus.fail_requests(METER_UNIT_ID, ModbusTimeoutError())

    with pytest.raises(DeviceUnreachable):
        await entry.runtime_data.meter_power._async_update_data()


async def test_a_shifted_meter_model_fails_the_fast_read(hass, mock_modbus):
    entry = make_entry(hass, meter_power_interval=1)
    await setup_entry(hass, entry)
    header = entry.runtime_data.device.meters[METER_UNIT_ID].model.address
    mock_modbus.unit(METER_UNIT_ID).holding[header] += 1

    with pytest.raises(UpdateFailed, match="register map has changed"):
        await entry.runtime_data.meter_power._async_update_data()
