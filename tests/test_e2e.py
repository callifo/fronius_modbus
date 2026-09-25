"""End-to-end: a real Home Assistant sets the entry up on the mocked shared connection."""

from dataclasses import replace
from types import SimpleNamespace

from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusTimeoutError,
    ServerDeviceFailureError,
)
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import fronius_modbus
from custom_components.fronius_modbus import config_flow, migrations
from custom_components.fronius_modbus.const import (
    CONF_RECONFIGURE_REQUIRED,
    DOMAIN,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
    entity_prefix,
    instance_key,
)
from custom_components.fronius_modbus.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.fronius_modbus.froniuswebclient import (
    FroniusWebAuthError,
    FroniusWebClient,
)
from custom_components.fronius_modbus.token_store import async_get_token_store
from custom_components.fronius_modbus.web_control import WebData
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.entity_platform import async_get_platforms

from .conftest import INVERTER_UNIT_ID, METER_UNIT_ID

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


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    """Let Home Assistant load custom_components/fronius_modbus in every test here."""
    return enable_custom_integrations


def make_entry(hass, *, minor_version: int = 10, **data) -> MockConfigEntry:
    """A Modbus-only entry (no web token) added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Fronius 192.0.2.10",
        data={**ENTRY_DATA, **data},
        unique_id=HOST,
        version=1,
        minor_version=minor_version,
    )
    entry.add_to_hass(hass)
    return entry


async def setup_entry(hass, entry: MockConfigEntry) -> None:
    """Run the entry through async_setup_entry and settle the event loop."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def log_in_to_the_web_api(hass, mock_modbus, monkeypatch) -> None:
    """A working web login with a confirmed two-meter topology.

    Cleanup waits for one (audit A24-01), so a test of any other cleanup gate
    needs it to reach that gate at all.
    """
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _FakeWebClientWithTopology)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")


def _only_what_the_real_client_has(name: str) -> None:
    """A fake that answers any name hid a call to an attribute the client lacks."""
    if not hasattr(FroniusWebClient, name):
        raise AttributeError(name)


def entity_id_for(hass, entry: MockConfigEntry, domain: str, key: str) -> str:
    """The registry entity id of one of the entry's entities, by description key."""
    unique_id = f"{entity_prefix(entry.entry_id)}_{key}"
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, f"no {domain} entity for {unique_id}"
    return entity_id


def state_of(hass, entry: MockConfigEntry, key: str, domain: str = "sensor") -> str:
    """The current state string of one of the entry's entities."""
    return hass.states.get(entity_id_for(hass, entry, domain, key)).state


def platform_entity(hass, entity_id: str):
    """The live entity object behind an entity id, for calls a service would skip."""
    for platform in async_get_platforms(hass, DOMAIN):
        if entity_id in platform.entities:
            return platform.entities[entity_id]
    raise AssertionError(f"{entity_id} is not on any fronius_modbus platform")


