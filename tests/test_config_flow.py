"""The config flow, with the web client stubbed and Modbus served by the mock connection."""

import asyncio
from ipaddress import ip_address
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modbus_connection import ModbusConnectionError
import pytest

from custom_components import fronius_modbus
from custom_components.fronius_modbus import (
    config_flow,
    datamanager,
    discovery,
    flow_forms,
    flow_state,
    froniuswebclient,
)
from custom_components.fronius_modbus.const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    entry_title,
    instance_key,
)
from custom_components.fronius_modbus.datamanager import DeviceFamily
from custom_components.fronius_modbus.fronius_modbus_api.device import DeviceIdentity
from custom_components.fronius_modbus.froniuswebclient import (
    FroniusWebResponseError,
    FroniusWebUnreachable,
)
from custom_components.fronius_modbus.token_store import async_get_token_store
from custom_components.fronius_modbus.web_errors import ClientIpResolutionError
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .conftest import INVERTER_UNIT_ID
from .ha_test_plugin import MockConfigEntry

HOST = "192.0.2.10"
USER_INPUT = {"host": HOST, "scan_interval": 10}
# Home Assistant's own address towards the inverter, as the socket names it.
RESOLVED_IP = "192.0.2.50"


def make_entry(hass) -> MockConfigEntry:
    """A configured entry on the customer role, added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**USER_INPUT, "web_scan_interval": 60, "api_username": "customer"},
        unique_id=HOST,
        version=1,
        minor_version=12,
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
        config_flow.FroniusWebClient, "resolve_client_ip", lambda self: RESOLVED_IP
    )
    monkeypatch.setattr(
        config_flow.FroniusWebClient, "restriction_allows", lambda self, address: False
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
    assert result["minor_version"] == 14
    # Leaving the choice alone must not lift a restriction the inverter has.
    assert result["data"]["modbus_restriction"] == "keep"


async def test_a_new_entry_polls_the_meter_power_with_the_rest(hass, mock_modbus):
    result = await run_flow(hass)

    assert result["data"]["meter_power_interval"] == 0


async def test_a_new_entry_keeps_a_fast_meter_power_interval(hass, mock_modbus):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "meter_power_interval": 1}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["data"]["meter_power_interval"] == 1


async def test_a_meter_power_interval_not_below_the_update_interval_is_refused(
    hass, mock_modbus
):
    """A slower fast poll would hide the full poll's newer reading."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "meter_power_interval": 10}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "meter_power_interval_too_long"}


