"""An entry on a Datamanager, end to end, with the registers of issue 110."""

from typing import Any

from modbus_connection import (
    GatewayTargetError,
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusTimeoutError,
)
import pytest
import requests

from custom_components.fronius_modbus import datamanager, froniuswebclient
from custom_components.fronius_modbus.const import (
    CONF_DATAMANAGER,
    CONF_METER_UNIT_ID,
    DOMAIN,
    entity_prefix,
)
from custom_components.fronius_modbus.datamanager import (
    DeviceFamily,
    Site,
    SolarApiMeter,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import (
    DATAMANAGER_METER_UNIT_IDS,
    INVERTER_UNIT_ID,
    METER_UNIT_ID,
    load_fixture,
)
from .test_e2e import (
    HOST,
    _redpomodoro_entry,
    make_entry,
    serve_the_public_endpoints,
    setup_entry,
    state_of,
)

# Fed by the event paths of a GEN24; a Datamanager has none of them.
EVENT_KEYS = {
    "inverter_event",
    "service_event",
    "active_event",
    "active_events",
    "active_service_event",
    "active_service_events",
}

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    """Let Home Assistant load custom_components/fronius_modbus in every test here."""
    return enable_custom_integrations


@pytest.fixture
def web_requests(monkeypatch) -> list[str]:
    """Every path asked over HTTP; a Datamanager answers the GEN24 ones with 404."""
    paths: list[str] = []

    def _not_found(method: str, url: str, **options: Any) -> requests.Response:
        del method, options
        paths.append(url.removeprefix(f"http://{HOST}"))
        response = requests.Response()
        response.status_code = 404
        response.url = url
        response._content = b"<html><title>404 Not Found</title></html>"
        return response

    monkeypatch.setattr(froniuswebclient, "_http", _not_found)
    return paths


def make_datamanager_entry(hass, **data):
    """An entry without the web API, the one way a Datamanager is set up."""
    return make_entry(hass, minor_version=14, api_username="none", **data)


def test_the_registers_carry_no_serial_number():
    """The dump in issue 110 has it replaced; a later capture must not bring one."""
    words = load_fixture("symo_datamanager.json")[INVERTER_UNIT_ID]["holding"]

    assert not any(words[address] for address in range(40052, 40068))


async def test_a_datamanager_gives_its_readings(hass, datamanager_modbus, web_requests):
    entry = make_datamanager_entry(hass)
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert state_of(hass, entry, "acpower") == "597.0"
    assert state_of(hass, entry, "acenergy") == "10698950"
    assert state_of(hass, entry, "pv_power") == "633.59"
    (inverter,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert inverter.model == "Symo 8.2-3-M"


def answer_family(monkeypatch, family: DeviceFamily) -> list[str]:
    """Let the device answer with ``family``; returns the hosts asked."""
    asked: list[str] = []

    def _identify(host: str) -> DeviceFamily:
        asked.append(host)
        return family

    monkeypatch.setattr(datamanager, "identify", _identify)
    return asked


def registered_keys(hass, entry) -> set[str]:
    """The description keys of the entry's registry rows."""
    prefix = f"{entity_prefix(entry.entry_id)}_"
    rows = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return {row.unique_id.removeprefix(prefix) for row in rows}


async def reload_entry(hass, entry) -> None:
    """Unload the entry and set it up again, as a restart does."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await setup_entry(hass, entry)


async def test_a_datamanager_entry_sends_no_web_request(
    hass, datamanager_modbus, web_requests, monkeypatch
):
    """Six GEN24 paths were asked at every start and fed entities that stay empty."""
    asked = answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    entry = make_datamanager_entry(hass)
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert asked == [HOST]
    assert web_requests == []
    assert entry.runtime_data.web is None
    assert not EVENT_KEYS & registered_keys(hass, entry)
    assert state_of(hass, entry, "acpower") == "597.0"


async def test_a_datamanager_asleep_at_the_start_stays_one(
    hass, datamanager_modbus, web_requests, monkeypatch
):
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    entry = make_datamanager_entry(hass)
    await setup_entry(hass, entry)
    answer_family(monkeypatch, DeviceFamily.UNKNOWN)

    await reload_entry(hass, entry)

    assert entry.data[CONF_DATAMANAGER] is True
    assert web_requests == []
    assert not EVENT_KEYS & registered_keys(hass, entry)


async def test_a_gen24_at_a_datamanagers_address_gets_its_web_poll_back(
    hass, mock_modbus, monkeypatch
):
    serve_the_public_endpoints(mock_modbus, monkeypatch)
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    entry = make_datamanager_entry(hass)
    await setup_entry(hass, entry)
    assert entry.runtime_data.web is None
    answer_family(monkeypatch, DeviceFamily.OTHER)

    await reload_entry(hass, entry)

    assert entry.data[CONF_DATAMANAGER] is False
    assert entry.runtime_data.web is not None
    assert registered_keys(hass, entry) >= EVENT_KEYS


@pytest.mark.parametrize("family", [DeviceFamily.OTHER, DeviceFamily.UNKNOWN])
async def test_any_other_device_keeps_its_web_poll(
    hass, mock_modbus, monkeypatch, family
):
    serve_the_public_endpoints(mock_modbus, monkeypatch)
    answer_family(monkeypatch, family)
    entry = make_datamanager_entry(hass)
    await setup_entry(hass, entry)

    assert entry.runtime_data.web is not None
    assert not entry.data.get(CONF_DATAMANAGER, False)


async def test_an_entry_with_a_role_is_not_asked_for_its_family(
    hass, mock_modbus, monkeypatch
):
    asked = answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    entry = make_entry(hass, minor_version=14, api_username="customer")
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert asked == []


async def test_learning_the_family_reloads_nothing(
    hass, datamanager_modbus, web_requests, monkeypatch
):
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    entry = make_datamanager_entry(hass)
    await setup_entry(hass, entry)

    assert len(datamanager_modbus.connections) == 1


def row_ids(hass, entry) -> set[str]:
    """Every registry row of the entry."""
    rows = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return {row.entity_id for row in rows}


@pytest.mark.parametrize("no_meter", [GatewayTargetError, IllegalDataAddressError])
async def test_a_datamanager_entry_loses_no_registry_row(
    hass, datamanager_modbus, web_requests, monkeypatch, no_meter
):
    """The meter of a 0.1.9 entry waits on unit 240, which nothing reads yet."""
    datamanager_modbus.fail_requests(METER_UNIT_ID, no_meter())
    entry, _ = _redpomodoro_entry(hass)
    await setup_entry(hass, entry)
    for key in EVENT_KEYS:
        er.async_get(hass).async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entity_prefix(entry.entry_id)}_{key}",
            config_entry=entry,
        )
    rows = row_ids(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    values = {**entry.data, "api_username": "none"}
    hass.config_entries.async_update_entry(entry, data=values, options={})
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)

    await setup_entry(hass, entry)

    assert rows - row_ids(hass, entry) == set()
    assert entry.runtime_data.web is None


# -- its meters ----------------------------------------------------------------------

# Where the serial number of a SunSpec common model starts, 16 registers long.
SERIAL_REGISTER = 40052


def add_meter(shared, unit_id: int, serial: str) -> None:
    """Serve the GEN24's meter on a Datamanager unit; the model is the same."""
    registers = dict(load_fixture("symo_gen24_fw1386.json")[METER_UNIT_ID]["holding"])
    text = serial.encode("ascii").ljust(32, b"\0")
    for offset in range(16):
        word = int.from_bytes(text[2 * offset : 2 * offset + 2], "big")
        registers[SERIAL_REGISTER + offset] = word
    shared._fixture[unit_id] = {"holding": registers}
    shared.fail_requests(unit_id, None)


def answer_site(monkeypatch, *meters: tuple[str, int], inverters: int = 1) -> None:
    """Let the Solar API list these meters and as many inverters."""
    listed = tuple(SolarApiMeter(serial, location) for serial, location in meters)
    monkeypatch.setattr(
        datamanager,
        "read_site",
        lambda host: Site(meters=listed, inverters=inverters),
    )


@pytest.fixture
def datamanager_entry(hass, datamanager_modbus, web_requests, monkeypatch):
    """An entry on a Datamanager, not set up yet."""
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    return make_datamanager_entry(hass)


async def test_the_meter_of_a_datamanager_is_read(
    hass, datamanager_modbus, datamanager_entry, monkeypatch
):
    add_meter(datamanager_modbus, 240, "A1")
    answer_site(monkeypatch, ("A1", 0))
    entry = datamanager_entry
    await setup_entry(hass, entry)

    assert state_of(hass, entry, "meter_240_power") == "30.0"
    # 30 W from the grid and the inverter's 597 W.
    assert state_of(hass, entry, "load") == "627.0"
    assert datamanager_modbus.unit(METER_UNIT_ID).read_events == []


async def test_two_meters_are_told_apart_by_their_serial_numbers(
    hass, datamanager_modbus, datamanager_entry, monkeypatch
):
    add_meter(datamanager_modbus, 240, "A1")
    add_meter(datamanager_modbus, 241, "B2")
    answer_site(monkeypatch, ("B2", 0), ("A1", 256))
    entry = datamanager_entry
    await setup_entry(hass, entry)

    assert entry.runtime_data.primary_meter_unit_id == 241
    assert state_of(hass, entry, "meter_240_power") == "30.0"
    assert state_of(hass, entry, "meter_241_power") == "30.0"
    assert state_of(hass, entry, "load") == "627.0"


@pytest.mark.parametrize("inverters", [0, 2])
async def test_the_load_is_unknown_beside_other_inverters(
    hass, datamanager_modbus, datamanager_entry, monkeypatch, inverters
):
    add_meter(datamanager_modbus, 240, "A1")
    answer_site(monkeypatch, ("A1", 0), inverters=inverters)
    entry = datamanager_entry
    await setup_entry(hass, entry)

    assert state_of(hass, entry, "meter_240_power") == "30.0"
    assert state_of(hass, entry, "load") == "unknown"


async def reload_with(hass, entry, monkeypatch, *, inverters: int) -> None:
    """Start the entry again while the Solar API lists so many inverters."""
    answer_site(monkeypatch, ("A1", 0), inverters=inverters)
    await reload_entry(hass, entry)


async def test_an_inverter_asleep_at_a_start_is_still_the_only_one(
    hass, datamanager_modbus, datamanager_entry, monkeypatch
):
    """An IG Plus was listed by day, and by night nothing, with the Datamanager up."""
    add_meter(datamanager_modbus, 240, "A1")
    answer_site(monkeypatch, ("A1", 0), inverters=1)
    entry = datamanager_entry
    await setup_entry(hass, entry)

    await reload_with(hass, entry, monkeypatch, inverters=0)

    assert state_of(hass, entry, "load") == "627.0"


async def test_one_of_two_inverters_asleep_is_not_forgotten(
    hass, datamanager_modbus, datamanager_entry, monkeypatch
):
    add_meter(datamanager_modbus, 240, "A1")
    answer_site(monkeypatch, ("A1", 0), inverters=2)
    entry = datamanager_entry
    await setup_entry(hass, entry)

    await reload_with(hass, entry, monkeypatch, inverters=1)

    assert state_of(hass, entry, "load") == "unknown"


async def test_a_second_inverter_added_later_is_noticed(
    hass, datamanager_modbus, datamanager_entry, monkeypatch
):
    add_meter(datamanager_modbus, 240, "A1")
    answer_site(monkeypatch, ("A1", 0), inverters=1)
    entry = datamanager_entry
    await setup_entry(hass, entry)

    await reload_with(hass, entry, monkeypatch, inverters=2)

    assert state_of(hass, entry, "load") == "unknown"


async def test_without_the_solar_api_the_meter_is_read_without_the_load(
    hass, datamanager_modbus, datamanager_entry
):
    add_meter(datamanager_modbus, 240, "A1")
    entry = datamanager_entry
    await setup_entry(hass, entry)

    assert state_of(hass, entry, "meter_240_power") == "30.0"
    assert state_of(hass, entry, "load") == "unknown"


@pytest.mark.parametrize("silence", [GatewayTargetError, ModbusTimeoutError])
async def test_a_meter_silent_at_the_start_is_read_once_it_answers(
    hass, datamanager_modbus, datamanager_entry, monkeypatch, silence
):
    add_meter(datamanager_modbus, 240, "A1")
    answer_site(monkeypatch, ("A1", 0))
    datamanager_modbus.fail_requests(240, silence())
    entry = datamanager_entry
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    datamanager_modbus.fail_requests(240, None)

    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()

    assert state_of(hass, entry, "meter_240_power") == "30.0"
    assert state_of(hass, entry, "load") == "627.0"


async def test_a_sub_meter_is_no_load_while_the_grid_meter_is_silent(
    hass, datamanager_modbus, datamanager_entry, monkeypatch
):
    add_meter(datamanager_modbus, 241, "B2")
    answer_site(monkeypatch, ("A1", 0), ("B2", 256))
    entry = datamanager_entry
    await setup_entry(hass, entry)

    assert state_of(hass, entry, "meter_241_power") == "30.0"
    assert state_of(hass, entry, "load") == "unknown"


async def test_a_datamanager_without_a_meter_is_set_up(
    hass, datamanager_modbus, datamanager_entry, caplog
):
    entry = datamanager_entry
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.device.meter_unit_ids == (240,)
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and "fronius_modbus" in record.name
    ]
    assert warnings == []


async def test_a_datamanager_gone_during_the_search_is_tried_again(
    hass, datamanager_modbus, datamanager_entry
):
    for unit_id in (INVERTER_UNIT_ID, *DATAMANAGER_METER_UNIT_IDS):
        datamanager_modbus.fail_requests(unit_id, ModbusConnectionError())
    entry = datamanager_entry

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_gen24_is_not_asked_on_a_datamanagers_units(
    hass, mock_modbus, monkeypatch
):
    serve_the_public_endpoints(mock_modbus, monkeypatch)
    answer_family(monkeypatch, DeviceFamily.OTHER)
    entry = make_datamanager_entry(hass)
    await setup_entry(hass, entry)

    for unit_id in DATAMANAGER_METER_UNIT_IDS:
        assert mock_modbus.unit(unit_id).read_events == []


async def test_the_meter_0_1_9_read_on_a_datamanager_keeps_its_rows(
    hass, datamanager_modbus, web_requests, monkeypatch
):
    """0.1.9 read the meter on the unit set in its setup, 240 on a Datamanager."""
    add_meter(datamanager_modbus, 240, "A1")
    entry, _ = _redpomodoro_entry(hass)
    values = {**entry.data, CONF_METER_UNIT_ID: 240}
    hass.config_entries.async_update_entry(entry, data=values)
    registry = er.async_get(hass)
    meter_rows = {
        row.entity_id
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if "_m1_" in row.unique_id
    }
    assert meter_rows
    await setup_entry(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    values = {**entry.data, "api_username": "none"}
    hass.config_entries.async_update_entry(entry, data=values, options={})
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    answer_site(monkeypatch, ("A1", 0))

    await setup_entry(hass, entry)

    prefix = f"{entity_prefix(entry.entry_id)}_meter_240_"
    moved = {
        row.entity_id
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if row.unique_id.startswith(prefix)
    }
    assert meter_rows & moved
    assert (
        hass.states.get(
            registry.async_get_entity_id("sensor", DOMAIN, f"{prefix}power")
        ).state
        == "30.0"
    )