async def test_setup_creates_the_inverter_storage_and_meter_devices(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert state_of(hass, entry, "acpower") == "3075.1"
    assert state_of(hass, entry, f"meter_{METER_UNIT_ID}_power") == "30.0"
    assert state_of(hass, entry, "soc") == "96.0"

    key = instance_key(entry.entry_id)
    identifiers = {
        identifier
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        for _domain, identifier in device.identifiers
    }
    assert identifiers == {
        f"{key}_inverter",
        f"{key}_battery_storage",
        f"{key}_meter_{METER_UNIT_ID}",
    }


async def test_the_configured_host_and_port_reach_the_shared_connection(
    hass, mock_modbus
):
    await setup_entry(hass, make_entry(hass))

    assert mock_modbus.params_seen[0].host == HOST
    assert mock_modbus.params_seen[0].port == 502


async def test_unload_releases_the_connection(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    assert mock_modbus.connected is True

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert mock_modbus.connected is False


async def test_a_meter_outage_only_takes_the_meter_entities_down(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)

    mock_modbus.fail_requests(METER_UNIT_ID, ModbusTimeoutError())
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()

    assert state_of(hass, entry, f"meter_{METER_UNIT_ID}_power") == "unavailable"
    assert float(state_of(hass, entry, "acpower")) == 3075.1


async def test_a_link_outage_and_recovery(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)

    mock_modbus.fail_requests(INVERTER_UNIT_ID, ModbusConnectionError())
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, entry, "acpower") == "unavailable"

    mock_modbus.fail_requests(INVERTER_UNIT_ID, None)
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()
    assert state_of(hass, entry, "acpower") == "3075.1"


async def test_the_total_sensor_keeps_its_value_through_an_outage(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    assert state_of(hass, entry, "acenergy") == "33187794.59"

    mock_modbus.fail_requests(INVERTER_UNIT_ID, ModbusConnectionError())
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()

    assert state_of(hass, entry, "acenergy") == "33187794.59"


async def test_a_number_write_reaches_the_register(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entity_id_for(hass, entry, "number", "soc_minimum"), "value": 7},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert mock_modbus.unit(INVERTER_UNIT_ID).holding[40350] == 700

    # In AUTO the charge limit is not writable; the service layer skips unavailable
    # entities, so the refusal is asserted on the entity itself.
    charge_limit = platform_entity(
        hass, entity_id_for(hass, entry, "number", "charge_limit")
    )
    assert charge_limit.available is False
    with pytest.raises(ServiceValidationError) as refused:
        await charge_limit.async_set_native_value(1000)
    # Quality scale exception-translations: the refusal reaches the user in
    # their language, not as the English text the library raised.
    assert refused.value.translation_domain == DOMAIN
    assert refused.value.translation_key == "charge_limit_not_in_mode"


async def test_the_select_changes_the_storage_mode(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)

    await hass.services.async_call(
        "select",
        "select_option",
        {
            "entity_id": entity_id_for(hass, entry, "select", "ext_control_mode"),
            "option": "charge_from_grid",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    unit = mock_modbus.unit(INVERTER_UNIT_ID)
    assert unit.holding[40348] == 2
    assert unit.holding[40355] == 0


async def test_minor_version_9_entries_migrate_to_the_current_shape(hass, mock_modbus):
    entry = make_entry(hass, minor_version=9)
    title_before = entry.title

    # Assert on the migration step alone: async_setup_entry's own token check
    # (unrelated to this migration) would add CONF_RECONFIGURE_REQUIRED anyway
    # for an entry with no stored token, masking what the migration itself did.
    assert await migrations.async_migrate_entry(hass, entry)

    assert entry.minor_version == 14
    # Minor 9 entries are already on the web-API shape: only the version bump
    # and the new role are expected, not the pre-web-API data migration.
    assert CONF_RECONFIGURE_REQUIRED not in entry.data
    assert entry.title == title_before

    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED


async def test_v019_mppt_entities_are_renamed(hass, mock_modbus):
    entry = make_entry(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "fm_mppt1_power",
        config_entry=entry,
        suggested_object_id="old",
    )

    await setup_entry(hass, entry)

    assert registry.async_get_entity_id("sensor", DOMAIN, "fm_mppt1_power") is None
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entity_prefix(entry.entry_id)}_mppt_module_0_dc_power"
        )
        is not None
    )


async def test_a_stale_entity_is_removed_after_a_clean_first_poll(
    hass, mock_modbus, monkeypatch
):
    """An entity for a key the integration no longer creates is dropped on a clean poll."""
    await log_in_to_the_web_api(hass, mock_modbus, monkeypatch)
    entry = make_entry(hass)
    registry = er.async_get(hass)
    unique_id = f"{entity_prefix(entry.entry_id)}_no_such_key"
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        config_entry=entry,
    )

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None