async def test_a_second_flow_for_the_same_host_aborts(hass, mock_modbus):
    MockConfigEntry(domain=DOMAIN, data={"host": HOST}, unique_id=HOST).add_to_hass(
        hass
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_an_unreachable_inverter_shows_cannot_connect(hass, mock_modbus):
    mock_modbus.fail_requests(INVERTER_UNIT_ID, ModbusConnectionError())

    result = await run_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_a_web_server_error_during_setup_shows_cannot_connect(
    hass, mock_modbus, monkeypatch
):
    """A 500 while enabling Modbus stopped being an OSError and crashed the flow."""

    def refuse(self, *_args):
        raise FroniusWebResponseError("HTTP 500 on /api/config/modbus", 500)

    monkeypatch.setattr(config_flow.FroniusWebClient, "ensure_modbus_enabled", refuse)

    result = await run_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_technician_role_mints_and_stores_a_technician_token(
    hass, mock_modbus, monkeypatch
):
    """Pull request 130: one role per entry; the password belongs to the selected role."""
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
            "modbus_restriction": "keep",
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


async def _configure(hass, entry, scan_interval=10):
    """Run Configure on the customer role and enter a password."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "host": HOST,
            "scan_interval": scan_interval,
            "web_scan_interval": 60,
            "modbus_restriction": "keep",
            "api_username": "customer",
        },
    )
    assert result["step_id"] == "password"
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )


@pytest.mark.parametrize(
    ("loaded", "scan_interval", "reloads"),
    [
        # A new password alone changes nothing in the entry: no listener fires.
        (True, 10, 1),
        # A changed entry reloads through its update listener; one more
        # reload here would run it twice.
        (True, 20, 0),
        # An entry that is not loaded has no listener at all.
        (False, 10, 1),
        (False, 20, 1),
    ],
)
async def test_configure_reloads_what_no_listener_reloads(
    hass, monkeypatch, loaded, scan_interval, reloads
):
    """Configure stored a new token, and the entry kept the old one."""
    entry = make_entry(hass)
    monkeypatch.setattr(
        config_flow, "_validate_input", AsyncMock(return_value={"title": "Fronius"})
    )
    reload = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    await _configure(hass, entry)
    if loaded:
        entry.mock_state(hass, ConfigEntryState.LOADED)
    reload.reset_mock()

    result = await _configure(hass, entry, scan_interval)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert reload.await_count == reloads


async def test_a_title_the_owner_gave_survives_every_settings_flow(hass, monkeypatch):
    """Configure, Reconfigure and Reauth put the generated title back."""
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(entry, title="Garage roof")
    monkeypatch.setattr(
        config_flow, "_validate_input", AsyncMock(return_value={"title": "Fronius"})
    )
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )

    await _configure(hass, entry, scan_interval=20)
    assert entry.title == "Garage roof"
    await config_flow.async_update_entry_from_input(
        hass, entry, config_flow.entry_defaults(entry) | {"scan_interval": 30}
    )
    assert entry.title == "Garage roof"


async def test_a_generated_title_follows_the_host(hass, monkeypatch):
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(
        entry, title=entry_title(config_flow.entry_defaults(entry))
    )
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    settings = config_flow.entry_defaults(entry) | {"host": "192.0.2.99"}

    await config_flow.async_update_entry_from_input(hass, entry, settings)

    assert entry.title == entry_title(settings)


async def test_changing_only_the_host_spelling_keeps_the_stored_token(
    hass, monkeypatch
):
    """The host comparison was case-sensitive, the token key never is."""
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


async def test_a_token_whose_setup_fails_is_not_kept(hass, mock_modbus):
    """The minted token was saved before the inverter was checked.

    A failed setup left a password-equivalent credential with no entry.
    """
    mock_modbus.fail_requests(INVERTER_UNIT_ID, ModbusConnectionError())

    result = await run_flow(hass)

    assert result["errors"]["base"] == "cannot_connect"
    assert await async_get_token_store(hass).async_load_token(HOST) is None


async def test_a_token_for_a_host_already_set_up_is_not_kept(
    hass, mock_modbus, monkeypatch
):
    """An entry set up while the inverter was checked; its new role token stayed behind."""
    validate = config_flow._validate_input

    async def validate_while_taken(hass, settings, **kwargs):
        info = await validate(hass, settings, **kwargs)
        MockConfigEntry(
            domain=DOMAIN,
            data={"host": HOST, "api_username": "customer"},
            unique_id=HOST,
            version=1,
            minor_version=12,
        ).add_to_hass(hass)
        return info

    monkeypatch.setattr(config_flow, "_validate_input", validate_while_taken)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT | {"api_username": "technician"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["reason"] == "already_configured"
    assert (
        await async_get_token_store(hass).async_load_token(HOST, "technician") is None
    )


async def test_a_duplicate_flow_leaves_the_existing_entrys_token_alone(
    hass, monkeypatch
):
    """The aborted flow had already replaced the entry's token.

    The entry appears while the inverter is checked: a host taken before is
    refused ahead of the login.
    """
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="old")

    async def validate(hass, settings, *, api_token, **_kwargs):
        if api_token == {"realm": "r", "token": "old"}:
            raise config_flow._InvalidApiCredentials
        make_entry(hass)
        return {"title": "Fronius"}

    monkeypatch.setattr(config_flow, "_validate_input", validate)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["reason"] == "already_configured"
    assert await store.async_load_token(HOST) == {"realm": "r", "token": "old"}


async def test_a_role_switch_failing_after_the_update_keeps_the_new_token(
    hass, monkeypatch
):
    """The entry already used the new role when its token went."""
    entry = make_entry(hass)
    monkeypatch.setattr(
        config_flow, "_validate_input", AsyncMock(return_value={"title": "Fronius"})
    )
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )

    def fail(self, **_kwargs):
        raise RuntimeError("after the update")

    monkeypatch.setattr(
        config_flow.FroniusModbusOptionsFlow, "async_create_entry", fail
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "host": HOST,
            "scan_interval": 10,
            "web_scan_interval": 60,
            "modbus_restriction": "keep",
            "api_username": "technician",
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["errors"]["base"] == "unknown"
    assert config_flow.entry_defaults(entry)["api_username"] == "technician"
    stored = await async_get_token_store(hass).async_load_token(HOST, "technician")
    assert stored == {"realm": "r", "token": "t"}


async def test_a_late_failure_does_not_bring_back_a_stale_token(hass, monkeypatch):
    """The stale target-role token came back over the fresh one."""
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(
        HOST, realm="r", token="stale", user="technician"
    )
    monkeypatch.setattr(
        config_flow, "_validate_input", AsyncMock(return_value={"title": "Fronius"})
    )
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )

    def fail(self, **_kwargs):
        raise RuntimeError("after the update")

    monkeypatch.setattr(
        config_flow.FroniusModbusOptionsFlow, "async_create_entry", fail
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "host": HOST,
            "scan_interval": 10,
            "web_scan_interval": 60,
            "modbus_restriction": "keep",
            "api_username": "technician",
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["errors"]["base"] == "unknown"
    assert config_flow.entry_defaults(entry)["api_username"] == "technician"
    stored = await async_get_token_store(hass).async_load_token(HOST, "technician")
    assert stored == {"realm": "r", "token": "t"}


OTHER_HOST = "192.0.2.20"


def _record_modbus_setup(monkeypatch) -> list[tuple]:
    applied: list[tuple] = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "ensure_modbus_enabled",
        lambda self, *args: applied.append(args) or True,
    )
    return applied


def _record_contact(monkeypatch) -> list[str]:
    """Every login and token mint: the inverter contacted at all."""
    contacted: list[str] = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "login",
        lambda self: contacted.append("login") or True,
    )
    monkeypatch.setattr(
        config_flow,
        "mint_token",
        lambda host, user, password: (
            contacted.append("mint") or {"realm": "r", "token": "t"}
        ),
    )
    return contacted


def _other_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": OTHER_HOST,
            "scan_interval": 10,
            "web_scan_interval": 60,
            "api_username": "customer",
        },
        unique_id=OTHER_HOST,
        version=1,
        minor_version=12,
    )
    entry.add_to_hass(hass)
    return entry


async def test_a_duplicate_setup_leaves_the_inverter_alone(
    hass, mock_modbus, monkeypatch
):
    """The duplicate was found only after Modbus was set up on it.

    With a stored token the settings step validates at once, without a
    password step to recheck the host.
    """
    make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    contacted = _record_contact(monkeypatch)
    applied = _record_modbus_setup(monkeypatch)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["reason"] == "already_configured"
    assert contacted == []
    assert applied == []


async def test_moving_to_a_host_taken_leaves_the_inverter_alone(
    hass, mock_modbus, monkeypatch
):
    """Reconfigure and options moved Modbus settings before refusing."""
    make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    other = _other_entry(hass)
    contacted = _record_contact(monkeypatch)
    applied = _record_modbus_setup(monkeypatch)
    moved = {
        "host": HOST,
        "scan_interval": 10,
        "web_scan_interval": 60,
        "modbus_restriction": "keep",
        "api_username": "customer",
    }

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": other.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], moved)
    if result.get("step_id", "").endswith("password"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_password": "secret"}
        )
    options = await hass.config_entries.options.async_init(other.entry_id)
    options = await hass.config_entries.options.async_configure(
        options["flow_id"], moved
    )
    if options.get("step_id", "").endswith("password"):
        options = await hass.config_entries.options.async_configure(
            options["flow_id"], {"api_password": "secret"}
        )

    assert result["errors"]["base"] == "already_configured"
    assert options["errors"]["base"] == "already_configured"
    assert contacted == []
    assert applied == []


async def test_a_reconfigure_failing_late_keeps_the_fresh_token(hass, monkeypatch):
    """The entry used the fresh token when the stale one came back."""
    entry = make_entry(hass)
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="stale", user="technician")

    async def validate(hass, settings, *, api_token, **_kwargs):
        if api_token == {"realm": "r", "token": "stale"}:
            raise config_flow._InvalidApiCredentials
        return {"title": "Fronius"}

    def fail(self, **_kwargs):
        raise RuntimeError("after the update")

    monkeypatch.setattr(config_flow, "_validate_input", validate)
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(config_flow.ConfigFlow, "async_abort", fail)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "host": HOST,
            "scan_interval": 10,
            "web_scan_interval": 60,
            "modbus_restriction": "keep",
            "api_username": "technician",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["errors"]["base"] == "unknown"
    assert config_flow.entry_defaults(entry)["api_username"] == "technician"
    assert await store.async_load_token(HOST, "technician") == {
        "realm": "r",
        "token": "t",
    }


async def test_a_host_taken_during_the_password_step_leaves_the_inverter_alone(
    hass, mock_modbus, monkeypatch
):
    """The password step validated, and set up Modbus, without a recheck."""
    contacted = _record_contact(monkeypatch)
    applied = _record_modbus_setup(monkeypatch)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "user_password"
    make_entry(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["reason"] == "already_configured"
    assert contacted == []
    assert applied == []


async def test_a_move_to_a_host_taken_during_the_password_step_is_refused(
    hass, mock_modbus, monkeypatch
):
    """Reconfigure and options rechecked the host only after validation."""
    other = _other_entry(hass)
    contacted = _record_contact(monkeypatch)
    applied = _record_modbus_setup(monkeypatch)
    moved = {
        "host": HOST,
        "scan_interval": 10,
        "web_scan_interval": 60,
        "modbus_restriction": "keep",
        "api_username": "customer",
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": other.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], moved)
    options = await hass.config_entries.options.async_init(other.entry_id)
    options = await hass.config_entries.options.async_configure(
        options["flow_id"], moved
    )
    assert result["step_id"] == "reconfigure_password"
    assert options["step_id"] == "password"
    make_entry(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )
    options = await hass.config_entries.options.async_configure(
        options["flow_id"], {"api_password": "secret"}
    )

    assert result["errors"]["base"] == "already_configured"
    assert options["errors"]["base"] == "already_configured"
    assert contacted == []
    assert applied == []


async def test_an_aborted_setup_does_not_hand_its_token_to_another_entry(
    hass, monkeypatch
):
    """A competing entry on the same role kept the aborted flow's token."""

    async def validate_while_taken(hass, settings, **_kwargs):
        make_entry(hass)
        return {"title": "Fronius"}

    monkeypatch.setattr(config_flow, "_validate_input", validate_while_taken)

    result = await run_flow(hass)

    assert result["reason"] == "already_configured"
    assert await async_get_token_store(hass).async_load_token(HOST) is None


async def test_a_host_taken_while_the_token_is_minted_leaves_the_inverter_alone(
    hass, mock_modbus, monkeypatch
):
    """The claim was not held through minting and validation."""
    applied = _record_modbus_setup(monkeypatch)

    async def mint_while_taken(hass, host, password, username):
        make_entry(hass)
        return {"realm": "r", "token": "t"}

    monkeypatch.setattr(config_flow, "_async_mint_token", mint_while_taken)

    result = await run_flow(hass)

    assert result["reason"] == "already_configured"
    assert applied == []


NO_WEB_INPUT = {**USER_INPUT, "api_username": "none"}


async def test_an_entry_without_the_web_api_needs_no_password(
    hass, mock_modbus, monkeypatch
):
    """Setup without a web login: no password step, no token, no settings write."""
    contacts: list[str] = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient, "login", lambda self: contacts.append("login")
    )
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "ensure_modbus_enabled",
        lambda self, *a: contacts.append("modbus"),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NO_WEB_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["api_username"] == "none"
    assert contacts == []
    assert await async_get_token_store(hass).async_load_token(HOST, "none") is None


