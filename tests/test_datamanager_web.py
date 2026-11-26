"""The export limit of a Datamanager, read with the technician's login."""

from datetime import timedelta
import logging

import pytest
import requests

from custom_components.fronius_modbus.const import (
    CONF_DATAMANAGER,
    CONF_INVERTER_UNIT_ID,
    DOMAIN,
    entity_prefix,
)
from custom_components.fronius_modbus.datamanager_client import EXPORT_LIMIT_PATH
from custom_components.fronius_modbus.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.fronius_modbus.fronius_modbus_api.storage import ExtendedMode
from custom_components.fronius_modbus.token_store import async_get_token_store
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import INVERTER_UNIT_ID
from .fake_datamanager import HOST, answer_body, minted, serve_datamanager
from .ha_test_plugin import async_fire_time_changed
from .test_datamanager_entry import registered_keys, reload_entry
from .test_e2e import entity_id_for, make_entry, setup_entry, state_of
from .test_hybrid_storage import EMPTY, serve_hybrid

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]

READ = [("GET", EXPORT_LIMIT_PATH)] * 2
SHOWN = {
    "export_soft_limit": "14500.0",
    "export_soft_limit_state": "enabled",
    "export_hard_limit": "0.0",
    "export_hard_limit_state": "disabled",
    "export_limit_mode": "entire_system",
    "export_fail_safe": "enabled",
}


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


@pytest.fixture
def device(datamanager_modbus, monkeypatch):
    return serve_datamanager(monkeypatch)


async def log_in(hass, token: str = minted()["token"]) -> None:
    """Store the technician's login, as a flow does."""
    await async_get_token_store(hass).async_save_token(
        HOST, user="technician", realm=minted()["realm"], token=token
    )


def technician_entry(hass, **data):
    """An entry on a Datamanager with the technician role."""
    values = {"api_username": "technician", CONF_DATAMANAGER: True} | data
    return make_entry(hass, minor_version=14, **values)


def shown(hass, entry) -> dict[str, str]:
    return {key: state_of(hass, entry, key) for key in SHOWN}


