"""The technician role on a Datamanager, through the four flows that ask for a role."""

from typing import Any

from modbus_connection import GatewayTargetError
import pytest
import requests

from custom_components import fronius_modbus
from custom_components.fronius_modbus import config_flow, datamanager
from custom_components.fronius_modbus.const import (
    CONF_DATAMANAGER,
    CONF_INVERTER_UNIT_ID,
    CONF_SITE_OWNER,
    entry_title,
    entry_unique_id,
)
from custom_components.fronius_modbus.datamanager import (
    DeviceFamily,
    async_on_datamanager,
)
from custom_components.fronius_modbus.datamanager_client import EXPORT_LIMIT_PATH
from custom_components.fronius_modbus.token_store import async_get_token_store
from homeassistant.data_entry_flow import FlowResultType

from .conftest import INVERTER_UNIT_ID
from .fake_datamanager import HOST, PASSWORD, minted, serve_datamanager
from .test_config_flow import answer_family
from .test_several_inverters import add_entry, choose, inverter

KINDS = ["user", "reconfigure", "options", "reauth"]


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


@pytest.fixture
def device(hass, datamanager_modbus, monkeypatch):
    """A Datamanager with its web interface, as far as a flow asks it."""
    answer_family(monkeypatch, DeviceFamily.DATAMANAGER)
    monkeypatch.setattr(datamanager, "read_inverter_units", lambda host: (1,))

    async def _skip_setup(hass, entry):
        return True

    monkeypatch.setattr(fronius_modbus, "async_setup_entry", _skip_setup)
    return serve_datamanager(monkeypatch)


class RoleFlow:
    """One of the four flows; the existing ones on an entry without the web API."""

    def __init__(self, hass, kind: str, **entry_data: Any) -> None:
        self.kind = kind
        self.manager = hass.config_entries.flow
        if kind == "options":
            self.manager = hass.config_entries.options
        self.entry = None
        if kind != "user":
            data = {**inverter(1), "name": "Fronius", **entry_data}
            self.entry = add_entry(
                hass, data, entry_unique_id(data), title=entry_title(data)
            )
        self.hass = hass

    async def submit(self, role: str) -> dict:
        """Open the flow and send its first form with this role."""
        if self.kind == "user":
            form = await self.manager.async_init(
                "fronius_modbus", context={"source": "user"}
            )
        elif self.kind == "reconfigure":
            form = await self.entry.start_reconfigure_flow(self.hass)
        elif self.kind == "reauth":
            form = await self.entry.start_reauth_flow(self.hass)
        else:
            form = await self.manager.async_init(self.entry.entry_id)
        typed: dict[str, Any] = {"api_username": role}
        if self.kind != "reauth":
            typed |= {"host": HOST, "scan_interval": 10}
        return await self.manager.async_configure(form["flow_id"], typed)

    async def enter(self, form: dict, password: str) -> dict:
        result = await self.manager.async_configure(
            form["flow_id"], {"api_password": password}
        )
        if self.kind == "user" and result["type"] is FlowResultType.CREATE_ENTRY:
            self.entry = result["result"]
        return result


PASSWORD_STEPS = {
    "user": "user_password_service",
    "reconfigure": "reconfigure_password_service",
    "options": "password_service",
    "reauth": "reauth_password_service",
}


async def stored(hass, role: str = "technician") -> dict[str, str] | None:
    return await async_get_token_store(hass).async_load_token(HOST, role)


@pytest.mark.parametrize("kind", KINDS)
async def test_the_technician_logs_in_to_a_datamanager(hass, device, kind):
    flow = RoleFlow(hass, kind)

    form = await flow.submit("technician")
    assert form["step_id"] == PASSWORD_STEPS[kind]
    assert device.calls == []
    result = await flow.enter(form, PASSWORD)

    assert result["type"] in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT)
    assert await stored(hass) == minted()
    settings = {**flow.entry.data, **flow.entry.options}
    assert settings["api_username"] == "technician"
    assert flow.entry.data[CONF_DATAMANAGER] is True
    assert CONF_DATAMANAGER not in flow.entry.options
    # Its page alone is read; no Modbus setting of a GEN24 is asked or written.
    assert set(device.calls) == {("GET", EXPORT_LIMIT_PATH)}


