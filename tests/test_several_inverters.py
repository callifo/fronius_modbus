"""Several inverters on one Datamanager, each an entry of its own on one host."""

from typing import Any

import pytest

from custom_components.fronius_modbus import datamanager
from custom_components.fronius_modbus.const import (
    CONF_DATAMANAGER,
    DOMAIN,
    entry_unique_id,
)
from custom_components.fronius_modbus.datamanager import (
    DeviceFamily,
    async_on_datamanager,
)
from custom_components.fronius_modbus.host_sharing import host_holder, unique_id_for
from custom_components.fronius_modbus.inverter_choice import async_free_units
from homeassistant.config_entries import SOURCE_IGNORE

from .ha_test_plugin import MockConfigEntry
from .test_config_flow import answer_family, discover, discovered

HOST = "192.0.2.10"
OTHER_HOST = "192.0.2.20"


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


def inverter(
    unit: int = 1, datamanager: bool | None = True, host: str = HOST
) -> dict[str, Any]:
    """The settings of one inverter; ``datamanager`` None is an entry never asked."""
    settings = {"host": host, "inverter_modbus_unit_id": unit, "api_username": "none"}
    return (
        settings if datamanager is None else settings | {CONF_DATAMANAGER: datamanager}
    )


def add_entry(hass, settings: dict[str, Any], unique_id: str | None, **more: Any):
    """An entry with these settings and the unique id it was stored with."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=settings,
        unique_id=unique_id,
        version=1,
        minor_version=14,
        **more,
    )
    entry.add_to_hass(hass)
    return entry


# -- the identity of an entry ---------------------------------------------------------


@pytest.mark.parametrize(
    "settings",
    [
        inverter(1, datamanager=None),
        inverter(7, datamanager=None),
        inverter(7, datamanager=False),
        inverter(1, datamanager=True),
        {"host": HOST, CONF_DATAMANAGER: True},
        {"host": HOST},
        {"host": " 192.0.2.10 "},
    ],
)
def test_the_unique_id_stays_the_host(settings):
    assert entry_unique_id(settings) == HOST


@pytest.mark.parametrize("unit", [2, 100])
def test_a_datamanager_inverter_is_host_and_unit(unit):
    assert entry_unique_id(inverter(unit)) == f"{HOST}#{unit}"


GEN24 = inverter(1, datamanager=None)
FIRST = inverter(1)
SECOND = inverter(2)


@pytest.mark.parametrize(
    ("held", "stored_id", "newcomer", "shared"),
    [
        (GEN24, HOST, GEN24, False),
        (GEN24, HOST, FIRST, False),
        (GEN24, HOST, SECOND, False),
        (GEN24, None, GEN24, False),
        (GEN24, None, SECOND, False),
        (FIRST, HOST, GEN24, False),
        (FIRST, HOST, FIRST, False),
        (FIRST, HOST, SECOND, True),
        (SECOND, f"{HOST}#2", GEN24, False),
        (SECOND, f"{HOST}#2", FIRST, True),
        (SECOND, f"{HOST}#2", SECOND, False),
        # 0.1.9 set none; another entry's number is still not free.
        (FIRST, None, SECOND, True),
        (SECOND, None, FIRST, True),
        (SECOND, None, SECOND, False),
        (SECOND, None, GEN24, False),
        # Stored before the device was known as a Datamanager. Home Assistant
        # replaces an entry whose unique id a new one brings.
        (SECOND, HOST, FIRST, False),
        (SECOND, HOST, SECOND, False),
        (SECOND, HOST, inverter(3), True),
    ],
)
async def test_who_may_share_a_host(hass, held, stored_id, newcomer, shared):
    holder = add_entry(hass, held, stored_id)

    assert host_holder(hass, newcomer) is (None if shared else holder)


async def test_an_ignored_card_keeps_its_host_from_a_move_only(hass):
    """A new setup takes the card's place, as Home Assistant has it."""
    card = add_entry(hass, {}, HOST, source=SOURCE_IGNORE)
    moving = add_entry(hass, FIRST | {"host": OTHER_HOST}, OTHER_HOST)

    assert host_holder(hass, FIRST) is None
    assert host_holder(hass, FIRST, moving) is card


