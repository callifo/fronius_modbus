"""The Fronius Modbus integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant

from . import hub, migrations
from .const import (
    API_USERNAME,
    CONF_API_USERNAME,
    CONF_INVERTER_UNIT_ID,
    CONF_RESTRICT_MODBUS_TO_THIS_IP,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_NAME,
    DEFAULT_METER_UNIT_IDS,
    DEFAULT_PORT,
    DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SELECT, Platform.SWITCH, Platform.NUMBER, Platform.SENSOR, Platform.BUTTON]

type HubConfigEntry = ConfigEntry[hub.Hub]


def _entry_value(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))

async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries."""
    return await migrations.async_migrate_entry(hass, entry)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: HubConfigEntry) -> bool:
    """Set up Fronius Modbus from a config entry."""
    name = _entry_value(entry, CONF_NAME, DEFAULT_NAME)
    host = _entry_value(entry, CONF_HOST)
    port = _entry_value(entry, CONF_PORT, DEFAULT_PORT)
    inverter_unit_id = _entry_value(entry, CONF_INVERTER_UNIT_ID, DEFAULT_INVERTER_UNIT_ID)
    scan_interval = _entry_value(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    api_username = _entry_value(entry, CONF_API_USERNAME, API_USERNAME)
    restrict_modbus_to_this_ip = _entry_value(
        entry,
        CONF_RESTRICT_MODBUS_TO_THIS_IP,
        DEFAULT_RESTRICT_MODBUS_TO_THIS_IP,
    )

    _LOGGER.debug("Setup %s.%s", DOMAIN, name)

    api_token = await migrations.async_prepare_entry_token(hass, entry, host)
    await migrations.async_sync_reconfigure_issue(hass, entry, has_token=api_token is not None)

    entry.runtime_data = hub.Hub(
        hass=hass,
        name=name,
        host=host,
        port=port,
        inverter_unit_id=inverter_unit_id,
        meter_unit_ids=list(DEFAULT_METER_UNIT_IDS),
        scan_interval=scan_interval,
        api_username=api_username if api_token else None,
        api_token=api_token,
        auto_enable_modbus=False,
        restrict_modbus_to_this_ip=restrict_modbus_to_this_ip,
    )

    await entry.runtime_data.init_data(config_entry=entry)
    await migrations.async_migrate_v019_mppt_statistics(hass, entry, entry.runtime_data)
    await migrations.async_migrate_name_based_unique_ids(hass, entry, entry.runtime_data)
    await migrations.async_remove_unexpected_entities(hass, entry, entry.runtime_data)
    await migrations.async_remove_legacy_devices(hass, entry)
    await migrations.async_sync_reconfigure_issue(
        hass,
        entry,
        has_token=entry.runtime_data.web_api_configured,
    )

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and getattr(entry, "runtime_data", None) is not None:
        entry.runtime_data.close()

    return unload_ok
