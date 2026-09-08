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
from custom_components.fronius_modbus import migrations
from custom_components.fronius_modbus.const import (
    CONF_RECONFIGURE_REQUIRED,
    DOMAIN,
    entity_prefix,
    instance_key,
)
from custom_components.fronius_modbus.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.fronius_modbus.token_store import async_get_token_store
from custom_components.fronius_modbus.web_control import WebData
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
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


def make_entry(hass, *, minor_version: int = 10) -> MockConfigEntry:
    """A Modbus-only entry (no web token) added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Fronius 192.0.2.10",
        data=ENTRY_DATA,
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
    with pytest.raises(ServiceValidationError):
        await charge_limit.async_set_native_value(1000)


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

    assert entry.minor_version == 11
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


async def test_a_stale_entity_is_removed_after_a_clean_first_poll(hass, mock_modbus):
    """An entity for a key the integration no longer creates is dropped on a clean poll."""
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


async def test_a_meter_that_times_out_at_setup_keeps_its_entities(hass, mock_modbus):
    """Audit F03: the cleanup after a timed-out meter probe removed the meter's registry entries."""
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


async def test_minor_version_10_entries_take_the_role_of_their_stored_token(
    hass, mock_modbus
):
    """Upstream #130: an entry that had a technician token keeps technician access after the migration."""
    entry = make_entry(hass, minor_version=10)
    await async_get_token_store(hass).async_save_token(
        HOST, realm="r", token="t", user="technician"
    )
    assert await migrations.async_migrate_entry(hass, entry)
    assert entry.minor_version == 11
    assert entry.data["api_username"] == "technician"
    assert entry.options["api_username"] == "technician"


async def test_minor_version_10_entries_without_a_technician_token_stay_customer(
    hass, mock_modbus
):
    entry = make_entry(hass, minor_version=10)
    assert await migrations.async_migrate_entry(hass, entry)
    assert entry.minor_version == 11
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
    entry = make_entry(hass)
    await setup_entry(hass, entry)
    removed: list[str] = []
    registry = dr.async_get(hass)
    monkeypatch.setattr(registry, "async_remove_device", removed.append)

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert removed == []
