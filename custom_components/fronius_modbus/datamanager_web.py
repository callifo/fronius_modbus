"""Polling the web interface of a Datamanager."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_API_USERNAME, DOMAIN
from .coordinator import DeviceUnreachable, OfflineIsNotAnError
from .datamanager_client import DatamanagerClient, ExportLimits
from .fronius_modbus_api.exceptions import error_text
from .token_store import async_get_token_store
from .web_errors import (
    FroniusWebAuthError,
    FroniusWebResponseError,
    FroniusWebUnreachable,
)

_LOGGER = logging.getLogger(__name__)
_LOGGER.addFilter(OfflineIsNotAnError())

# A Symo Hybrid restarted while its Solar API was read every 10 seconds.
MINIMUM_INTERVAL_SECONDS = 60


class DatamanagerWebCoordinator(DataUpdateCoordinator[ExportLimits]):
    """Reads the export limit; a refused login ends the polls until a new one."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        host: str,
        client: DatamanagerClient,
        *,
        interval_seconds: int,
    ) -> None:
        """Bind to the client that holds the login stored for the host."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{entry.entry_id} Datamanager",
            update_interval=timedelta(
                seconds=max(interval_seconds, MINIMUM_INTERVAL_SECONDS)
            ),
        )
        self._host = host
        self._role = str({**entry.data, **entry.options}[CONF_API_USERNAME])
        self._client = client

    async def _async_update_data(self) -> ExportLimits:
        try:
            return await self.hass.async_add_executor_job(
                self._client.read_export_limits
            )
        except FroniusWebUnreachable as err:
            raise DeviceUnreachable(
                translation_domain=DOMAIN, translation_key="web_api_unreachable"
            ) from err
        except FroniusWebAuthError as err:
            # A restart would send the refused login once more.
            await async_get_token_store(self.hass).async_delete_token(
                self._host, self._role
            )
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="web_api_auth_failed"
            ) from err
        except FroniusWebResponseError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="web_api_refresh_failed",
                translation_placeholders={"error": error_text(err)},
            ) from err


async def async_start(
    hass: HomeAssistant,
    entry: ConfigEntry,
    host: str,
    token: dict[str, str] | None,
    *,
    interval_seconds: int,
) -> DatamanagerWebCoordinator | None:
    """Read the export limit once and go on polling it; None without a login.

    An outage of the web interface does not hold the Modbus entities back.
    """
    if token is None:
        return None
    client = DatamanagerClient(host, token=token)
    coordinator = DatamanagerWebCoordinator(
        hass, entry, host, client, interval_seconds=interval_seconds
    )
    await coordinator.async_refresh()
    return coordinator