async def test_an_unreachable_modbus_without_the_web_api_says_so(hass, mock_modbus):
    mock_modbus.fail_requests(INVERTER_UNIT_ID, ModbusConnectionError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NO_WEB_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect_modbus"}


async def test_turning_the_web_api_on_applies_the_saved_restriction(
    hass, mock_modbus, monkeypatch
):
    """The restriction chosen without the web API was never written."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **USER_INPUT,
            "web_scan_interval": 60,
            "api_username": "none",
            "modbus_restriction": "home_assistant",
        },
        unique_id=HOST,
        version=1,
        minor_version=13,
    )
    entry.add_to_hass(hass)
    written: list[tuple] = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "ensure_modbus_enabled",
        lambda self, *args: written.append(args),
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "host": HOST,
            "scan_interval": 10,
            "web_scan_interval": 60,
            "modbus_restriction": "home_assistant",
            "api_username": "customer",
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )
    assert result["step_id"] == "allowed_host"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"allowed_host": RESOLVED_IP}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # port, meter address, inverter id, the restriction, the model type
    # consent, then the address to allow
    assert [(args[3], args[5]) for args in written] == [("home_assistant", RESOLVED_IP)]


# -- discovery ---------------------------------------------------------------------

ZEROCONF_TYPE = "_Fronius-SE-Inverter._tcp.local."
SERIAL = "12345678"
MOVED_HOST = "192.0.2.20"


def discovered(host: str = HOST, serial: str = SERIAL, txt: dict | None = None):
    """What the inverter announces over mDNS; its JSON is split over numbered keys."""
    meta = json.dumps(
        {"DeviceMeta": {"Device-Information": {"DeviceSerialNumber": serial}}}
    )
    properties = {"FSEI-DID": "V 1|P JSON|PFC 2", "00": meta[:30], "01": meta[30:]}
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        port=80,
        hostname="inverter.local.",
        type=ZEROCONF_TYPE,
        name=f"Fronius Symo GEN24 10.0-{serial}.{ZEROCONF_TYPE}",
        properties=properties if txt is None else txt,
    )


def with_inverter_device(hass, entry: MockConfigEntry) -> None:
    """The inverter device a set-up entry has, with the serial Modbus reports."""
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{instance_key(entry.entry_id)}_inverter")},
        serial_number=SERIAL,
    )


async def discover(hass, info) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=info
    )


@pytest.fixture(autouse=True)
def inverters(monkeypatch) -> dict[str, str]:
    """The serial each address answers with over Modbus; the moved inverter's by default."""
    serials = {MOVED_HOST: SERIAL, "192.0.2.30": SERIAL}
    monkeypatch.setattr(discovery, "MOVE_READ_RETRY_SECONDS", 0)

    async def serial_at(_hass, host, _port, _unit_id):
        return serials.get(host)

    monkeypatch.setattr(discovery, "async_serial_at", serial_at)
    return serials


async def test_a_discovered_inverter_opens_the_setup_with_its_host(hass):
    result = await discover(hass, discovered())

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["data_schema"]({})["host"] == HOST
    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"]["title_placeholders"] == {"name": "Fronius Symo GEN24 10.0"}


async def test_a_discovered_host_already_set_up_is_not_offered(hass):
    make_entry(hass)

    result = await discover(hass, discovered())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_an_inverter_set_up_by_name_is_not_offered_again(hass, inverters):
    inverters[HOST] = SERIAL
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "inverter.example", "api_username": "none"},
        unique_id="inverter.example",
    )
    entry.add_to_hass(hass)
    with_inverter_device(hass, entry)

    result = await discover(hass, discovered())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "inverter.example"


async def test_a_second_inverter_is_offered(hass):
    entry = make_entry(hass)
    with_inverter_device(hass, entry)

    result = await discover(hass, discovered(host=MOVED_HOST, serial="87654321"))

    assert result["type"] is FlowResultType.FORM
    assert config_flow.entry_defaults(entry)["host"] == HOST


async def test_unreadable_discovery_data_still_offers_the_setup(hass):
    result = await discover(hass, discovered(txt={"00": "{not json"}))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_a_moved_inverter_is_followed_to_its_new_address(hass):
    entry = make_entry(hass)
    # A settings change leaves the host in the options too, and those win.
    hass.config_entries.async_update_entry(
        entry, options={"host": HOST}, title=f"Fronius {HOST}"
    )
    with_inverter_device(hass, entry)
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="t")

    result = await discover(hass, discovered(host=MOVED_HOST))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_flow.entry_defaults(entry)["host"] == MOVED_HOST
    assert entry.unique_id == MOVED_HOST
    assert entry.title == f"Fronius {MOVED_HOST}"
    assert await store.async_load_token(MOVED_HOST) == {"realm": "r", "token": "t"}
    assert await store.async_load_token(HOST) is None


async def test_a_moved_inverter_keeps_the_title_its_owner_gave(hass):
    """Following the address silently replaced a renamed entry's title."""
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(entry, title="Garage inverter")
    with_inverter_device(hass, entry)

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == MOVED_HOST
    assert entry.title == "Garage inverter"


async def test_a_moved_inverter_is_not_followed_to_a_taken_address(hass):
    entry = make_entry(hass)
    with_inverter_device(hass, entry)
    MockConfigEntry(
        domain=DOMAIN, data={"host": MOVED_HOST}, unique_id=MOVED_HOST
    ).add_to_hass(hass)

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == HOST
    assert entry.unique_id == HOST


async def test_an_ipv6_discovery_does_not_replace_an_ipv4_host(hass, inverters):
    inverters["2001:db8::10"] = SERIAL
    entry = make_entry(hass)
    with_inverter_device(hass, entry)

    await discover(hass, discovered(host="2001:db8::10"))

    assert config_flow.entry_defaults(entry)["host"] == HOST


async def test_a_manual_setup_is_not_blocked_by_an_open_card(hass, mock_modbus):
    """The card held the host, and adding it by hand aborted as in progress."""
    card = await discover(hass, discovered())

    result = await run_flow(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert card["flow_id"] not in {
        flow["flow_id"] for flow in hass.config_entries.flow.async_progress()
    }


async def test_a_discovered_inverter_is_set_up_from_its_card(hass, mock_modbus):
    card = await discover(hass, discovered())

    result = await hass.config_entries.flow.async_configure(card["flow_id"], USER_INPUT)
    assert result["step_id"] == "user_password"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].source == "zeroconf"
    assert result["result"].unique_id == HOST


