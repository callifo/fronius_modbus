"""Telling an older inverter behind a Datamanager from the others.

A Datamanager has a web interface of its own: other logins and other paths
than a GEN24.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, replace
from enum import StrEnum
from http import HTTPStatus
from typing import Any

from modbus_connection import ModbusConnectionError, ModbusError, ModbusTcpParams

from homeassistant.components.modbus import async_get_unit
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_DATAMANAGER,
    CONF_DATAMANAGER_INVERTERS,
    DOMAIN,
    UNIT_OF_NUMBER_0,
    entry_unique_id,
    web_api_disabled,
)
from .derived import METER_LOCATION_CONSUMPTION, METER_LOCATION_FEED_IN
from .fronius_modbus_api.device import FroniusInverter
from .froniuswebclient import _base_url, _http
from .host_sharing import async_claim_site, host_entries, site_owner
from .meter_topology import MeterTopology
from .web_errors import FroniusWebUnreachable

# Answers without a login. A GEN24 refuses it in the body, with HTTP 200.
LOGGER_INFO_PATH = "/solar_api/v1/GetLoggerInfo.cgi"
# Answers without a login on a GEN24, also with its Solar API off; a
# Datamanager does not have it.
GEN24_PATH = "/api/status/common"
# A GEN24 answers GetLoggerInfo with this code: it has no such request.
GEN24_REFUSAL_CODE = 11
# Seen are fronius-datamanager-card, whose box is expected to start the same way,
# and fronius-hybrid on a Symo Hybrid, whose manager has another name.
DATAMANAGER_PRODUCT_PREFIXES = ("fronius-datamanager", "fronius-hybrid")
METERS_PATH = "/solar_api/v1/GetMeterRealtimeData.cgi?Scope=System"
INVERTERS_PATH = "/solar_api/v1/GetInverterInfo.cgi"
# The fixed Modbus units of its meters, for the meter addresses 1 to 5
# (Fronius' manual 42,0410,2049).
METER_UNIT_IDS = range(240, 245)
HIGHEST_INVERTER_NUMBER = 99
# Seconds to connect and to answer. A Datamanager was logged with 3.2 s for one
# of these answers.
SOLAR_API_TIMEOUT = (4.0, 10.0)
# What the Solar API shows for a meter without a serial number.
NO_SERIAL = "n.a."


class DeviceFamily(StrEnum):
    """Which web interface the device at an address has."""

    DATAMANAGER = "datamanager"
    OTHER = "other"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SolarApiMeter:
    """A meter the Solar API lists: its serial number and where it is installed."""

    serial: str
    location: int | None


@dataclass(frozen=True)
class Site:
    """What the Solar API says about a Datamanager's meters and inverters."""

    meters: tuple[SolarApiMeter, ...]
    # 0 when the inverters could not be read.
    inverters: int

    @property
    def has_grid_meter(self) -> bool:
        """Whether a meter is listed where the house meets the grid."""
        return any(meter.location == METER_LOCATION_FEED_IN for meter in self.meters)


def _family(payload: Any) -> DeviceFamily:
    """The family a GetLoggerInfo body shows.

    Only a product name, or the GEN24's refusal, tells: another error or an
    answer without a product proves no other device.
    """
    head = payload.get("Head") if isinstance(payload, dict) else None
    if not isinstance(head, dict):
        return DeviceFamily.UNKNOWN
    status = head.get("Status")
    if isinstance(status, dict) and status.get("Code") == GEN24_REFUSAL_CODE:
        return DeviceFamily.OTHER
    body = payload.get("Body")
    logger_info = body.get("LoggerInfo") if isinstance(body, dict) else None
    product = logger_info.get("ProductID") if isinstance(logger_info, dict) else None
    if not isinstance(product, str) or not product.strip():
        return DeviceFamily.UNKNOWN
    if product.startswith(DATAMANAGER_PRODUCT_PREFIXES):
        return DeviceFamily.DATAMANAGER
    return DeviceFamily.OTHER


async def async_identify(hass: HomeAssistant, host: str) -> DeviceFamily:
    """Ask the device at ``host`` without blocking the event loop."""
    return await hass.async_add_executor_job(identify, host)


def _read_json(host: str, path: str) -> Any:
    """What the device answers on ``path``; None unless it is JSON with HTTP 200."""
    response = _http("get", f"{_base_url(host)}{path}", timeout=SOLAR_API_TIMEOUT)
    if response.status_code != HTTPStatus.OK:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def identify(host: str) -> DeviceFamily:
    """The family of the device at ``host``; UNKNOWN without a clear answer."""
    try:
        family = _family(_read_json(host, LOGGER_INFO_PATH))
        if family is not DeviceFamily.UNKNOWN:
            return family
        is_gen24 = isinstance(_read_json(host, GEN24_PATH), dict)
    except FroniusWebUnreachable:
        return DeviceFamily.UNKNOWN
    return DeviceFamily.OTHER if is_gen24 else DeviceFamily.UNKNOWN


