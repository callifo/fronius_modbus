"""The grid meter's power on its own short interval, for a battery control that follows it."""

import pytest

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
    # W, WphA-C and W_SF: the scale factor is four registers on.
    span = range(power, power + 5)
    unit.read_events.clear()

    await _poll_fast(hass, entry)

    assert unit.read_events
    assert all(
        event.address in span and event.address + event.count - 1 in span
        for event in unit.read_events
    )


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

    async def _no_answer(*_args, **_kwargs):
        raise UpdateFailed("no answer")

    monkeypatch.setattr(
        entry.runtime_data.meter_power, "_async_update_data", _no_answer
    )
    await _poll_fast(hass, entry)
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