async def test_an_address_taken_while_the_token_moves_is_not_shared(hass, monkeypatch):
    """Another flow took the address during the token save; both held it."""
    entry = make_entry(hass)
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="t")
    other = MockConfigEntry(
        domain=DOMAIN, data={"host": "192.0.2.99"}, unique_id="192.0.2.99"
    )
    other.add_to_hass(hass)
    save = store._store.async_save

    async def take_the_address(data):
        hass.config_entries.async_update_entry(
            other, data={"host": MOVED_HOST}, unique_id=MOVED_HOST
        )
        await save(data)

    monkeypatch.setattr(store._store, "async_save", take_the_address)

    await discovery.async_follow_host(hass, entry, MOVED_HOST, SERIAL)

    holders = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.unique_id == MOVED_HOST
    ]
    assert len(holders) == 1


async def test_two_announcements_leave_the_token_under_one_address(hass, monkeypatch):
    """Overlapping follows left a password-equivalent token at an unused host."""
    entry = make_entry(hass)
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="t")
    third = "192.0.2.30"
    save = store._store.async_save

    async def save_like_a_disk(data):
        await asyncio.sleep(0)
        await save(data)

    monkeypatch.setattr(store._store, "async_save", save_like_a_disk)

    await asyncio.gather(
        discovery.async_follow_host(hass, entry, MOVED_HOST, SERIAL),
        discovery.async_follow_host(hass, entry, third, SERIAL),
    )

    stored = [
        host for host in (HOST, MOVED_HOST, third) if await store.async_load_token(host)
    ]
    assert stored == [config_flow.entry_defaults(entry)["host"]]


async def test_two_announcements_read_at_once_leave_one_address(hass, monkeypatch):
    """Both read the old address before either moved the entry; the later one kept going."""
    entry = make_entry(hass)
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="t")
    third = "192.0.2.30"

    async def serial_at(_hass, host, _port, _unit_id):
        await asyncio.sleep(0)
        return SERIAL if host != HOST else None

    monkeypatch.setattr(discovery, "async_serial_at", serial_at)

    await asyncio.gather(
        discovery.async_follow_host(hass, entry, MOVED_HOST, SERIAL),
        discovery.async_follow_host(hass, entry, third, SERIAL),
    )

    stored = [
        host for host in (HOST, MOVED_HOST, third) if await store.async_load_token(host)
    ]
    assert stored == [config_flow.entry_defaults(entry)["host"]]


async def test_an_ipv6_only_announcement_is_not_offered(hass):
    """The web client cannot reach an unbracketed IPv6 host."""
    result = await discover(hass, discovered(host="2001:db8::10"))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_ipv4_address"


@pytest.mark.parametrize(
    ("error", "field", "message"),
    [
        (flow_state._CannotConnect, "base", "cannot_connect"),
        (flow_state._CannotConnectModbus, "base", "cannot_connect_modbus"),
        (flow_state._InvalidPort, "base", "invalid_port"),
        (flow_state._InvalidHost, "host", "invalid_host"),
        (flow_state._ScanIntervalTooShort, "base", "scan_interval_too_short"),
        (
            flow_state._MeterPowerIntervalTooLong,
            "base",
            "meter_power_interval_too_long",
        ),
        (flow_state._MissingApiPassword, "base", "missing_api_password"),
        (flow_state._InvalidApiCredentials, "base", "invalid_api_credentials"),
        (flow_state._UnsupportedHardware, "base", "unsupported_hardware"),
        (flow_state._AddressesNotUnique, "base", "modbus_address_conflict"),
        (flow_state._AlreadyConfigured, "base", "already_configured"),
        (flow_state._DatamanagerFound, "api_username", "datamanager_no_login"),
        (RuntimeError, "base", "unknown"),
    ],
)
def test_every_flow_error_shows_its_message(error, field, message):
    errors: dict[str, str] = {}

    flow_forms._set_form_error(errors, error())

    assert errors == {field: message}


# -- reauthentication ----------------------------------------------------------------


async def test_a_reauth_stores_the_new_token(hass, mock_modbus):
    entry = make_entry(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_username": "customer"}
    )
    assert result["step_id"] == "reauth_password"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert await async_get_token_store(hass).async_load_token(HOST) == {
        "realm": "r",
        "token": "t",
    }


async def test_a_reauth_can_leave_the_web_api_off(hass, mock_modbus):
    entry = make_entry(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_username": "none"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_flow.entry_defaults(entry)["api_username"] == "none"


async def test_an_entry_deleted_during_the_reauth_keeps_no_token(
    hass, mock_modbus, monkeypatch
):
    """Success was reported for a gone entry."""
    entry = make_entry(hass)
    validate = config_flow._validate_input

    async def validate_then_delete(hass, *args, **kwargs):
        info = await validate(hass, *args, **kwargs)
        await hass.config_entries.async_remove(entry.entry_id)
        return info

    monkeypatch.setattr(config_flow, "_validate_input", validate_then_delete)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_username": "customer"}
    )
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert await async_get_token_store(hass).async_load_token(HOST) is None


async def test_a_reauth_failing_late_keeps_the_fresh_token(
    hass, mock_modbus, monkeypatch
):
    """The stale token came back."""
    entry = make_entry(hass)
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="stale", user="technician")

    async def validate(hass, settings, *, api_token, **_kwargs):
        if api_token == {"realm": "r", "token": "stale"}:
            raise config_flow._InvalidApiCredentials
        return {"title": "Fronius"}

    abort = config_flow.ConfigFlow.async_abort

    def fail_on_success(self, *, reason, **kwargs):
        if reason == "reauth_successful":
            raise RuntimeError("after the update")
        return abort(self, reason=reason, **kwargs)

    monkeypatch.setattr(config_flow, "_validate_input", validate)
    monkeypatch.setattr(config_flow.ConfigFlow, "async_abort", fail_on_success)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_username": "technician"}
    )
    # The reload the update scheduled ends a reauth flow that is still running.
    with pytest.raises(UnknownFlow):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_password": "secret"}
        )

    assert config_flow.entry_defaults(entry)["api_username"] == "technician"
    assert await store.async_load_token(HOST, "technician") == {
        "realm": "r",
        "token": "t",
    }


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"host": "ab"}, config_flow._InvalidHost),
        ({"port": 70000}, config_flow._InvalidPort),
        ({"scan_interval": 4}, config_flow._ScanIntervalTooShort),
        ({"meter_power_interval": 10}, config_flow._MeterPowerIntervalTooLong),
        ({"inverter_modbus_unit_id": 200}, config_flow._AddressesNotUnique),
    ],
)
def test_a_setting_out_of_bounds_is_refused_before_the_inverter(change, error):
    settings = config_flow._expand_settings_input(USER_INPUT) | change

    with pytest.raises(error):
        config_flow._validate_static_input(settings)


async def test_an_entry_removed_while_following_keeps_no_token(hass, monkeypatch):
    """The token moved to the new host of a gone entry."""
    entry = make_entry(hass)
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="t")
    ready = store.async_ready

    async def ready_while_the_entry_goes():
        await hass.config_entries.async_remove(entry.entry_id)
        await ready()

    monkeypatch.setattr(store, "async_ready", ready_while_the_entry_goes)

    await discovery.async_follow_host(hass, entry, MOVED_HOST, SERIAL)

    assert await store.async_load_token(MOVED_HOST) is None


async def test_a_reauth_keeps_settings_changed_while_it_ran(hass, mock_modbus):
    """The finished login wrote back its older snapshot."""
    entry = make_entry(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_username": "customer"}
    )
    hass.config_entries.async_update_entry(entry, options={"scan_interval": 30})

    await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert config_flow.entry_defaults(entry)["scan_interval"] == 30


