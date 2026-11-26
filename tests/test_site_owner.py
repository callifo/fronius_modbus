"""What the inverters of one Datamanager share: one entry has it, the others do not."""

from modbus_connection import ModbusConnectionError, ModbusTimeoutError
import pytest

from custom_components.fronius_modbus.const import (
    CONF_DATAMANAGER,
    CONF_INVERTER_UNIT_ID,
    CONF_SITE_OWNER,
    DOMAIN,
    entity_prefix,
    instance_key,
)
from custom_components.fronius_modbus.datamanager import DeviceFamily
from custom_components.fronius_modbus.fronius_modbus_api.device import FroniusInverter
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import INVERTER_UNIT_ID
from .test_datamanager_entry import (
    SITE_KEYS,
    add_fronius_registers,
    add_meter,
    answer_family,
    answer_site,
    make_datamanager_entry,
    registered_keys,
    reload_entry,
    row_ids,
)
from .test_e2e import HOST, state_of

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]

SECOND_UNIT_ID = 2
METER_KEYS = {"meter_240_power", "load", "grid_status"}


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


@pytest.fixture
def site(datamanager_modbus, web_requests, monkeypatch):
    """A Datamanager with two inverters and one grid meter."""
    add_fronius_registers(datamanager_modbus)
    datamanager_modbus.add_unit(SECOND_UNIT_ID, like=INVERTER_UNIT_ID)
    add_meter(datamanager_modbus, 240, "A1")
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    answer_site(monkeypatch, ("A1", 0), inverters=2)
    return datamanager_modbus


def add_inverter(hass, unit: int, *, unique_id: str | None = "", **data):
    """An entry on this unit, with its usual unique id unless another is given."""
    entry = make_datamanager_entry(hass, **{CONF_INVERTER_UNIT_ID: unit}, **data)
    usual = HOST if unit == 1 else f"{HOST}#{unit}"
    hass.config_entries.async_update_entry(
        entry, unique_id=usual if unique_id == "" else unique_id
    )
    return entry


async def start(hass, *entries) -> None:
    """Start Home Assistant with these entries; the first one sets up the domain."""
    assert await hass.config_entries.async_setup(entries[0].entry_id)
    await hass.async_block_till_done()
    assert [entry.state for entry in entries] == [ConfigEntryState.LOADED] * len(
        entries
    )


def owns(hass, entry) -> bool:
    """Whether the entry has the meter and the site totals; never only a part."""
    keys = registered_keys(hass, entry)
    shared = METER_KEYS | SITE_KEYS
    assert shared <= keys or not shared & keys
    return shared <= keys


@pytest.mark.parametrize("unique_id", ["", None])
@pytest.mark.parametrize("order", [(1, 2), (2, 1)])
async def test_the_lowest_inverter_has_what_they_share(hass, site, order, unique_id):
    """0.1.9 set no unique id for either of them."""
    entries = {unit: add_inverter(hass, unit, unique_id=unique_id) for unit in order}

    await start(hass, *entries.values())

    first, second = entries[1], entries[2]
    assert owns(hass, first) and not owns(hass, second)
    assert state_of(hass, first, "meter_240_power") == "30.0"
    assert state_of(hass, second, "acpower") == "597.0"
    assert state_of(hass, second, "state_code") == "307"
    assert (first.data.get(CONF_SITE_OWNER), CONF_SITE_OWNER in second.data) == (
        True,
        False,
    )


async def test_a_second_inverter_asks_nothing_the_first_one_asks(hass, site):
    second = add_inverter(hass, SECOND_UNIT_ID)
    add_inverter(hass, 1, disabled_by=None, **{CONF_SITE_OWNER: True})

    await start(hass, second)
    units = second.runtime_data.device.meter_unit_ids

    assert units == ()
    asked = {event.address for event in site.unit(SECOND_UNIT_ID).read_events}
    assert asked
    assert not asked & set(range(499, 513))


async def test_the_only_entry_has_it_whatever_its_number(hass, site):
    entry = add_inverter(hass, SECOND_UNIT_ID)

    await start(hass, entry)

    assert owns(hass, entry)
    assert len(site.connections) == 1


