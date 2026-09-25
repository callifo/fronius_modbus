"""The config flow, with the web client stubbed and Modbus served by the mock connection."""

import asyncio
from ipaddress import ip_address
import json
from unittest.mock import AsyncMock

from modbus_connection import ModbusConnectionError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import fronius_modbus
from custom_components.fronius_modbus import config_flow, discovery
from custom_components.fronius_modbus.const import DOMAIN, instance_key
from custom_components.fronius_modbus.froniuswebclient import FroniusWebResponseError
from custom_components.fronius_modbus.token_store import async_get_token_store
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .conftest import INVERTER_UNIT_ID

HOST = "192.0.2.10"
USER_INPUT = {"host": HOST, "scan_interval": 10}


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
    """Own reaudit: a 500 while enabling Modbus stopped being an OSError and crashed the flow."""

    def refuse(self, *_args):
        raise FroniusWebResponseError("HTTP 500 on /api/config/modbus", 500)

    monkeypatch.setattr(config_flow.FroniusWebClient, "ensure_modbus_enabled", refuse)

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


async def test_a_token_whose_setup_fails_is_not_kept(hass, mock_modbus):
    """Audit RA24-01: the minted token was saved before the inverter was checked.

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
    """Reaudit RE26-04: the aborted flow had already replaced the entry's token.

    The entry appears while the inverter is checked: a host taken before is
    refused ahead of the login (audit FA0FB-02, RR770-01).
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
    """Own reaudit R26-02: the entry already used the new role when its token went."""
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
    """Audit D8AE-02: the stale target-role token came back over the fresh one."""
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
    """Audit FA0FB-02: the duplicate was found only after Modbus was set up on it.

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
    """Audit FA0FB-02: reconfigure and options moved Modbus settings before refusing."""
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
    """Audit D8AE-02, reconfigure: the entry used the fresh token when the stale one came back."""
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
    """Audit RR770-01: the password step validated, and set up Modbus, without a recheck."""
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
    """Audit RR770-01: reconfigure and options rechecked the host only after validation."""
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
    """Audit RR770-02: a competing entry on the same role kept the aborted flow's token."""

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
    """Audit R730-01: the claim was not held through minting and validation."""
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
    """Audit R6D-01: the restriction chosen without the web API was never written."""
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

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [args[-1] for args in written] == ["home_assistant"]


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


async def test_an_inverter_set_up_by_name_is_not_offered_again(hass):
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
    """Reaudit Z-02: following the address silently replaced a renamed entry's title."""
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


async def test_an_ipv6_discovery_does_not_replace_an_ipv4_host(hass):
    entry = make_entry(hass)
    with_inverter_device(hass, entry)

    await discover(hass, discovered(host="2001:db8::10"))

    assert config_flow.entry_defaults(entry)["host"] == HOST


async def test_a_manual_setup_is_not_blocked_by_an_open_card(hass, mock_modbus):
    """Reaudit Z-01: the card held the host, and adding it by hand aborted as in progress."""
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
    """Audit R3B-01: another flow took the address during the token save; both held it."""
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

    await discovery.async_follow_host(hass, entry, MOVED_HOST)

    holders = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.unique_id == MOVED_HOST
    ]
    assert len(holders) == 1


async def test_two_announcements_leave_the_token_under_one_address(hass, monkeypatch):
    """Audit R3B-02: overlapping follows left a password-equivalent token at an unused host."""
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
        discovery.async_follow_host(hass, entry, MOVED_HOST),
        discovery.async_follow_host(hass, entry, third),
    )

    stored = [
        host for host in (HOST, MOVED_HOST, third) if await store.async_load_token(host)
    ]
    assert stored == [config_flow.entry_defaults(entry)["host"]]