def logins_asked(hass) -> list[str]:
    """The entries a new login is asked for."""
    return [
        flow["context"]["entry_id"]
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


async def after(hass, **interval) -> None:
    """Let this much time pass."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(**interval))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_the_export_limit_is_shown(hass, device):
    await log_in(hass)
    entry = technician_entry(hass)

    await setup_entry(hass, entry)

    assert shown(hass, entry) == SHOWN
    assert entry.runtime_data.web is None
    assert device.calls == READ
    assert logins_asked(hass) == []


async def test_without_the_login_its_page_is_not_asked(hass, device):
    await log_in(hass)
    entry = technician_entry(hass, api_username="none")

    await setup_entry(hass, entry)

    assert device.calls == []
    assert not set(SHOWN) & registered_keys(hass, entry)


async def test_a_page_of_another_form_is_unknown(hass, device):
    device.body = answer_body({"exportLimits": []})
    await log_in(hass)
    entry = technician_entry(hass)

    await setup_entry(hass, entry)

    assert shown(hass, entry) == dict.fromkeys(SHOWN, "unknown")


async def test_a_mode_nobody_has_seen_is_unknown(hass, device):
    active = device.body["Body"]["Data"]["powerLimits"]["exportLimits"]["activePower"]
    active["mode"] = "perPhase"
    await log_in(hass)
    entry = technician_entry(hass)

    await setup_entry(hass, entry)

    assert state_of(hass, entry, "export_limit_mode") == "unknown"
    assert state_of(hass, entry, "export_soft_limit") == "14500.0"


@pytest.mark.parametrize(("configured", "polled"), [(5, 60), (60, 60), (300, 300)])
async def test_its_page_is_read_every_minute_at_most(hass, device, configured, polled):
    """A Symo Hybrid restarted while its Solar API was read every 10 s."""
    await log_in(hass)
    entry = technician_entry(hass, web_scan_interval=configured)

    await setup_entry(hass, entry)

    interval = entry.runtime_data.datamanager_web.update_interval
    assert interval == timedelta(seconds=polled)
    device.calls.clear()
    await after(hass, seconds=polled - 2)
    assert device.calls == []
    await after(hass, seconds=polled + 1)
    assert device.calls == READ


async def test_an_outage_is_one_line_in_the_log(hass, device, monkeypatch, caplog):
    await log_in(hass)
    entry = technician_entry(hass)
    await setup_entry(hass, entry)
    answering = requests.adapters.HTTPAdapter.send

    def refuse(*_args, **_options):
        raise requests.ConnectionError

    caplog.clear()
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)
    coordinator = entry.runtime_data.datamanager_web
    for _ in range(3):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert shown(hass, entry) == dict.fromkeys(SHOWN, "unavailable")
    assert state_of(hass, entry, "acpower") == "597.0"
    lines = [
        record
        for record in caplog.records
        if "fronius_modbus" in record.name and record.levelno >= logging.INFO
    ]
    assert [record.levelno for record in lines] == [logging.INFO]
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", answering)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert shown(hass, entry) == SHOWN


async def test_a_refused_login_is_sent_once(hass, device):
    """The service password was changed on the device."""
    await log_in(hass, token="0" * 32)
    entry = technician_entry(hass)

    await setup_entry(hass, entry)
    await after(hass, minutes=30)
    await after(hass, minutes=60)

    assert entry.state is ConfigEntryState.LOADED
    assert len(device.logins) == 1
    assert logins_asked(hass) == [entry.entry_id]
    assert shown(hass, entry) == dict.fromkeys(SHOWN, "unavailable")
    assert state_of(hass, entry, "acpower") == "597.0"
    store = async_get_token_store(hass)
    assert await store.async_load_token(HOST, "technician") is None


async def test_a_login_refused_later_is_asked_for_once(hass, device):
    await log_in(hass)
    entry = technician_entry(hass)
    await setup_entry(hass, entry)
    device.password = "changed"

    await after(hass, seconds=61)
    tries = len(device.logins)
    await after(hass, minutes=30)

    assert len(device.logins) == tries
    assert logins_asked(hass) == [entry.entry_id]

    device.password = "secret"
    await log_in(hass)
    await reload_entry(hass, entry)
    assert shown(hass, entry) == SHOWN


async def test_an_entry_without_its_login_asks_for_it(hass, device):
    entry = technician_entry(hass)

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert logins_asked(hass) == [entry.entry_id]
    assert device.calls == []
    # They come back with the login.
    assert not set(SHOWN) & registered_keys(hass, entry)


async def test_a_server_error_is_no_refused_login(hass, device):
    device.status = 500
    await log_in(hass)
    entry = technician_entry(hass)

    await setup_entry(hass, entry)

    assert shown(hass, entry) == dict.fromkeys(SHOWN, "unavailable")
    assert logins_asked(hass) == []
    device.status = 200
    await after(hass, seconds=61)
    assert shown(hass, entry) == SHOWN


async def test_a_page_forbidden_before_the_login_asks_for_no_new_login(hass, device):
    device.unsigned_status = 403
    await log_in(hass)
    entry = technician_entry(hass)

    await setup_entry(hass, entry)

    assert logins_asked(hass) == []
    store = async_get_token_store(hass)
    assert await store.async_load_token(HOST, "technician") == minted()
    device.unsigned_status = None
    await after(hass, seconds=61)
    assert shown(hass, entry) == SHOWN


async def test_one_inverter_of_a_datamanager_reads_it(hass, device, datamanager_modbus):
    datamanager_modbus.add_unit(2, like=INVERTER_UNIT_ID)
    await log_in(hass)
    first = technician_entry(hass)
    second = technician_entry(hass, **{CONF_INVERTER_UNIT_ID: 2})
    hass.config_entries.async_update_entry(second, unique_id=f"{HOST}#2")

    await setup_entry(hass, first)

    assert shown(hass, first) == SHOWN
    assert second.state is ConfigEntryState.LOADED
    assert not set(SHOWN) & registered_keys(hass, second)
    assert device.calls == READ
    assert logins_asked(hass) == []


async def test_the_battery_of_a_hybrid_is_written_over_modbus_alone(
    hass, device, datamanager_modbus
):
    """A GEN24 writes its web interface too; a Datamanager has none of that."""
    serve_hybrid(datamanager_modbus, EMPTY)
    await log_in(hass)
    entry = technician_entry(hass)
    await setup_entry(hass, entry)
    runtime = entry.runtime_data

    await runtime.async_set_soc_minimum(30)
    await runtime.modbus.async_refresh()
    assert runtime.device.storage.min_rsv_pct == 30
    await runtime.async_set_extended_mode(ExtendedMode.CHARGE_FROM_GRID)

    assert runtime.storage_control.extended_mode is ExtendedMode.CHARGE_FROM_GRID
    assert device.calls == READ


async def test_the_diagnostics_hold_the_limits(hass, device):
    await log_in(hass)
    entry = technician_entry(hass)
    await setup_entry(hass, entry)

    data = await async_get_config_entry_diagnostics(hass, entry)

    assert data["export_limits"]["soft_w"] == 14500.0
    assert data["web"] is None


# -- the soft limit as a number -------------------------------------------------------


async def number_entry(hass):
    """An entry whose soft limit number the owner switched on."""
    await log_in(hass)
    entry = technician_entry(hass)
    er.async_get(hass).async_get_or_create(
        "number",
        DOMAIN,
        f"{entity_prefix(entry.entry_id)}_export_soft_limit",
        config_entry=entry,
        disabled_by=None,
    )
    await setup_entry(hass, entry)
    return entry


async def set_number(hass, entry, value: float) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {
            "entity_id": entity_id_for(hass, entry, "number", "export_soft_limit"),
            "value": value,
        },
        blocking=True,
    )


async def test_the_soft_limit_number_is_off_until_it_is_switched_on(hass, device):
    """Nobody has seen this write on a device yet."""
    await log_in(hass)
    entry = technician_entry(hass)

    await setup_entry(hass, entry)

    row = er.async_get(hass).async_get(
        entity_id_for(hass, entry, "number", "export_soft_limit")
    )
    assert row.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_the_soft_limit_is_set_and_shown(hass, device):
    entry = await number_entry(hass)
    assert state_of(hass, entry, "export_soft_limit", "number") == "14500.0"

    await set_number(hass, entry, 9000)

    assert state_of(hass, entry, "export_soft_limit", "number") == "9000.0"
    assert state_of(hass, entry, "export_soft_limit") == "9000.0"
    assert len(device.written) == 1


async def test_the_number_ends_at_the_highest_soft_limit(hass, device):
    entry = await number_entry(hass)
    state = hass.states.get(entity_id_for(hass, entry, "number", "export_soft_limit"))

    assert (state.attributes["min"], state.attributes["max"]) == (0, 18200)
    with pytest.raises(ServiceValidationError):
        await set_number(hass, entry, 18201)
    assert device.written == []


async def test_the_number_ends_below_an_enabled_hard_limit(hass, device):
    active = device.body["Body"]["Data"]["powerLimits"]["exportLimits"]["activePower"]
    active["hardLimit"] = {"enabled": True, "powerLimit": 8000}
    entry = await number_entry(hass)
    state = hass.states.get(entity_id_for(hass, entry, "number", "export_soft_limit"))

    assert state.attributes["max"] == 7999


async def test_an_enabled_hard_limit_of_0_leaves_the_number_unavailable(
    hass, device
):
    active = device.body["Body"]["Data"]["powerLimits"]["exportLimits"]["activePower"]
    active["hardLimit"] = {"enabled": True, "powerLimit": 0}
    entry = await number_entry(hass)

    assert state_of(hass, entry, "export_soft_limit", "number") == "unavailable"


async def test_a_form_nobody_has_seen_cannot_be_written(hass, device):
    active = device.body["Body"]["Data"]["powerLimits"]["exportLimits"]["activePower"]
    entry = await number_entry(hass)
    active["mode"] = "perPhase"

    with pytest.raises(ServiceValidationError) as refused:
        await set_number(hass, entry, 9000)

    assert refused.value.translation_key == "export_limit_unknown_form"
    await entry.runtime_data.datamanager_web.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, entry, "export_soft_limit", "number") == "unavailable"


async def test_an_unconfirmed_write_says_so(hass, device):
    """Taken, but with an error: the device is shown as it is now."""
    entry = await number_entry(hass)
    device.write_answer = {"Head": {"Status": {"Code": 255}}}

    with pytest.raises(HomeAssistantError) as unconfirmed:
        await set_number(hass, entry, 9000)

    assert unconfirmed.value.translation_key == "export_limit_unconfirmed"
    assert state_of(hass, entry, "export_soft_limit", "number") == "9000.0"


async def test_a_login_refused_at_a_write_is_asked_for(hass, device):
    entry = await number_entry(hass)
    device.password = "changed"

    with pytest.raises(HomeAssistantError) as refused:
        await set_number(hass, entry, 9000)

    assert refused.value.translation_key == "web_api_auth_failed"
    assert logins_asked(hass) == [entry.entry_id]
    assert device.written == []
    tries = len(device.logins)
    await after(hass, minutes=30)
    assert len(device.logins) == tries


async def test_a_device_off_at_a_write_is_unreachable(hass, device, monkeypatch):
    entry = await number_entry(hass)

    def refuse(*_args, **_options):
        raise requests.ConnectionError

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)
    with pytest.raises(HomeAssistantError) as unreachable:
        await set_number(hass, entry, 9000)

    assert unreachable.value.translation_key == "web_api_unreachable"