@pytest.mark.parametrize("kind", KINDS)
async def test_the_password_step_names_the_service_login(hass, device, kind):
    """The role is called technician; the Datamanager's login is called service."""
    flow = RoleFlow(hass, kind)

    form = await flow.submit("technician")

    assert form["step_id"] == PASSWORD_STEPS[kind]
    result = await flow.enter(form, "wrong")
    assert result["step_id"] == PASSWORD_STEPS[kind]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_refused_password_stays_on_its_step(hass, device, kind):
    flow = RoleFlow(hass, kind)
    form = await flow.submit("technician")

    result = await flow.enter(form, "wrong")

    assert result["step_id"] == form["step_id"]
    assert result["errors"] == {"base": "invalid_api_credentials"}
    assert await stored(hass) is None
    assert len(device.logins) == 1
    result = await flow.enter(form, PASSWORD)
    assert result["type"] in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT)


@pytest.mark.parametrize("kind", KINDS)
async def test_the_customer_login_is_refused_with_its_reason(hass, device, kind):
    flow = RoleFlow(hass, kind)

    result = await flow.submit("customer")

    assert result["errors"] == {"api_username": "datamanager_customer"}
    assert device.calls == []


@pytest.mark.parametrize("kind", KINDS)
async def test_a_challenge_of_another_kind_is_no_wrong_password(hass, device, kind):
    device.challenge = 'Digest realm="r", nonce="{nonce}", qop="auth", algorithm=X'
    flow = RoleFlow(hass, kind)
    form = await flow.submit("technician")

    result = await flow.enter(form, PASSWORD)

    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize("kind", KINDS)
async def test_a_datamanager_without_the_export_limit_page_says_so(hass, device, kind):
    """Its firmware may have no such page; the password can be right."""
    device.status = 404
    flow = RoleFlow(hass, kind)
    form = await flow.submit("technician")

    result = await flow.enter(form, PASSWORD)

    assert result["errors"] == {"base": "datamanager_page_missing"}


async def store_a_login_where_the_page_is_missing(hass, device) -> None:
    device.status = 404
    await async_get_token_store(hass).async_save_token(
        HOST, user="technician", **minted()
    )


async def test_a_stored_login_kept_without_the_page_says_so(hass, device):
    await store_a_login_where_the_page_is_missing(hass, device)
    flow = RoleFlow(hass, "options", api_username="technician")
    form = await flow.submit("technician")

    result = await flow.enter(form, "")

    assert result["errors"] == {"base": "datamanager_page_missing"}


async def test_a_stored_login_tried_without_the_page_says_so(hass, device):
    await store_a_login_where_the_page_is_missing(hass, device)
    flow = RoleFlow(hass, "reconfigure", api_username="technician")

    result = await flow.submit("technician")

    assert result["errors"] == {"base": "datamanager_page_missing"}


async def test_a_device_gone_during_the_login_is_not_reached(hass, device, monkeypatch):
    flow = RoleFlow(hass, "user")
    form = await flow.submit("technician")

    def refuse(*_args, **_options):
        raise requests.ConnectionError

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)
    result = await flow.enter(form, PASSWORD)

    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize("kind", ["reconfigure", "options", "reauth"])
async def test_a_silent_datamanager_is_remembered_for_the_login(
    hass, device, monkeypatch, kind
):
    """Its Solar API may be off while its web interface answers."""
    answer_family(monkeypatch, DeviceFamily.UNKNOWN)
    flow = RoleFlow(hass, kind)

    form = await flow.submit("technician")
    await flow.enter(form, PASSWORD)

    assert flow.entry.data[CONF_DATAMANAGER] is True
    assert await stored(hass) == minted()


