"""An entry takes over what a deleted entry of an old version left behind.

Switching from redpomodoro to this repository through the Marketplace deletes
the 0.1.9 entry before the new one is set up. Home Assistant keeps the deleted
entities for 30 days and restores a row, with its entity id, name and area,
when an entity with the same unique id is created again; the new entry builds
other ids, so the migrations of 0.1.9 found nothing to move.

Deleting an entry whose version cannot be loaded fails. Its entities can then be
gone while the entry itself is back after a restart.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import logging
import re
from typing import Any

from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN, ENTITY_PREFIX, entity_prefix
from .entities import expected_unique_ids
from .entity import expected_device_identifiers
from .migrations import (
    V019_METER_UNIT_ID,
    V019_METER_UNKNOWN,
    resolve_v019_meter,
    successor_key,
    v019_device_moves,
)

_LOGGER = logging.getLogger(__name__)
# A unique id of a later version: per entry, fm_<instance key>_<key> with a
# ULID or, before Home Assistant 2024.8, a UUID as the entry id; or named
# after the entry, fm_<name>__<key>. 0.1.9 wrote fm_<key>.
_LATER_UNIQUE_ID = re.compile(
    rf"^{ENTITY_PREFIX}_(?:(?:[0-9a-z]{{26}}|[0-9a-f]{{32}})_(?!_)|.+__)"
)
# Kept for the devices: a retried setup or a device found only later still
# needs to know that the entry took over 0.1.9's.
ADOPTED_V019 = "_adopted_v019"
# The devices already taken over: one retired or deleted later comes back
# under its own identifier, as Home Assistant restores any device.
ADOPTED_DEVICES = "_adopted_devices"


def _left_behind(hass: HomeAssistant, row: er.DeletedRegistryEntry) -> bool:
    """A row whose entry is gone.

    Home Assistant clears the entry id once the removal is through; a removal
    that stopped after the devices, as with 0.1.9 on 2026.7 and later, which
    cannot load, left the id of an entry that no longer exists.
    """
    return (
        row.config_entry_id is None
        or hass.config_entries.async_get_entry(row.config_entry_id) is None
    )


def _own_rows(
    registry: er.EntityRegistry, entry: ConfigEntry
) -> list[er.DeletedRegistryEntry]:
    """The rows of an old version deleted under this entry's own id.

    A removal that fails is saved only with the next change of any entry. A
    restart before that brings the entry back, but not the rows cleaned up with
    its devices. Home Assistant restores an id of today's format by itself.
    """
    current = f"{entity_prefix(entry.entry_id)}_"
    return [
        row
        for row in registry.deleted_entities.values()
        if row.platform == DOMAIN
        and row.config_entry_id == entry.entry_id
        and not row.unique_id.startswith(current)
    ]


def _is_the_only_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """With a second entry the rows left behind may be the other inverter's."""
    entries = [
        candidate
        for candidate in hass.config_entries.async_entries(DOMAIN)
        if candidate.source != SOURCE_IGNORE
    ]
    return entries == [entry]


def _deleted_v019_rows(
    hass: HomeAssistant, registry: er.EntityRegistry
) -> list[er.DeletedRegistryEntry]:
    """The rows of the one deleted entry, if it was a 0.1.9 entry.

    Home Assistant orphans all deleted rows of an entry at once, with one time;
    a removal that stopped half way left them under the id of its entry. Of
    two deleted entries either may be this inverter's, so neither is taken.
    """
    removals: dict[object, list[er.DeletedRegistryEntry]] = {}
    for row in list(registry.deleted_entities.values()):
        if row.platform == DOMAIN and _left_behind(hass, row):
            removal = row.config_entry_id or row.orphaned_timestamp
            removals.setdefault(removal, []).append(row)
    if len(removals) != 1:
        if removals:
            _LOGGER.info(
                "Deleted entities of %s earlier entries; taking over none",
                len(removals),
            )
        return []
    (rows,) = removals.values()
    if any(_LATER_UNIQUE_ID.match(row.unique_id) for row in rows):
        return []
    return rows


