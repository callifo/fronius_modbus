"""How a finished flow lands in its entry: the host it claims, the update, the reload."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from .const import CONF_API_PASSWORD, DOMAIN, entry_title_after, entry_unique_id
from .flow_settings import _entry_payload
from .flow_state import _AlreadyConfigured
from .token_store import async_forget_unused_tokens


def _claim_host(
    hass: HomeAssistant, entry: config_entries.ConfigEntry, settings: dict[str, Any]
) -> str:
    """The unique id for ``settings``' host, unless another entry already holds it.

    The unique id is the host; a reconfigure or options change that moves the
    host has to move the id with it, or duplicate detection keeps guarding the
    old host and lets a second entry for the new one through (audit F12).
    """
    unique_id = entry_unique_id(settings)
    holder = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
    if holder is not None and holder.entry_id != entry.entry_id:
        raise _AlreadyConfigured
    return unique_id


async def async_update_entry_from_input(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    validated_input: dict[str, Any],
    *,
    previous_host: str | None = None,
) -> None:
    """Write a reconfigure or reauth into the entry, and reload it once."""
    unique_id = _claim_host(hass, entry, validated_input)
    updated_payload = _entry_payload(validated_input, reconfigure_required=False)
    new_data = {**entry.data, **updated_payload}
    new_options = {**entry.options, **updated_payload}
    new_data.pop(CONF_API_PASSWORD, None)
    new_options.pop(CONF_API_PASSWORD, None)
    new_data.pop("meter_modbus_unit_id", None)
    new_options.pop("meter_modbus_unit_id", None)
    new_data.pop("meter_modbus_unit_ids", None)
    new_options.pop("meter_modbus_unit_ids", None)
    # Read before the update: its listener starts the reload right away.
    loaded = entry.state is config_entries.ConfigEntryState.LOADED
    changed = hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        title=entry_title_after(
            entry.title, {**entry.data, **entry.options}, validated_input
        ),
        unique_id=unique_id,
    )
    if previous_host:
        await async_forget_unused_tokens(hass, previous_host)
    await _async_reload_unless_listened(hass, entry, changed=changed, loaded=loaded)


async def _async_reload_unless_listened(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    *,
    changed: bool,
    loaded: bool,
) -> None:
    """Reload the entry unless its update listener already does.

    A change to a loaded entry reloads it through that listener; a second call
    here reloaded it twice (audit F24-10). A new token alone changes nothing in
    the entry, and an entry that is not loaded has no listener: both need it.
    """
    if changed and loaded:
        return
    await hass.config_entries.async_reload(entry.entry_id)
