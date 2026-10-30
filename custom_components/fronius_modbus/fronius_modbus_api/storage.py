"""The storage as the integration controls it: an extended mode over the SunSpec 124 registers.

SunSpec 124 knows four control modes and two signed percent rates. The
integration exposes eight "extended" modes on top (charge from grid, block
discharging, ...), each a fixed combination of mode and rates. The device
cannot name the extended mode, so it is re-derived from the registers on
every poll; a written mode is only held over the few polls the device needs
to apply it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import IntEnum
import logging

from .exceptions import ControlRefused
from .sunspec_models import Storage
from .writes import register_value, registers_holding, written_value

_LOGGER = logging.getLogger(__name__)

FULL_RATE_PCT = 100.0
# Polls a set_mode() may stay unconfirmed before the device's registers win.
MODE_WRITE_GRACE_POLLS = 3
SOC_MINIMUM_LOWEST = 5
SOC_MINIMUM_HIGHEST = 100
MODE_AUTO, MODE_CHARGE, MODE_DISCHARGE, MODE_CHARGE_AND_DISCHARGE = 0, 1, 2, 3


class ExtendedMode(IntEnum):
    """The eight modes the integration exposes, each a fixed SunSpec mode/rate combination."""

    AUTO = 0
    PV_CHARGE_LIMIT = 1
    DISCHARGE_LIMIT = 2
    CHARGE_AND_DISCHARGE_LIMIT = 3
    CHARGE_FROM_GRID = 4
    DISCHARGE_TO_GRID = 5
    BLOCK_DISCHARGING = 6
    BLOCK_CHARGING = 7


# extended mode -> (StorCtl_Mod, InWRte %, OutWRte %); a grid mode is entered at 0 W.
_MODE_TABLE: dict[ExtendedMode, tuple[int, float, float]] = {
    ExtendedMode.AUTO: (MODE_AUTO, FULL_RATE_PCT, FULL_RATE_PCT),
    ExtendedMode.PV_CHARGE_LIMIT: (MODE_CHARGE, FULL_RATE_PCT, FULL_RATE_PCT),
    ExtendedMode.DISCHARGE_LIMIT: (MODE_DISCHARGE, FULL_RATE_PCT, FULL_RATE_PCT),
    ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT: (
        MODE_CHARGE_AND_DISCHARGE,
        FULL_RATE_PCT,
        FULL_RATE_PCT,
    ),
    ExtendedMode.CHARGE_FROM_GRID: (MODE_DISCHARGE, FULL_RATE_PCT, 0.0),
    ExtendedMode.DISCHARGE_TO_GRID: (MODE_CHARGE, 0.0, FULL_RATE_PCT),
    ExtendedMode.BLOCK_DISCHARGING: (MODE_CHARGE_AND_DISCHARGE, FULL_RATE_PCT, 0.0),
    ExtendedMode.BLOCK_CHARGING: (MODE_CHARGE_AND_DISCHARGE, 0.0, FULL_RATE_PCT),
}
CHARGE_LIMIT_MODES = {
    ExtendedMode.PV_CHARGE_LIMIT,
    ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT,
    ExtendedMode.BLOCK_DISCHARGING,
}
DISCHARGE_LIMIT_MODES = {
    ExtendedMode.DISCHARGE_LIMIT,
    ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT,
    ExtendedMode.BLOCK_CHARGING,
}
_IMAGE_FIELDS = ("stor_ctl_mod", "in_w_rte", "out_w_rte")
# The rate each grid mode leaves to the user, as its grid power.
_GRID_POWER_FIELDS = {
    ExtendedMode.CHARGE_FROM_GRID: "out_w_rte",
    ExtendedMode.DISCHARGE_TO_GRID: "in_w_rte",
}


def _derive_extended_mode(mode: int, in_rate: float, out_rate: float) -> ExtendedMode:
    if mode == MODE_AUTO:
        return ExtendedMode.AUTO
    # A negative rate is the grid modes' signature and must win over the
    # plain charge/discharge branches (audit F08: forced discharge after a
    # restart read as a PV charge limit).
    if out_rate < 0:
        return ExtendedMode.CHARGE_FROM_GRID
    if in_rate < 0:
        return ExtendedMode.DISCHARGE_TO_GRID
    if mode in (MODE_CHARGE, MODE_CHARGE_AND_DISCHARGE) and in_rate == 0:
        return ExtendedMode.BLOCK_CHARGING
    if mode == MODE_CHARGE:
        return ExtendedMode.PV_CHARGE_LIMIT
    if out_rate == 0:
        return ExtendedMode.BLOCK_DISCHARGING
    if mode == MODE_DISCHARGE:
        return ExtendedMode.DISCHARGE_LIMIT
    return ExtendedMode.CHARGE_AND_DISCHARGE_LIMIT


def _registers_fit(
    extended_mode: ExtendedMode, mode: int, in_rate: float, out_rate: float
) -> bool:
    """Whether the registers can still mean ``extended_mode``.

    The grid modes leave one rate to the user, so several register images
    map to the same extended mode; a plain re-derivation would flip between
    them (Charge from Grid at 0 W reads exactly like Block Discharging).

    Another controller may write the same mode on a different base mode -
    evcc holds with StorCtl_Mod 2 where this integration writes 3 - and an
    image that derives to the mode is that mode, whoever wrote it. Judged
    against our own image alone, every poll was an "outside change".
    """
    if _derive_extended_mode(mode, in_rate, out_rate) is extended_mode:
        return True
    expected_mode, expected_in, expected_out = _MODE_TABLE[extended_mode]
    if mode != expected_mode:
        return False
    if extended_mode is ExtendedMode.CHARGE_FROM_GRID:
        return in_rate == expected_in and out_rate <= 0
    if extended_mode is ExtendedMode.DISCHARGE_TO_GRID:
        return out_rate == expected_out and in_rate <= 0
    charge_free = extended_mode in CHARGE_LIMIT_MODES
    discharge_free = extended_mode in DISCHARGE_LIMIT_MODES
    in_fits = in_rate >= 0 if charge_free else in_rate == expected_in
    out_fits = out_rate >= 0 if discharge_free else out_rate == expected_out
    return in_fits and out_fits


def _watts_to_percent(watts: float, maximum_w: float) -> float:
    if maximum_w <= 0:
        return 0.0
    return max(-FULL_RATE_PCT, min(FULL_RATE_PCT, watts / maximum_w * FULL_RATE_PCT))


class StorageControl:
    """Reads the mode off the storage component and writes the sequences behind each mode."""

    def __init__(
        self, storage: Storage, *, max_charge_rate_w: float, max_discharge_rate_w: float
    ) -> None:
        """Bind to an already-updated storage component; call sync_from_device() next."""
        self._storage = storage
        self.max_charge_rate_w = max_charge_rate_w
        self.max_discharge_rate_w = max_discharge_rate_w
        self._extended_mode: ExtendedMode | None = None
        # Polls that did not confirm the last set_mode() yet; a write the
        # device silently refused must not be reported as done forever
        # (upstream #127), so the device wins after MODE_WRITE_GRACE_POLLS.
        self._unconfirmed_polls: int | None = None
        # Two automations firing together must not interleave mode and rate writes.
        self._write_lock = asyncio.Lock()

    def sync_from_device(self) -> None:
        """Reconcile the extended mode with the registers of the last poll.

        The registers are the truth: a mode the device changed on its own
        (schedule, restart, another controller) replaces the chosen one. A
        register image that still fits the chosen mode keeps it, because the
        grid modes share images with the block modes.
        """
        mode, in_rate, out_rate = (
            self._storage.stor_ctl_mod,
            self._storage.in_w_rte,
            self._storage.out_w_rte,
        )
        if mode is None or in_rate is None or out_rate is None:
            return
        derived = _derive_extended_mode(mode, in_rate, out_rate)
        if self._extended_mode is None:
            self._extended_mode = derived
            return
        if _registers_fit(self._extended_mode, mode, in_rate, out_rate):
            self._unconfirmed_polls = None
            return
        if self._unconfirmed_polls is not None:
            self._unconfirmed_polls += 1
            if self._unconfirmed_polls < MODE_WRITE_GRACE_POLLS:
                return
            _LOGGER.warning(
                "The inverter did not take storage mode %s; it reports %s",
                self._extended_mode.name,
                derived.name,
            )
        else:
            _LOGGER.warning(
                "Storage mode changed outside Home Assistant: %s -> %s",
                self._extended_mode.name,
                derived.name,
            )
        self._extended_mode = derived
        self._unconfirmed_polls = None

    @property
    def extended_mode(self) -> ExtendedMode:
        """The extended mode as the device confirms it, or the one just written."""
        return ExtendedMode.AUTO if self._extended_mode is None else self._extended_mode

    @property
    def control_mode(self) -> int | None:
        """The SunSpec mode, normalised the way the sensor always showed it."""
        mode, in_rate, out_rate = (
            self._storage.stor_ctl_mod,
            self._storage.in_w_rte,
            self._storage.out_w_rte,
        )
        if mode is None or in_rate is None or out_rate is None:
            return None
        if out_rate < 0 or (
            mode == MODE_DISCHARGE and self._storage.cha_gri_set == 1 and out_rate == 0
        ):
            return MODE_CHARGE
        if in_rate < 0 or (
            mode == MODE_CHARGE
            and self.extended_mode is ExtendedMode.DISCHARGE_TO_GRID
            and in_rate == 0
        ):
            return MODE_DISCHARGE
        return mode

    @property
    def discharge_limit_pct(self) -> float | None:
        """The positive share of OutWRte, or None before the first update."""
        rate = self._storage.out_w_rte
        return None if rate is None else max(rate, 0.0)

    @property
    def grid_charge_power_pct(self) -> float | None:
        """The negative share of OutWRte, as a positive percent, or None before the first update."""
        rate = self._storage.out_w_rte
        return None if rate is None else max(-rate, 0.0)

    @property
    def charge_limit_pct(self) -> float | None:
        """The positive share of InWRte, or None before the first update."""
        rate = self._storage.in_w_rte
        return None if rate is None else max(rate, 0.0)

    @property
    def grid_discharge_power_pct(self) -> float | None:
        """The negative share of InWRte, as a positive percent, or None before the first update."""
        rate = self._storage.in_w_rte
        return None if rate is None else max(-rate, 0.0)

    @property
    def soc_minimum(self) -> int | None:
        """MinRsvPct rounded to a whole percent, or None before the first update."""
        value = self._storage.min_rsv_pct
        return None if value is None else int(round(value))

    async def set_mode(self, mode: ExtendedMode) -> None:
        """Write the mode/rate combination behind the extended mode, then own it."""
        async with self._write_lock:
            sunspec_mode, in_rate, out_rate = _MODE_TABLE[mode]
            # The device refuses a read for a moment right after a write (real
            # GEN24, fw 1.38.6-1: exception 4 on the block read that follows).
            # The coordinator's next poll picks the new values up instead.
            # The higher rate goes first: every mode has one at full, which
            # keeps the window valid whatever the other still holds. With both
            # at or below zero for an instant the firmware refuses the second
            # write (exception 3, upstream #126). Ordered by the target alone:
            # the last poll may not hold what the previous write left.
            rate_writes = sorted(
                [("in_w_rte", in_rate), ("out_w_rte", out_rate)],
                key=lambda write: -write[1],
            )
            kept = await self._grid_power_kept(mode)
            rate_writes = [write for write in rate_writes if write[0] != kept]
            writes = [("stor_ctl_mod", sunspec_mode), *rate_writes]
            holding = await registers_holding(self._storage, dict(writes))
            for field_name, value in writes:
                if field_name not in holding:
                    await self._storage.write(field_name, value)
            self._extended_mode = mode
            self._unconfirmed_polls = 0

    async def set_charge_limit_w(self, watts: float) -> None:
        """Write InWRte from watts, clamped to the charge rate maximum."""
        await self._write_rate(
            "in_w_rte",
            lambda: _watts_to_percent(max(watts, 0.0), self.max_charge_rate_w),
            allowed=lambda: self.extended_mode in CHARGE_LIMIT_MODES,
            refusal=ControlRefused(
                "charge_limit_not_in_mode",
                "Charge limit cannot be changed in the current storage mode",
            ),
        )

    async def set_discharge_limit_w(self, watts: float) -> None:
        """Write OutWRte from watts, clamped to the discharge rate maximum."""
        await self._write_rate(
            "out_w_rte",
            lambda: _watts_to_percent(max(watts, 0.0), self.max_discharge_rate_w),
            allowed=lambda: self.extended_mode in DISCHARGE_LIMIT_MODES,
            refusal=ControlRefused(
                "discharge_limit_not_in_mode",
                "Discharge limit cannot be changed in the current storage mode",
            ),
        )

    async def set_grid_charge_power_w(self, watts: float) -> None:
        """Write OutWRte as a negative rate: charging from the grid, not the PV."""
        await self._write_rate(
            "out_w_rte",
            lambda: -_watts_to_percent(max(watts, 0.0), self.max_charge_rate_w),
            allowed=lambda: self.extended_mode is ExtendedMode.CHARGE_FROM_GRID,
            refusal=ControlRefused(
                "grid_charge_power_not_in_mode",
                "Grid charge power can only be changed in Charge from Grid mode",
            ),
        )

    async def set_grid_discharge_power_w(self, watts: float) -> None:
        """Write InWRte as a negative rate: discharging to the grid, not covering load."""
        await self._write_rate(
            "in_w_rte",
            lambda: -_watts_to_percent(max(watts, 0.0), self.max_discharge_rate_w),
            allowed=lambda: self.extended_mode is ExtendedMode.DISCHARGE_TO_GRID,
            refusal=ControlRefused(
                "grid_discharge_power_not_in_mode",
                "Grid discharge power can only be changed in Discharge to Grid mode",
            ),
        )

    async def set_minimum_reserve(self, percent: int) -> None:
        """Write MinRsvPct as a whole percent between the SoC minimum bounds."""
        if not float(percent).is_integer():
            raise ControlRefused(
                "value_not_whole", "SoC Minimum must be a whole number"
            )
        if not SOC_MINIMUM_LOWEST <= percent <= SOC_MINIMUM_HIGHEST:
            raise ControlRefused(
                "value_out_of_range",
                f"SoC Minimum must be between {SOC_MINIMUM_LOWEST} and {SOC_MINIMUM_HIGHEST}",
                minimum=str(SOC_MINIMUM_LOWEST),
                maximum=str(SOC_MINIMUM_HIGHEST),
            )
        async with self._write_lock:
            await self._write_changed("min_rsv_pct", float(int(percent)))

    async def _write_rate(
        self,
        field_name: str,
        percent: Callable[[], float],
        *,
        allowed: Callable[[], bool],
        refusal: ControlRefused,
    ) -> None:
        # Decided under the lock: a mode switch queued ahead of this write
        # would otherwise be validated against the mode it is replacing
        # (audit F02), and the rate maximum may change with it.
        async with self._write_lock:
            if not allowed():
                raise refusal
            value = await written_value(self._storage, field_name, percent())
            mode, in_rate, out_rate = await self._image_for_a_rate()
            if field_name == "in_w_rte":
                in_rate = value
            else:
                out_rate = value
            # With both at or below 0 the firmware refuses the write
            # (exception 3, upstream #126), so say why instead (reaudit W-02).
            if in_rate <= 0 and out_rate <= 0:
                raise ControlRefused(
                    "limit_blocks_both_ways",
                    "A limit of 0 W would block charging and discharging at once, "
                    "which the inverter refuses",
                )
            # In the combined mode it also refuses a negative rate beyond the
            # other one (Fronius register map); another controller's image can
            # hold a grid mode there (reaudit CX-01).
            if mode == MODE_CHARGE_AND_DISCHARGE and -in_rate > out_rate:
                raise ControlRefused(
                    "grid_power_beyond_window",
                    "The grid power exceeds the combined limit the inverter holds; "
                    "select the storage mode again",
                )
            await self._write_changed(field_name, value)

    async def _grid_power_kept(self, mode: ExtendedMode) -> str | None:
        """The grid power field selecting ``mode`` again leaves as set, if any.

        Entering a grid mode starts its power at 0 W. The last poll may not
        hold a power written since, nor the one of a mode left since (reaudit
        RC-01), and the mode last chosen may not be what the registers hold:
        another controller, a schedule or an ignored write leaves another
        image there (reaudit D-02, E-01).
        """
        field_name = _GRID_POWER_FIELDS.get(mode)
        if field_name is None or mode is not self._extended_mode:
            return None
        image = await self._live_image()
        # Unreadable for a moment right after a write: the power just set stays.
        if image is None or _registers_fit(mode, *image):
            return field_name
        return None

    async def _image_for_a_rate(self) -> tuple[int, float, float]:
        """The image a rate write lands in: the live one, while it is the selected mode's.

        A limit mode leaves both sides to the user, so its table says nothing
        about them (reaudit D-01), and a schedule or another controller may
        have changed the mode since the last poll (reaudit AC-01). Unreadable
        for a moment right after a write, the mode's table stands in.
        """
        image = await self._live_image()
        if image is None:
            return _MODE_TABLE[self.extended_mode]
        if not _registers_fit(self.extended_mode, *image):
            raise ControlRefused(
                "storage_mode_left",
                "The inverter is no longer in the selected storage mode; "
                "select it again",
            )
        return image

    async def _live_image(self) -> tuple[int, float, float] | None:
        """StorCtl_Mod, InWRte and OutWRte as the registers hold them.

        None when one cannot be read. Read before the first write.
        """
        image = [await register_value(self._storage, name) for name in _IMAGE_FIELDS]
        if None in image:
            return None
        mode, in_rate, out_rate = image
        return int(mode), float(in_rate), float(out_rate)

    async def _write_changed(self, field_name: str, value: float) -> None:
        if not await registers_holding(self._storage, {field_name: value}):
            await self._storage.write(field_name, value)
