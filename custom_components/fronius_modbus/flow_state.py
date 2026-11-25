"""What one flow step hands to the next: its pending state and the errors a step ends in."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant import exceptions


@dataclass(slots=True)
class _PendingFlowState:
    settings: dict[str, Any]
    previous_host: str | None
    apply_modbus_config: bool
    # Set when the password step is shown although a token exists (Configure):
    # an empty password then keeps this token.
    existing_token: dict[str, str] | None = None
    # Set while the model type question is open: the token that logged in,
    # and whether this flow minted it, so the answer finishes like the step
    # that asked would have.
    login_token: dict[str, str] | None = None
    token_minted: bool = False
    sunspec_mode: str | None = None
    # The address the Modbus restriction allows: confirmed by the owner, or
    # one the list already allowed. The resolved one fills in the question,
    # empty when none was found.
    allowed_host: str | None = None
    resolved_host: str | None = None
    # The owner asked to see the address although the list allows it.
    ask_allowed_host: bool = False


class _CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class _CannotConnectModbus(exceptions.HomeAssistantError):
    """Modbus did not answer, and without the web API nothing switched it on."""


class _InvalidHost(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid hostname."""


class _InvalidPort(exceptions.HomeAssistantError):
    """Error to indicate there is an invalid port."""


class _UnsupportedHardware(exceptions.HomeAssistantError):
    """Error to indicate there is unsupported hardware."""


class _AddressesNotUnique(exceptions.HomeAssistantError):
    """Error to indicate that the modbus addresses are not unique."""


class _DatamanagerFound(exceptions.HomeAssistantError):
    """A role was chosen for a Datamanager, which has other logins."""


class _AlreadyConfigured(exceptions.HomeAssistantError):
    """Another entry already serves the host being configured."""


class _ScanIntervalTooShort(exceptions.HomeAssistantError):
    """Error to indicate the scan interval is too short."""


class _MeterPowerIntervalTooLong(exceptions.HomeAssistantError):
    """Error to indicate the meter power interval is not below the scan interval."""


class _MissingApiPassword(exceptions.HomeAssistantError):
    """Error to indicate the Web API password is required."""


class _InvalidApiCredentials(exceptions.HomeAssistantError):
    """Error to indicate Fronius web API credentials are invalid."""


class _InvalidAllowedHost(exceptions.HomeAssistantError):
    """The address to allow is no IPv4 address a device on the network can have."""


class _AllowedHostNeeded(exceptions.HomeAssistantError):
    """The address to allow has to be confirmed: behind NAT the inverter sees another."""

    def __init__(self, resolved_ip: str) -> None:
        """Name the address Home Assistant resolved towards the inverter."""
        # Not in the message: an unexpected path logs it with the exception.
        super().__init__()
        self.resolved_ip = resolved_ip


class _SunSpecSwitchNeeded(exceptions.HomeAssistantError):
    """The inverter serves another SunSpec model type; switching it needs consent."""

    def __init__(self, mode: str, allowed_host: str | None) -> None:
        """Name the model type the inverter serves now and the address settled on."""
        super().__init__(mode)
        self.mode = mode
        # The answer writes this address without checking the list again.
        self.allowed_host = allowed_host