async def test_a_shifted_sunspec_map_reloads_the_entry(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    connections_before = len(mock_modbus.params_seen)

    mock_modbus.unit(INVERTER_UNIT_ID).holding[40343] = 0
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()

    assert len(mock_modbus.params_seen) > connections_before
    assert entry.state is ConfigEntryState.LOADED


async def test_options_change_reloads_with_the_new_interval(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "scan_interval": 30}
    )
    await hass.async_block_till_done()

    assert entry.runtime_data.modbus.update_interval.total_seconds() == 30


class _FakeWebClientDownAfterMeterInfo:
    """A web client whose meter-info call succeeds but everything else is down."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get_power_meter_info(self, *_args, **_kwargs):
        return None

    def __getattr__(self, name: str):
        _only_what_the_real_client_has(name)

        def _raise(*_args, **_kwargs):
            raise RuntimeError("down")

        return _raise


async def test_a_web_api_outage_does_not_block_the_modbus_entities(
    hass, mock_modbus, monkeypatch
):
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    monkeypatch.setattr(
        fronius_modbus, "FroniusWebClient", _FakeWebClientDownAfterMeterInfo
    )

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert state_of(hass, entry, "acpower") == "3075.1"
    assert state_of(hass, entry, "inverter_temperature") == "unavailable"


async def test_diagnostics_redact_the_serial_numbers(hass, mock_modbus):
    entry = make_entry(hass)
    await setup_entry(hass, entry)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["identity"]["serial"] == "**REDACTED**"
    registers_unit_1 = result["registers"]["1"]["holding"]
    assert not (set(registers_unit_1) & {str(a) for a in range(40052, 40068)})
    assert "inverter" in result["updated"]

    # The restriction IP is the owner's LAN address, which a bug report is
    # routinely pasted into a public issue.
    entry.runtime_data = replace(
        entry.runtime_data,
        web=SimpleNamespace(
            data=WebData(modbus_restriction_ip="192.0.2.99", storage_serial="S")
        ),
    )
    web = (await async_get_config_entry_diagnostics(hass, entry))["web"]
    assert web["modbus_restriction_ip"] == "**REDACTED**"
    assert web["storage_serial"] == "**REDACTED**"


# Inside the SunSpec model 160 block in the fixture, past the header the model
# walk itself reads: failing here fails only the MPPT sub-system's own poll.
MPPT_REGISTER_ADDRESS = 40260


async def test_a_failed_mppt_read_at_startup_keeps_the_mppt_entities(hass, mock_modbus):
    """A sub-system that is silent on the first poll must not cost the user its history.

    The MPPT descriptions are built from the values the first poll returned, so a
    read failure there makes every MPPT entity look retired to the registry cleanup.
    """
    entry = make_entry(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entity_prefix(entry.entry_id)}_mppt_module_0_dc_power",
        config_entry=entry,
    )
    mock_modbus.fail_read(
        INVERTER_UNIT_ID, MPPT_REGISTER_ADDRESS, ServerDeviceFailureError()
    )

    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entity_prefix(entry.entry_id)}_mppt_module_0_dc_power"
        )
        is not None
    )


async def test_a_meter_that_times_out_at_setup_keeps_its_entities(
    hass, mock_modbus, monkeypatch
):
    """Audit F03: the cleanup after a timed-out meter probe removed the meter's registry entries."""
    await log_in_to_the_web_api(hass, mock_modbus, monkeypatch)
    entry = make_entry(hass)
    registry = er.async_get(hass)
    original = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entity_prefix(entry.entry_id)}_meter_200_power",
        config_entry=entry,
        suggested_object_id="existing_meter_power",
    )
    mock_modbus.fail_requests(METER_UNIT_ID, ModbusTimeoutError())
    await setup_entry(hass, entry)
    assert registry.async_get(original.entity_id) is not None


