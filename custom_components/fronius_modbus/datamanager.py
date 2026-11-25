"""Telling an older inverter behind a Datamanager from the others.

A Datamanager has a web interface of its own: other logins and other paths
than a GEN24.
"""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DATAMANAGER, web_api_disabled
from .froniuswebclient import _base_url, _http
from .web_errors import FroniusWebUnreachable

# Answers without a login. A GEN24 refuses it in the body, with HTTP 200.
LOGGER_INFO_PATH = "/solar_api/v1/GetLoggerInfo.cgi"
# The one value seen is fronius-datamanager-card; the box is expected to start
# the same way.
DATAMANAGER_PRODUCT_PREFIX = "fronius-datamanager"


class DeviceFamily(StrEnum):
    """Which web interface the device at an address has."""

    DATAMANAGER = "datamanager"
    OTHER = "other"
    UNKNOWN = "unknown"


def _family(payload: Any) -> DeviceFamily:
    """The family a GetLoggerInfo body shows."""
    if not isinstance(payload, dict) or not isinstance(payload.get("Head"), dict):
        return DeviceFamily.UNKNOWN
    body = payload.get("Body")
    logger_info = body.get("LoggerInfo") if isinstance(body, dict) else None
    product = logger_info.get("ProductID") if isinstance(logger_info, dict) else None
    if isinstance(product, str) and product.startswith(DATAMANAGER_PRODUCT_PREFIX):
        return DeviceFamily.DATAMANAGER
    return DeviceFamily.OTHER


async def async_identify(hass: HomeAssistant, host: str) -> DeviceFamily:
    """Ask the device at ``host`` without blocking the event loop."""
    return await hass.async_add_executor_job(identify, host)


def identify(host: str, timeout: float = 4.0) -> DeviceFamily:
    """The family of the device at ``host``; UNKNOWN without a clear answer."""
    try:
        response = _http("get", f"{_base_url(host)}{LOGGER_INFO_PATH}", timeout=timeout)
        payload = response.json()
    except FroniusWebUnreachable, ValueError:
        return DeviceFamily.UNKNOWN
    if response.status_code != HTTPStatus.OK:
        return DeviceFamily.UNKNOWN
    return _family(payload)


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
    return found
