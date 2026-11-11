"""Switching from redpomodoro 0.1.9 through the Marketplace keeps entity ids and history.

The Marketplace deletes the 0.1.9 entry before the new one is set up; its
deleted rows are taken over.
"""

from modbus_connection import ModbusConnectionError, ModbusTimeoutError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fronius_modbus.const import DOMAIN, entity_prefix, instance_key
from custom_components.fronius_modbus.token_store import async_get_token_store
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntryState
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from .conftest import INVERTER_UNIT_ID, METER_UNIT_ID
from .test_e2e import (
    HOST,
    REDPOMODORO_NEEDS_THE_LOGIN,
    REDPOMODORO_NO_SUCCESSOR,
    _FakeWebClientWithTopology,
    _redpomodoro_entry,
    _redpomodoro_key,
    _rows_that_move,
    log_in_to_the_web_api,
    make_entry,
    setup_entry,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]

OTHER_ENTRY_ID = "01AAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    """Let Home Assistant load custom_components/fronius_modbus in every test here."""
    return enable_custom_integrations


async def _log_in_with_one_meter(hass, mock_modbus, monkeypatch) -> None:
    """A web login whose topology confirms the single meter."""
    await log_in_to_the_web_api(hass, mock_modbus, monkeypatch)
    monkeypatch.setattr(
        _FakeWebClientWithTopology,
        "topology",
        {
            "unit_ids": [METER_UNIT_ID],
            "primary_unit_id": METER_UNIT_ID,
            "locations_by_unit_id": {METER_UNIT_ID: 0},
        },
    )


async def _deleted_0_1_9_entry(hass, monkeypatch=None, *, stopped_half_way=False):
    """A 0.1.9 entry with the owner's changes, deleted: its devices and changes.

    On Home Assistant 2026.7 and later 0.1.9 cannot be loaded, and deleting
    its entry stopped after the devices: the rows kept the id of an entry
    that is gone.
    """
    old, devices = _redpomodoro_entry(hass)
    registry = er.async_get(hass)
    renamed = registry.async_get_entity_id("sensor", DOMAIN, "fm_acpower")
    registry.async_update_entity(renamed, name="My own name")
    area = ar.async_get(hass).async_create("Garage")
    dr.async_get(hass).async_update_device(
        devices["Fronius_inverter"], area_id=area.id, name_by_user="PV roof"
    )
    rows = {
        row.entity_id: row.id
        for row in er.async_entries_for_config_entry(registry, old.entry_id)
    }
    if stopped_half_way:
        with monkeypatch.context() as patch:
            patch.setattr(registry, "async_clear_config_entry", lambda _entry_id: None)
            assert await hass.config_entries.async_remove(old.entry_id)
            await hass.async_block_till_done()
    else:
        assert await hass.config_entries.async_remove(old.entry_id)
        await hass.async_block_till_done()
    return devices, renamed, area.id, rows


def _deleted_rows(hass, entry_id, *unique_ids, platform=DOMAIN):
    """Rows of one entry, then the entry deleted: their keys in the deleted list."""
    entry = MockConfigEntry(domain=platform, entry_id=entry_id)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for number, unique_id in enumerate(unique_ids):
        registry.async_get_or_create(
            "sensor",
            platform,
            unique_id,
            config_entry=entry,
            suggested_object_id=f"row_{number}_{entry_id.lower()}",
        )
    return entry, [("sensor", platform, unique_id) for unique_id in unique_ids]


async def _remove(hass, entry) -> None:
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()


def _untouched(hass, key: tuple[str, str, str]) -> bool:
    """Still deleted under its own id: taken over, it would have left under another."""
    return key in er.async_get(hass).deleted_entities


def _device(hass, entry, suffix: str) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{instance_key(entry.entry_id)}_{suffix}"), entry.entry_id
    )


@pytest.mark.parametrize("stopped_half_way", [False, True], ids=["complete", "half"])
async def test_a_new_entry_takes_over_a_deleted_0_1_9_entry(
    hass, mock_modbus, monkeypatch, stopped_half_way
):
    """Deleting a 0.1.9 entry before the update lost what the migration keeps."""
    devices, _, _, _ = await _deleted_0_1_9_entry(
        hass, monkeypatch, stopped_half_way=stopped_half_way
    )
    # After the delete: it forgets the token no entry uses any more.
    await _log_in_with_one_meter(hass, mock_modbus, monkeypatch)
    new = make_entry(hass)

    await setup_entry(hass, new)

    registry = er.async_get(hass)
    prefix = entity_prefix(new.entry_id)
    rows = _rows_that_move(REDPOMODORO_NO_SUCCESSOR, REDPOMODORO_NEEDS_THE_LOGIN)
    found = {
        f"{row['domain']}.{row['object_id']}": getattr(
            registry.async_get(f"{row['domain']}.{row['object_id']}"), "unique_id", None
        )
        for row in rows
    }
    assert found == {
        f"{row['domain']}.{row['object_id']}": f"{prefix}_{_redpomodoro_key(row['unique_id'])}"
        for row in rows
    }
    device_registry = dr.async_get(hass)
    key = instance_key(new.entry_id)
    assert {
        old: device_registry.async_get(device_id).identifiers
        for old, device_id in devices.items()
    } == {
        "Fronius_inverter": {(DOMAIN, f"{key}_inverter")},
        "Fronius_battery_storage": {(DOMAIN, f"{key}_battery_storage")},
        "Fronius_meter1": {(DOMAIN, f"{key}_meter_200")},
    }


async def test_the_owners_changes_of_a_deleted_0_1_9_entry_are_kept(
    hass, mock_modbus, monkeypatch
):
    """A new setup started over: the owner's names and areas stayed with the deleted rows."""
    devices, renamed, area_id, rows = await _deleted_0_1_9_entry(hass)
    await _log_in_with_one_meter(hass, mock_modbus, monkeypatch)
    new = make_entry(hass)

    await setup_entry(hass, new)

    registry = er.async_get(hass)
    assert registry.async_get(renamed).name == "My own name"
    assert registry.async_get(renamed).id == rows[renamed]
    inverter = _device(hass, new, "inverter")
    assert (inverter.id, inverter.area_id, inverter.name_by_user) == (
        devices["Fronius_inverter"],
        area_id,
        "PV roof",
    )


