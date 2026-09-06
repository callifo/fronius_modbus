"""Values no single register carries: the household load and the grid status."""

from __future__ import annotations

from .const import GRID_STATUS

GRID_FREQUENCY_HZ = 50.0
METER_GRID_BAND_HZ = 0.2
INVERTER_GRID_BAND_HZ = 5.0
STANDSTILL_HZ = 1.0
METER_LOCATION_FEED_IN = 0
METER_LOCATION_CONSUMPTION = 1
METER_LOCATION_CONSUMPTION_RANGE = range(256, 512)
# Fronius occasionally drops the inverter AC power to ~0 for one poll while PV is clearly active.
LOAD_GLITCH_MIN_EXPORT_W = 1000.0
LOAD_GLITCH_MAX_ABS_INVERTER_W = 50.0
LOAD_GLITCH_MIN_PV_W = 1000.0
# During strong battery charging on Verto the meter export can exceed the inverter
# AC power and the grid-meter formula produces a bogus negative load.
LOAD_STORAGE_CHARGE_MIN_W = 1000.0


def _within(value: float, centre: float, band: float) -> bool:
    """Return whether value lies strictly within band of centre."""
    return centre - band < value < centre + band


def grid_status(inverter_hz: float | None, meter_hz: float | None) -> str | None:
    """Derive the grid status from the inverter and meter grid frequencies."""
    if inverter_hz is None or meter_hz is None:
        return None
    meter_online = _within(meter_hz, GRID_FREQUENCY_HZ, METER_GRID_BAND_HZ)
    if meter_online and _within(inverter_hz, GRID_FREQUENCY_HZ, METER_GRID_BAND_HZ):
        return GRID_STATUS[3]
    if not meter_online and _within(
        inverter_hz, GRID_FREQUENCY_HZ, INVERTER_GRID_BAND_HZ
    ):
        return GRID_STATUS[1]
    if inverter_hz < STANDSTILL_HZ:
        if meter_online:
            return GRID_STATUS[2]
        if meter_hz < STANDSTILL_HZ:
            return GRID_STATUS[0]
    return None


class LoadEstimator:
    """The household load from the primary meter and the inverter, glitch-guarded."""

    def __init__(self) -> None:
        """Initialize with no prior good reading."""
        self._last_good_load_w: float | None = None
        self._last_good_inverter_power_w: float | None = None
        self._consecutive_bad_polls = 0

    def _good(self, load_w: float, inverter_power_w: float | None = None) -> float:
        self._last_good_load_w = round(load_w, 2)
        if inverter_power_w is not None:
            self._last_good_inverter_power_w = inverter_power_w
        self._consecutive_bad_polls = 0
        return self._last_good_load_w

    def update(
        self,
        *,
        meter_power_w: float | None,
        inverter_power_w: float | None,
        meter_location: int | None,
        pv_power_w: float | None,
        storage_charge_power_w: float | None,
        storage_present: bool,
    ) -> float | None:
        """Derive the current household load from this poll's meter and inverter data."""
        if meter_power_w is None or meter_location is None:
            self._consecutive_bad_polls = 0
            return None
        if (
            meter_location == METER_LOCATION_CONSUMPTION
            or meter_location in METER_LOCATION_CONSUMPTION_RANGE
        ):
            load = -meter_power_w
            if meter_power_w <= 0:
                return self._good(load)
            self._consecutive_bad_polls = 0
            return round(load, 2)
        if meter_location != METER_LOCATION_FEED_IN or inverter_power_w is None:
            self._consecutive_bad_polls = 0
            return None
        candidate = meter_power_w + inverter_power_w
        pv_active = pv_power_w is not None and pv_power_w >= LOAD_GLITCH_MIN_PV_W
        previously_active = (
            self._last_good_inverter_power_w or 0.0
        ) >= LOAD_GLITCH_MIN_PV_W
        glitch = (
            candidate < 0
            and meter_power_w <= -LOAD_GLITCH_MIN_EXPORT_W
            and abs(inverter_power_w) <= LOAD_GLITCH_MAX_ABS_INVERTER_W
            and (pv_active or previously_active)
        )
        if glitch:
            self._consecutive_bad_polls += 1
            return self._last_good_load_w if self._consecutive_bad_polls == 1 else None
        charging_hard = (
            storage_present
            and (storage_charge_power_w or 0.0) >= LOAD_STORAGE_CHARGE_MIN_W
        )
        if candidate < 0 and charging_hard:
            self._consecutive_bad_polls = 0
            return None
        return self._good(max(candidate, 0.0), inverter_power_w)


class TotalGuard:
    """Accepts readings of a monotonically increasing counter, one verdict per poll.

    A single lower or far-too-high sample is a bad reading and is ignored. The
    same kind of sample on ``confirmations`` consecutive polls is the device
    telling the truth - a counter reset after a hardware swap, or a large but
    genuine gap while Home Assistant was offline (audit F05/F06) - and is
    accepted. Property reads never advance this state: only observe() does.
    """

    def __init__(self, *, max_step: float, confirmations: int) -> None:
        """Bound single-poll jumps by max_step; adopt a new range after confirmations polls."""
        self.value: float | None = None
        self._max_step = max_step
        self._confirmations = confirmations
        self._suspect_polls = 0

    def seed(self, value: float | None) -> None:
        """Start from a restored value without counting it as a poll."""
        self.value = value
        self._suspect_polls = 0

    def observe(self, reading: float | None) -> str | None:
        """Record one poll; returns why a reading was rejected or accepted late, else None."""
        if reading is None:
            self._suspect_polls = 0
            return None
        if self.value is None or 0 <= reading - self.value <= self._max_step:
            self.value = reading
            self._suspect_polls = 0
            return None
        self._suspect_polls += 1
        kind = "lower than" if reading < self.value else "far above"
        if self._suspect_polls < self._confirmations:
            return f"ignoring {reading}: {kind} the last value {self.value}"
        message = (
            f"accepting {reading}: {kind} the last value {self.value} for "
            f"{self._confirmations} polls, so the counter really moved"
        )
        self.value = reading
        self._suspect_polls = 0
        return message