@pytest.mark.parametrize("model", ["Tauro 50-3-D", "Tauro ECO 100-3-P"])
async def test_a_tauro_is_no_untested_model(
    hass, mock_modbus, monkeypatch, caplog, model
):
    """Fronius' register maps give the Tauro the GEN24's SunSpec models."""
    identity = DeviceIdentity(
        manufacturer="Fronius",
        model=model,
        serial="1",
        version="1.0",
        options="",
        address=1.0,
    )
    monkeypatch.setattr(
        config_flow.FroniusInverter, "async_probe", AsyncMock(return_value=identity)
    )

    await config_flow._validate_input(
        hass, config_flow._expand_settings_input(NO_WEB_INPUT)
    )

    assert "Untested model" not in caplog.text


async def test_an_inverter_answering_at_its_address_is_not_moved(hass, inverters):
    """An announcement for its serial cannot move an entry that the inverter answers.

    The serial number goes out in every announcement: anyone in the network could
    otherwise turn the entry, and its login, to another address.
    """
    entry = make_entry(hass)
    with_inverter_device(hass, entry)
    inverters[HOST] = SERIAL

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == HOST


async def test_both_addresses_are_read_on_the_entry_s_port_and_unit(hass, monkeypatch):
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(
        entry, options={"port": 1502, "inverter_modbus_unit_id": 7}
    )
    with_inverter_device(hass, entry)
    reads = []

    async def serial_at(_hass, host, port, unit_id):
        reads.append((host, port, unit_id))
        return SERIAL if host == MOVED_HOST else None

    monkeypatch.setattr(discovery, "async_serial_at", serial_at)

    await discover(hass, discovered(host=MOVED_HOST))

    assert reads == [(HOST, 1502, 7), (MOVED_HOST, 1502, 7)]


async def test_an_announced_address_without_the_inverter_is_not_followed(
    hass, inverters
):
    """An announcement alone proves nothing: the new address has to answer as the inverter."""
    entry = make_entry(hass)
    with_inverter_device(hass, entry)
    inverters[MOVED_HOST] = "87654321"

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == HOST


async def test_an_inverter_whose_address_another_took_is_followed(hass, inverters):
    """Another inverter answering at the old address blocked the move."""
    entry = make_entry(hass)
    with_inverter_device(hass, entry)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(
        modbus=SimpleNamespace(last_update_success=True)
    )
    inverters[HOST] = "87654321"

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == MOVED_HOST


async def test_a_new_address_answering_late_is_followed(hass, monkeypatch):
    """One failed read at the new address lost the only announcement."""
    entry = make_entry(hass)
    with_inverter_device(hass, entry)
    answers = iter([None, SERIAL])

    async def serial_at(_hass, host, _port, _unit_id):
        return next(answers) if host == MOVED_HOST else None

    monkeypatch.setattr(discovery, "async_serial_at", serial_at)

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == MOVED_HOST


async def test_an_inverter_back_at_its_address_while_waiting_is_not_moved(
    hass, monkeypatch
):
    """Every attempt reads the old address again: the inverter may have come back."""
    entry = make_entry(hass)
    with_inverter_device(hass, entry)
    old_answers = iter([None, SERIAL])
    new_answers = iter([None, SERIAL])

    async def serial_at(_hass, host, _port, _unit_id):
        return next(old_answers) if host == HOST else next(new_answers)

    monkeypatch.setattr(discovery, "async_serial_at", serial_at)

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == HOST


async def test_a_new_address_that_never_answers_is_not_followed(hass, inverters):
    entry = make_entry(hass)
    with_inverter_device(hass, entry)
    del inverters[MOVED_HOST]

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == HOST


async def test_an_endpoint_changed_during_the_reads_is_not_moved(hass, monkeypatch):
    """The reads checked a port and unit the entry no longer had."""
    entry = make_entry(hass)
    with_inverter_device(hass, entry)

    async def serial_at(_hass, host, _port, _unit_id):
        if host != MOVED_HOST:
            return None
        hass.config_entries.async_update_entry(
            entry, options={"port": 1502, "inverter_modbus_unit_id": 7}
        )
        return SERIAL

    monkeypatch.setattr(discovery, "async_serial_at", serial_at)

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == HOST


async def test_an_announcement_before_the_failed_poll_is_followed(hass):
    """The last poll still succeeded, so the only announcement was lost."""
    entry = make_entry(hass)
    with_inverter_device(hass, entry)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(
        modbus=SimpleNamespace(last_update_success=True)
    )

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == MOVED_HOST


def _float_map(monkeypatch) -> list[tuple]:
    """An inverter serving the float map: switching it waits for consent."""
    applied: list[tuple] = []

    def ensure(self, *args):
        if not (len(args) > 4 and args[4]):
            raise froniuswebclient.SunSpecModeChangeNeeded("float")
        applied.append(args)
        return True

    monkeypatch.setattr(config_flow.FroniusWebClient, "ensure_modbus_enabled", ensure)
    return applied


async def test_a_float_map_is_switched_only_after_the_user_agrees(
    hass, mock_modbus, monkeypatch
):
    """Another device may read the float map (a Bosch energy manager, SolarEdge issue 345).

    The setup switched it to int + SF without a word; that device stopped.
    """
    applied = _float_map(monkeypatch)

    result = await run_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user_sunspec"
    assert result["description_placeholders"]["mode"] == "float"
    assert applied == []
    assert await async_get_token_store(hass).async_load_token(HOST, "customer") is None

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [args[4] for args in applied] == [True]
    stored = await async_get_token_store(hass).async_load_token(HOST, "customer")
    assert stored == {"realm": "r", "token": "t"}


async def test_leaving_the_model_type_question_changes_nothing(
    hass, mock_modbus, monkeypatch
):
    applied = _float_map(monkeypatch)
    result = await run_flow(hass)
    assert result["step_id"] == "user_sunspec"

    hass.config_entries.flow.async_abort(result["flow_id"])

    assert applied == []
    assert await async_get_token_store(hass).async_load_token(HOST, "customer") is None
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_stored_token_asks_before_switching_too(hass, mock_modbus, monkeypatch):
    """With a stored token the settings step validates at once, without a password step."""
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    applied = _float_map(monkeypatch)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "user_sunspec"
    assert applied == []

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [args[4] for args in applied] == [True]


async def test_reauthentication_asks_before_switching(hass, mock_modbus, monkeypatch):
    entry = make_entry(hass)
    applied = _float_map(monkeypatch)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_username": "customer"}
    )
    if result.get("step_id") == "reauth_password":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_password": "secret"}
        )
    assert result["step_id"] == "reauth_sunspec"
    assert applied == []

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["reason"] == "reauth_successful"
    assert [args[4] for args in applied] == [True]


async def test_the_options_ask_before_switching(hass, mock_modbus, monkeypatch):
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    applied = _float_map(monkeypatch)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "host": HOST,
            "scan_interval": 10,
            "web_scan_interval": 60,
            "modbus_restriction": "off",
            "api_username": "customer",
        },
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )
    assert result["step_id"] == "sunspec"
    assert applied == []

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [args[4] for args in applied] == [True]