@callback
def async_adopt_deleted_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Take over the rows an old entry left, before this entry has any.

    Under their old ids: the migrations that run next move them, with the
    statistics of the strings. Rows under this entry's own id are certain, so
    all of them are taken and treated as if they had never been deleted. Of a
    deleted 0.1.9 entry only this runtime's entities are; the rest stays
    deleted, for Home Assistant to purge.
    """
    registry = er.async_get(hass)
    # An entry already set up would take the rows of one deleted beside it.
    if er.async_entries_for_config_entry(registry, entry.entry_id):
        return
    if own_rows := _own_rows(registry, entry):
        _take_over(
            hass,
            entry,
            own_rows,
            _keys(entry, own_rows, entry.data.get(V019_METER_UNIT_ID)),
            entry.data,
        )
        return
    if not _is_the_only_entry(hass, entry):
        return
    # The address 0.1.9 read went with its entry.
    data = {**entry.data, V019_METER_UNIT_ID: V019_METER_UNKNOWN}
    rows = _deleted_v019_rows(hass, registry)
    keys = _keys(entry, rows, V019_METER_UNKNOWN)
    # Taken over, a row counts as registered: a sensor dark at this first
    # start, a string at night, is expected once registered.
    runtime = entry.runtime_data
    widened = replace(
        runtime, registered_keys=runtime.registered_keys | set(keys.values())
    )
    expected = expected_unique_ids(entry, widened)
    prefix = entity_prefix(entry.entry_id)
    # Up to 0.3.0 a sensor and a control shared one unique id.
    chosen = [row for row in rows if f"{prefix}_{keys[row.unique_id]}" in expected]
    if chosen:
        _take_over(hass, entry, chosen, keys, data)


def _keys(
    entry: ConfigEntry, rows: list[er.DeletedRegistryEntry], meter_setting: Any
) -> dict[str, str]:
    """The keys the old ids of these rows continue under."""
    runtime = entry.runtime_data
    meter = resolve_v019_meter(
        meter_setting,
        runtime.device.meters,
        runtime.primary_meter_unit_id,
        runtime.reported_meter_unit_ids,
    )
    return {row.unique_id: successor_key(entry, row.unique_id, meter) for row in rows}


def _take_over(
    hass: HomeAssistant,
    entry: ConfigEntry,
    rows: list[er.DeletedRegistryEntry],
    keys: dict[str, str],
    data: Mapping[str, Any],
) -> None:
    """Bring the rows back for this entry, and note it for the devices."""
    registry = er.async_get(hass)
    for row in rows:
        registry.async_get_or_create(
            row.domain, DOMAIN, row.unique_id, config_entry=entry
        )
    runtime = entry.runtime_data
    entry.runtime_data = replace(
        runtime,
        registered_keys=runtime.registered_keys | {keys[row.unique_id] for row in rows},
    )
    hass.config_entries.async_update_entry(entry, data={**data, ADOPTED_V019: True})
    _LOGGER.info("Took over %s entities of an earlier version", len(rows))


@callback
def async_adopt_deleted_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Take over the devices of the 0.1.9 entry whose entities were taken over.

    Asked for by their old identifiers, only for the devices this runtime
    registers: Home Assistant restores a deleted one with its area and name,
    and anything else is the device the platforms would create anyway. The
    0.1.9 migration that runs next gives them this entry's identifiers.
    """
    if not entry.data.get(ADOPTED_V019):
        return
    current = expected_device_identifiers(entry.runtime_data, entry)
    done = set(entry.data.get(ADOPTED_DEVICES, ()))
    pending = {
        old: new
        for old, new in v019_device_moves(entry).items()
        if new in current and new not in done
    }
    if not pending:
        return
    registry = dr.async_get(hass)
    for old, new in pending.items():
        # Made by the platforms while the 0.1.9 meter was not certain: the old
        # device could not take its identifier and would stay beside it, empty,
        # until a cleanup.
        if registry.async_get_device_by_identifier((DOMAIN, new), entry.entry_id):
            continue
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, old)}
        )
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, ADOPTED_DEVICES: sorted(done | set(pending.values()))},
    )