async def test_mppt_entities_appear_once_the_first_read_succeeds(hass, mock_modbus):
    """Audit F03: a failed model-160 read at setup left the MPPT entities missing until a manual reload."""
    entry = make_entry(hass)
    mock_modbus.fail_read(
        INVERTER_UNIT_ID, MPPT_REGISTER_ADDRESS, ServerDeviceFailureError()
    )
    await setup_entry(hass, entry)
    mock_modbus.fail_read(INVERTER_UNIT_ID, MPPT_REGISTER_ADDRESS, None)
    await entry.runtime_data.modbus.async_refresh()
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entity_prefix(entry.entry_id)}_mppt_module_0_dc_power"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id) is not None


@pytest.mark.parametrize(
    ("restricted", "choice"), [(True, "home_assistant"), (False, "keep")]
)
async def test_minor_version_11_entries_turn_the_checkbox_into_a_choice(
    hass, mock_modbus, restricted, choice
):
    """An unchecked box used to write "off"; it migrates to "keep", never to "off".

    Lifting a restriction is now a choice of its own (audit A24-02).
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**ENTRY_DATA, "restrict_modbus_to_this_ip": restricted},
        options={"restrict_modbus_to_this_ip": restricted},
        unique_id=HOST,
        version=1,
        minor_version=11,
    )
    entry.add_to_hass(hass)

    assert await migrations.async_migrate_entry(hass, entry)

    assert entry.minor_version == 14
    for values in (entry.data, entry.options):
        assert values["modbus_restriction"] == choice
        assert "restrict_modbus_to_this_ip" not in values


async def test_minor_version_10_entries_take_the_role_of_their_stored_token(
    hass, mock_modbus
):
    """Upstream #130: an entry that had a technician token keeps technician access after the migration."""
    entry = make_entry(hass, minor_version=10)
    await async_get_token_store(hass).async_save_token(
        HOST, realm="r", token="t", user="technician"
    )
    assert await migrations.async_migrate_entry(hass, entry)
    assert entry.minor_version == 14
    assert entry.data["api_username"] == "technician"
    assert entry.options["api_username"] == "technician"


async def test_minor_version_10_entries_without_a_technician_token_stay_customer(
    hass, mock_modbus
):
    entry = make_entry(hass, minor_version=10)
    assert await migrations.async_migrate_entry(hass, entry)
    assert entry.minor_version == 14
    assert entry.data["api_username"] == "customer"


# The storage model header in the captured fixture.
STORAGE_HEADER_ADDRESS = 40343


async def test_a_refused_chain_keeps_the_storage_entities(hass, mock_modbus):
    """Audit A03: an incomplete scan looked like an absent battery, so cleanup removed it."""
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    entity_id = entity_id_for(hass, entry, "sensor", "soc")

    mock_modbus.fail_read(
        INVERTER_UNIT_ID, STORAGE_HEADER_ADDRESS, IllegalDataAddressError()
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(entity_id) is not None


class _FakeWebClientWithTopology:
    """A web client that serves two meters until `topology` is set to None."""

    topology: dict | None = {
        "unit_ids": [200, 201],
        "primary_unit_id": 200,
        "locations_by_unit_id": {200: 0, 201: 1},
    }

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get_power_meter_info(self, *_args, **_kwargs):
        return self.topology

    def __getattr__(self, name: str):
        _only_what_the_real_client_has(name)

        def _quiet(*_args, **_kwargs):
            return None

        return _quiet


async def test_a_topology_outage_keeps_the_second_meters_entities(
    hass, mock_modbus, monkeypatch
):
    """Audit A04: one failed topology read fell back to a single meter and deleted the rest."""
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _FakeWebClientWithTopology)
    monkeypatch.setattr(
        _FakeWebClientWithTopology,
        "topology",
        dict(_FakeWebClientWithTopology.topology),
    )
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    await setup_entry(hass, entry)
    entity_id = entity_id_for(hass, entry, "sensor", "meter_201_power")

    monkeypatch.setattr(_FakeWebClientWithTopology, "topology", None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(entity_id) is not None


async def test_a_reload_retires_no_live_meter_device(hass, mock_modbus, monkeypatch):
    """The legacy meter pattern matches the identifiers this version builds, too.

    Home Assistant restores a device that comes straight back, so the damage is
    only visible as the removal itself: assert none happens.
    """
    await log_in_to_the_web_api(hass, mock_modbus, monkeypatch)
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    removed: list[str] = []
    registry = dr.async_get(hass)
    monkeypatch.setattr(registry, "async_remove_device", removed.append)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert removed == []


async def test_a_confirmed_single_meter_topology_is_applied(
    hass, mock_modbus, monkeypatch
):
    """Audit B04: a confirmed answer about the meter we already poll never reached the runtime."""
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _FakeWebClientWithTopology)
    monkeypatch.setattr(_FakeWebClientWithTopology, "topology", None)
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    await setup_entry(hass, entry)
    assert entry.runtime_data.topology_confirmed is False

    monkeypatch.setattr(
        _FakeWebClientWithTopology,
        "topology",
        {
            "unit_ids": [METER_UNIT_ID],
            "primary_unit_id": METER_UNIT_ID,
            "locations_by_unit_id": {METER_UNIT_ID: 0},
        },
    )
    await entry.runtime_data.web.async_refresh()
    await hass.async_block_till_done()

    runtime = hass.config_entries.async_get_entry(entry.entry_id).runtime_data
    assert runtime.topology_confirmed
    assert runtime.meter_locations == {METER_UNIT_ID: 0}


