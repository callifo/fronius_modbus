"""The forms the config, options and reauth flows show."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_HOST, CONF_SCAN_INTERVAL
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    API_USERNAME,
    API_USERNAMES,
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_METER_POWER_INTERVAL,
    CONF_MODBUS_RESTRICTION,
    CONF_WEB_SCAN_INTERVAL,
    DEFAULT_METER_POWER_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WEB_SCAN_INTERVAL,
    MAXIMUM_METER_POWER_INTERVAL,
    MINIMUM_SCAN_INTERVAL,
    WEB_API_DISABLED,
    ModbusRestriction,
)
from .flow_state import (
    _AddressesNotUnique,
    _AlreadyConfigured,
    _CannotConnect,
    _CannotConnectModbus,
    _DatamanagerCustomer,
    _InvalidAllowedHost,
    _InvalidApiCredentials,
    _InvalidHost,
    _InvalidPort,
    _MeterPowerIntervalTooLong,
    _MissingApiPassword,
    _ScanIntervalTooShort,
    _UnsupportedHardware,
)

_LOGGER = logging.getLogger(__name__)


def _role_field(defaults: dict[str, Any]) -> dict[vol.Marker, SelectSelector]:
    return {
        vol.Required(
            CONF_API_USERNAME,
            default=defaults.get(CONF_API_USERNAME, API_USERNAME),
        ): SelectSelector(
            SelectSelectorConfig(
                options=[*API_USERNAMES, WEB_API_DISABLED],
                mode=SelectSelectorMode.LIST,
                translation_key="api_username",
            )
        )
    }


def role_schema(defaults: dict[str, Any]) -> vol.Schema:
    """The role alone: a new login changes nothing else; Reconfigure does."""
    return vol.Schema(_role_field(defaults))


# Asks for the address to allow although the inverter allows Home Assistant's
# own: behind NAT an earlier version wrote that one, and the inverter sees
# another. Never stored: it is a request for this flow only.
CONF_CHANGE_ALLOWED_HOST = "change_allowed_host"


def settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Host, intervals, role and Modbus restriction, filled in with ``defaults``."""
    fields: dict[vol.Marker, Any] = {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Required(
            CONF_SCAN_INTERVAL,
            default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        ): vol.Coerce(int),
        vol.Required(
            CONF_WEB_SCAN_INTERVAL,
            default=defaults.get(CONF_WEB_SCAN_INTERVAL, DEFAULT_WEB_SCAN_INTERVAL),
        ): vol.All(vol.Coerce(int), vol.Range(min=MINIMUM_SCAN_INTERVAL, max=3600)),
        vol.Required(
            CONF_METER_POWER_INTERVAL,
            default=defaults.get(
                CONF_METER_POWER_INTERVAL, DEFAULT_METER_POWER_INTERVAL
            ),
        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAXIMUM_METER_POWER_INTERVAL)),
        **_role_field(defaults),
        vol.Required(
            CONF_MODBUS_RESTRICTION,
            default=defaults.get(CONF_MODBUS_RESTRICTION, ModbusRestriction.KEEP),
        ): SelectSelector(
            SelectSelectorConfig(
                options=list(ModbusRestriction),
                mode=SelectSelectorMode.LIST,
                translation_key=CONF_MODBUS_RESTRICTION,
            )
        ),
        vol.Optional(CONF_CHANGE_ALLOWED_HOST, default=False): bool,
    }
    return vol.Schema(fields)


def asks_for_the_address(user_input: dict[str, Any], settings: dict[str, Any]) -> bool:
    """The owner asked to see the address, and the restriction is to allow one."""
    requested = bool(user_input.get(CONF_CHANGE_ALLOWED_HOST, False))
    restricted = settings[CONF_MODBUS_RESTRICTION] == ModbusRestriction.HOME_ASSISTANT
    return requested and restricted


# The address the Modbus restriction allows for this Home Assistant.
CONF_ALLOWED_HOST = "allowed_host"


def allowed_host_schema(default: str) -> vol.Schema:
    """The address to allow, filled in with ``default``: empty when none is known."""
    return vol.Schema({vol.Required(CONF_ALLOWED_HOST, default=default): str})


def password_schema(*, keep_stored: bool = False) -> vol.Schema:
    """The role's password; left empty it keeps a stored token when there is one."""
    password_field = (
        vol.Optional(CONF_API_PASSWORD, default="")
        if keep_stored
        else vol.Required(CONF_API_PASSWORD)
    )
    return vol.Schema(
        {
            password_field: TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                )
            ),
        }
    )


def checked_allowed_host(value: Any) -> str:
    """The address to allow, as the IPv4 address the inverter's list takes."""
    try:
        address = ipaddress.IPv4Address(str(value).strip())
    except ValueError as err:
        raise _InvalidAllowedHost from err
    # No client sends from these; allowed alone they lock every device out.
    if address.is_loopback or address.is_unspecified:
        raise _InvalidAllowedHost
    if address.is_multicast or address.is_reserved:
        raise _InvalidAllowedHost
    return str(address)


_FORM_ERRORS: dict[type[Exception], tuple[str, str]] = {
    _CannotConnect: ("base", "cannot_connect"),
    _CannotConnectModbus: ("base", "cannot_connect_modbus"),
    _InvalidPort: ("base", "invalid_port"),
    _InvalidHost: ("host", "invalid_host"),
    _ScanIntervalTooShort: ("base", "scan_interval_too_short"),
    _MeterPowerIntervalTooLong: ("base", "meter_power_interval_too_long"),
    _MissingApiPassword: ("base", "missing_api_password"),
    _InvalidApiCredentials: ("base", "invalid_api_credentials"),
    _InvalidAllowedHost: (CONF_ALLOWED_HOST, "invalid_allowed_host"),
    _UnsupportedHardware: ("base", "unsupported_hardware"),
    _AddressesNotUnique: ("base", "modbus_address_conflict"),
    _AlreadyConfigured: ("base", "already_configured"),
    _DatamanagerCustomer: (CONF_API_USERNAME, "datamanager_customer"),
}


def _set_form_error(errors: dict[str, str], err: Exception) -> None:
    field, message = _FORM_ERRORS.get(type(err), ("base", "unknown"))
    if message == "unknown":
        _LOGGER.error("Unexpected exception", exc_info=err)
    errors[field] = message