async def test_a_restart_after_taking_over_changes_no_device(
    hass, mock_modbus, monkeypatch
):
    """The adoption stays noted for later devices; one already there is left as it is."""
    await _deleted_0_1_9_entry(hass)
    await _log_in_with_one_meter(hass, mock_modbus, monkeypatch)
    new = make_entry(hass)
    await setup_entry(hass, new)
    registry = dr.async_get(hass)

    def devices():
        return {
            device.id: device.identifiers
            for device in dr.async_entries_for_config_entry(registry, new.entry_id)
        }

    first = devices()
    assert await hass.config_entries.async_reload(new.entry_id)
    await hass.async_block_till_done()

    assert devices() == first


async def test_devices_are_taken_over_when_the_first_setup_is_retried(
    hass, mock_modbus, monkeypatch
):
    """Noted only in the first attempt, the retry gave the devices new ids."""
    devices, _, area_id, _ = await _deleted_0_1_9_entry(hass)
    await _log_in_with_one_meter(hass, mock_modbus, monkeypatch)
    new = make_entry(hass)
    mock_modbus.fail_requests(INVERTER_UNIT_ID, ModbusConnectionError())
    assert not await hass.config_entries.async_setup(new.entry_id)
    await hass.async_block_till_done()

    mock_modbus.fail_requests(INVERTER_UNIT_ID, None)
    assert await hass.config_entries.async_reload(new.entry_id)
    await hass.async_block_till_done()

    inverter = _device(hass, new, "inverter")
    assert (inverter.id, inverter.area_id) == (devices["Fronius_inverter"], area_id)


async def test_a_device_taken_over_comes_back_as_it_was_after_its_removal(
    hass, mock_modbus, monkeypatch
):
    """Taken over again on its return, it got a new id and lost its area and name.

    Home Assistant restores a device removed from an entry when the entry
    registers it again; a second takeover replaced that record.
    """
    devices, _, area_id, _ = await _deleted_0_1_9_entry(hass)
    await _log_in_with_one_meter(hass, mock_modbus, monkeypatch)
    new = make_entry(hass)
    await setup_entry(hass, new)
    assert await hass.config_entries.async_unload(new.entry_id)
    await hass.async_block_till_done()
    dr.async_get(hass).async_remove_device(devices["Fronius_inverter"])

    await setup_entry(hass, new)

    inverter = _device(hass, new, "inverter")
    assert (inverter.id, inverter.area_id, inverter.name_by_user) == (
        devices["Fronius_inverter"],
        area_id,
        "PV roof",
    )


