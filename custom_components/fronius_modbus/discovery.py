"""Finding the inverter from what it announces over mDNS."""

from __future__ import annotations

from collections.abc import Mapping
import ipaddress
import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, entry_title, entry_unique_id, instance_key
from .token_store import async_move_tokens, canonical_host

_LOGGER = logging.getLogger(__name__)


def discovered_serial(properties: Mapping[str, Any]) -> str | None:
    """The serial number in the announced JSON; the inverter splits it over numbered keys."""
    parts = sorted((key for key in properties if key.isdigit()), key=int)
    try:
        meta = json.loads("".join(str(properties[key]) for key in parts))
        serial = meta["DeviceMeta"]["Device-Information"]["DeviceSerialNumber"]
    except ValueError, TypeError, KeyError:
        return None
    return str(serial) if serial else None


def discovered_model(name: str) -> str:
    """The instance name without its service type and the serial number it ends in."""
    return name.split("._", 1)[0].rsplit("-", 1)[0]


def entry_for_serial(hass: HomeAssistant, serial: str | None) -> ConfigEntry | None:
    """The entry whose inverter reports this serial number over Modbus."""
    if serial is None:
        return None
    devices = dr.async_get(hass)
    for entry in hass.config_entries.async_entries(DOMAIN):
        inverter = devices.async_get_device(
            identifiers={(DOMAIN, f"{instance_key(entry.entry_id)}_inverter")}
        )
        if inverter is not None and inverter.serial_number == serial:
            return entry
    return None


def _is_ipv4(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        return False


async def async_follow_host(hass: HomeAssistant, entry: ConfigEntry, host: str) -> None:
    """Move an entry set up by IPv4 address to the address its inverter announces now.

    An entry set up by name keeps it: the name resolves the new address. The
    unique id is the host, so it moves too, and the stored token with it.
    """
    values = {**entry.data, **entry.options}
    old_host = str(values.get(CONF_HOST, ""))
    moved = canonical_host(old_host) != canonical_host(host)
    if not (moved and _is_ipv4(old_host) and _is_ipv4(host)):
        return
    unique_id = entry_unique_id({CONF_HOST: host})
    holder = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
    if holder is not None and holder.entry_id != entry.entry_id:
        _LOGGER.warning(
            "The inverter of %s announces an address another entry serves; "
            "the entry is left as it is",
            entry.entry_id,
        )
        return
    await async_move_tokens(hass, old_host, host)
    data = {**entry.data, CONF_HOST: host}
    options = dict(entry.options)
    if CONF_HOST in options:
        options[CONF_HOST] = host
    # A title the owner gave is theirs; only the one made from the address moves
    # with it (reaudit Z-02).
    title = entry.title
    if title == entry_title(values):
        title = entry_title({**data, **options})
    hass.config_entries.async_update_entry(
        entry, data=data, options=options, unique_id=unique_id, title=title
    )
