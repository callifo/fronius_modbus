"""Telling an older inverter behind a Datamanager from the others.

A Datamanager has a web interface of its own: other logins and other paths
than a GEN24.
"""

from __future__ import annotations

from enum import StrEnum
from http import HTTPStatus
from typing import Any

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