async def test_a_host_taken_during_the_model_type_question_is_refused(
    hass, mock_modbus, monkeypatch
):
    """Like the password step: another entry may take the host while the question is open.

    The answer must not even log in to an inverter another entry serves.
    """
    contacted = _record_contact(monkeypatch)
    applied = _float_map(monkeypatch)
    result = await run_flow(hass)
    assert result["step_id"] == "user_sunspec"
    contacted_before = len(contacted)
    make_entry(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["reason"] == "already_configured"
    assert len(contacted) == contacted_before
    assert applied == []


def _host_taken_during_the_probe(hass, monkeypatch, host: str) -> None:
    """Another entry takes ``host`` while the flow probes the inverter: after every check."""
    original_probe = config_flow.FroniusInverter.async_probe

    async def probe(unit):
        other = make_entry(hass)
        hass.config_entries.async_update_entry(
            other, data={**other.data, "host": host}, unique_id=host
        )
        return await original_probe(unit)

    monkeypatch.setattr(config_flow.FroniusInverter, "async_probe", probe)


async def test_a_host_taken_after_the_answer_shows_its_error(
    hass, mock_modbus, monkeypatch
):
    """The finish found the duplicate; the form that showed it had lost its state."""
    source = make_entry(hass)
    target = "192.0.2.20"
    _float_map(monkeypatch)
    result = await hass.config_entries.options.async_init(source.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"host": target, "modbus_restriction": "off", "api_username": "customer"},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )
    assert result["step_id"] == "sunspec"
    _host_taken_during_the_probe(hass, monkeypatch, target)

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["step_id"] == "sunspec"
    assert result["errors"] == {"base": "already_configured"}
    assert config_flow.entry_defaults(source)["host"] == HOST


async def test_the_password_step_keeps_its_state_after_a_late_error(
    hass, mock_modbus, monkeypatch
):
    """Its form came back without the entry it asked for, and the next try restarted."""
    source = make_entry(hass)
    target = "192.0.2.20"
    result = await hass.config_entries.options.async_init(source.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"host": target, "modbus_restriction": "off", "api_username": "customer"},
    )
    assert result["step_id"] == "password"
    _host_taken_during_the_probe(hass, monkeypatch, target)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["step_id"] == "password"
    assert result["errors"] == {"base": "already_configured"}
    assert result["description_placeholders"]["host"] == target


def _entry_without_unique_id(hass) -> MockConfigEntry:
    """A 0.1.9 entry, disabled before it ever ran here: no unique id was claimed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": HOST, "name": "Fronius"},
        unique_id=None,
        disabled_by=ConfigEntryDisabler.USER,
        version=1,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    return entry


async def test_a_host_an_entry_without_unique_id_has_is_not_set_up_again(
    hass, mock_modbus
):
    """Only the unique id was checked: a second entry for the host came through."""
    _entry_without_unique_id(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_a_host_an_entry_without_unique_id_has_is_not_offered(hass):
    """A discovery card for it would set the host up a second time."""
    _entry_without_unique_id(hass)

    result = await discover(hass, discovered())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_no_entry_moves_to_a_host_an_entry_without_unique_id_has(
    hass, mock_modbus, monkeypatch
):
    """A reconfigure checked the unique id only and moved onto the host."""
    _entry_without_unique_id(hass)
    other = _other_entry(hass)
    _record_contact(monkeypatch)
    _record_modbus_setup(monkeypatch)
    moved = {
        "host": HOST,
        "scan_interval": 10,
        "web_scan_interval": 60,
        "modbus_restriction": "keep",
        "api_username": "customer",
    }

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": other.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], moved)
    if result.get("step_id", "").endswith("password"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"api_password": "secret"}
        )

    assert result["errors"]["base"] == "already_configured"


async def test_a_host_whose_discovery_was_ignored_can_be_set_up_by_hand(
    hass, mock_modbus
):
    """Home Assistant lets the owner set up an ignored host by hand; the host check refused it."""
    MockConfigEntry(domain=DOMAIN, source=SOURCE_IGNORE, unique_id=HOST).add_to_hass(
        hass
    )

    result = await run_flow(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_moved_inverter_is_not_followed_to_a_host_an_entry_without_unique_id_has(
    hass,
):
    """The move checked the unique id only and took the host of a disabled 0.1.9 entry."""
    entry = make_entry(hass)
    with_inverter_device(hass, entry)
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="t")
    MockConfigEntry(
        domain=DOMAIN,
        data={"host": MOVED_HOST, "name": "Fronius"},
        unique_id=None,
        disabled_by=ConfigEntryDisabler.USER,
    ).add_to_hass(hass)

    await discover(hass, discovered(host=MOVED_HOST))

    assert config_flow.entry_defaults(entry)["host"] == HOST
    assert entry.unique_id == HOST
    assert await store.async_load_token(MOVED_HOST) is None


# -- refusals and restarts ------------------------------------------------------------


async def _password_step(hass, restriction: str = "keep") -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "modbus_restriction": restriction}
    )
    assert result["step_id"] == "user_password"
    return result


async def test_the_address_to_allow_is_shown_and_can_be_changed(
    hass, mock_modbus, monkeypatch
):
    """Behind NAT the inverter sees another address than Home Assistant's own,
    and allowing the wrong one locked every Modbus control out."""
    written: list[tuple] = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "ensure_modbus_enabled",
        lambda self, *args: written.append(args),
    )
    result = await _password_step(hass, restriction="home_assistant")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["step_id"] == "user_allowed_host"
    assert result["data_schema"]({}) == {"allowed_host": RESOLVED_IP}
    assert written == []

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"allowed_host": "192.0.2.77"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [(args[3], args[5]) for args in written] == [
        ("home_assistant", "192.0.2.77")
    ]


async def test_an_address_to_allow_that_is_no_address_is_refused(
    hass, mock_modbus, monkeypatch
):
    written: list[tuple] = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "ensure_modbus_enabled",
        lambda self, *args: written.append(args),
    )
    result = await _password_step(hass, restriction="home_assistant")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"allowed_host": "fronius.local"}
    )

    assert result["step_id"] == "user_allowed_host"
    assert result["errors"] == {"allowed_host": "invalid_allowed_host"}
    assert written == []


async def test_an_address_the_inverter_already_allows_is_not_asked_again(
    hass, mock_modbus, monkeypatch
):
    """Reconfigure and reauthentication asked every time, although the list would not change."""
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "modbus_restriction": "home_assistant"}
    )
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    written: list[tuple] = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "ensure_modbus_enabled",
        lambda self, *args: written.append(args),
    )
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "restriction_allows",
        lambda self, address: address == RESOLVED_IP,
    )
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_username": "customer"}
    )

    assert result["reason"] == "reauth_successful"
    assert [args[5] for args in written] == [RESOLVED_IP]


def _restricted_entry_the_list_allows(hass, monkeypatch) -> tuple:
    """An entry whose inverter already allows Home Assistant's own address.

    Behind NAT that is the address an earlier version wrote without asking.
    """
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "modbus_restriction": "home_assistant"}
    )
    written: list[tuple] = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "ensure_modbus_enabled",
        lambda self, *args: written.append(args),
    )
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "restriction_allows",
        lambda self, address: address == RESOLVED_IP,
    )
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )
    return entry, written


async def test_an_address_allowed_before_can_be_replaced_in_reconfigure(
    hass, mock_modbus, monkeypatch
):
    """Behind NAT an earlier version allowed the wrong address, and since the
    question is skipped for an allowed one, no flow could replace it."""
    entry, written = _restricted_entry_the_list_allows(hass, monkeypatch)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **USER_INPUT,
            "modbus_restriction": "home_assistant",
            "change_allowed_host": True,
        },
    )

    assert result["step_id"] == "reconfigure_allowed_host"
    assert result["data_schema"]({}) == {"allowed_host": RESOLVED_IP}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"allowed_host": "192.0.2.77"}
    )

    assert result["reason"] == "reconfigure_successful"
    assert [args[5] for args in written] == ["192.0.2.77"]
    assert "change_allowed_host" not in {**entry.data, **entry.options}


async def test_an_address_allowed_before_can_be_replaced_in_configure(
    hass, mock_modbus, monkeypatch
):
    """Configure writes the Modbus settings only when they change: the request
    alone has to bring the question."""
    entry, written = _restricted_entry_the_list_allows(hass, monkeypatch)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **USER_INPUT,
            "modbus_restriction": "home_assistant",
            "change_allowed_host": True,
        },
    )
    assert result["step_id"] == "password"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_password": ""}
    )

    assert result["step_id"] == "allowed_host"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"allowed_host": "192.0.2.77"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [args[5] for args in written] == ["192.0.2.77"]
    assert "change_allowed_host" not in result["data"]


async def test_an_allowed_address_is_not_asked_for_unless_requested(
    hass, mock_modbus, monkeypatch
):
    """Existing installs keep finishing Reconfigure without the extra question."""
    entry, written = _restricted_entry_the_list_allows(hass, monkeypatch)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "modbus_restriction": "home_assistant"}
    )

    assert result["reason"] == "reconfigure_successful"
    assert [args[5] for args in written] == [RESOLVED_IP]


async def test_an_address_request_without_the_restriction_asks_nothing(
    hass, mock_modbus, monkeypatch
):
    """Kept or lifted, the restriction allows no address of Home Assistant's,
    and Configure leaves unchanged Modbus settings alone."""
    entry, written = _restricted_entry_the_list_allows(hass, monkeypatch)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "modbus_restriction": "keep"}
    )
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**USER_INPUT, "modbus_restriction": "keep", "change_allowed_host": True},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_password": ""}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert written == []


async def test_the_model_type_answer_writes_the_address_the_list_allowed(
    hass, mock_modbus, monkeypatch
):
    """The skipped question kept no address, so the answer checked again; a
    list changed meanwhile ended the flow in an unknown error that logged
    Home Assistant's address."""
    applied = _float_map(monkeypatch)
    checks: list[str] = []

    def allowed_once(self, address):
        checks.append(address)
        return len(checks) == 1

    monkeypatch.setattr(
        config_flow.FroniusWebClient, "restriction_allows", allowed_once
    )
    result = await _password_step(hass, restriction="home_assistant")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )
    assert result["step_id"] == "user_sunspec"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [(args[4], args[5]) for args in applied] == [(True, RESOLVED_IP)]