async def async_on_datamanager(
    hass: HomeAssistant, entry: ConfigEntry, host: str
) -> bool:
    """Whether the entry's device is a Datamanager, remembered from its last clear answer.

    A Datamanager can be asleep when Home Assistant starts. An entry with a role
    is not asked: the flow that took the role did.
    """
    remembered = entry.data.get(CONF_DATAMANAGER)
    if not web_api_disabled({**entry.data, **entry.options}):
        _follow_with_the_unique_id(hass, entry, bool(remembered))
        return bool(remembered)
    family = await async_identify(hass, host)
    if family is DeviceFamily.UNKNOWN:
        return bool(remembered)
    found = family is DeviceFamily.DATAMANAGER
    # A missing key means no already; most entries never get one.
    if found != bool(remembered):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DATAMANAGER: found}
        )
    _follow_with_the_unique_id(hass, entry, found)
    return found


def _follow_with_the_unique_id(
    hass: HomeAssistant, entry: ConfigEntry, found: bool
) -> None:
    """Add an inverter's number to its unique id, or take it off again.

    Only where the entry has the one of the other device: an entry without a
    unique id keeps none, and a taken one is left to its entry.
    """
    settings = {**entry.data, **entry.options}
    before = entry_unique_id({**settings, CONF_DATAMANAGER: not found})
    after = entry_unique_id({**settings, CONF_DATAMANAGER: found})
    if entry.unique_id != before or before == after:
        return
    if not hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, after):
        hass.config_entries.async_update_entry(entry, unique_id=after)


def _data(payload: Any) -> Any:
    """The data of a Solar API answer; None when it is no such answer."""
    body = payload.get("Body") if isinstance(payload, dict) else None
    return body.get("Data") if isinstance(body, dict) else None


