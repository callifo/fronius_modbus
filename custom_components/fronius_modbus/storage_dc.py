"""The battery's DC side, as the storage sensors show it: charging and discharging.

A GEN24 measures each direction on its own module. A Symo Hybrid has one for
both, without a sign; the storage status says which way it flows.
"""

from __future__ import annotations

from typing import Literal

from .coordinator import FroniusRuntimeData
from .fronius_modbus_api.device import REPORT_STORAGE

type Direction = Literal["charge", "discharge"]
type Point = Literal["dca", "dcv", "dcw", "dcwh"]

_FLOWS: dict[int, Direction] = {3: "discharge", 4: "charge"}
# Off, empty, full and holding: nothing flows.
_IDLE = frozenset({1, 2, 5, 6})


def storage_dc(
    runtime: FroniusRuntimeData, direction: Direction, point: Point
) -> float | None:
    """What the battery's module shows for one direction; None when unknown."""
    channels = runtime.device.mppt_channels
    index = channels.charge if direction == "charge" else channels.discharge
    module = None if index is None else runtime.device.mppt_module(index)
    if module is None:
        return None
    value: float | None = getattr(module, point)
    if channels.charge != channels.discharge or point == "dcv":
        return value
    # One energy counter for both directions would be neither.
    return None if point == "dcwh" else _of_direction(runtime, direction, value)


def _of_direction(
    runtime: FroniusRuntimeData, direction: Direction, value: float | None
) -> float | None:
    """The shared module's value, if it flows this way."""
    storage = runtime.device.storage
    # A status from an earlier poll would give the power to the wrong side.
    if storage is None or REPORT_STORAGE not in runtime.modbus.data.report.updated:
        return None
    status = storage.cha_st
    if status in _IDLE:
        return 0.0
    if status not in _FLOWS:
        return None
    return value if _FLOWS[status] == direction else 0.0