def test_the_address_question_carries_no_address_in_its_message():
    """An unexpected path logs the exception, and with it the message."""
    needed = config_flow._AllowedHostNeeded(RESOLVED_IP)

    assert RESOLVED_IP not in str(needed)
    assert needed.resolved_ip == RESOLVED_IP


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "0.0.0.0", "255.255.255.255", "224.0.0.1"]
)
async def test_an_address_no_client_can_have_is_refused(
    hass, mock_modbus, monkeypatch, address
):
    """Allowed alone, such an address locked every device out of Modbus control."""
    written: list[tuple] = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "ensure_modbus_enabled",
        lambda self, *args: written.append(args),
    )
    result = await _password_step(hass, restriction="home_assistant")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"allowed_host": address}
    )

    assert result["errors"] == {"allowed_host": "invalid_allowed_host"}
    assert written == []


async def test_a_corrected_address_stays_when_the_step_is_shown_again(
    hass, mock_modbus, monkeypatch
):
    """After an error the step brought back the resolved address, and submitting
    it again wrote the address the inverter does not see."""
    attempts: list[tuple] = []

    def unreachable_once(self, *args):
        attempts.append(args)
        if len(attempts) == 1:
            raise FroniusWebUnreachable("ConnectTimeout")
        return True

    monkeypatch.setattr(
        config_flow.FroniusWebClient, "ensure_modbus_enabled", unreachable_once
    )
    result = await _password_step(hass, restriction="home_assistant")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"allowed_host": "192.0.2.77"}
    )

    assert result["step_id"] == "user_allowed_host"
    assert result["errors"] == {"base": "cannot_connect"}
    assert result["data_schema"]({}) == {"allowed_host": "192.0.2.77"}


async def test_an_address_that_cannot_be_resolved_is_asked_for(
    hass, mock_modbus, monkeypatch
):
    """The flow stopped at the settings form, though the owner could type it."""
    written: list[tuple] = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "ensure_modbus_enabled",
        lambda self, *args: written.append(args),
    )

    def unresolvable(self):
        raise ClientIpResolutionError("no local address")

    monkeypatch.setattr(config_flow.FroniusWebClient, "resolve_client_ip", unresolvable)
    result = await _password_step(hass, restriction="home_assistant")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["step_id"] == "user_allowed_host"
    assert result["data_schema"]({}) == {"allowed_host": ""}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"allowed_host": "192.0.2.77"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [args[5] for args in written] == ["192.0.2.77"]


async def test_the_address_and_the_model_type_are_both_asked_before_writing(
    hass, mock_modbus, monkeypatch
):
    """The confirmed address has to reach the write the model type answer starts."""
    applied = _float_map(monkeypatch)
    result = await _password_step(hass, restriction="home_assistant")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )
    assert result["step_id"] == "user_allowed_host"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"allowed_host": "192.0.2.77"}
    )
    assert result["step_id"] == "user_sunspec"
    assert applied == []

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [(args[4], args[5]) for args in applied] == [(True, "192.0.2.77")]


async def test_a_token_request_that_fails_shows_cannot_connect(
    hass, mock_modbus, monkeypatch
):
    def fail(host, user, password):
        raise OSError("no answer")

    monkeypatch.setattr(config_flow, "mint_token", fail)
    result = await _password_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_a_refused_password_asks_for_it_again(hass, mock_modbus, monkeypatch):
    monkeypatch.setattr(config_flow, "mint_token", lambda host, user, password: None)
    result = await _password_step(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "wrong"}
    )

    assert result["step_id"] == "user_password"
    assert result["errors"] == {"base": "invalid_api_credentials"}


async def test_a_stored_token_the_inverter_refuses_asks_for_the_password(
    hass, mock_modbus, monkeypatch
):
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="old")
    monkeypatch.setattr(config_flow.FroniusWebClient, "login", lambda self: False)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["step_id"] == "user_password"


async def test_a_host_taken_while_a_stored_token_is_checked_aborts(
    hass, mock_modbus, monkeypatch
):
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    validate = config_flow._validate_input

    async def validate_then_take(hass, *args, **kwargs):
        info = await validate(hass, *args, **kwargs)
        MockConfigEntry(domain=DOMAIN, data={"host": HOST}, unique_id=HOST).add_to_hass(
            hass
        )
        return info

    monkeypatch.setattr(config_flow, "_validate_input", validate_then_take)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_an_entry_set_to_leave_modbus_alone_is_not_set_up_on_reauth(
    hass, mock_modbus, monkeypatch
):
    entry = make_entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "auto_enable_modbus": False}
    )
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    calls = []
    monkeypatch.setattr(
        config_flow.FroniusWebClient,
        "ensure_modbus_enabled",
        lambda self, *args: calls.append(args),
    )
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_username": "customer"}
    )

    assert result["reason"] == "reauth_successful"
    assert calls == []


def _probed(monkeypatch, *, manufacturer: str, model: str) -> None:
    identity = DeviceIdentity(
        manufacturer=manufacturer,
        model=model,
        serial="1",
        version="1.0",
        options="",
        address=1.0,
    )
    monkeypatch.setattr(
        config_flow.FroniusInverter, "async_probe", AsyncMock(return_value=identity)
    )


