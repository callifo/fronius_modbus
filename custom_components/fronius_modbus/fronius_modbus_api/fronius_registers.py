"""The registers of a Datamanager that are not part of SunSpec.

They sit at fixed addresses, one below the register numbers of Fronius' manual
42,0410,2049, and need no scale factors.
"""

from __future__ import annotations

from modbus_connection.model import Component, integer, uint32, uint64

NOT_IMPLEMENTED_UINT16 = 0xFFFF
NOT_IMPLEMENTED_UINT32 = 0xFFFFFFFF
NOT_IMPLEMENTED_UINT64 = 0xFFFFFFFFFFFFFFFF


class StateCode(Component):
    """Register 214: the inverter's current state code, as its manual lists it."""

    code = integer(213, signed=False, nan=NOT_IMPLEMENTED_UINT16)


class SiteTotals(Component):
    """Registers 500 to 513: the sums over every inverter on the Datamanager."""

    power = uint32(499, nan=NOT_IMPLEMENTED_UINT32, unit="W")
    energy_day = uint64(501, nan=NOT_IMPLEMENTED_UINT64, unit="Wh")
    energy_year = uint64(505, nan=NOT_IMPLEMENTED_UINT64, unit="Wh")
    energy_total = uint64(509, nan=NOT_IMPLEMENTED_UINT64, unit="Wh")
