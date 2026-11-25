"""Telling an older inverter behind a Datamanager from the others.

A Datamanager has a web interface of its own: other logins and other paths
than a GEN24.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from http import HTTPStatus
from typing import Any

from modbus_connection import ModbusConnectionError, ModbusTcpParams

from homeassistant.components.modbus import async_get_unit
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DATAMANAGER,
    CONF_DATAMANAGER_INVERTERS,
    DOMAIN,
    UNIT_OF_NUMBER_0,
    entry_unique_id,
    web_api_disabled,
)
from .derived import METER_LOCATION_FEED_IN
from .fronius_modbus_api.device import FroniusInverter
from .froniuswebclient import _base_url, _http
from .meter_topology import MeterTopology
from .web_errors import FroniusWebUnreachable

# Answers without a login. A GEN24 refuses it in the body, with HTTP 200.
LOGGER_INFO_PATH = "/solar_api/v1/GetLoggerInfo.cgi"
# Answers without a login on a GEN24, also with its Solar API off; a
# Datamanager does not have it.
GEN24_PATH = "/api/status/common"
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
    """The family a GetLoggerInfo body shows."""
    if not isinstance(payload, dict) or not isinstance(payload.get("Head"), dict):
        return DeviceFamily.UNKNOWN
    body = payload.get("Body")
    logger_info = body.get("LoggerInfo") if isinstance(body, dict) else None
    product = logger_info.get("ProductID") if isinstance(logger_info, dict) else None
    if isinstance(product, str) and product.startswith(DATAMANAGER_PRODUCT_PREFIXES):
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
    is not asked: a Datamanager accepts none of them.
    """
    if not web_api_disabled({**entry.data, **entry.options}):
        return False
    remembered = entry.data.get(CONF_DATAMANAGER)
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
    42,0410,2049). A listing can hold a meter's number too.
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


def meter_topology(found: dict[int, str], site: Site | None) -> MeterTopology:
    """The meters of a Datamanager, from the units that answered and the Solar API.

    The first unit is always listed: a meter silent now is asked again with
    every poll. Never confirmed: no answer does not prove that a meter is gone.
    """
    locations = _locations(found, site) if site is not None else {}
    grid_meters = [
        unit for unit in sorted(locations) if locations[unit] == METER_LOCATION_FEED_IN
    ]
    primary = next(iter([*grid_meters, *sorted(found)]), METER_UNIT_IDS[0])
    if site is not None and site.inverters != 1:
        # The load adds the one inverter read here to the grid meter's power.
        for unit in grid_meters:
            del locations[unit]
    if site is not None and site.has_grid_meter and not grid_meters:
        # Its power would pass for the whole load while the grid meter is silent.
        locations.pop(primary, None)
    return MeterTopology(
        unit_ids=sorted({METER_UNIT_IDS[0], *found}),
        primary_unit_id=primary,
        locations=locations,
        confirmed=False,
        several_inverters=site is not None and site.inverters > 1,
    )


async def async_meter_topology(
    hass: HomeAssistant, entry: ConfigEntry, params: ModbusTcpParams
) -> MeterTopology:
    """Look for the meters of the Datamanager at the entry's address."""
    found: dict[int, str] = {}
    try:
        for unit_id in METER_UNIT_IDS:
            unit = async_get_unit(hass, entry, params, unit_id)
            identity = await FroniusInverter.async_probe_meter(unit)
            if identity is not None:
                found[unit_id] = identity.serial.strip()
    except ModbusConnectionError:
        # The first poll reports it and has the setup tried again.
        return meter_topology(found, None)
    site = await hass.async_add_executor_job(read_site, params.host)
    if site is not None:
        site = replace(site, inverters=_known_inverters(hass, entry, site.inverters))
    return meter_topology(found, site)


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
