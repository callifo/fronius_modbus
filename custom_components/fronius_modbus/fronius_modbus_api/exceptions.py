"""Errors the device library raises on its own account."""

from modbus_connection import ModbusError


class NotAFroniusInverter(ModbusError):
    """The unit answered, but not with a SunSpec inverter model.

    A ModbusError so a coordinator treats it as a failed refresh: a device
    that answers the marker but serves no inverter is the wrong device.
    """


class ControlLeftDisabledError(ModbusError):
    """A control was switched off for a write, the write failed, and switching it back on failed too."""


class IncompleteChainError(ModbusError):
    """The SunSpec chain ended in a refused read, so later models are undecided."""