async def test_a_meter_device_the_platforms_made_stays_once_the_0_1_9_meter_is_known(
    hass, mock_modbus, monkeypatch
):
    """Left to the platforms beside two meters, it collided with the 0.1.9 meter later.

    Once the web API named one meter, every setup tried to give the 0.1.9
    meter's device the identifier the platforms' device already had, and failed.
    """
    await _deleted_0_1_9_entry(hass)
    await log_in_to_the_web_api(hass, mock_modbus, monkeypatch)
    new = make_entry(hass)
    await setup_entry(hass, new)
    meter = _device(hass, new, "meter_200")

    monkeypatch.setattr(
        _FakeWebClientWithTopology,
        "topology",
        {"unit_ids": [200], "primary_unit_id": 200, "locations_by_unit_id": {200: 0}},
    )
    await hass.config_entries.async_reload(new.entry_id)
    await hass.async_block_till_done()

    assert new.state is ConfigEntryState.LOADED
    assert _device(hass, new, "meter_200").id == meter.id


async def test_a_deleted_0_1_9_meter_is_not_guessed_among_two(
    hass, mock_modbus, monkeypatch
):
    """Its address went with the deleted entry; the primary meter may be the wrong one."""
    devices, _, _, _ = await _deleted_0_1_9_entry(hass)
    await log_in_to_the_web_api(hass, mock_modbus, monkeypatch)
    new = make_entry(hass)

    await setup_entry(hass, new)

    assert set(new.runtime_data.device.meters) == {200, 201}
    assert _untouched(hass, ("sensor", DOMAIN, "fm_m1_power"))
    assert dr.async_get(hass).async_get(devices["Fronius_meter1"]) is None


@pytest.mark.parametrize("web_api", ["second_meter_silent", "no_web_client"])
async def test_a_deleted_0_1_9_meter_waits_for_a_confirmed_single_meter(
    hass, mock_modbus, monkeypatch, web_api
):
    """One meter answering, or the default without a web client, read as a one-meter site.

    The 0.1.9 meter then joined the history of meter 200, though it may have
    been the one that did not answer.
    """
    devices, _, _, _ = await _deleted_0_1_9_entry(hass)
    if web_api == "second_meter_silent":
        await log_in_to_the_web_api(hass, mock_modbus, monkeypatch)
        mock_modbus.fail_requests(201, ModbusTimeoutError())
    new = make_entry(hass)

    await setup_entry(hass, new)

    assert set(new.runtime_data.device.meters) == {METER_UNIT_ID}
    assert _untouched(hass, ("sensor", DOMAIN, "fm_m1_power"))
    assert dr.async_get(hass).async_get(devices["Fronius_meter1"]) is None


async def test_a_deleted_0_1_9_meter_waits_while_the_one_meter_is_silent(
    hass, mock_modbus, monkeypatch
):
    """The web API names the meter, but only its answer puts it on the bus."""
    devices, _, _, _ = await _deleted_0_1_9_entry(hass)
    await _log_in_with_one_meter(hass, mock_modbus, monkeypatch)
    mock_modbus.fail_requests(METER_UNIT_ID, ModbusTimeoutError())
    new = make_entry(hass)

    await setup_entry(hass, new)

    assert new.runtime_data.device.meters == {}
    assert _untouched(hass, ("sensor", DOMAIN, "fm_m1_power"))
    assert dr.async_get(hass).async_get(devices["Fronius_meter1"]) is None


async def test_a_string_dark_at_the_new_setup_keeps_its_sensor(
    hass, mock_modbus, monkeypatch, symo_gen24
):
    """Set up again at night: the dark string's sensor counted as never there and stayed deleted."""
    await _log_in_with_one_meter(hass, mock_modbus, monkeypatch)
    probe = make_entry(hass)
    await setup_entry(hass, probe)
    address = probe.runtime_data.device.mppt_module(0).resolved_fields["dcw"].address
    await _remove(hass, probe)
    registry = er.async_get(hass)
    # The probe only finds the register; its rows are no origin here.
    for key in [
        key
        for key, row in registry.deleted_entities.items()
        if row.unique_id.startswith(entity_prefix(probe.entry_id))
    ]:
        del registry.deleted_entities[key]
    await _deleted_0_1_9_entry(hass)
    row_id = registry.deleted_entities[("sensor", DOMAIN, "fm_mppt1_power")].id
    await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    symo_gen24[INVERTER_UNIT_ID]["holding"][address] = 0xFFFF
    new = make_entry(hass)

    await setup_entry(hass, new)

    rows = {
        row.id: row for row in er.async_entries_for_config_entry(registry, new.entry_id)
    }
    assert rows[row_id].unique_id == (
        f"{entity_prefix(new.entry_id)}_mppt_module_0_dc_power"
    )


