"""Which meters the inverter's web API reports, as setup and the pollers use it."""

from __future__ import annotations

from dataclasses import dataclass

from .const import DEFAULT_METER_UNIT_ID


@dataclass(slots=True)
class MeterTopology:
    """Which meters the web API reports, and whether it actually answered.

    `confirmed` is the difference between "this device has one meter" and "the
    question could not be asked": only the former may retire entities (audit A04).
    """

    unit_ids: list[int]
    primary_unit_id: int
    locations: dict[int, int]
    confirmed: bool


def parse_meter_topology(info: dict | None) -> MeterTopology:
    """The topology an answer describes, or the unconfirmed single-meter default."""
    default = MeterTopology([DEFAULT_METER_UNIT_ID], DEFAULT_METER_UNIT_ID, {}, False)
    if not info or not info.get("unit_ids"):
        return default
    unit_ids = [int(unit) for unit in info["unit_ids"] if int(unit) > 0]
    if not unit_ids:
        return default
    return MeterTopology(
        unit_ids=unit_ids,
        primary_unit_id=int(info.get("primary_unit_id") or unit_ids[0]),
        locations={
            int(unit_id): int(location)
            for unit_id, location in (info.get("locations_by_unit_id") or {}).items()
        },
        confirmed=True,
    )
