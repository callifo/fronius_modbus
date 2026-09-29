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
    # an empty password then keeps this token (audit F11).
    existing_token: dict[str, str] | None = None
    # Set while the model type question is open: the token that logged in,
    # and whether this flow minted it, so the answer finishes like the step
    # that asked would have.
    login_token: dict[str, str] | None = None
    token_minted: bool = False
    sunspec_mode: str | None = None


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


class _AlreadyConfigured(exceptions.HomeAssistantError):
    """Another entry already serves the host being configured."""


class _ScanIntervalTooShort(exceptions.HomeAssistantError):
    """Error to indicate the scan interval is too short."""


class _MissingApiPassword(exceptions.HomeAssistantError):
    """Error to indicate the Web API password is required."""


class _InvalidApiCredentials(exceptions.HomeAssistantError):
    """Error to indicate Fronius web API credentials are invalid."""


class _CannotResolveLocalIp(exceptions.HomeAssistantError):
    """Error to indicate the local IP for Modbus restriction cannot be resolved."""


class _SunSpecSwitchNeeded(exceptions.HomeAssistantError):
    """The inverter serves another SunSpec model type; switching it needs consent."""

    def __init__(self, mode: str) -> None:
        """Name the model type the inverter serves now."""
        super().__init__(mode)
        self.mode = mode