class _WebClientLosingItsLogin(_FakeWebClientWithTopology):
    """Answers the public topology; `lost_at` names the call the login fails on."""

    lost_at: str | None = None

    def __getattr__(self, name: str):
        if name == self.lost_at:

            def _refused(*_args, **_kwargs):
                raise FroniusWebAuthError("token rejected")

            return _refused
        return super().__getattr__(name)

    def get_power_meter_info(self, *_args, **_kwargs):
        if self.lost_at == "get_power_meter_info":
            raise FroniusWebAuthError("token rejected")
        return self.topology


@pytest.mark.parametrize(
    "lose_login",
    ["first_protected_read", "topology_read", "token_gone_before_restart"],
)
async def test_a_lost_web_login_retires_no_web_entity(
    hass, mock_modbus, monkeypatch, lose_login
):
    """Audit A24-01: a login lost at setup read as "no web API", and cleanup deleted its entities.

    The owner's chosen entity id went with them. A lost login is "not seen", like
    a failed poll, whichever way it goes missing.
    """
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientLosingItsLogin)
    entry = make_entry(hass)
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="t")
    await setup_entry(hass, entry)
    registry = er.async_get(hass)
    chosen = "sensor.my_inverter_temperature"
    registry.async_update_entity(
        entity_id_for(hass, entry, "sensor", "inverter_temperature"),
        new_entity_id=chosen,
    )

    if lose_login == "first_protected_read":
        monkeypatch.setattr(_WebClientLosingItsLogin, "lost_at", "get_inverter_info")
    elif lose_login == "topology_read":
        monkeypatch.setattr(_WebClientLosingItsLogin, "lost_at", "get_power_meter_info")
    else:
        await store.async_delete_token(HOST, "customer")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(chosen) is not None
    assert registry.async_get(entity_id_for(hass, entry, "sensor", "meter_201_power"))


async def test_a_rejected_technician_token_is_the_one_deleted(
    hass, mock_modbus, monkeypatch
):
    """The topology read deleted the customer token whatever role had been rejected.

    The rejected technician token survived and was offered again on every start.
    """
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientLosingItsLogin)
    monkeypatch.setattr(_WebClientLosingItsLogin, "lost_at", "get_power_meter_info")
    entry = make_entry(hass)
    store = async_get_token_store(hass)
    await store.async_save_token(HOST, realm="r", token="t", user="technician")

    await setup_entry(hass, entry)

    assert await store.async_load_token(HOST, "technician") is None


