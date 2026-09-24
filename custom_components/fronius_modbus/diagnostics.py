"""Diagnostics: the identity, the last poll's outcome, and every raw register."""

from __future__ import annotations

import dataclasses
from typing import Any

from modbus_connection import ModbusError

from homeassistant.core import HomeAssistant

from .coordinator import FroniusConfigEntry, FroniusRuntimeData

REDACTED = "**REDACTED**"


async def _async_registers(runtime: FroniusRuntimeData) -> dict[str, Any]:
    """Every raw register, or what kept them from being read.

    An offline inverter is when the last poll and its report are needed most;
    a failed read must not take them with it (audit F24-08).
    """
    try:
        raw = await runtime.device.async_read_raw()
    except ModbusError as err:
        return {"error": type(err).__name__}
    return {
        str(unit_id): {
            space: {str(address): word for address, word in words.items()}
            for space, words in spaces.items()
        }
        for unit_id, spaces in raw.items()
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FroniusConfigEntry
) -> dict[str, Any]:
    """Everything a bug report needs about one entry, minus the serial numbers."""
    del hass
    runtime = entry.runtime_data
    poll = runtime.modbus.data
    web_data = runtime.web_data
    return {
        "identity": runtime.device.identity._replace(serial=REDACTED)._asdict(),
        "updated": sorted(poll.report.updated),
        "failed": {name: str(err) for name, err in poll.report.failed.items()},
        "registers": await _async_registers(runtime),
        "web": None
        if web_data is None
        else dataclasses.asdict(web_data)
        | {"storage_serial": REDACTED, "modbus_restriction_ip": REDACTED},
    }
