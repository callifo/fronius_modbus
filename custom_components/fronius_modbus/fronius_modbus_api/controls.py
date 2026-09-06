"""Immediate controls (SunSpec 123) with the enable pulse Fronius needs.

A new limit or power factor only takes effect when its enable flag is
written after the value. When the flag is already on, it is pulsed off,
the value written, and the flag written on again a second later.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import time

from modbus_connection import ModbusError

from .exceptions import ControlLeftDisabledError
from .sunspec_models import Controls

APPLY_TOGGLE_DELAY_SECONDS = 1.0
# ponytail: a poll inside the pulse reads the flag as off; the state is masked to
# "on" for this long instead of tracking the pulse per register.
APPLY_MASK_SECONDS = APPLY_TOGGLE_DELAY_SECONDS + 0.5
PERCENT = 100.0
ENABLED, DISABLED = 1, 0


class InverterControls:
    """Reads and writes the immediate controls of one inverter."""

    def __init__(
        self,
        controls: Controls,
        *,
        max_power_w: float | None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wrap a Controls component; max_power_w converts AC limit percent to watts."""
        self._controls = controls
        self.max_power_w = max_power_w
        self._monotonic = monotonic
        self._sleep = sleep
        self._ac_limit_mask_until = 0.0
        self._power_factor_mask_until = 0.0
        self._write_lock = asyncio.Lock()

    def _flag(self, value: int | None, mask_until: float) -> bool | None:
        if self._monotonic() < mask_until:
            return True
        return None if value is None else value == ENABLED

    @property
    def ac_limit_enabled(self) -> bool | None:
        """Whether the AC power limit is active, masked True right after a pulse."""
        return self._flag(self._controls.w_max_lim_ena, self._ac_limit_mask_until)

    @property
    def ac_limit_pct(self) -> float | None:
        """The AC power limit as a percentage of max power."""
        return self._controls.w_max_lim_pct

    @property
    def ac_limit_w(self) -> float | None:
        """The AC power limit in watts, or None without a known max power."""
        if self.ac_limit_pct is None or not self.max_power_w:
            return None
        return round(self.max_power_w * self.ac_limit_pct / PERCENT)

    @property
    def power_factor(self) -> float | None:
        """The configured power factor, -1..1."""
        return self._controls.out_pf_set

    @property
    def power_factor_enabled(self) -> bool | None:
        """Whether the power factor override is active, masked True right after a pulse."""
        return self._flag(self._controls.out_pf_set_ena, self._power_factor_mask_until)

    @property
    def var_percent_enabled(self) -> bool | None:
        """Whether the reactive power percent override is active."""
        value = self._controls.v_ar_pct_ena
        return None if value is None else value == ENABLED

    @property
    def connected(self) -> bool | None:
        """Whether the inverter is connected to the grid."""
        value = self._controls.conn
        return None if value is None else value == ENABLED

    async def _write_with_pulse(
        self, enable_field: str, value_field: str, value: float
    ) -> bool:
        """Write the value; pulse the enable flag off and on around it when it was on. Returns whether it was on."""
        # No read-back after the last write here: the device refuses a read
        # for a moment right after a write (real GEN24, fw 1.38.6-1); the
        # coordinator's next poll picks the new values up instead.
        async with self._write_lock:
            await self._controls.async_update()
            was_enabled: bool = getattr(self._controls, enable_field) == ENABLED
            if not was_enabled:
                await self._controls.write(value_field, value)
                return False
            await self._controls.write(enable_field, DISABLED)
            try:
                await self._controls.write(value_field, value)
            except ModbusError as err:
                # The limit was live before this call; a failed value write
                # must not leave it switched off (audit F01).
                await self._restore_enable(enable_field, err)
                raise
            await self._sleep(APPLY_TOGGLE_DELAY_SECONDS)
            await self._controls.write(enable_field, ENABLED)
            return True

    async def _restore_enable(self, enable_field: str, cause: ModbusError) -> None:
        try:
            await self._controls.write(enable_field, ENABLED)
        except ModbusError as restore_error:
            raise ControlLeftDisabledError(
                f"{enable_field} could not be re-enabled after a failed write: "
                f"{restore_error}"
            ) from cause

    async def set_ac_limit_w(self, watts: float) -> None:
        """Set the AC power limit in watts, converted to percent of max power."""
        if not self.max_power_w:
            raise ValueError("Cannot set AC limit rate, missing max power")
        percent = max(0.0, min(PERCENT, watts / self.max_power_w * PERCENT))
        self._ac_limit_mask_until = self._monotonic() + APPLY_MASK_SECONDS
        was_enabled = await self._write_with_pulse(
            "w_max_lim_ena", "w_max_lim_pct", percent
        )
        if not was_enabled:
            self._ac_limit_mask_until = 0.0

    async def set_ac_limit_enable(self, enabled: bool) -> None:
        """Enable or disable the AC power limit."""
        async with self._write_lock:
            await self._controls.write(
                "w_max_lim_ena", ENABLED if enabled else DISABLED
            )
            self._ac_limit_mask_until = 0.0

    async def set_power_factor(self, value: float) -> None:
        """Set the power factor, -1..1."""
        if not -1.0 <= value <= 1.0:
            raise ValueError("Power factor must be between -1 and 1")
        self._power_factor_mask_until = self._monotonic() + APPLY_MASK_SECONDS
        was_enabled = await self._write_with_pulse(
            "out_pf_set_ena", "out_pf_set", value
        )
        if not was_enabled:
            self._power_factor_mask_until = 0.0

    async def set_power_factor_enable(self, enabled: bool) -> None:
        """Enable or disable the power factor override."""
        async with self._write_lock:
            await self._controls.write(
                "out_pf_set_ena", ENABLED if enabled else DISABLED
            )
            self._power_factor_mask_until = 0.0

    async def set_connected(self, connected: bool) -> None:
        """Connect or disconnect the inverter from the grid."""
        async with self._write_lock:
            await self._controls.write("conn", ENABLED if connected else DISABLED)
