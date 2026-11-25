"""An entry on a Datamanager, end to end, with the registers of issue 110."""

from typing import Any

import pytest
import requests

from custom_components.fronius_modbus import froniuswebclient
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr

from .conftest import INVERTER_UNIT_ID, load_fixture
from .test_e2e import HOST, make_entry, setup_entry, state_of

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