async def test_a_lower_inverter_added_later_takes_nothing(hass, site):
    """Its meter would go on under new entities, without its history."""
    second = add_inverter(hass, SECOND_UNIT_ID)
    await start(hass, second)
    rows = row_ids(hass, second)
    first = add_inverter(hass, 1)
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()

    await reload_entry(hass, second)

    assert owns(hass, second) and not owns(hass, first)
    assert row_ids(hass, second) == rows


async def test_the_remaining_inverter_takes_over_at_its_next_start(hass, site):
    first, second = add_inverter(hass, 1), add_inverter(hass, SECOND_UNIT_ID)
    await start(hass, first, second)

    await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()
    assert not owns(hass, second)

    await reload_entry(hass, second)
    assert owns(hass, second)
    assert state_of(hass, second, "meter_240_power") == "30.0"


async def test_an_owner_that_is_disabled_keeps_it(hass, site):
    """Enabled again it would find its meter with another entry."""
    first, second = add_inverter(hass, 1), add_inverter(hass, SECOND_UNIT_ID)
    await start(hass, first, second)
    await hass.config_entries.async_set_disabled_by(
        first.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()

    await reload_entry(hass, second)

    assert not owns(hass, second)


async def test_a_disabled_entry_that_never_had_it_does_not_hold_it_back(hass, site):
    first = add_inverter(hass, 1)
    await hass.config_entries.async_set_disabled_by(
        first.entry_id, ConfigEntryDisabler.USER
    )
    second = add_inverter(hass, SECOND_UNIT_ID)

    await start(hass, second)

    assert owns(hass, second)


async def test_an_entry_with_a_login_does_not_hold_it_back(hass, site):
    """No login of ours opens a Datamanager: it would wait for a login forever."""
    add_inverter(hass, 1, unique_id=None)
    (first,) = hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.async_update_entry(
        first, data={**first.data, "api_username": "customer"}
    )
    second = add_inverter(hass, SECOND_UNIT_ID)

    await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()

    assert owns(hass, second)


async def test_another_host_has_its_own(hass, site):
    other = add_inverter(hass, 1, unique_id="192.0.2.99", host="192.0.2.99")
    entry = add_inverter(hass, SECOND_UNIT_ID)

    await start(hass, entry, other)

    assert owns(hass, entry) and owns(hass, other)


async def test_the_load_stays_unknown_with_two_entries(hass, site, monkeypatch):
    """By night the Solar API lists one of them, or none."""
    answer_site(monkeypatch, ("A1", 0), inverters=1)
    first, second = add_inverter(hass, 1), add_inverter(hass, SECOND_UNIT_ID)

    await start(hass, first, second)

    assert state_of(hass, first, "load") == "unknown"


async def test_the_load_turns_unknown_when_a_second_inverter_joins(
    hass, site, monkeypatch
):
    """The owner added its own inverter alone to the grid meter's power."""
    answer_site(monkeypatch, ("A1", 0), inverters=1)
    first = add_inverter(hass, 1)
    await start(hass, first)
    assert state_of(hass, first, "load") not in ("unknown", "unavailable")

    second = add_inverter(hass, SECOND_UNIT_ID)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()

    assert state_of(hass, first, "load") == "unknown"
    assert first.runtime_data.several_inverters is True


async def test_the_load_turns_unknown_when_a_second_inverter_joins_the_search(
    hass, site, monkeypatch
):
    """The owner had counted the entries before its search; nobody told it later."""
    answer_site(monkeypatch, ("A1", 0), inverters=1)
    first = add_inverter(hass, 1)
    probe = FroniusInverter.async_probe_meter
    joined = []

    async def set_up_the_second_meanwhile(unit):
        if not joined:
            joined.append(add_inverter(hass, SECOND_UNIT_ID))
            assert await hass.config_entries.async_setup(joined[0].entry_id)
        return await probe(unit)

    monkeypatch.setattr(
        FroniusInverter, "async_probe_meter", staticmethod(set_up_the_second_meanwhile)
    )

    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()

    assert joined[0].state is ConfigEntryState.LOADED
    assert state_of(hass, first, "load") == "unknown"


async def test_a_link_lost_in_the_owner_s_search_still_counts_the_entries(
    hass, site, monkeypatch
):
    """Without the count the owner restarted at the end of every such setup."""
    probe = FroniusInverter.async_probe_meter
    looked_at = []

    async def lose_the_link_at_the_second_unit(unit):
        looked_at.append(unit)
        if len(looked_at) == 2:
            raise ModbusConnectionError("link lost")
        return await probe(unit)

    monkeypatch.setattr(
        FroniusInverter,
        "async_probe_meter",
        staticmethod(lose_the_link_at_the_second_unit),
    )
    first, second = add_inverter(hass, 1), add_inverter(hass, SECOND_UNIT_ID)

    await start(hass, first, second)

    assert first.runtime_data.several_inverters is True
    assert len(looked_at) == 2


@pytest.mark.parametrize("answers", [True, False])
async def test_the_site_totals_are_on_with_two_entries(
    hass, site, monkeypatch, answers
):
    """The Solar API lists no inverter by night, or does not answer."""
    if answers:
        answer_site(monkeypatch, ("A1", 0), inverters=0)
    else:
        monkeypatch.setattr(
            "custom_components.fronius_modbus.datamanager.read_site", lambda host: None
        )
    first, second = add_inverter(hass, 1), add_inverter(hass, SECOND_UNIT_ID)

    await start(hass, first, second)

    assert state_of(hass, first, "site_power") == "1200"


async def test_a_second_inverter_loses_nothing(hass, site):
    """Both entries of 0.1.9 may have read the meter."""
    second = add_inverter(hass, SECOND_UNIT_ID)
    add_inverter(hass, 1, **{CONF_SITE_OWNER: True, CONF_DATAMANAGER: True})
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=second.entry_id,
        identifiers={(DOMAIN, f"{instance_key(second.entry_id)}_meter_240")},
    )
    for key in ("meter_240_power", "load", "site_power"):
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entity_prefix(second.entry_id)}_{key}",
            config_entry=second,
            device_id=device.id,
        )
    rows = row_ids(hass, second)

    await start(hass, second)

    assert rows <= row_ids(hass, second)
    assert dr.async_get(hass).async_get(device.id) is not None


