"""Polling the web interface of a Datamanager."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_API_USERNAME, DOMAIN
from .coordinator import DeviceUnreachable, OfflineIsNotAnError
from .datamanager_client import DatamanagerClient, ExportLimits
from .fronius_modbus_api.exceptions import ControlUnavailable, error_text
from .token_store import async_get_token_store, canonical_host
from .web_errors import (
    FroniusWebAuthError,
    FroniusWebResponseError,
    FroniusWebUnreachable,
)

_LOGGER = logging.getLogger(__name__)
_LOGGER.addFilter(OfflineIsNotAnError())

# A Symo Hybrid restarted while its Solar API was read every 10 seconds.
MINIMUM_INTERVAL_SECONDS = 60


_WRITE_TURNS_DATA_KEY = "datamanager_write_turns"


def _write_turn(hass: HomeAssistant, host: str) -> asyncio.Lock:
    """The lock a write to ``host`` waits for; it wakes its waiters in order.

    One per host, kept across reloads: a reloaded entry has a new coordinator
    while a write of the old one may still be on its way. Keyed like the stored
    login, so that a host reconfigured in another spelling keeps its turn.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    turns: dict[str, asyncio.Lock] = domain_data.setdefault(_WRITE_TURNS_DATA_KEY, {})
    return turns.setdefault(canonical_host(host), asyncio.Lock())


def _report_a_write_nobody_waits_for(write: asyncio.Task[None]) -> None:
    """Log the failure of a write its caller gave up, without the device's address."""
    error = None if write.cancelled() else write.exception()
    if error is None:
        return
    _LOGGER.warning(
        "A soft limit write whose caller gave up failed: %s", error_text(error)
    )


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
        self._write_turn = _write_turn(hass, host)

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

    async def async_set_soft_limit(self, watts: int) -> None:
        """Set the export soft limit and show what the device holds after it.

        Writes go out in the order they were asked for, also across a reload,
        and one its caller gave up runs to its end first: its thread cannot be
        stopped, and a later value would otherwise be overwritten by it.
        """
        write = self.hass.async_create_task(self._async_write_in_turn(watts))
        try:
            await asyncio.wait([write])
        except asyncio.CancelledError:
            write.add_done_callback(_report_a_write_nobody_waits_for)
            raise
        write.result()

    async def _async_write_in_turn(self, watts: int) -> None:
        async with self._write_turn:
            await self._async_write(watts)

    async def _async_write(self, watts: int) -> None:
        try:
            limits = await self.hass.async_add_executor_job(
                self._client.set_soft_limit, watts
            )
        except FroniusWebAuthError as err:
            # The poll asks for the new login; the refused one is not sent again.
            await self.async_refresh()
            raise ControlUnavailable(
                "web_api_auth_failed",
                "The Datamanager refused the login. Log in again when asked.",
            ) from err
        except ControlUnavailable:
            # Show what the device holds now.
            await self.async_refresh()
            raise
        self.async_set_updated_data(limits)


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
