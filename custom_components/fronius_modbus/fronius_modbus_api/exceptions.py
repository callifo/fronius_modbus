"""Errors the device library raises on its own account."""

from modbus_connection import ModbusConnectionError, ModbusError, ModbusTimeoutError


def error_text(err: BaseException) -> str:
    """An error as logs and reports show it: a transport error by its kind alone.

    The library writes the target, host and port, into a connect error's text,
    and Home Assistant logs and diagnostics travel with public bug reports
    (audit F07).
    """
    if isinstance(err, (ModbusConnectionError, ModbusTimeoutError)):
        return type(err).__name__
    return str(err)


class NotAFroniusInverter(ModbusError):
    """The unit answered, but not with a SunSpec inverter model.

    A ModbusError so a coordinator treats it as a failed refresh: a device
    that answers the marker but serves no inverter is the wrong device.
    """


class ControlLeftDisabledError(ModbusError):
    """A control was switched off for a write, the write failed, and switching it back on failed too."""


class ControlRefused(ValueError):
    """A write the device state or the value rules out; the user can change the input.

    Carries a translation key and its placeholders, so the integration can show
    the message in the user's language; the English text stays for logs.
    """

    def __init__(self, key: str, message: str, **placeholders: str) -> None:
        """Keep the key and placeholders next to the English message."""
        super().__init__(message)
        self.key = key
        self.placeholders = placeholders


class ControlUnavailable(RuntimeError):
    """A write the integration cannot carry out: no login, or the device declined."""

    def __init__(self, key: str, message: str, **placeholders: str) -> None:
        """Keep the key and placeholders next to the English message."""
        super().__init__(message)
        self.key = key
        self.placeholders = placeholders


class IncompleteChainError(ModbusError):
    """The SunSpec chain ended in a refused read, so later models are undecided."""