async def test_an_ignored_card_does_not_hide_the_entry_on_its_host(hass):
    add_entry(hass, {}, HOST, source=SOURCE_IGNORE)
    holder = add_entry(hass, GEN24, None)

    assert host_holder(hass, GEN24) is holder


@pytest.mark.parametrize(
    ("data", "unique_id"),
    [(GEN24, HOST), (GEN24, None), (inverter(7, datamanager=None), HOST)],
)
async def test_an_existing_entry_keeps_its_unique_id(hass, data, unique_id):
    """Asked again with its own settings, as Reconfigure does."""
    entry = add_entry(hass, data, unique_id)

    assert unique_id_for(hass, data, entry) == HOST
    assert host_holder(hass, data, entry) is None


async def test_the_second_entry_of_0_1_9_keeps_the_host_it_has(hass):
    """Asked on its own it is a Datamanager only once it ran without the web API."""
    add_entry(hass, FIRST, HOST)
    second = add_entry(hass, inverter(2, datamanager=None), None)

    assert host_holder(hass, dict(second.data), second) is None
    assert unique_id_for(hass, dict(second.data), second) == f"{HOST}#2"


@pytest.mark.parametrize(
    ("held", "moves"), [(None, True), (FIRST, True), (SECOND, False), (GEN24, False)]
)
async def test_an_inverter_moves_to_another_address(hass, held, moves):
    """The device there decides; with no entry there it is taken for the same device."""
    if held is not None:
        add_entry(
            hass,
            {**held, "host": OTHER_HOST},
            entry_unique_id(held | {"host": OTHER_HOST}),
        )
    entry = add_entry(hass, SECOND, f"{HOST}#2")
    moved = inverter(2, datamanager=None, host=OTHER_HOST)

    assert (host_holder(hass, moved, entry) is None) is moves
    if moves:
        assert unique_id_for(hass, moved, entry) == f"{OTHER_HOST}#2"


@pytest.mark.parametrize("unique_id", [f"{HOST}#2", None])
async def test_a_host_only_a_second_inverter_has_is_not_offered(hass, unique_id):
    add_entry(hass, SECOND, unique_id)

    result = await discover(hass, discovered())

    assert result["reason"] == "already_configured"


# -- an entry that learns its device --------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "stored", "family", "after"),
    [
        (2, HOST, DeviceFamily.DATAMANAGER, f"{HOST}#2"),
        (1, HOST, DeviceFamily.DATAMANAGER, HOST),
        (2, None, DeviceFamily.DATAMANAGER, None),
        (2, f"{HOST}#2", DeviceFamily.OTHER, HOST),
        (2, f"{HOST}#2", DeviceFamily.UNKNOWN, f"{HOST}#2"),
        (2, HOST, DeviceFamily.OTHER, HOST),
    ],
)
async def test_the_unique_id_follows_what_the_device_is(
    hass, monkeypatch, unit, stored, family, after
):
    """Without it another inverter of the host cannot take the host alone."""
    entry = add_entry(hass, inverter(unit, datamanager="#" in str(stored)), stored)
    answer_family(monkeypatch, family)

    await async_on_datamanager(hass, entry, HOST)

    assert entry.unique_id == after


async def test_a_taken_unique_id_is_left_to_its_entry(hass, monkeypatch):
    add_entry(hass, SECOND, f"{HOST}#2")
    entry = add_entry(hass, inverter(2, datamanager=None), HOST)
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)

    await async_on_datamanager(hass, entry, HOST)

    assert entry.unique_id == HOST


# -- which inverters there are --------------------------------------------------------


@pytest.mark.parametrize(
    ("listed", "taken", "free"),
    [
        ((1, 2, 100), (), [1, 2, 100]),
        ((1, 2), (1,), [2]),
        ((1, 2), (1, 2), []),
        ((), (1,), []),
    ],
)
async def test_the_inverters_without_an_entry(hass, monkeypatch, listed, taken, free):
    monkeypatch.setattr(datamanager, "read_inverter_units", lambda host: listed)
    for unit in taken:
        add_entry(hass, inverter(unit), entry_unique_id(inverter(unit)))
    add_entry(hass, inverter(1, host=OTHER_HOST), OTHER_HOST)

    assert await async_free_units(hass, HOST) == free