async def test_a_deleted_row_with_no_entity_now_stays_deleted(
    hass, mock_modbus, monkeypatch
):
    """It came back as an unavailable entity until a cleanup with a login removed it again."""
    old, (matching, unknown) = _deleted_rows(
        hass, OTHER_ENTRY_ID, "fm_PhVphA", "fm_no_such_sensor"
    )
    await _remove(hass, old)
    new = make_entry(hass)

    await setup_entry(hass, new)

    assert not _untouched(hass, matching)
    assert _untouched(hass, unknown)


async def test_rows_of_another_integration_count_for_nothing(hass, mock_modbus):
    """Only this integration's deleted rows are an origin."""
    foreign, (foreign_key,) = _deleted_rows(
        hass, "01FFFFFFFFFFFFFFFFFFFFFFFF", "fm_PhVphB", platform="other"
    )
    await _remove(hass, foreign)
    old, (key,) = _deleted_rows(hass, OTHER_ENTRY_ID, "fm_PhVphA")
    await _remove(hass, old)
    new = make_entry(hass)

    await setup_entry(hass, new)

    assert not _untouched(hass, key)
    assert _untouched(hass, foreign_key)


async def test_nothing_is_taken_over_from_two_deleted_entries(hass, mock_modbus):
    """With two inverters the first new entry took over the other one's rows.

    The Marketplace deletes every entry; which one an origin was cannot be told.
    """
    first, (first_key,) = _deleted_rows(hass, OTHER_ENTRY_ID, "fm_PhVphA")
    second, (second_key,) = _deleted_rows(
        hass, "01BBBBBBBBBBBBBBBBBBBBBBBB", "fm_PhVphB"
    )
    for entry in (first, second):
        await _remove(hass, entry)
    new = make_entry(hass)

    await setup_entry(hass, new)

    assert _untouched(hass, first_key) and _untouched(hass, second_key)


@pytest.mark.parametrize(
    "unique_id",
    [f"{entity_prefix(OTHER_ENTRY_ID)}_PhVphA", "fm_fronius__PhVphA"],
    ids=["per entry", "named after the entry"],
)
async def test_rows_of_a_later_version_are_not_taken_over(hass, mock_modbus, unique_id):
    """Only a switch from 0.1.9 is taken over; a later entry's rows stay deleted."""
    old, (key,) = _deleted_rows(hass, OTHER_ENTRY_ID, unique_id)
    await _remove(hass, old)
    new = make_entry(hass)

    await setup_entry(hass, new)

    assert _untouched(hass, key)


async def test_nothing_is_taken_over_beside_another_entry(hass, mock_modbus):
    """With two inverters the deleted rows may belong to the other one."""
    old, (key,) = _deleted_rows(hass, OTHER_ENTRY_ID, "fm_PhVphA")
    await _remove(hass, old)
    MockConfigEntry(domain=DOMAIN, entry_id="01CCCCCCCCCCCCCCCCCCCCCCCC").add_to_hass(
        hass
    )
    new = make_entry(hass)

    await setup_entry(hass, new)

    assert _untouched(hass, key)


async def test_an_entry_already_set_up_takes_nothing_over(hass, mock_modbus):
    """Deleting a second inverter's entry must not hand its rows to the first."""
    kept = make_entry(hass)
    await setup_entry(hass, kept)
    old, (key,) = _deleted_rows(hass, OTHER_ENTRY_ID, "fm_PhVphA")
    await _remove(hass, old)

    assert await hass.config_entries.async_reload(kept.entry_id)
    await hass.async_block_till_done()

    assert _untouched(hass, key)


async def test_an_ignored_discovery_does_not_stop_the_takeover(hass, mock_modbus):
    """An ignored discovery card is no second inverter."""
    MockConfigEntry(
        domain=DOMAIN, source=SOURCE_IGNORE, unique_id="192.0.2.99"
    ).add_to_hass(hass)
    old, (key,) = _deleted_rows(hass, OTHER_ENTRY_ID, "fm_PhVphA")
    await _remove(hass, old)
    new = make_entry(hass)

    await setup_entry(hass, new)

    assert not _untouched(hass, key)
