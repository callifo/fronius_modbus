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


async def register_value(component: Component, name: str) -> Any:
    """What the register of ``name`` holds now, or None when it cannot be read.

    Read before the first write, like ``registers_holding``: the last poll may
    not hold what a write since left there.
    """
    unit = component.modbus_unit
    resolved = component.resolved_fields[name]
    field = _register_field(resolved)
    try:
        words = await unit.read_holding_registers(resolved.address, resolved.count)
        exponent = await _scale_exponent(unit, resolved, field)
    except ModbusError:
        return None
    return field.decode(list(words), exponent)


async def _encoded(unit: Any, resolved: ResolvedField, value: Any) -> list[int]:
    """The words a write of ``value`` puts into the register, at the device's scale."""
    field = _register_field(resolved)
    return field.encode(value, await _scale_exponent(unit, resolved, field))


def _register_field(resolved: ResolvedField) -> RegisterField[Any]:
    field = resolved.field
    if not isinstance(field, RegisterField):
        raise TypeError(f"{field!r} is not a register")
    return field


async def _scale_exponent(
    unit: Any, resolved: ResolvedField, field: RegisterField[Any]
) -> int | None:
    if field.scale_register is None or resolved.scale_address is None:
        return None
    (scale,) = await unit.read_holding_registers(resolved.scale_address, 1)
    return decode_int([scale], signed=True)