class _WebClientAnsweringEmpty(_FakeWebClientWithTopology):
    """The inverter component endpoint answers, but with a node without channels."""

    readings: dict | None = None

    def get_inverter_info(self):
        return {"readings": self.readings, "missing": False}


async def test_an_empty_component_answer_retires_no_entity(
    hass, mock_modbus, monkeypatch
):
    """Audit R25-01: the cleanup at the next start removed 28 entities, this one among them."""
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientAnsweringEmpty)
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    await setup_entry(hass, entry)
    registry = er.async_get(hass)
    chosen = "sensor.my_inverter_temperature"
    registry.async_update_entity(
        entity_id_for(hass, entry, "sensor", "inverter_temperature"),
        new_entity_id=chosen,
    )

    monkeypatch.setattr(_WebClientAnsweringEmpty, "readings", {})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(chosen) is not None


async def test_a_power_module_missing_from_one_answer_keeps_its_entity(
    hass, mock_modbus, monkeypatch
):
    """Reaudit RE26-03: an answer naming module 1 only retired module 2 at the next start."""
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientAnsweringEmpty)
    monkeypatch.setattr(
        _WebClientAnsweringEmpty,
        "readings",
        {
            "MODULE_TEMPERATURE_MEAN_01_F32": 40.0,
            "MODULE_TEMPERATURE_MEAN_02_F32": 41.0,
        },
    )
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    await setup_entry(hass, entry)
    module_2 = entity_id_for(hass, entry, "sensor", "module_temperature_2")

    monkeypatch.setattr(
        _WebClientAnsweringEmpty, "readings", {"MODULE_TEMPERATURE_MEAN_01_F32": 40.0}
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(module_2) is not None


async def test_a_reconfigure_reloads_the_entry_once(hass, mock_modbus, monkeypatch):
    """Audit F24-10: the update listener and an explicit call both reloaded it."""
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    reload = hass.config_entries.async_reload
    reloads: list[str] = []

    async def counting(entry_id):
        reloads.append(entry_id)
        return await reload(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", counting)
    settings = config_flow.entry_defaults(entry) | {"scan_interval": 20}

    await config_flow.async_update_entry_from_input(
        hass, entry, settings, previous_host=HOST
    )
    await hass.async_block_till_done()

    assert reloads == [entry.entry_id]


async def test_diagnostics_still_answer_while_the_inverter_is_offline(
    hass, mock_modbus
):
    """Audit F24-08: the fresh register read raised, so no diagnostics at all.

    An outage is when the last poll and its report are needed most.
    """
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    mock_modbus.unit(INVERTER_UNIT_ID).fail_requests(ModbusConnectionError())

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["registers"] == {"error": "ModbusConnectionError"}
    assert diagnostics["identity"]["serial"] == "**REDACTED**"


@pytest.mark.parametrize("first_answer", [None, {"FANCONTROL_PERCENT_01_F32": 0.0}])
async def test_a_placeholder_power_module_is_not_kept(
    hass, mock_modbus, monkeypatch, first_answer
):
    """Own reaudit R26-01: a module made while unread or unnamed stayed as if reported."""
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientAnsweringEmpty)
    monkeypatch.setattr(_WebClientAnsweringEmpty, "readings", first_answer)
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    await setup_entry(hass, entry)
    module_2 = entity_id_for(hass, entry, "sensor", "module_temperature_2")

    monkeypatch.setattr(
        _WebClientAnsweringEmpty,
        "readings",
        {
            "MODULE_TEMPERATURE_MEAN_01_F32": 40.0,
            "MODULE_TEMPERATURE_MEAN_03_F32": 40.0,
            "MODULE_TEMPERATURE_MEAN_04_F32": 40.0,
        },
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(module_2) is None


class _WebClientLosingTheEndpoint(_WebClientAnsweringEmpty):
    """The inverter component endpoint answers 404 once it is flagged missing."""

    missing = False

    def get_inverter_info(self):
        if self.missing:
            return {"readings": None, "missing": True}
        return super().get_inverter_info()


async def test_a_single_404_keeps_the_registered_component_sensors(
    hass, mock_modbus, monkeypatch
):
    """Audit FA0FB-03: one 404 read as firmware without the endpoint retired them."""
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientLosingTheEndpoint)
    monkeypatch.setattr(
        _WebClientLosingTheEndpoint,
        "readings",
        {"DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 40.0},
    )
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    await setup_entry(hass, entry)
    temperature = entity_id_for(hass, entry, "sensor", "inverter_temperature")

    monkeypatch.setattr(_WebClientLosingTheEndpoint, "missing", True)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(temperature) is not None


async def test_a_module_registered_before_the_marker_survives_the_upgrade(
    hass, mock_modbus, monkeypatch
):
    """Audit D8AE-01: an unmarked real module fell on the first upgraded partial answer."""
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientAnsweringEmpty)
    monkeypatch.setattr(
        _WebClientAnsweringEmpty,
        "readings",
        {
            "MODULE_TEMPERATURE_MEAN_01_F32": 40.0,
            "MODULE_TEMPERATURE_MEAN_02_F32": 41.0,
        },
    )
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    await setup_entry(hass, entry)
    module_2 = entity_id_for(hass, entry, "sensor", "module_temperature_2")
    await hass.config_entries.async_unload(entry.entry_id)
    # As an entry set up before the marker existed left it.
    er.async_get(hass).async_update_entity_options(module_2, DOMAIN, None)
    hass.config_entries.async_update_entry(entry, minor_version=12)

    monkeypatch.setattr(
        _WebClientAnsweringEmpty, "readings", {"MODULE_TEMPERATURE_MEAN_01_F32": 40.0}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(module_2) is not None


async def test_limit_sensors_from_before_they_followed_the_report_stay(
    hass, mock_modbus, monkeypatch
):
    """Registered limit sensors are kept on upgrade: no automatic cleanup (owner's call)."""
    limit_fields = {
        "ACBRIDGE_POWERACTIVE_PRODUCTION_LIMIT_F32": 10000.0,
        "DCDC_POWERACTIVE_BAT_MAX_F32": 5000.0,
    }
    module = {"MODULE_TEMPERATURE_MEAN_01_F32": 40.0}
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientAnsweringEmpty)
    monkeypatch.setattr(_WebClientAnsweringEmpty, "readings", module | limit_fields)
    entry = make_entry(hass)
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    await setup_entry(hass, entry)
    limits = [
        entity_id_for(hass, entry, "sensor", key)
        for key in ("production_limit", "battery_max_charge_power")
    ]
    await hass.config_entries.async_unload(entry.entry_id)
    # As 1.2.0b2 left them: those rows were not marked then.
    for entity_id in limits:
        er.async_get(hass).async_update_entity_options(entity_id, DOMAIN, None)
    hass.config_entries.async_update_entry(entry, minor_version=13)

    monkeypatch.setattr(_WebClientAnsweringEmpty, "readings", module)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert all(er.async_get(hass).async_get(entity_id) for entity_id in limits)


def serve_the_public_endpoints(mock_modbus, monkeypatch) -> None:
    """A fake web client for the endpoints that answer without a login.

    An entry without the web API polls them; the real client would reach for
    the network, which the test fixture refuses.
    """
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _FakeWebClientWithTopology)