def _location(value: Any) -> int | None:
    """Where a meter is installed, as Fronius numbers it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # A number that is not a whole one, NaN included, names no place.
    return int(value) if value >= 0 and value % 1 == 0 else None


def _solar_api_meter(value: Any) -> SolarApiMeter | None:
    """A meter that can be matched to a Modbus unit."""
    details = value.get("Details") if isinstance(value, dict) else None
    serial = details.get("Serial") if isinstance(details, dict) else None
    # A pulse counter on an inverter's input has no serial number, and no unit.
    if not isinstance(serial, str) or serial.strip() in ("", NO_SERIAL):
        return None
    if value.get("Enable") == 0:
        return None
    return SolarApiMeter(serial.strip(), _location(value.get("Meter_Location_Current")))


def _inverter_numbers(host: str) -> list[str]:
    """The numbers of the inverters the Solar API lists."""
    listed = _data(_read_json(host, INVERTERS_PATH))
    return list(listed) if isinstance(listed, dict) else []


def unit_of(number: str) -> int | None:
    """The Modbus unit of the inverter with this number; None where there is none.

    Number 00 is on a unit of its own, 0 being every device's (Fronius' manual
    42,0410,2049). The Solar API's listing can name another device too, as
    240 in one published answer: above the highest number, it is no inverter.
    """
    if not (number.isascii() and number.isdigit()):
        return None
    value = int(number)
    if value > HIGHEST_INVERTER_NUMBER:
        return None
    return value or UNIT_OF_NUMBER_0


def read_inverter_units(host: str) -> tuple[int, ...]:
    """The Modbus units of the inverters the Solar API lists; none without an answer."""
    try:
        numbers = _inverter_numbers(host)
    except FroniusWebUnreachable:
        return ()
    units = {unit_of(number) for number in numbers}
    return tuple(sorted(unit for unit in units if unit is not None))


def read_site(host: str) -> Site | None:
    """The meters and inverters the Solar API lists; None without an answer."""
    try:
        meters = _read_json(host, METERS_PATH)
        if meters is None:
            return None
        inverters = _inverter_numbers(host)
    except FroniusWebUnreachable:
        return None
    listed = _data(meters)
    return Site(
        meters=tuple(
            meter
            for value in (listed.values() if isinstance(listed, dict) else ())
            if (meter := _solar_api_meter(value)) is not None
        ),
        inverters=len(inverters),
    )


def _locations(found: dict[int, str], site: Site) -> dict[int, int]:
    """Where the found meters are installed; a meter without a match is left out."""
    by_serial = {meter.serial: meter.location for meter in site.meters}
    if len(found) == 1 and len(site.meters) == 1:
        matched = dict.fromkeys(found, site.meters[0].location)
    else:
        matched = {unit: by_serial.get(serial) for unit, serial in found.items()}
    return {
        unit: location for unit, location in matched.items() if location is not None
    }


def meter_topology(
    found: dict[int, str], site: Site | None, open_units: Collection[int] = ()
) -> MeterTopology:
    """The meters of a Datamanager, from the units that answered and the Solar API.

    The first unit is always listed, and so are the units that gave no clear
    answer: a meter silent now is asked again with every poll. Never confirmed:
    no answer does not prove that a meter is gone.
    """
    locations = _locations(found, site) if site is not None else {}
    grid_meters = [
        unit for unit in sorted(locations) if locations[unit] == METER_LOCATION_FEED_IN
    ]
    consumption_meters = [
        unit
        for unit in sorted(locations)
        if locations[unit] == METER_LOCATION_CONSUMPTION
    ]
    leading = [*grid_meters, *consumption_meters, *sorted(found)]
    primary = next(iter(leading), METER_UNIT_IDS[0])
    if site is not None and site.inverters != 1:
        # The load adds the one inverter read here to the grid meter's power.
        for unit in grid_meters:
            del locations[unit]
    if site is not None and site.has_grid_meter and not grid_meters:
        # Its power would pass for the whole load while the grid meter is silent.
        locations.pop(primary, None)
    unit_ids = sorted({METER_UNIT_IDS[0], *found, *open_units})
    return MeterTopology(
        unit_ids=unit_ids,
        primary_unit_id=primary,
        locations=locations,
        confirmed=False,
        several_inverters=site is not None and site.inverters > 1,
        # The first unit too: it is listed whether its meter answered or not.
        open_unit_ids=tuple(unit for unit in unit_ids if unit not in found),
    )


async def async_meter_topology(
    hass: HomeAssistant, entry: ConfigEntry, params: ModbusTcpParams
) -> MeterTopology:
    """Look for the meters of the Datamanager at the entry's address.

    No meter when another entry reads them.
    """
    if not async_claim_site(hass, entry):
        _async_tell_the_owner(hass, params.host)
        # Nothing is asked, so nothing is known to be gone.
        return MeterTopology(
            [], METER_UNIT_IDS[0], {}, confirmed=False, several_inverters=True
        )
    entries = len(host_entries(hass, params.host))
    found, open_units, link_lost = await _async_look_for_meters(hass, entry, params)
    site = None
    if not link_lost:
        site = await hass.async_add_executor_job(read_site, params.host)
    if site is not None:
        known = _known_inverters(hass, entry, site.inverters)
        # Unread inverters stay unknown beside a single entry.
        site = replace(site, inverters=max(known, entries) if entries > 1 else known)
    topology = meter_topology(found, site, open_units)
    # Without the Solar API's answer even a meter found has no place yet.
    unplaced = topology.unit_ids if link_lost else topology.open_unit_ids
    return replace(
        topology,
        several_inverters=topology.several_inverters or entries > 1,
        open_unit_ids=tuple(unplaced),
    )


async def _async_look_for_meters(
    hass: HomeAssistant, entry: ConfigEntry, params: ModbusTcpParams
) -> tuple[dict[int, str], set[int], bool]:
    """The serials of the meters that answered, the units left open, and a lost link."""
    found: dict[int, str] = {}
    open_units: set[int] = set()
    for position, unit_id in enumerate(METER_UNIT_IDS):
        unit = async_get_unit(hass, entry, params, unit_id)
        try:
            identity = await FroniusInverter.async_probe_meter(unit)
        except ModbusConnectionError:
            # The first poll reports it and has the setup tried again.
            open_units.update(METER_UNIT_IDS[position:])
            return found, open_units, True
        except ModbusError:
            open_units.add(unit_id)
            continue
        if identity is not None:
            found[unit_id] = identity.serial.strip()
    return found, open_units, False


@callback
def _async_tell_the_owner(hass: HomeAssistant, host: str) -> None:
    """Restart the owner that counted its own inverter alone into the load."""
    owner = site_owner(hass, host)
    if owner is None or owner.state is not ConfigEntryState.LOADED:
        return
    if not owner.runtime_data.several_inverters:
        hass.config_entries.async_schedule_reload(owner.entry_id)


@callback
def async_recount_the_site(hass: HomeAssistant, entry: ConfigEntry, host: str) -> None:
    """Restart the owner when an entry was set up during its own setup.

    That entry found the owner still starting, so nothing told it.
    """
    if not entry.data.get(CONF_DATAMANAGER) or site_owner(hass, host) is not entry:
        return
    if entry.runtime_data.several_inverters:
        return
    if len(host_entries(hass, host)) > 1:
        hass.config_entries.async_schedule_reload(entry.entry_id)


def _known_inverters(hass: HomeAssistant, entry: ConfigEntry, listed: int) -> int:
    """The most inverters ever listed: one asleep at the start is left out.

    A removed one is not noticed; the load then stays unknown, never wrong.
    """
    known = int(entry.data.get(CONF_DATAMANAGER_INVERTERS, 0))
    if listed > known:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DATAMANAGER_INVERTERS: listed}
        )
    return max(known, listed)