@pytest.mark.parametrize("kind", ["reconfigure", "options", "reauth"])
async def test_a_login_tells_a_datamanager_not_known_before(
    hass, device, monkeypatch, kind
):
    """An entry with a role is not asked at its start."""
    flow = RoleFlow(hass, kind, **{CONF_DATAMANAGER: False, "api_username": "customer"})

    form = await flow.submit("technician")
    await flow.enter(form, PASSWORD)

    assert flow.entry.data[CONF_DATAMANAGER] is True
    asked = answer_family(monkeypatch, DeviceFamily.OTHER)
    assert await async_on_datamanager(hass, flow.entry, HOST) is True
    assert asked == []


@pytest.mark.parametrize(("known", "after"), [(True, f"{HOST}#2"), (None, HOST)])
async def test_an_entry_with_a_login_gets_its_number_in_the_id(hass, known, after):
    """Chosen right after the update from 0.1.9, its device was not known before."""
    data = inverter(2, datamanager=known) | {"api_username": "technician"}
    entry = add_entry(hass, data, HOST)

    assert await async_on_datamanager(hass, entry, HOST) is bool(known)
    assert entry.unique_id == after


@pytest.mark.parametrize("kind", ["reconfigure", "options", "reauth"])
@pytest.mark.parametrize("remembered", [True, None])
async def test_a_gen24_in_its_place_is_logged_in_as_one(
    hass, device, monkeypatch, kind, remembered
):
    answer_family(monkeypatch, DeviceFamily.OTHER)
    logins: list[str] = []

    def _mint(host, user, password):
        logins.append(user)
        return {"realm": "r", "token": "t"}

    monkeypatch.setattr(
        "custom_components.fronius_modbus.config_flow.mint_token", _mint
    )
    monkeypatch.setattr(
        "custom_components.fronius_modbus.config_flow.FroniusWebClient.login",
        lambda self: True,
    )
    monkeypatch.setattr(
        "custom_components.fronius_modbus.config_flow.FroniusWebClient"
        ".ensure_modbus_enabled",
        lambda self, *args: True,
    )
    flow = RoleFlow(hass, kind)
    if remembered is None:
        data = dict(flow.entry.data)
        del data[CONF_DATAMANAGER]
        hass.config_entries.async_update_entry(flow.entry, data=data)

    form = await flow.submit("technician")
    await flow.enter(form, PASSWORD)

    assert logins == ["technician"]
    # A missing key means no; an entry that never had one gets none.
    expected = {CONF_DATAMANAGER: False} if remembered else {}
    assert {
        key: value for key, value in flow.entry.data.items() if key == CONF_DATAMANAGER
    } == expected


async def test_configure_keeps_the_stored_login_with_an_empty_password(hass, device):
    await async_get_token_store(hass).async_save_token(
        HOST, user="technician", **minted()
    )
    flow = RoleFlow(hass, "options", api_username="technician")

    form = await flow.submit("technician")
    result = await flow.enter(form, "")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert await stored(hass) == minted()


async def test_a_stored_login_the_device_refuses_asks_for_the_password(hass, device):
    await async_get_token_store(hass).async_save_token(
        HOST, user="technician", realm=minted()["realm"], token="0" * 32
    )
    flow = RoleFlow(hass, "reconfigure", api_username="technician")

    form = await flow.submit("technician")

    assert form["step_id"] == "reconfigure_password_service"


# -- with several inverters -----------------------------------------------------------


@pytest.fixture
def two_inverters(device, datamanager_modbus, monkeypatch):
    datamanager_modbus.add_unit(2, like=INVERTER_UNIT_ID)
    datamanager_modbus.fail_requests(9, GatewayTargetError())
    monkeypatch.setattr(datamanager, "read_inverter_units", lambda host: (1, 2))
    return device


async def test_the_inverter_is_asked_before_the_password(hass, two_inverters):
    flow = RoleFlow(hass, "user")

    form = await flow.submit("technician")
    assert form["step_id"] == "inverter"
    form = await choose(hass, form, "2")
    assert form["step_id"] == "user_password_service"
    assert two_inverters.calls == []
    result = await flow.enter(form, PASSWORD)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INVERTER_UNIT_ID] == 2
    assert result["result"].unique_id == f"{HOST}#2"
    assert await stored(hass) == minted()