def _reconfigure_issue(hass, entry: MockConfigEntry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{entry.entry_id}"
    )


async def test_an_entry_without_the_web_api_is_not_asked_to_reconfigure(
    hass, mock_modbus, monkeypatch
):
    serve_the_public_endpoints(mock_modbus, monkeypatch)
    entry = make_entry(hass, minor_version=13, api_username="none")
    await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert _reconfigure_issue(hass, entry) is None
    assert entry.data[CONF_RECONFIGURE_REQUIRED] is False


def _reauth_flows(hass, entry: MockConfigEntry) -> list:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"].get("source") == "reauth"
        and flow["context"].get("entry_id") == entry.entry_id
    ]


async def test_a_role_entry_without_token_asks_to_log_in_again(hass, mock_modbus):
    """Home Assistant's own reauthentication, not a Repairs item (quality scale Silver)."""
    entry = make_entry(hass, minor_version=13, api_username="customer")
    await setup_entry(hass, entry)

    assert _reconfigure_issue(hass, entry) is None
    assert len(_reauth_flows(hass, entry)) == 1


async def test_a_repairs_item_from_before_is_cleared(hass, mock_modbus, monkeypatch):
    await log_in_to_the_web_api(hass, mock_modbus, monkeypatch)
    entry = make_entry(hass, minor_version=13, api_username="customer")
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX}{entry.entry_id}",
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="legacy_modbus_only_entry_reconfigure",
    )

    await setup_entry(hass, entry)

    assert _reconfigure_issue(hass, entry) is None
    assert _reauth_flows(hass, entry) == []