async def test_the_owner_asleep_is_still_the_owner(hass, site):
    """A start in the dark must not hand its meter to the inverter that answers."""
    first, second = add_inverter(hass, 1), add_inverter(hass, SECOND_UNIT_ID)
    site.fail_requests(INVERTER_UNIT_ID, ModbusTimeoutError())

    await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()

    assert first.state is ConfigEntryState.SETUP_RETRY
    assert not owns(hass, second)


async def test_a_start_by_night_changes_nothing(hass, site, monkeypatch):
    """The Datamanager may answer without its inverters, or not at all."""
    entries = [add_inverter(hass, 1), add_inverter(hass, SECOND_UNIT_ID)]
    await start(hass, *entries)
    before = [
        (entry.unique_id, dict(entry.data), row_ids(hass, entry)) for entry in entries
    ]
    answer_family(monkeypatch, DeviceFamily.UNKNOWN)
    answer_site(monkeypatch, inverters=0)

    for entry in reversed(entries):
        await reload_entry(hass, entry)

    assert before == [
        (entry.unique_id, dict(entry.data), row_ids(hass, entry)) for entry in entries
    ]
    assert owns(hass, entries[0]) and not owns(hass, entries[1])


@pytest.mark.parametrize("unit", [2, 100])
async def test_nothing_looks_at_unit_1_for_another_inverter(hass, site, unit):
    site.add_unit(unit, like=INVERTER_UNIT_ID)
    entry = add_inverter(hass, unit)

    await start(hass, entry)

    assert state_of(hass, entry, "acpower") == "597.0"
    assert site.unit(INVERTER_UNIT_ID).read_events == []
