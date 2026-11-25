"""Choosing which of a Datamanager's inverters a new entry reads."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from . import datamanager
from .host_sharing import taken_units


async def async_free_units(hass: HomeAssistant, host: str) -> list[int]:
    """The units of the inverters the Solar API lists, without those an entry reads."""
    listed = await hass.async_add_executor_job(datamanager.read_inverter_units, host)
    taken = taken_units(hass, host)
    return [unit for unit in listed if unit not in taken]