async def test_an_entry_without_the_web_api_cleans_up_stale_entities(
    hass, mock_modbus, monkeypatch
):
    serve_the_public_endpoints(mock_modbus, monkeypatch)
    entry = make_entry(hass, minor_version=13, api_username="none")
    registry = er.async_get(hass)
    unique_id = f"{entity_prefix(entry.entry_id)}_api_modbus_mode"
    registry.async_get_or_create("sensor", DOMAIN, unique_id, config_entry=entry)

    await setup_entry(hass, entry)

    assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None


async def test_an_entry_without_the_web_api_reads_the_public_endpoints(
    hass, mock_modbus, monkeypatch
):
    """Stage B: component sensors and a second meter without any login."""
    mock_modbus.add_unit(201, like=METER_UNIT_ID)
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientAnsweringEmpty)
    monkeypatch.setattr(
        _WebClientAnsweringEmpty,
        "readings",
        {"DEVICE_TEMPERATURE_AMBIENTMEAN_01_F32": 41.5},
    )
    entry = make_entry(hass, minor_version=13, api_username="none")
    await setup_entry(hass, entry)

    assert state_of(hass, entry, "inverter_temperature") == "41.5"
    assert state_of(hass, entry, "meter_201_power") != "unavailable"
    assert entry.runtime_data.web_control.configured is False


async def test_switching_off_the_web_api_drops_its_token_and_entities(
    hass, mock_modbus, monkeypatch
):
    await log_in_to_the_web_api(hass, mock_modbus, monkeypatch)
    entry = make_entry(hass, minor_version=13, api_username="customer")
    await setup_entry(hass, entry)
    web_entity = entity_id_for(hass, entry, "sensor", "api_modbus_mode")
    modbus_entity = entity_id_for(hass, entry, "sensor", "acpower")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "host": HOST,
            "scan_interval": 10,
            "web_scan_interval": 60,
            "modbus_restriction": "keep",
            "api_username": "none",
        },
    )
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert await async_get_token_store(hass).async_load_token(HOST) is None
    assert registry.async_get(web_entity) is None
    assert registry.async_get(modbus_entity) is not None


async def test_a_reauth_of_a_running_entry_reports_its_success(
    hass, mock_modbus, monkeypatch
):
    """A reload during the flow aborts it: the owner saw an error for a login that worked."""
    serve_the_public_endpoints(mock_modbus, monkeypatch)
    monkeypatch.setattr(config_flow.FroniusWebClient, "login", lambda self: True)
    monkeypatch.setattr(
        config_flow.FroniusWebClient, "ensure_modbus_enabled", lambda self, *a: True
    )
    monkeypatch.setattr(
        config_flow,
        "mint_token",
        lambda host, user, password: {"realm": "r", "token": "t"},
    )
    entry = make_entry(hass, minor_version=14, api_username="customer")
    await setup_entry(hass, entry)
    (flow,) = _reauth_flows(hass, entry)

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"api_username": "customer"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_password": "secret"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.web_control.configured
