"""Where the inverter's export limit settings keep the soft limit."""

from __future__ import annotations

from typing import Any

from .component_readings import flag_value
from .fronius_modbus_api.exceptions import ControlRefused

# Present in the activePower block from firmware 1.42 on.
EXPORT_LIMIT_KEY = "exportLimit"
# "Power Control" on the inverter's Export Limitation page.
POWER_CONTROL_KEY = "activated"
# The limits the inverter keeps at or above the soft limit while they are on;
# the protection limit exists from firmware 1.42 on.
_LIMITS_ABOVE_SOFT = (
    ("hardLimit", "powerLimit"),
    ("protectionLimit", "acPowerInstalled"),
)


def power_control_off(active_power: Any) -> bool:
    """Whether the inverter reports its export limitation as switched off.

    Firmware 1.41 then refuses a soft limit with softLimit_disabled (issue 154),
    and with the limitation off the soft limit has no effect anyway.
    Firmware that does not report the switch counts as on.
    """
    if not isinstance(active_power, dict):
        return False
    return flag_value(active_power.get(POWER_CONTROL_KEY)) is False


def require_power_control(active_power: Any) -> None:
    """Refuse a soft limit while Power Control is reported off."""
    if power_control_off(active_power):
        raise ControlRefused(
            "export_limit_power_control_off",
            "Export limitation is off on the inverter: switch on Power Control "
            "on its Export Limitation page first",
        )


def soft_limit_location(active_power: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The object holding the soft limit's watts, and their key in it.

    Firmware 1.42 moved them from softLimit.powerLimit up to
    activePower.exportLimit; its new protection limit keeps its own value
    (issue 154). A write to the old place is refused there.
    """
    if EXPORT_LIMIT_KEY in active_power:
        return active_power, EXPORT_LIMIT_KEY
    soft = active_power.get("softLimit")
    return (soft if isinstance(soft, dict) else {}), "powerLimit"


def soft_limit_ceiling(active_power: Any) -> float | None:
    """The highest soft limit the inverter takes: its lowest enabled limit above it.

    None while neither limit is on: the inverter then sets no bound of its own.
    The rated power is none, as the limit counts at the grid meter, where other
    inverters and generation add up (issue 154).
    """
    if not isinstance(active_power, dict):
        return None
    ceilings = []
    for block_key, value_key in _LIMITS_ABOVE_SOFT:
        block = active_power.get(block_key)
        if not isinstance(block, dict) or flag_value(block.get("enabled")) is not True:
            continue
        value = block.get(value_key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            ceilings.append(float(value))
    return min(ceilings, default=None)


def soft_limit_value(active_power: Any) -> Any:
    """The soft limit's watts as answered; None for a block of the wrong shape."""
    if not isinstance(active_power, dict):
        return None
    holder, key = soft_limit_location(active_power)
    return holder.get(key)
