"""Finding the inverter from what it announces over mDNS."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import ipaddress
import json
import logging
from typing import Any

from modbus_connection import ModbusError, ModbusTcpParams

from homeassistant.components.modbus import async_get_temporary_unit
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_INVERTER_UNIT_ID,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_PORT,
    DOMAIN,
    entry_title,
    entry_unique_id,
    instance_key,
)
from .fronius_modbus_api.device import FroniusInverter
from .token_store import async_get_token_store, canonical_host

_LOGGER = logging.getLogger(__name__)

# The announcement comes once: a new address that does not answer yet, or
# answers a read with a timeout, is read again for a few minutes before the
# move is given up (reaudit 1cd9c57 P2-01).
MOVE_READ_ATTEMPTS = 6
MOVE_READ_RETRY_SECONDS = 30


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
        inverter = devices.async_get_device_by_identifier(
            (DOMAIN, f"{instance_key(entry.entry_id)}_inverter"), entry.entry_id
        )
        if inverter is not None and inverter.serial_number == serial:
            return entry
    return None


async def async_serial_at(
    hass: HomeAssistant, host: str, port: int, unit_id: int
) -> str | None:
    """The serial number the inverter at ``host`` reports now; None if none answers."""
    try:
        async with async_get_temporary_unit(
            hass, ModbusTcpParams(host=host, port=port), unit_id
        ) as unit:
            identity = await FroniusInverter.async_probe(unit)
    except (ModbusError, HomeAssistantError, OSError, TimeoutError) as err:
        _LOGGER.debug("No inverter read for an announced move: %s", err)
        return None
    return identity.serial


def _is_ipv4(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        return False


def _endpoint(entry: ConfigEntry) -> tuple[str, int, int]:
    """The host, port and inverter unit the entry reads the inverter at."""
    values = {**entry.data, **entry.options}
    return (
        str(values.get(CONF_HOST, "")),
        int(values.get(CONF_PORT, DEFAULT_PORT)),
        int(values.get(CONF_INVERTER_UNIT_ID, DEFAULT_INVERTER_UNIT_ID)),
    )


async def _async_inverter_moved(
    hass: HomeAssistant, endpoint: tuple[str, int, int], host: str, serial: str
) -> bool:
    """Whether the inverter answers at ``host`` now and no longer at its old address.

    The serial number goes out in every announcement, so the announcement alone
    proves nothing. Read now, not taken from the last poll: that one may predate
    the move, or come from another inverter given the old address (reaudit
    b58ccec P2-01, P2-02).
    """
    old_host, port, unit_id = endpoint
    for attempt in range(MOVE_READ_ATTEMPTS):
        if attempt:
            await asyncio.sleep(MOVE_READ_RETRY_SECONDS)
        if await async_serial_at(hass, old_host, port, unit_id) == serial:
            return False
        announced = await async_serial_at(hass, host, port, unit_id)
        if announced is not None:
            return announced == serial
    return False


async def async_follow_host(
    hass: HomeAssistant, entry: ConfigEntry, host: str, serial: str
) -> None:
    """Move an entry set up by IPv4 address to the address its inverter announces now.

    An entry set up by name keeps it: the name resolves the new address. The
    unique id is the host, so it moves too, and the stored token with it.
    """
    endpoint = _endpoint(entry)
    old_host = endpoint[0]
    moved = canonical_host(old_host) != canonical_host(host)
    if not (moved and _is_ipv4(old_host) and _is_ipv4(host)):
        return
    if not await _async_inverter_moved(hass, endpoint, host, serial):
        return
    token_store = async_get_token_store(hass)
    # From the ownership check to the entry update nothing may await: another
    # flow took the address in between (audit R3B-01, R3B-02).
    await token_store.async_ready()
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        return
    # The reads awaited: a concurrent announcement or reconfiguration may have
    # changed the endpoint they checked (reaudit 1cd9c57 P2-02).
    if _endpoint(entry) != endpoint:
        return
    values = {**entry.data, **entry.options}
    unique_id = entry_unique_id({CONF_HOST: host})
    holder = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
    if holder is not None and holder.entry_id != entry.entry_id:
        _LOGGER.warning(
            "The inverter of %s announces an address another entry serves; "
            "the entry is left as it is",
            entry.entry_id,
        )
        return
    token_store.move_tokens(old_host, host)
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
