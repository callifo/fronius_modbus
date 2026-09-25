"""The Fronius Modbus integration, served from Home Assistant's shared Modbus connection."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import logging
import time

from modbus_connection import ModbusTcpParams

from homeassistant.components.modbus import async_get_unit
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant, callback

from . import migrations
from .component_readings import reported_rows
from .const import (
    API_USERNAME,
    CONF_API_USERNAME,
    CONF_INVERTER_UNIT_ID,
    CONF_WEB_SCAN_INTERVAL,
    DEFAULT_INVERTER_UNIT_ID,
    DEFAULT_METER_UNIT_ID,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_WEB_SCAN_INTERVAL,
)
from .coordinator import (
    FroniusConfigEntry,
    FroniusModbusCoordinator,
    FroniusRuntimeData,
    FroniusWebCoordinator,
)
from .fronius_modbus_api.device import FroniusInverter
from .froniuswebclient import FroniusWebAuthError, FroniusWebClient
from .token_store import async_forget_unused_tokens, async_get_token_store
from .web_control import (
    BATTERY_WRITE_MODBUS_RECOVERY_SECONDS,
    FroniusWebControl,
    MeterTopology,
    parse_meter_topology,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.BUTTON,
]


def _entry_value(entry: ConfigEntry, key: str, default=None):
    return entry.options.get(key, entry.data.get(key, default))


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to the current version."""
    return await migrations.async_migrate_entry(hass, entry)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_meter_topology(
    hass: HomeAssistant,
    entry: FroniusConfigEntry,
    client: FroniusWebClient | None,
    public_client: FroniusWebClient | None,
) -> tuple[FroniusWebClient | None, MeterTopology]:
    """Ask the web API which meters exist; fall back to the single default meter.

    An authentication failure drops the client for good: the stored token is
    deleted, so the entry runs without its login until the user reconfigures it.
    """
    unconfirmed = parse_meter_topology(None)
    if public_client is None:
        # Without a web client the entry only ever serves the default meter, so
        # that is the configuration, not an unread answer.
        return None, replace(unconfirmed, confirmed=True)

    try:
        info = await hass.async_add_executor_job(
            public_client.get_power_meter_info, DEFAULT_METER_UNIT_ID
        )
    except FroniusWebAuthError as err:
        _LOGGER.warning(
            "Disabling the Fronius web API after an auth failure: %s",
            err,
        )
        await async_get_token_store(hass).async_delete_token(
            str(_entry_value(entry, CONF_HOST)),
            str(_entry_value(entry, CONF_API_USERNAME, API_USERNAME)),
        )
        return None, unconfirmed
    except Exception as err:
        _LOGGER.warning(
            "Could not read the meter topology from the web API: %s",
            err,
            exc_info=True,
        )
        return client, unconfirmed

    return client, parse_meter_topology(info)


async def async_setup_entry(hass: HomeAssistant, entry: FroniusConfigEntry) -> bool:
    """Set up Fronius Modbus from a config entry."""
    host = str(_entry_value(entry, CONF_HOST))
    port = int(_entry_value(entry, CONF_PORT, DEFAULT_PORT))
    inverter_unit_id = int(
        _entry_value(entry, CONF_INVERTER_UNIT_ID, DEFAULT_INVERTER_UNIT_ID)
    )
    scan_interval = int(_entry_value(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    web_scan_interval = int(
        _entry_value(entry, CONF_WEB_SCAN_INTERVAL, DEFAULT_WEB_SCAN_INTERVAL)
    )

    api_username = str(_entry_value(entry, CONF_API_USERNAME, API_USERNAME))
    api_token = await migrations.async_prepare_entry_token(hass, entry, host)
    await migrations.async_sync_login_request(
        hass, entry, has_token=api_token is not None
    )
    client = (
        FroniusWebClient(host=host, username=api_username, password="", token=api_token)
        if api_token
        else None
    )
    public_client = client
    if migrations.web_api_disabled(entry):
        public_client = FroniusWebClient(host=host, username=API_USERNAME, password="")

    client, topology = await _async_meter_topology(hass, entry, client, public_client)
    meter_unit_ids, primary = topology.unit_ids, topology.primary_unit_id
    locations = topology.locations

    params = ModbusTcpParams(host=host, port=port)
    unit = async_get_unit(hass, entry, params, inverter_unit_id)
    meter_units = {
        unit_id: async_get_unit(hass, entry, params, unit_id)
        for unit_id in meter_unit_ids
    }
    device = FroniusInverter(unit, inverter_unit_id, meter_units)
    modbus = FroniusModbusCoordinator(
        hass,
        entry,
        device,
        interval=timedelta(seconds=scan_interval),
        primary_meter_unit_id=primary,
        meter_locations=locations,
    )
    await modbus.async_config_entry_first_refresh()

    web_control = None
    web = None
    if public_client is not None:
        web_control = FroniusWebControl(
            hass,
            entry,
            host=host,
            client=client,
            public_client=public_client,
            api_username=api_username,
            storage_present=device.storage is not None,
            inverter_firmware=lambda: (
                device.identity.version if device.identity else None
            ),
            on_battery_write=lambda: modbus.tolerate_failures_until(
                time.monotonic() + BATTERY_WRITE_MODBUS_RECOVERY_SECONDS
            ),
        )
        web = FroniusWebCoordinator(
            hass,
            entry,
            web_control,
            interval=timedelta(seconds=web_scan_interval),
            recheck_topology=not topology.confirmed,
        )
        web_control.attach_coordinator(web)
        entry.async_on_unload(web_control.shutdown)
        # A web-API outage must not block setup: refresh (not first_refresh) so
        # web entities come up unavailable while Modbus entities still load.
        await web.async_refresh()

    entry.runtime_data = FroniusRuntimeData(
        device=device,
        modbus=modbus,
        web=web,
        web_control=web_control,
        meter_locations=locations,
        primary_meter_unit_id=primary,
        topology_confirmed=topology.confirmed,
        reported_keys=migrations.reported_keys(hass, entry),
        registered_keys=migrations.registered_keys(hass, entry),
    )

    await migrations.async_migrate_v019_mppt_statistics(hass, entry)
    await migrations.async_migrate_name_based_unique_ids(hass, entry)
    await migrations.async_remove_unexpected_entities(hass, entry)
    await migrations.async_remove_legacy_devices(hass, entry)
    await migrations.async_sync_login_request(
        hass, entry, has_token=web_control is not None and web_control.configured
    )

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if web is not None:
        web_poll = web

        @callback
        def _mark_reported_rows() -> None:
            # ponytail: the report-following rows are all inverter rows today.
            readings = web_poll.data.inverter_readings if web_poll.data else None
            migrations.async_mark_reported(
                hass, entry, reported_rows("inverter", readings)
            )

        _mark_reported_rows()
        entry.async_on_unload(web.async_add_listener(_mark_reported_rows))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FroniusConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: FroniusConfigEntry) -> None:
    """Delete the entry's web token unless another entry still logs in with it."""
    await async_forget_unused_tokens(hass, str(_entry_value(entry, CONF_HOST, "")))
