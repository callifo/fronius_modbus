"""The two repair flows, driven through Home Assistant's own repairs API."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import fronius_modbus
from custom_components.fronius_modbus import config_flow, repairs
from custom_components.fronius_modbus.const import (
    DOMAIN,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
    SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX,
)
from custom_components.fronius_modbus.token_store import async_get_token_store
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from .conftest import INVERTER_UNIT_ID
from .test_web_control import FakeWebClient

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]

HOST = "192.0.2.10"
ENTRY_DATA = {
    "name": "Fronius",
    "host": HOST,
    "port": 502,
    "inverter_modbus_unit_id": INVERTER_UNIT_ID,
    "scan_interval": 10,
    "restrict_modbus_to_this_ip": False,
}
SETTINGS_INPUT = {
    "host": HOST,
    "scan_interval": 10,
    "restrict_modbus_to_this_ip": False,
}
PASSWORD_INPUT = {"api_password": "secret"}


class SetupWebClient(FakeWebClient):
    """The web control's fake client, plus what async_setup_entry asks of it."""

    def __init__(self, *_args, **_kwargs) -> None:
        super().__init__()
        self.solar_api_enabled = True

    def get_solar_api_config(self):
        return {"SolarAPIv1Enabled": self.solar_api_enabled}

    def set_solar_api_enabled(self, enabled):
        self.solar_api_enabled = bool(enabled)
        return super().set_solar_api_enabled(enabled)

    def get_power_meter_info(self, *_args, **_kwargs):
        return None


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


@pytest.fixture
def web_client(monkeypatch) -> SetupWebClient:
    """One fake client behind every FroniusWebClient the integration constructs."""
    client = SetupWebClient()
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", lambda *a, **k: client)
    monkeypatch.setattr(config_flow.FroniusWebClient, "login", lambda self: True)
    monkeypatch.setattr(
        config_flow.FroniusWebClient, "ensure_modbus_enabled", lambda self, *a: True
    )
    monkeypatch.setattr(
        config_flow,
        "mint_token",
        lambda host, user, password: {"realm": "r", "token": "t"},
    )
    return client


@pytest.fixture
async def repairs_client(hass, hass_client):
    """A HTTP client with the repairs component and this integration's platform loaded."""
    assert await async_setup_component(hass, "repairs", {})
    await hass.async_block_till_done()
    return await hass_client()


async def start_fix_flow(client, issue_id: str) -> dict:
    response = await client.post(
        "/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": issue_id}
    )
    assert response.status == 200
    return await response.json()


async def advance_fix_flow(
    client, flow_id: str, user_input: dict | None = None
) -> dict:
    response = await client.post(
        f"/api/repairs/issues/fix/{flow_id}", json=user_input or {}
    )
    assert response.status == 200
    return await response.json()


async def make_entry(hass, mock_modbus, *, with_token: bool) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Fronius 192.0.2.10",
        data=ENTRY_DATA,
        unique_id=HOST,
        version=1,
        minor_version=10,
    )
    entry.add_to_hass(hass)
    if with_token:
        await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# -- the reconfigure repair --------------------------------------------------------


async def test_the_reconfigure_repair_stores_a_token_and_closes_the_issue(
    hass, mock_modbus, web_client, repairs_client
):
    entry = await make_entry(hass, mock_modbus, with_token=False)
    issue_id = f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    flow = await start_fix_flow(repairs_client, issue_id)
    assert flow["step_id"] == "init"

    flow = await advance_fix_flow(repairs_client, flow["flow_id"], SETTINGS_INPUT)
    assert flow["step_id"] == "password"

    flow = await advance_fix_flow(repairs_client, flow["flow_id"], PASSWORD_INPUT)
    await hass.async_block_till_done()

    assert flow["type"] == "create_entry"
    assert await async_get_token_store(hass).async_load_token(HOST, "customer") == {
        "realm": "r",
        "token": "t",
    }
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_the_reconfigure_repair_closes_the_issue_of_a_deleted_entry(
    hass, mock_modbus, web_client, repairs_client
):
    entry = await make_entry(hass, mock_modbus, with_token=False)
    issue_id = f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{entry.entry_id}"
    await hass.config_entries.async_remove(entry.entry_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="legacy_modbus_only_entry_reconfigure",
    )

    flow = await start_fix_flow(repairs_client, issue_id)

    assert flow["type"] == "create_entry"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# -- the Solar API repair ----------------------------------------------------------


async def test_the_solar_api_repair_disables_the_api_and_closes_the_issue(
    hass, mock_modbus, web_client, repairs_client
):
    entry = await make_entry(hass, mock_modbus, with_token=True)
    issue_id = f"{SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX}{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    flow = await start_fix_flow(repairs_client, issue_id)
    assert flow["type"] == "menu"
    assert flow["menu_options"] == ["fix", "ignore"]

    flow = await advance_fix_flow(
        repairs_client, flow["flow_id"], {"next_step_id": "fix"}
    )
    assert flow["step_id"] == "confirm"

    flow = await advance_fix_flow(repairs_client, flow["flow_id"])
    await hass.async_block_till_done()

    assert flow["type"] == "create_entry"
    assert ("solar", False) in web_client.calls
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_ignoring_the_solar_api_repair_keeps_the_api_enabled(
    hass, mock_modbus, web_client, repairs_client
):
    entry = await make_entry(hass, mock_modbus, with_token=True)
    issue_id = f"{SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX}{entry.entry_id}"

    flow = await start_fix_flow(repairs_client, issue_id)
    flow = await advance_fix_flow(
        repairs_client, flow["flow_id"], {"next_step_id": "ignore"}
    )

    assert flow["type"] == "abort"
    assert flow["reason"] == "issue_ignored"
    assert web_client.solar_api_enabled is True
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id).dismissed_version


async def test_a_solar_api_disable_that_does_not_take_shows_the_error(
    hass, mock_modbus, web_client, repairs_client, monkeypatch
):
    entry = await make_entry(hass, mock_modbus, with_token=True)
    issue_id = f"{SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX}{entry.entry_id}"
    monkeypatch.setattr(web_client, "set_solar_api_enabled", lambda enabled: True)

    flow = await start_fix_flow(repairs_client, issue_id)
    flow = await advance_fix_flow(
        repairs_client, flow["flow_id"], {"next_step_id": "fix"}
    )
    flow = await advance_fix_flow(repairs_client, flow["flow_id"])

    assert flow["errors"] == {"base": "cannot_connect"}
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_the_solar_api_repair_closes_the_issue_of_a_deleted_entry(
    hass, mock_modbus, web_client, repairs_client
):
    entry = await make_entry(hass, mock_modbus, with_token=True)
    issue_id = f"{SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX}{entry.entry_id}"
    await hass.config_entries.async_remove(entry.entry_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="solar_api_low_firmware",
    )

    flow = await start_fix_flow(repairs_client, issue_id)

    assert flow["type"] == "create_entry"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# -- flow selection ----------------------------------------------------------------


async def test_an_unknown_issue_has_no_repair_flow(hass):
    with pytest.raises(ValueError):
        await repairs.async_create_fix_flow(hass, "something_else", None)


async def test_the_entry_id_is_taken_from_the_issue_data_when_it_is_there(hass):
    flow = await repairs.async_create_fix_flow(
        hass,
        f"{SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX}stale",
        {"entry_id": "from-data"},
    )

    assert flow._entry_id == "from-data"
