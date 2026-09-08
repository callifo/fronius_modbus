"""The config flow, with the web client stubbed and Modbus served by the mock connection."""

from unittest.mock import AsyncMock

from modbus_connection import ModbusConnectionError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import fronius_modbus
from custom_components.fronius_modbus import config_flow
from custom_components.fronius_modbus.const import DOMAIN
from custom_components.fronius_modbus.token_store import async_get_token_store
from homeassistant.data_entry_flow import FlowResultType

from .conftest import INVERTER_UNIT_ID

HOST = "192.0.2.10"
USER_INPUT = {"host": HOST, "scan_interval": 10, "restrict_modbus_to_this_ip": False}


def make_entry(hass) -> MockConfigEntry:
    """A configured entry on the customer role, added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, "web_scan_interval": 60, "api_username": "customer"},
        unique_id=HOST,
        version=1,
        minor_version=11,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


@pytest.fixture(autouse=True)
def _stub_web_client(monkeypatch):
    """The web API is not what this flow test is about: login and token minting succeed."""
    monkeypatch.setattr(config_flow.FroniusWebClient, "login", lambda self: True)
    monkeypatch.setattr(
        config_flow.FroniusWebClient, "ensure_modbus_enabled", lambda self, *a: True
    )
    monkeypatch.setattr(
        config_flow,
        "mint_token",
        lambda host, user, password: {"realm": "r", "token": "t"},
    )

    # A created entry is set up right away; that path is what test_e2e covers, and
    # letting it run here would take the stubbed web client onto the real network.
    async def _skip_setup(hass, entry):
        return True

    monkeypatch.setattr(fronius_modbus, "async_setup_entry", _skip_setup)


async def run_flow(hass) -> dict:
    """Walk the user step and the password step it asks for."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "user_password"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )


async def test_the_config_flow_creates_an_entry(hass, mock_modbus):
    result = await run_flow(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Fronius 192.0.2.10"
    assert result["data"]["host"] == HOST
    assert result["minor_version"] == 11


async def test_a_second_flow_for_the_same_host_aborts(hass, mock_modbus):
    MockConfigEntry(domain=DOMAIN, data={"host": HOST}, unique_id=HOST).add_to_hass(
        hass
    )

    result = await run_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_an_unreachable_inverter_shows_cannot_connect(hass, mock_modbus):
    mock_modbus.fail_requests(INVERTER_UNIT_ID, ModbusConnectionError())

    result = await run_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_technician_role_mints_and_stores_a_technician_token(
    hass, mock_modbus, monkeypatch
):
    """Upstream #130: one role per entry; the password belongs to the selected role."""
    minted = []
    monkeypatch.setattr(
        config_flow,
        "mint_token",
        lambda host, user, password: (
            minted.append(user) or {"realm": "r", "token": "t"}
        ),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT | {"api_username": "technician"}
    )
    assert result["step_id"] == "user_password"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["api_username"] == "technician"
    assert minted == ["technician"]
    store = async_get_token_store(hass)
    assert await store.async_load_token(HOST, "technician") == {
        "realm": "r",
        "token": "t",
    }
    assert await store.async_load_token(HOST, "customer") is None


async def test_switching_the_role_asks_for_that_roles_password(hass, monkeypatch):
    """A stored customer token must not stand in for the technician role."""
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="stored")
    monkeypatch.setattr(
        config_flow, "_validate_input", AsyncMock(return_value={"title": "Fronius"})
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "host": HOST,
            "scan_interval": 10,
            "web_scan_interval": 60,
            "restrict_modbus_to_this_ip": False,
            "api_username": "technician",
        },
    )
    assert result["step_id"] == "password"
    # No technician token yet: an empty password is not accepted.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_password": ""}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "missing_api_password"


async def test_changing_only_the_host_spelling_keeps_the_stored_token(
    hass, monkeypatch
):
    """Audit A06: the host comparison was case-sensitive, the token key never is."""
    named_host = "inverter.example"
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(
        named_host, realm="r", token="stored"
    )
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    settings = config_flow.entry_defaults(entry) | {"host": named_host.upper()}

    await config_flow.async_update_entry_from_input(
        hass, entry, settings, previous_host=named_host
    )

    stored = await async_get_token_store(hass).async_load_token(named_host)
    assert stored == {"realm": "r", "token": "stored"}
