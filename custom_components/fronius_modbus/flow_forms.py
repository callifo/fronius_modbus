"""The forms the config, options and reauth flows show."""

from __future__ import annotations

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
    CONF_MODBUS_RESTRICTION,
    CONF_WEB_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WEB_SCAN_INTERVAL,
    MINIMUM_SCAN_INTERVAL,
    WEB_API_DISABLED,
    ModbusRestriction,
)


def settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Host, intervals, role and Modbus restriction, filled in with ``defaults``."""
    return vol.Schema(
        {
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
                CONF_API_USERNAME,
                default=defaults.get(CONF_API_USERNAME, API_USERNAME),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[*API_USERNAMES, WEB_API_DISABLED],
                    mode=SelectSelectorMode.LIST,
                    translation_key="api_username",
                )
            ),
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
        }
    )


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
