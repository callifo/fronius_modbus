"""The ways a request to the inverter's web interface fails."""

from __future__ import annotations


class ClientIpResolutionError(RuntimeError):
    """Raised when the local IP for Modbus restriction cannot be resolved."""


class FroniusWebUnreachable(OSError):
    """No answer from the web server: the same switched-off device as on Modbus.

    Carries the requests error type only. Requests puts the URL, and with it
    the host, into its messages, and Home Assistant logs travel with bug
    reports (audit E01).
    """


class FroniusWebResponseError(RuntimeError):
    """The web server answered with an error status: a device that is up and refusing."""

    def __init__(self, message: str, status_code: int) -> None:
        """Keep the status, so a caller can tell a missing endpoint from a failure."""
        super().__init__(message)
        self.status_code = status_code


class SunSpecModeChangeNeeded(RuntimeError):
    """The inverter serves another SunSpec model type than the int + SF one read here.

    An external device reading that type over Modbus may no longer work after
    a switch, so it waits for the owner's consent.
    """

    def __init__(self, current_mode: str) -> None:
        """Name the model type the inverter serves now."""
        super().__init__(f"the inverter serves the {current_mode} SunSpec model type")
        self.current_mode = current_mode


class FroniusWebAuthError(RuntimeError):
    """Raised when Fronius Web API authentication fails."""
