"""Where the inverter's export limit settings keep the soft limit."""

from __future__ import annotations

from typing import Any

# Present in the activePower block from firmware 1.42 on.
EXPORT_LIMIT_KEY = "exportLimit"


def soft_limit_location(active_power: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The object holding the soft limit's watts, and their key in it.

    Firmware 1.42 moved them from softLimit.powerLimit up to
    activePower.exportLimit; its new protection limit keeps its own value
    (upstream #154). A write to the old place is refused there.
    """
    if EXPORT_LIMIT_KEY in active_power:
        return active_power, EXPORT_LIMIT_KEY
    soft = active_power.get("softLimit")
    return (soft if isinstance(soft, dict) else {}), "powerLimit"


def soft_limit_value(active_power: Any) -> Any:
    """The soft limit's watts as answered; None for a block of the wrong shape."""
    if not isinstance(active_power, dict):
        return None
    holder, key = soft_limit_location(active_power)
    return holder.get(key)
