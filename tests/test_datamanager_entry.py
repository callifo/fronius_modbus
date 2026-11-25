"""An entry on a Datamanager, end to end, with the registers of issue 110."""

from typing import Any

from modbus_connection import GatewayTargetError, IllegalDataAddressError
import pytest
import requests

from custom_components.fronius_modbus import datamanager, froniuswebclient
from custom_components.fronius_modbus.const import (
    CONF_DATAMANAGER,
    DOMAIN,
    entity_prefix,
)
from custom_components.fronius_modbus.datamanager import DeviceFamily
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import INVERTER_UNIT_ID, METER_UNIT_ID, load_fixture
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

    def _identify(host: str, timeout: float = 4.0) -> DeviceFamily:
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
