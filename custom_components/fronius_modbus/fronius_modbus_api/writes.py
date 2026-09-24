"""Which registers already hold what a write would put there.

Every register write reconfigures the inverter, and the AC limit and power
factor pulse their enable flag around it: the same value again switched a
live limit off for a second. The comparison is made on the register words,
at the scale the device reports, so no rounding decides it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from modbus_connection import ModbusError
from modbus_connection.decode import decode_int
from modbus_connection.model import Component, RegisterField, ResolvedField


async def registers_holding(
    component: Component, wanted: Mapping[str, Any]
) -> set[str]:
    """The fields of ``wanted`` whose registers already hold that value.

    Read before the first write, never between writes: the inverter refuses
    a read for a moment right after a write (real GEN24, fw 1.38.6-1). A field
    that cannot be read is left out, so it is written - the owner's input is
    not dropped for want of a comparison.
    """
    unit = component.modbus_unit
    holding: set[str] = set()
    for name, value in wanted.items():
        resolved = component.resolved_fields[name]
        try:
            words = await unit.read_holding_registers(resolved.address, resolved.count)
            written = await _encoded(unit, resolved, value)
        except ModbusError, ValueError, TypeError:
            continue
        if list(words) == written:
            holding.add(name)
    return holding


async def _encoded(unit: Any, resolved: ResolvedField, value: Any) -> list[int]:
    """The words a write of ``value`` puts into the register, at the device's scale."""
    field = resolved.field
    if not isinstance(field, RegisterField):
        raise TypeError(f"{field!r} is not a register")
    exponent = None
    if field.scale_register is not None and resolved.scale_address is not None:
        (scale,) = await unit.read_holding_registers(resolved.scale_address, 1)
        exponent = decode_int([scale], signed=True)
    return field.encode(value, exponent)
