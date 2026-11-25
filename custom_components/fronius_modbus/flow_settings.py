"""The settings a flow collects: the defaults, the input made whole, what an entry keeps."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant

from . import datamanager
from .const import (
    API_USERNAME,
    API_USERNAMES,
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_AUTO_ENABLE_MODBUS,
    CONF_DATAMANAGER,
    CONF_DATAMANAGER_INVERTERS,
    CONF_INVERTER_UNIT_ID,
    CONF_METER_POWER_INTERVAL,
    CONF_MODBUS_RESTRICTION,
    CONF_RECONFIGURE_REQUIRED,
    CONF_WEB_SCAN_INTERVAL,
    DEFAULT_AUTO_ENABLE_MODBUS,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_METER_POWER_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WEB_SCAN_INTERVAL,
    WEB_API_DISABLED,
    ModbusRestriction,
)
from .flow_state import _DatamanagerFound


def _default_payload() -> dict[str, Any]:
    return {
        CONF_NAME: DEFAULT_NAME,
        CONF_HOST: "",
        CONF_PORT: DEFAULT_PORT,
        CONF_INVERTER_UNIT_ID: DEFAULT_INVERTER_UNIT_ID,
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        CONF_WEB_SCAN_INTERVAL: DEFAULT_WEB_SCAN_INTERVAL,
        CONF_METER_POWER_INTERVAL: DEFAULT_METER_POWER_INTERVAL,
        CONF_API_USERNAME: API_USERNAME,
        CONF_AUTO_ENABLE_MODBUS: DEFAULT_AUTO_ENABLE_MODBUS,
        CONF_MODBUS_RESTRICTION: ModbusRestriction.KEEP,
    }


def _expand_settings_input(
    user_input: dict[str, Any],
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _default_payload()
    if defaults:
        payload.update(defaults)
    payload[CONF_HOST] = str(user_input.get(CONF_HOST, payload[CONF_HOST])).strip()
    payload[CONF_SCAN_INTERVAL] = int(
        user_input.get(CONF_SCAN_INTERVAL, payload[CONF_SCAN_INTERVAL])
    )
    payload[CONF_MODBUS_RESTRICTION] = ModbusRestriction(
        user_input.get(CONF_MODBUS_RESTRICTION, payload[CONF_MODBUS_RESTRICTION])
    )
    payload[CONF_WEB_SCAN_INTERVAL] = int(
        user_input.get(CONF_WEB_SCAN_INTERVAL, payload[CONF_WEB_SCAN_INTERVAL])
    )
    payload[CONF_METER_POWER_INTERVAL] = int(
        user_input.get(CONF_METER_POWER_INTERVAL, payload[CONF_METER_POWER_INTERVAL])
    )
    username = (
        str(user_input.get(CONF_API_USERNAME, payload[CONF_API_USERNAME]))
        .strip()
        .lower()
    )
    choices = (*API_USERNAMES, WEB_API_DISABLED)
    payload[CONF_API_USERNAME] = username if username in choices else API_USERNAME
    payload.pop(CONF_API_PASSWORD, None)
    payload.pop("meter_modbus_unit_id", None)
    payload.pop("meter_modbus_unit_ids", None)
    return payload


def _entry_payload(
    data: dict[str, Any], *, reconfigure_required: bool
) -> dict[str, Any]:
    payload = dict(data)
    payload.pop(CONF_API_PASSWORD, None)
    payload.pop("meter_modbus_unit_id", None)
    payload.pop("meter_modbus_unit_ids", None)
    # Kept as the answer it is; a missing key means no.
    if not payload.get(CONF_DATAMANAGER):
        payload.pop(CONF_DATAMANAGER, None)
    payload[CONF_RECONFIGURE_REQUIRED] = reconfigure_required
    return payload


def entry_defaults(entry: config_entries.ConfigEntry) -> dict[str, Any]:
    """The entry's settings as a flow shows them, gaps filled with the defaults."""
    defaults = {**entry.data, **entry.options}
    try:
        defaults[CONF_SCAN_INTERVAL] = int(
            defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
    except TypeError, ValueError:
        defaults[CONF_SCAN_INTERVAL] = DEFAULT_SCAN_INTERVAL
    # What the device answered is no setting: the setup asks it again.
    for remembered in (CONF_DATAMANAGER, CONF_DATAMANAGER_INVERTERS):
        defaults.pop(remembered, None)
    return _expand_settings_input({}, defaults)


def _should_apply_modbus_config(
    settings: dict[str, Any],
    previous_settings: dict[str, Any] | None,
) -> bool:
    if previous_settings is None:
        return True
    # Without a login nothing was written, so the saved choices were never
    # applied to the inverter.
    if previous_settings.get(CONF_API_USERNAME) == WEB_API_DISABLED:
        return True

    return bool(
        settings[CONF_HOST] != previous_settings.get(CONF_HOST, "")
        or settings[CONF_PORT] != previous_settings.get(CONF_PORT, DEFAULT_PORT)
        or settings[CONF_INVERTER_UNIT_ID]
        != previous_settings.get(CONF_INVERTER_UNIT_ID, DEFAULT_INVERTER_UNIT_ID)
        or settings[CONF_MODBUS_RESTRICTION]
        != previous_settings.get(
            CONF_MODBUS_RESTRICTION,
            ModbusRestriction.KEEP,
        )
    )


async def async_check_role(hass: HomeAssistant, settings: dict[str, Any]) -> None:
    """Refuse a role on a Datamanager: none of our logins works there."""
    if settings[CONF_API_USERNAME] == WEB_API_DISABLED:
        return
    family = await datamanager.async_identify(hass, settings[CONF_HOST])
    if family is datamanager.DeviceFamily.DATAMANAGER:
        raise _DatamanagerFound


def defaults_after(
    errors: dict[str, str],
    user_input: dict[str, Any] | None,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """The form's defaults; after a refused role, what was typed, without the web API."""
    if user_input is None or CONF_API_USERNAME not in errors:
        return defaults
    typed = _expand_settings_input(user_input, defaults)
    return {**typed, CONF_API_USERNAME: WEB_API_DISABLED}
