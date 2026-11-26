"""Which entries may serve one host: only the inverters of one Datamanager."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_DATAMANAGER,
    CONF_INVERTER_UNIT_ID,
    CONF_SITE_OWNER,
    DEFAULT_INVERTER_UNIT_ID,
    DOMAIN,
    entry_unique_id,
    web_api_disabled,
)


def _settings(entry: ConfigEntry) -> dict[str, Any]:
    return {**entry.data, **entry.options}


def _unit(entry: ConfigEntry) -> int:
    return int(_settings(entry).get(CONF_INVERTER_UNIT_ID, DEFAULT_INVERTER_UNIT_ID))


def _host(settings: Mapping[str, Any]) -> str:
    return entry_unique_id({CONF_HOST: settings.get(CONF_HOST, "")})


def host_entries(hass: HomeAssistant, host: str) -> list[ConfigEntry]:
    """Every entry on ``host``, without the ignored discovery cards."""
    wanted = _host({CONF_HOST: host})
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN, include_ignore=False)
        if _host(_settings(entry)) == wanted
    ]


def taken_units(hass: HomeAssistant, host: str) -> set[int]:
    """The inverter units on ``host`` that have an entry."""
    return {_unit(entry) for entry in host_entries(hass, host)}


def is_datamanager_host(hass: HomeAssistant, host: str) -> bool:
    """Whether an entry on ``host`` remembers the device there as a Datamanager."""
    return any(entry.data.get(CONF_DATAMANAGER) for entry in host_entries(hass, host))


@callback
def async_claim_site(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Whether this entry reads what the inverters of its Datamanager share.

    The mark stays with its entry, whatever is added later: another entry would
    go on with the meter under new entities. A deleted owner leaves it to the
    lowest inverter that can be set up.
    """
    if site_owner(hass, str(_settings(entry).get(CONF_HOST, ""))) is not entry:
        return False
    if not entry.data.get(CONF_SITE_OWNER):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SITE_OWNER: True}
        )
    return True


def site_owner(hass: HomeAssistant, host: str) -> ConfigEntry | None:
    """The entry that reads what the inverters of the Datamanager at ``host`` share."""
    entries = host_entries(hass, host)
    marked = [other for other in entries if other.data.get(CONF_SITE_OWNER)]
    able = [other for other in entries if _can_own_a_site(other)]
    candidates = marked or able
    if not candidates:
        return None
    return min(candidates, key=lambda other: (_unit(other), other.entry_id))


def would_own_site(
    hass: HomeAssistant, settings: Mapping[str, Any], entry: ConfigEntry | None
) -> bool:
    """Whether an entry with these settings reads what its Datamanager's inverters share.

    The rule of ``async_claim_site``, for ``entry`` or a new one, marking nothing.
    """
    if entry is not None and entry.data.get(CONF_SITE_OWNER):
        return True
    host = str(settings.get(CONF_HOST, ""))
    others = [other for other in host_entries(hass, host) if other is not entry]
    if any(other.data.get(CONF_SITE_OWNER) for other in others):
        return False
    unit = int(settings.get(CONF_INVERTER_UNIT_ID, DEFAULT_INVERTER_UNIT_ID))
    # Two entries on one host never share a number.
    return all(unit < _unit(other) for other in others if _can_own_a_site(other))


def _can_own_a_site(entry: ConfigEntry) -> bool:
    """An entry with a login that opens no Datamanager is never set up as one."""
    without_login = entry.data.get(CONF_DATAMANAGER) or web_api_disabled(
        _settings(entry)
    )
    return entry.disabled_by is None and bool(without_login)


def _on_datamanager(
    hass: HomeAssistant, settings: Mapping[str, Any], entry: ConfigEntry | None
) -> bool:
    """What the host's entries remember; without one, what this inverter brings.

    The second entry of 0.1.9 is told by the first: it was never asked itself.
    """
    host = str(settings.get(CONF_HOST, ""))
    if host_entries(hass, host):
        return is_datamanager_host(hass, host)
    return bool((settings if entry is None else entry.data).get(CONF_DATAMANAGER))


def unique_id_for(
    hass: HomeAssistant, settings: Mapping[str, Any], entry: ConfigEntry | None = None
) -> str:
    """The unique id of a new inverter with these settings, or of ``entry`` given them."""
    known = {CONF_DATAMANAGER: _on_datamanager(hass, settings, entry)}
    return entry_unique_id({**settings, **known})


def host_holder(
    hass: HomeAssistant, settings: Mapping[str, Any], entry: ConfigEntry | None = None
) -> ConfigEntry | None:
    """The other entry that keeps these settings off their host.

    Matched by its unique id, or by its host and number without one: 0.1.9 set
    none and nothing gives one later. On a Datamanager by the number always: an
    entry from before that was known still has the host alone.
    """
    host = str(settings.get(CONF_HOST, ""))
    unique_id = unique_id_for(hass, settings, entry)
    datamanager = is_datamanager_host(hass, host)
    known = {CONF_DATAMANAGER: datamanager}
    others = host_entries(hass, host)
    by_id = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
    # A new setup takes the place of an ignored discovery card.
    if entry is None and by_id is not None and by_id.source == SOURCE_IGNORE:
        by_id = None
    holder = by_id or next(
        (
            other
            for other in others
            if (other.unique_id is None or datamanager)
            and entry_unique_id({**_settings(other), **known}) == unique_id
        ),
        None,
    )
    if holder is not None:
        return None if holder is entry else holder
    # A new device that is no Datamanager, where the entries remember one.
    if entry is None and datamanager and not settings.get(CONF_DATAMANAGER):
        return others[0]
    return None