async def test_an_inverter_of_another_maker_is_refused(hass, mock_modbus, monkeypatch):
    _probed(monkeypatch, manufacturer="Other", model="Primo GEN24")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], NO_WEB_INPUT
    )

    assert result["errors"] == {"base": "unsupported_hardware"}


async def test_an_unknown_fronius_model_is_set_up_with_a_warning(
    hass, mock_modbus, monkeypatch, caplog
):
    _probed(monkeypatch, manufacturer="Fronius", model="Unknown 8.0")

    await config_flow._validate_input(
        hass, config_flow._expand_settings_input(NO_WEB_INPUT)
    )

    assert "Untested model Unknown 8.0" in caplog.text


@pytest.mark.parametrize(
    "step", ["async_step_user_password", "async_step_user_sunspec"]
)
async def test_a_follow_up_step_without_its_settings_starts_over(hass, step):
    """Nothing to log in with: the step shows the settings form again."""
    flow = config_flow.ConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}

    result = await getattr(flow, step)({"api_password": "secret"})

    assert result["step_id"] == "user"


async def test_reconfiguring_onto_a_float_map_asks_before_switching(
    hass, mock_modbus, monkeypatch
):
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    applied = _float_map(monkeypatch)
    monkeypatch.setattr(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "host": HOST,
            "scan_interval": 10,
            "web_scan_interval": 60,
            "modbus_restriction": "keep",
            "api_username": "customer",
        },
    )
    assert result["step_id"] == "reconfigure_sunspec"
    assert applied == []

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["reason"] == "reconfigure_successful"
    assert [args[4] for args in applied] == [True]


async def test_a_reauth_whose_entry_went_aborts_at_the_password(
    hass, mock_modbus, monkeypatch
):
    """Removing the entry aborts its flows, but a step already running sees it gone."""
    entry = make_entry(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_username": "technician"}
    )
    assert result["step_id"] == "reauth_password"
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda entry_id: None)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


def test_an_unreadable_update_interval_shows_the_default(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={**USER_INPUT, "scan_interval": "often"}, unique_id=HOST
    )

    assert config_flow.entry_defaults(entry)["scan_interval"] == DEFAULT_SCAN_INTERVAL


# -- a Datamanager -------------------------------------------------------------------


def answer_family(monkeypatch, family: DeviceFamily) -> list[str]:
    """Let the device answer with ``family``; returns the hosts asked."""
    asked: list[str] = []

    def _identify(host: str) -> DeviceFamily:
        asked.append(host)
        return family

    monkeypatch.setattr(datamanager, "identify", _identify)
    return asked


def record_the_login(monkeypatch) -> list[str]:
    """Every login, minted token and Modbus settings write a flow makes."""
    contacts: list[str] = []
    for name in ("login", "ensure_modbus_enabled"):
        monkeypatch.setattr(
            config_flow.FroniusWebClient,
            name,
            lambda self, *_args, name=name: contacts.append(name),
        )
    monkeypatch.setattr(
        config_flow, "mint_token", lambda *_args: contacts.append("mint_token")
    )
    return contacts


class _SettingsFlow:
    """One of the four flows that show the role, up to its settings form."""

    def __init__(self, hass, kind: str) -> None:
        self.kind = kind
        self.hass = hass
        self.manager = hass.config_entries.flow
        if kind == "options":
            self.manager = hass.config_entries.options

    async def start(self) -> dict:
        if self.kind == "user":
            return await self.manager.async_init(DOMAIN, context={"source": "user"})
        entry = make_entry(self.hass)
        if self.kind == "reconfigure":
            return await entry.start_reconfigure_flow(self.hass)
        if self.kind == "reauth":
            return await entry.start_reauth_flow(self.hass)
        return await self.manager.async_init(entry.entry_id)

    async def submit(self, flow_id: str, role: str) -> dict:
        typed = {"api_username": role}
        if self.kind != "reauth":
            typed |= {"host": HOST, "scan_interval": 25}
        return await self.manager.async_configure(flow_id, typed)


def shown_defaults(result: dict) -> dict:
    """What a form is filled in with."""
    return {
        str(marker): marker.default()
        for marker in result["data_schema"].schema
        if callable(marker.default)
    }


@pytest.mark.parametrize("role", ["customer", "technician"])
@pytest.mark.parametrize("kind", ["user", "reconfigure", "options", "reauth"])
async def test_a_role_on_a_datamanager_asks_to_go_without_the_web_api(
    hass, mock_modbus, monkeypatch, kind, role
):
    """Its login failed with any password, and the form did not say why."""
    asked = answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    monkeypatch.setattr(datamanager, "read_inverter_units", lambda host: (1,))
    contacts = record_the_login(monkeypatch)
    flow = _SettingsFlow(hass, kind)
    form = await flow.start()

    result = await flow.submit(form["flow_id"], role)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == form["step_id"]
    assert result["errors"] == {"api_username": "datamanager_no_login"}
    defaults = shown_defaults(result)
    assert defaults["api_username"] == "none"
    if kind != "reauth":
        assert (defaults["host"], defaults["scan_interval"]) == (HOST, 25)
    assert asked == [HOST]

    result = await flow.submit(form["flow_id"], "none")

    assert result["type"] in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT)
    assert contacts == []
    assert await async_get_token_store(hass).async_load_token(HOST, role) is None


@pytest.mark.parametrize("family", [DeviceFamily.OTHER, DeviceFamily.UNKNOWN])
async def test_any_other_answer_goes_on_to_the_password(
    hass, mock_modbus, monkeypatch, family
):
    answer_family(monkeypatch, family)

    result = await run_flow(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("family", [DeviceFamily.OTHER, DeviceFamily.UNKNOWN])
@pytest.mark.parametrize("kind", ["user", "reconfigure", "options", "reauth"])
async def test_only_a_datamanager_refuses_a_role(
    hass, mock_modbus, monkeypatch, kind, family
):
    answer_family(monkeypatch, family)
    flow = _SettingsFlow(hass, kind)
    form = await flow.start()

    result = await flow.submit(form["flow_id"], "technician")

    assert result["step_id"].endswith("password")


async def test_another_error_keeps_the_role_offered(hass, mock_modbus):
    """Only a role refused on a Datamanager switches the choice."""
    flow = _SettingsFlow(hass, "user")
    form = await flow.start()

    result = await flow.manager.async_configure(
        form["flow_id"], {"host": "", "scan_interval": 25, "api_username": "customer"}
    )

    assert result["errors"] == {"host": "invalid_host"}
    assert shown_defaults(result)["api_username"] == "customer"


@pytest.mark.parametrize(
    ("kind", "times"), [("user", 1), ("reconfigure", 0), ("options", 0), ("reauth", 0)]
)
async def test_without_the_web_api_only_a_new_entry_asks_for_the_family(
    hass, mock_modbus, monkeypatch, kind, times
):
    """A new one may have to ask which inverter it reads."""
    asked = answer_family(monkeypatch, DeviceFamily.OTHER)
    flow = _SettingsFlow(hass, kind)
    form = await flow.start()

    result = await flow.submit(form["flow_id"], "none")

    assert result["type"] in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT)
    assert len(asked) == times


async def test_a_taken_host_is_refused_before_the_device_is_asked(
    hass, mock_modbus, monkeypatch
):
    asked = answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    make_entry(hass)
    flow = _SettingsFlow(hass, "user")
    form = await flow.start()

    result = await flow.submit(form["flow_id"], "customer")

    assert result["type"] is FlowResultType.ABORT
    assert asked == []