async def test_an_inverter_that_does_not_answer_asks_for_no_password(
    hass, two_inverters
):
    flow = RoleFlow(hass, "user")
    form = await flow.submit("technician")

    result = await choose(hass, form, "9")

    assert result["errors"] == {CONF_INVERTER_UNIT_ID: "inverter_not_found"}


async def test_a_second_inverter_cannot_take_the_technician_role(hass, two_inverters):
    """The first one reads the meters and the export limit; a login here shows nothing."""
    await async_get_token_store(hass).async_save_token(
        HOST, user="technician", **minted()
    )
    add_entry(hass, inverter(1) | {"api_username": "technician"}, HOST)
    flow = RoleFlow(hass, "user")

    form = await flow.submit("technician")
    result = await choose(hass, form, "2")

    assert result["errors"] == {"base": "datamanager_login_on_owner"}
    assert two_inverters.calls == []


async def test_an_owner_set_up_while_the_password_is_asked_keeps_the_role(
    hass, two_inverters
):
    """Another flow set up the first inverter meanwhile; it reads the site now."""
    flow = RoleFlow(hass, "user")
    form = await flow.submit("technician")
    form = await choose(hass, form, "2")
    assert form["step_id"] == "user_password_service"
    add_entry(hass, inverter(1), HOST)

    result = await flow.enter(form, PASSWORD)

    assert result["errors"] == {"base": "datamanager_login_on_owner"}
    assert two_inverters.calls == []
    assert await stored(hass) is None


async def test_an_owner_set_up_while_the_login_is_checked_keeps_the_role(
    hass, two_inverters, monkeypatch
):
    """The check before the login passed; the owner came while it ran."""
    flow = RoleFlow(hass, "user")
    form = await flow.submit("technician")
    form = await choose(hass, form, "2")
    log_in = config_flow.async_datamanager_login

    async def set_up_the_owner_meanwhile(*args, **options):
        if not hass.config_entries.async_entries("fronius_modbus"):
            add_entry(hass, inverter(1), HOST)
        return await log_in(*args, **options)

    monkeypatch.setattr(
        config_flow, "async_datamanager_login", set_up_the_owner_meanwhile
    )

    result = await flow.enter(form, PASSWORD)

    assert result["errors"] == {"base": "datamanager_login_on_owner"}
    assert await stored(hass) is None
    assert len(hass.config_entries.async_entries("fronius_modbus")) == 1


async def test_a_lower_inverter_cannot_take_the_role_from_the_owner(
    hass, two_inverters
):
    add_entry(hass, inverter(2) | {CONF_SITE_OWNER: True}, f"{HOST}#2")
    flow = RoleFlow(hass, "user")

    form = await flow.submit("technician")
    result = await choose(hass, form, "1")

    assert result["errors"] == {"base": "datamanager_login_on_owner"}


@pytest.mark.parametrize("kind", ["reconfigure", "options", "reauth"])
async def test_an_entry_that_is_not_the_owner_cannot_take_the_role(
    hass, two_inverters, kind
):
    add_entry(hass, inverter(1), HOST)
    flow = RoleFlow(hass, kind, inverter_modbus_unit_id=2)

    result = await flow.submit("technician")

    assert result["errors"] == {"base": "datamanager_login_on_owner"}
    assert two_inverters.calls == []


@pytest.mark.parametrize("kind", ["reconfigure", "options", "reauth"])
async def test_the_owner_takes_the_role_beside_another_inverter(
    hass, two_inverters, kind
):
    """It has the meters, although a lower inverter came later."""
    add_entry(hass, inverter(1), HOST)
    flow = RoleFlow(hass, kind, inverter_modbus_unit_id=2, **{CONF_SITE_OWNER: True})

    form = await flow.submit("technician")

    assert form["step_id"] == PASSWORD_STEPS[kind]
